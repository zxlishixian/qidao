from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .capture import WarpedBoard
from .ml_inference import IntersectionONNXClassifier
from .model import (
    Move,
    Stone,
    diff_boards,
    empty_board,
    legal_move_result,
    normalize_snapshot_captures,
)


class BoardAlignmentError(RuntimeError):
    pass


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, value))))


@dataclass(frozen=True)
class PatchFeature:
    lab_median: np.ndarray
    saturation_median: float
    ring_lab_median: np.ndarray
    ring_saturation_median: float
    edge_strength: float
    pixels_lab: np.ndarray
    marker_strength: float


@dataclass(frozen=True)
class PointEvidence:
    black: float
    white: float
    change: float
    circle: float
    delta_luma: float
    empty: float = 0.0
    unknown: float = 0.0

    def score(self, color: Stone) -> float:
        raw = self.black if color == Stone.BLACK else self.white
        other = self.white if color == Stone.BLACK else self.black
        # A stone changes the empty-board patch enough that both raw colour
        # scores can be moderately high.  Reward the colour that actually
        # dominates instead of accepting an opposite-colour stone merely
        # because it crossed the generic change threshold.
        selectivity = 0.58 + 0.42 * _sigmoid((raw - other - 0.02) / 0.05)
        return raw * selectivity

    def raw_score(self, color: Stone) -> float:
        return self.black if color == Stone.BLACK else self.white


@dataclass(frozen=True)
class PositionRecognition:
    board: tuple[tuple[Stone, ...], ...]
    confidence: float
    unknown_points: int
    next_color: Stone
    last_move: Move | None
    # Per-intersection likelihoods in logical coordinates: empty, black,
    # white. They let the baseline burst combine evidence even when a single
    # frame's conservative classifier returned UNKNOWN.
    point_scores: tuple[tuple[tuple[float, float, float], ...], ...] = ()


@dataclass(frozen=True)
class ScanAnalysis:
    expected_color: Stone
    scores: tuple[tuple[float, ...], ...]
    evidences: tuple[tuple[PointEvidence, ...], ...]
    best: Move | None
    best_score: float
    second_score: float
    stable_frames: int
    accepted: Move | None
    opposite_best: Move | None = None
    opposite_best_score: float = 0.0
    board_agreement: float = 1.0
    observed_board: tuple[tuple[Stone, ...], ...] = ()
    observed_confidence: float = 0.0
    unexpected_stones: int = 0
    unknown_points: int = 0
    frame_valid: bool = True
    fast_accepted: bool = False
    snapshot_board: tuple[tuple[Stone, ...], ...] = ()
    snapshot_next_color: Stone | None = None
    snapshot_last_move: Move | None = None
    snapshot_stable_frames: int = 0
    hover_previews: tuple[Move, ...] = ()
    absolute_board: tuple[tuple[Stone, ...], ...] = ()
    reconciliation_differences: int = 0

    @property
    def confidence(self) -> float:
        separation = max(0.0, self.best_score - self.second_score)
        confidence = self.best_score * 0.82 + separation * 1.8
        if not self.frame_valid:
            return 0.0
        return max(0.0, min(1.0, confidence * self.board_agreement))

    @property
    def color_mismatch_likely(self) -> bool:
        return self.opposite_best_score >= 0.62 and self.opposite_best_score > self.best_score + 0.10


class AdaptiveBoardTracker:
    """Tracks new stones against an empty-board reference and validates moves with Go rules."""

    def __init__(
        self,
        baseline: WarpedBoard,
        size: int,
        rotation: int = 0,
        threshold: float = 0.61,
        stable_required: int = 2,
    ):
        self.size = size
        self.rotation = rotation
        self.threshold = threshold
        self.stable_required = stable_required
        self.baseline_image = baseline.image.copy()
        self.spacing = baseline.spacing
        self.intersections = baseline.intersections
        self.baseline_grid_score = self.grid_alignment_score(baseline.image)
        self.last_grid_score = self.baseline_grid_score
        if self.baseline_grid_score < 0.42:
            raise BoardAlignmentError(
                "拖选区域没有识别到完整棋盘网格；请重新拉框并覆盖整个棋盘"
            )
        self.baseline_features = self._features(baseline.image)
        self.intersection_classifier = IntersectionONNXClassifier.load_default()
        self.board = empty_board(size)
        self.board_history = [self.board]
        self.move_history: list[Move] = []
        self.move_count = 0
        self._next_color = Stone.BLACK
        self.next_color_history = [self._next_color]
        self.pending: tuple[int, int, Stone] | None = None
        self.pending_frames = 0
        self.pending_snapshot: tuple[tuple[Stone, ...], ...] | None = None
        self.pending_snapshot_frames = 0
        self.absolute_observations: list[
            tuple[
                tuple[tuple[Stone, ...], ...],
                tuple[tuple[float, ...], ...],
            ]
        ] = []
        self.analysis_frame_index = 0
        self.quick_frame_signature: np.ndarray | None = None
        self.quick_frame_skips = 0
        self.marker_capable = False
        self.last_analysis: ScanAnalysis | None = None

    @property
    def next_color(self) -> Stone:
        return self._next_color

    @property
    def recognition_backend(self) -> str:
        if self.intersection_classifier is not None:
            return "ONNX 交叉点分类 + OpenCV 复核 + 围棋状态机"
        return "OpenCV 交叉点分类 + 围棋状态机（模型降级）"

    def can_skip_unchanged_frame(self, warped: WarpedBoard) -> bool:
        """Cheaply reject a visually identical board before ONNX inference.

        Screen capture and geometric tracking still run on every cycle, so a
        moved window is followed immediately. Only the expensive 361-point
        feature/model pass is skipped, and never while a move or corrected
        snapshot is waiting for its second confirmation frame.
        """
        gray = cv2.cvtColor(warped.image, cv2.COLOR_BGR2GRAY)
        signature = cv2.resize(gray, (190, 190), interpolation=cv2.INTER_AREA)
        previous = self.quick_frame_signature
        if previous is None or previous.shape != signature.shape:
            self.quick_frame_signature = signature
            self.quick_frame_skips = 0
            return False
        if self.pending is not None or self.pending_snapshot is not None:
            self.quick_frame_signature = signature
            self.quick_frame_skips = 0
            return False
        # Force a complete position check periodically even on a static board
        # so slow theme/rendering changes cannot accumulate indefinitely.
        if self.quick_frame_skips >= 6:
            self.quick_frame_signature = signature
            self.quick_frame_skips = 0
            return False
        delta = cv2.absdiff(signature, previous).reshape(-1)
        top_count = max(32, delta.size // 250)
        strongest_changes = np.partition(delta, delta.size - top_count)[-top_count:]
        if float(np.mean(strongest_changes)) >= 3.0:
            self.quick_frame_signature = signature
            self.quick_frame_skips = 0
            return False
        # Keep comparing against the last frame that actually went through the
        # full classifier. Updating the reference here made slow stone fade-in
        # animations disappear as a sequence of individually tiny changes.
        self.quick_frame_skips += 1
        return True

    def _evidence_grid(
        self,
        image: np.ndarray,
        features: tuple[tuple[PatchFeature, ...], ...],
    ) -> tuple[tuple[tuple[PointEvidence, ...], ...], bool]:
        probabilities = None
        if self.intersection_classifier is not None:
            try:
                probabilities = self.intersection_classifier.classify(
                    image,
                    self.intersections,
                    self.spacing,
                )
            except cv2.error:
                probabilities = None
        rows: list[tuple[PointEvidence, ...]] = []
        for y, feature_row in enumerate(features):
            row: list[PointEvidence] = []
            for x, feature in enumerate(feature_row):
                classical = self._absolute_evidence(feature)
                if probabilities is None:
                    row.append(classical)
                    continue
                empty_score, black_score, white_score, unknown_score = (
                    float(value) for value in probabilities[y, x]
                )
                # ONNX is primary, while the colour/radial detector acts as an
                # independent guard against a synthetic-to-real domain miss.
                classical_stone = max(classical.black, classical.white)
                if (
                    unknown_score >= 0.48
                    and classical_stone < 0.40
                    and classical.circle < 0.35
                ):
                    # The wide classifier crop may see the rim of a stone on
                    # the neighbouring intersection.  Its own centre/ring
                    # geometry is still an unambiguous empty grid point.
                    empty_score = max(empty_score, unknown_score * 0.92)
                    unknown_score *= 0.08
                geometric_stone = (
                    classical_stone >= 0.84
                    and classical.circle >= 0.50
                    and abs(classical.black - classical.white) >= 0.22
                )
                # Compact browser boards often give glossy white stones a soft
                # edge. The synthetic ONNX model may then call them UNKNOWN,
                # even though their centre/ring colour is unambiguous. Trust
                # strong radial colour evidence without requiring a hard rim.
                radial_stone = (
                    classical_stone >= 0.82
                    and abs(classical.black - classical.white) >= 0.38
                    and abs(classical.delta_luma) >= 16.0
                    and classical.circle >= 0.24
                )
                marked_stone = feature.marker_strength >= 0.70 and classical_stone >= 0.76
                if marked_stone or radial_stone or (unknown_score >= 0.48 and geometric_stone):
                    # A red latest-move dot is an overlay, not an unknown
                    # intersection.  Preserve the stone body identified by
                    # radial geometry underneath it.
                    black = black_score * 0.18 + classical.black * 0.82
                    white = white_score * 0.18 + classical.white * 0.82
                    unknown_score *= 0.12
                else:
                    black = black_score * 0.86 + classical.black * 0.14
                    white = white_score * 0.86 + classical.white * 0.14
                row.append(
                    PointEvidence(
                        max(0.0, min(1.0, black)),
                        max(0.0, min(1.0, white)),
                        max(0.0, min(1.0, 1.0 - empty_score)),
                        classical.circle,
                        classical.delta_luma,
                        empty_score,
                        unknown_score,
                    )
                )
            rows.append(tuple(row))
        return tuple(rows), probabilities is not None

    def _logical_to_screen(self, x: int, y: int) -> tuple[int, int]:
        if self.rotation == 180:
            return self.size - 1 - x, self.size - 1 - y
        return x, y

    def _screen_to_logical(self, x: int, y: int) -> tuple[int, int]:
        return self._logical_to_screen(x, y)

    def grid_alignment_score(self, image: np.ndarray) -> float:
        """Measure whether all expected full-length grid lines are still present."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
        margin = self.spacing
        end = margin + self.spacing * (self.size - 1)
        search_radius = max(4, round(self.spacing * 0.19))
        center_half = max(1, round(self.spacing * 0.025))
        side_inner = max(center_half + 2, round(self.spacing * 0.09))
        side_outer = max(side_inner + 2, round(self.spacing * 0.16))
        responses: list[float] = []
        for vertical in (True, False):
            for index in range(self.size):
                expected = margin + index * self.spacing
                best = 0.0
                for offset in range(-search_radius, search_radius + 1):
                    point = expected + offset
                    if vertical:
                        center = gray[
                            margin : end + 1,
                            point - center_half : point + center_half + 1,
                        ]
                        sides = np.concatenate(
                            (
                                gray[margin : end + 1, point - side_outer : point - side_inner].ravel(),
                                gray[margin : end + 1, point + side_inner : point + side_outer].ravel(),
                            )
                        )
                    else:
                        center = gray[
                            point - center_half : point + center_half + 1,
                            margin : end + 1,
                        ]
                        sides = np.concatenate(
                            (
                                gray[point - side_outer : point - side_inner, margin : end + 1].ravel(),
                                gray[point + side_inner : point + side_outer, margin : end + 1].ravel(),
                            )
                        )
                    if center.size and sides.size:
                        contrast = abs(float(np.mean(sides)) - float(np.mean(center)))
                        best = max(best, contrast / 35.0)
                responses.append(min(1.0, best))
        return float(np.mean(responses)) if responses else 0.0

    def ensure_alignment(self, warped: WarpedBoard) -> float:
        """Record grid visibility without stopping a geometrically locked scan.

        The region tracker already validates the board location with two
        independent templates and periodic full-grid fits.  Dense fighting,
        coordinate labels and last-move overlays can hide enough grid pixels
        to make this appearance-only score low even while both geometric
        templates remain above 95%. Treating that score as a fatal alignment
        error put the capture loop into permanent recovery and stopped all
        move updates. Shape changes are still rejected; the score itself is
        now diagnostic only.
        """
        if warped.image.shape != self.baseline_image.shape:
            raise ValueError("实时棋盘尺寸与基准不一致，请重新采集基准")
        self.last_grid_score = self.grid_alignment_score(warped.image)
        return self.last_grid_score

    def _feature(
        self,
        lab_image: np.ndarray,
        hsv_image: np.ndarray,
        gradient_magnitude: np.ndarray,
        cx: int,
        cy: int,
        masks: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    ) -> PatchFeature:
        # Only compare the body of a possible stone pixel-for-pixel.  The old
        # implementation used nearly the whole cell, so a one-pixel grid
        # shift or moving wood grain could look like a stone on dozens of
        # intersections.  A real stone covers most of this inner disk.
        radius = max(8, int(self.spacing * 0.65))
        y0, y1 = max(0, cy - radius), min(lab_image.shape[0], cy + radius + 1)
        x0, x1 = max(0, cx - radius), min(lab_image.shape[1], cx + radius + 1)
        mask_y0 = y0 - (cy - radius)
        mask_x0 = x0 - (cx - radius)
        mask_y1 = mask_y0 + (y1 - y0)
        mask_x1 = mask_x0 + (x1 - x0)
        body_template, ring_template, marker_template, annulus_template = masks
        body = body_template[mask_y0:mask_y1, mask_x0:mask_x1]
        ring = ring_template[mask_y0:mask_y1, mask_x0:mask_x1]
        marker = marker_template[mask_y0:mask_y1, mask_x0:mask_x1]
        annulus = annulus_template[mask_y0:mask_y1, mask_x0:mask_x1]
        lab = lab_image[y0:y1, x0:x1]
        hsv = hsv_image[y0:y1, x0:x1]
        pixels_lab = lab[body].astype(np.float32)
        median = np.median(pixels_lab, axis=0)
        saturation = float(np.median(hsv[..., 1][body]))
        ring_median = np.median(lab[ring].astype(np.float32), axis=0)
        ring_saturation = float(np.median(hsv[..., 1][ring]))
        marker_hue = float(np.median(hsv[..., 0][marker]))
        marker_saturation = float(np.median(hsv[..., 1][marker]))
        marker_value = float(np.median(hsv[..., 2][marker]))
        red_hue = 1.0 if marker_hue <= 12.0 or marker_hue >= 168.0 else 0.0
        # Near-black glossy pixels have unstable HSV hue/saturation, so hue
        # alone incorrectly marks every black stone as the latest move.  A red
        # UI marker is both saturated and visibly bright.
        marker_strength = (
            red_hue
            * _sigmoid((marker_saturation - 105.0) / 14.0)
            * _sigmoid((marker_value - 105.0) / 14.0)
        )

        magnitude = gradient_magnitude[y0:y1, x0:x1]
        edge_strength = float(np.percentile(magnitude[annulus], 76)) if np.any(annulus) else 0.0
        return PatchFeature(
            median,
            saturation,
            ring_median,
            ring_saturation,
            edge_strength,
            pixels_lab,
            marker_strength,
        )

    def _features(self, image: np.ndarray) -> tuple[tuple[PatchFeature, ...], ...]:
        # Colour conversion, blur and Sobel are board-wide operations. Doing
        # them inside every one of the 361 patches multiplied live latency by
        # hundreds with no additional information.
        lab = cv2.GaussianBlur(cv2.cvtColor(image, cv2.COLOR_BGR2LAB), (5, 5), 0)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        magnitude = cv2.magnitude(grad_x, grad_y)
        radius = max(8, int(self.spacing * 0.65))
        coordinates = np.arange(-radius, radius + 1, dtype=np.float32)
        yy, xx = np.meshgrid(coordinates, coordinates, indexing="ij")
        distance = np.sqrt(xx * xx + yy * yy)
        masks = (
            distance <= self.spacing * 0.35,
            (distance >= self.spacing * 0.49) & (distance <= self.spacing * 0.62),
            distance <= self.spacing * 0.12,
            (distance >= self.spacing * 0.34) & (distance <= self.spacing * 0.50),
        )
        return tuple(
            tuple(
                self._feature(
                    lab,
                    hsv,
                    magnitude,
                    *self.intersections[y][x],
                    masks,
                )
                for x in range(self.size)
            )
            for y in range(self.size)
        )

    def _normalize_to_baseline(self, image: np.ndarray) -> np.ndarray:
        """Remove whole-board brightness/colour changes before classifying cells.

        Screen brightness, a translucent window and browser colour management
        can move every Lab value enough to cross a fixed threshold.  Most of
        the board is still unchanged, so a robust affine fit can map the live
        frame back to the empty-board reference without learning the stones.
        """
        baseline_lab = cv2.cvtColor(self.baseline_image, cv2.COLOR_BGR2LAB).astype(np.float32)
        current_lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
        margin = max(0, self.spacing // 2)
        end = min(image.shape[0], self.spacing + self.spacing * (self.size - 1) + margin + 1)
        mask = np.zeros(image.shape[:2], dtype=bool)
        mask[margin:end, margin:end] = True

        # Known stones are deliberately excluded from the fit.  A newly played
        # stone occupies far below half of the remaining pixels and is rejected
        # by the robust residual filter below.
        yy, xx = np.ogrid[: image.shape[0], : image.shape[1]]
        exclusion_radius = self.spacing * 0.48
        for logical_y in range(self.size):
            for logical_x in range(self.size):
                if self.board[logical_y][logical_x] == Stone.EMPTY:
                    continue
                screen_x, screen_y = self._logical_to_screen(logical_x, logical_y)
                cx, cy = self.intersections[screen_y][screen_x]
                mask &= (xx - cx) ** 2 + (yy - cy) ** 2 > exclusion_radius**2

        normalized = current_lab.copy()
        for channel in range(3):
            source = current_lab[..., channel][mask]
            target = baseline_lab[..., channel][mask]
            if source.size < 100:
                continue
            difference = target - source
            center = float(np.median(difference))
            residual_limit = 15.0 if channel == 0 else 9.0
            inliers = np.abs(difference - center) <= residual_limit
            if np.count_nonzero(inliers) < 100:
                normalized[..., channel] = current_lab[..., channel] + center
                continue
            fit_source = source[inliers]
            fit_target = target[inliers]
            variance = float(np.var(fit_source))
            gain = (
                float(np.mean((fit_source - np.mean(fit_source)) * (fit_target - np.mean(fit_target))))
                / variance
                if variance > 4.0
                else 1.0
            )
            gain = max(0.82, min(1.22, gain))
            offset = float(np.median(fit_target - gain * fit_source))
            normalized[..., channel] = current_lab[..., channel] * gain + offset
        normalized = np.clip(normalized, 0, 255).astype(np.uint8)
        return cv2.cvtColor(normalized, cv2.COLOR_LAB2BGR)

    @staticmethod
    def _evidence(baseline: PatchFeature, current: PatchFeature) -> PointEvidence:
        count = min(len(baseline.pixels_lab), len(current.pixels_lab))
        lab_delta = current.pixels_lab[:count] - baseline.pixels_lab[:count]
        pixel_delta = np.linalg.norm(lab_delta, axis=1)
        changed_value = float(np.percentile(pixel_delta, 62))
        changed_fraction = float(np.mean(pixel_delta >= 18.0))
        changed = 0.52 * _sigmoid((changed_value - 14.0) / 4.5) + 0.48 * _sigmoid(
            (changed_fraction - 0.38) / 0.10
        )
        luma_delta = lab_delta[:, 0]
        delta_luma = float(np.median(luma_delta))
        black_coverage = _sigmoid((float(np.mean(luma_delta <= -14.0)) - 0.43) / 0.09)
        white_coverage = _sigmoid((float(np.mean(luma_delta >= 14.0)) - 0.43) / 0.09)
        saturation_drop = baseline.saturation_median - current.saturation_median
        circle_delta = current.edge_strength - baseline.edge_strength
        circle = _sigmoid((circle_delta - 7.0) / 7.0)

        black_direction = _sigmoid((-delta_luma - 13.0) / 6.0)
        white_direction = _sigmoid((delta_luma - 12.0) / 6.0)
        # Black and white stones are normally more neutral than wood.  This
        # term suppresses saturated last-move/candidate markers while still
        # allowing neutral grey board themes where the saturation drop is 0.
        neutral = max(
            _sigmoid((saturation_drop - 4.0) / 10.0),
            _sigmoid((48.0 - current.saturation_median) / 13.0),
        )

        black_shape = black_direction * 0.45 + black_coverage * 0.35 + neutral * 0.12 + circle * 0.08
        white_shape = white_direction * 0.45 + white_coverage * 0.35 + neutral * 0.12 + circle * 0.08
        # Change is a gate, not an additive vote.  Thus a shifted grid line or
        # wood texture cannot become a stone merely by having a large delta.
        black = max(0.0, min(1.0, changed * black_shape))
        white = max(0.0, min(1.0, changed * white_shape))
        return PointEvidence(black, white, changed, circle, delta_luma)

    @staticmethod
    def _absolute_evidence(current: PatchFeature) -> PointEvidence:
        """Classify one intersection from its own centre/ring geometry.

        This is deliberately independent from the calibration image.  The
        latter may already contain a mid-game position, so treating every
        calibration patch as empty makes all existing stones invisible.
        """
        ring_luma = float(current.ring_lab_median[0])
        radial_luma = float(current.lab_median[0]) - ring_luma
        body_luma = current.pixels_lab[:, 0]
        dark_fraction = float(np.mean(body_luma <= ring_luma - 14.0))
        bright_fraction = float(np.mean(body_luma >= ring_luma + 11.0))

        black_direction = _sigmoid((-radial_luma - 14.0) / 7.0)
        white_direction = _sigmoid((radial_luma - 10.0) / 5.0)
        black_coverage = _sigmoid((dark_fraction - 0.42) / 0.09)
        white_coverage = _sigmoid((bright_fraction - 0.40) / 0.09)
        saturation_drop = current.ring_saturation_median - current.saturation_median
        neutral = max(
            _sigmoid((saturation_drop - 4.0) / 10.0),
            _sigmoid((48.0 - current.saturation_median) / 13.0),
        )
        circle = _sigmoid((current.edge_strength - 58.0) / 16.0)

        black = (
            black_direction * 0.46
            + black_coverage * 0.34
            + neutral * 0.10
            + circle * 0.10
        )
        white = (
            white_direction * 0.46
            + white_coverage * 0.34
            + neutral * 0.12
            + circle * 0.08
        )
        black = max(0.0, min(1.0, black))
        white = max(0.0, min(1.0, white))
        return PointEvidence(black, white, max(black, white), circle, radial_luma)

    def recognize_position(self, warped: WarpedBoard) -> PositionRecognition:
        """Recognize a complete current position without requiring an empty board."""
        self.ensure_alignment(warped)
        features = self._features(warped.image)
        evidence_rows, model_active = self._evidence_grid(warped.image, features)
        rows = [[Stone.EMPTY for _ in range(self.size)] for _ in range(self.size)]
        score_rows = [[(1.0, 0.0, 0.0) for _ in range(self.size)] for _ in range(self.size)]
        confidences: list[float] = []
        markers: list[tuple[float, int, int, Stone]] = []
        unknown_points = 0

        for screen_y, feature_row in enumerate(features):
            for screen_x, feature in enumerate(feature_row):
                evidence = evidence_rows[screen_y][screen_x]
                black_score = evidence.score(Stone.BLACK)
                white_score = evidence.score(Stone.WHITE)
                strongest = max(black_score, white_score)
                logical_x, logical_y = self._screen_to_logical(screen_x, screen_y)
                empty_score = (
                    evidence.empty
                    if model_active
                    else max(0.0, min(1.0, 1.0 - evidence.change))
                )
                score_total = max(1e-6, empty_score + black_score + white_score)
                score_rows[logical_y][logical_x] = (
                    empty_score / score_total,
                    black_score / score_total,
                    white_score / score_total,
                )
                stone_threshold = self.threshold * 0.82 if model_active else self.threshold
                ambiguous = (
                    evidence.unknown >= 0.48
                    or (
                        model_active
                        and strongest >= evidence.empty - 0.04
                        and abs(black_score - white_score) < 0.085
                    )
                )
                if model_active and ambiguous:
                    rows[logical_y][logical_x] = Stone.UNKNOWN
                    confidences.append(0.0)
                    unknown_points += 1
                elif strongest < stone_threshold or (
                    model_active and strongest < evidence.empty + 0.045
                ):
                    rows[logical_y][logical_x] = Stone.EMPTY
                    confidences.append(evidence.empty if model_active else max(0.0, 1.0 - evidence.change))
                elif abs(black_score - white_score) < 0.10:
                    rows[logical_y][logical_x] = Stone.UNKNOWN
                    confidences.append(0.0)
                    unknown_points += 1
                else:
                    color = Stone.BLACK if black_score > white_score else Stone.WHITE
                    rows[logical_y][logical_x] = color
                    confidences.append(strongest)
                    markers.append((feature.marker_strength, logical_x, logical_y, color))

        board = tuple(tuple(row) for row in rows)
        markers.sort(reverse=True)
        last_move: Move | None = None
        if markers:
            marker_score, marker_x, marker_y, marker_color = markers[0]
            second_marker = markers[1][0] if len(markers) > 1 else 0.0
            if marker_score >= 0.72 and marker_score >= second_marker + 0.10:
                last_move = Move(marker_x, marker_y, marker_color, self.size)

        raw_black_count = sum(value == Stone.BLACK for row in board for value in row)
        raw_white_count = sum(value == Stone.WHITE for row in board for value in row)
        if last_move is not None:
            next_color = Stone.WHITE if last_move.color == Stone.BLACK else Stone.BLACK
        else:
            next_color = Stone.BLACK if raw_black_count <= raw_white_count else Stone.WHITE
        last_mover = (
            last_move.color
            if last_move is not None
            else (Stone.WHITE if next_color == Stone.BLACK else Stone.BLACK)
        )
        board = normalize_snapshot_captures(board, last_mover)
        confidence = float(np.mean(confidences)) if confidences else 0.0
        return PositionRecognition(
            board,
            confidence,
            unknown_points,
            next_color,
            last_move,
            tuple(tuple(row) for row in score_rows),
        )

    def bootstrap(self, recognition: PositionRecognition) -> None:
        """Install an arbitrary recognized position as the authoritative state."""
        if recognition.unknown_points:
            raise ValueError(f"当前局面仍有 {recognition.unknown_points} 个交叉点无法确认")
        last_mover = (
            recognition.last_move.color
            if recognition.last_move is not None
            else (Stone.WHITE if recognition.next_color == Stone.BLACK else Stone.BLACK)
        )
        self.board = normalize_snapshot_captures(recognition.board, last_mover)
        self.board_history = [self.board]
        self.move_history = []
        stone_count = sum(value in (Stone.BLACK, Stone.WHITE) for row in self.board for value in row)
        self.move_count = stone_count
        self._next_color = recognition.next_color
        self.next_color_history = [self._next_color]
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []
        self.analysis_frame_index = 0
        self.quick_frame_signature = None
        self.quick_frame_skips = 0
        self.marker_capable = self.marker_capable or recognition.last_move is not None
        self.last_analysis = None

    def _absolute_position_from_evidence(
        self,
        evidence_grid: tuple[tuple[PointEvidence, ...], ...],
        model_active: bool,
    ) -> tuple[
        tuple[tuple[Stone, ...], ...],
        float,
        int,
        tuple[tuple[float, ...], ...],
    ]:
        """Classify the whole board without consulting confirmed state.

        This independent view is the recovery authority. Unlike the normal
        transition detector it never preserves an old stone merely because
        the state machine previously accepted it, so any number of false
        positives, missed stones or wrong colours can eventually be replaced
        by a stable visual snapshot.
        """
        rows = [[Stone.EMPTY for _ in range(self.size)] for _ in range(self.size)]
        confidence_rows = [[0.0 for _ in range(self.size)] for _ in range(self.size)]
        confidences: list[float] = []
        unknown_points = 0
        for screen_y, evidence_row in enumerate(evidence_grid):
            for screen_x, evidence in enumerate(evidence_row):
                black_score = evidence.score(Stone.BLACK)
                white_score = evidence.score(Stone.WHITE)
                strongest = max(black_score, white_score)
                logical_x, logical_y = self._screen_to_logical(screen_x, screen_y)
                stone_threshold = self.threshold * 0.82 if model_active else self.threshold
                ambiguous = (
                    evidence.unknown >= 0.48
                    or (
                        model_active
                        and strongest >= evidence.empty - 0.04
                        and abs(black_score - white_score) < 0.085
                    )
                )
                if ambiguous:
                    value = Stone.UNKNOWN
                    confidence = 0.0
                    unknown_points += 1
                elif strongest < stone_threshold or (
                    model_active and strongest < evidence.empty + 0.045
                ):
                    value = Stone.EMPTY
                    if model_active:
                        # Adjacent stone rims can lower ONNX's absolute EMPTY
                        # probability, yet EMPTY is still decisive when it wins
                        # by a large margin. Conversely, a weak EMPTY score that
                        # loses to stone evidence must never authorize deletion.
                        confidence = max(
                            0.0,
                            min(1.0, 0.55 + (evidence.empty - strongest) * 0.80),
                        )
                    else:
                        confidence = max(0.0, 1.0 - evidence.change)
                elif abs(black_score - white_score) < 0.10:
                    value = Stone.UNKNOWN
                    confidence = 0.0
                    unknown_points += 1
                elif black_score > white_score:
                    value = Stone.BLACK
                    confidence = min(1.0, black_score + abs(black_score - white_score))
                else:
                    value = Stone.WHITE
                    confidence = min(1.0, white_score + abs(black_score - white_score))
                rows[logical_y][logical_x] = value
                confidence_rows[logical_y][logical_x] = confidence
                confidences.append(confidence)
        board = tuple(tuple(row) for row in rows)
        return (
            board,
            float(np.mean(confidences)) if confidences else 0.0,
            unknown_points,
            tuple(tuple(row) for row in confidence_rows),
        )

    def _absolute_consensus(
        self,
    ) -> tuple[tuple[tuple[Stone, ...], ...], float]:
        """Return a stable 2-of-3 correction of the confirmed position.

        Requiring all 361 intersections to clear an absolute confidence gate
        made recovery practically impossible: one weak but *unchanged* empty
        point rejected an otherwise exact new position forever.  The state
        machine already owns a legal confirmed board, so only points that want
        to change that board need strong, repeated evidence.  Weak/unknown
        unchanged points retain their confirmed value while confident deltas
        can still repair one missed move, captures, or several earlier visual
        mistakes.
        """
        if len(self.absolute_observations) < 2:
            return (), 0.0
        rows: list[list[Stone]] = []
        changed_confidences: list[float] = []
        for y in range(self.size):
            row: list[Stone] = []
            for x in range(self.size):
                votes = {
                    stone: sum(
                        board[y][x] == stone
                        for board, _ in self.absolute_observations
                    )
                    for stone in (Stone.EMPTY, Stone.BLACK, Stone.WHITE)
                }
                winner = max(votes, key=votes.get)
                confirmed = self.board[y][x]
                if votes[winner] < 2 or winner == confirmed:
                    row.append(confirmed)
                    continue
                winner_confidences = [
                    point_confidences[y][x]
                    for board, point_confidences in self.absolute_observations
                    if board[y][x] == winner
                ]
                point_confidence = float(np.mean(winner_confidences))
                # Deleting a confirmed stone is deliberately stricter than
                # adding one. Captures are also verified by Go rules before a
                # normal single-move transition is committed.
                required_confidence = 0.62 if winner == Stone.EMPTY else 0.55
                if point_confidence < required_confidence:
                    row.append(confirmed)
                    continue
                changed_confidences.append(point_confidence)
                row.append(winner)
            rows.append(row)
        # Confidence describes the proposed correction, not unrelated stable
        # intersections.  An unchanged consensus remains useful for comparison
        # but is never selected as a snapshot candidate below.
        confidence = float(np.mean(changed_confidences)) if changed_confidences else 1.0
        return tuple(tuple(row) for row in rows), confidence

    def analyze(
        self,
        warped: WarpedBoard,
        expected_color: Stone | None = None,
        track_stability: bool = True,
    ) -> ScanAnalysis:
        if track_stability:
            self.analysis_frame_index += 1
        # The region tracker validates geometry on every captured frame. The
        # dense 19-line appearance score is diagnostic only, so sampling it
        # periodically avoids another full-board pass on every 120 ms scan.
        if not track_stability or self.analysis_frame_index % 8 == 1:
            self.ensure_alignment(warped)
        elif warped.image.shape != self.baseline_image.shape:
            raise ValueError("实时棋盘尺寸与基准不一致，请重新采集基准")
        expected_color = expected_color or self.next_color
        current_features = self._features(warped.image)
        evidence_grid, model_active = self._evidence_grid(warped.image, current_features)
        (
            absolute_board,
            absolute_confidence,
            _,
            absolute_point_confidences,
        ) = self._absolute_position_from_evidence(evidence_grid, model_active)
        last_mover = Stone.WHITE if expected_color == Stone.BLACK else Stone.BLACK
        absolute_board = normalize_snapshot_captures(absolute_board, last_mover)
        if track_stability:
            self.absolute_observations.append((absolute_board, absolute_point_confidences))
            self.absolute_observations = self.absolute_observations[-3:]
        reconciled_board, reconciled_confidence = self._absolute_consensus()
        score_rows = [[0.0 for _ in range(self.size)] for _ in range(self.size)]
        candidates: list[tuple[float, int, int]] = []
        opposite_candidates: list[tuple[float, int, int]] = []
        opposite_color = Stone.WHITE if expected_color == Stone.BLACK else Stone.BLACK

        for screen_y in range(self.size):
            for screen_x in range(self.size):
                evidence = evidence_grid[screen_y][screen_x]
                logical_x, logical_y = self._screen_to_logical(screen_x, screen_y)
                if self.board[logical_y][logical_x] == Stone.EMPTY:
                    score = evidence.score(expected_color)
                    score_rows[logical_y][logical_x] = score
                    candidates.append((score, logical_x, logical_y))
                    opposite_candidates.append((evidence.score(opposite_color), logical_x, logical_y))

        observed_rows = [[Stone.EMPTY for _ in range(self.size)] for _ in range(self.size)]
        observed_scores: list[float] = []
        unexpected_stones = 0
        unknown_points = 0
        for screen_y, evidence_row in enumerate(evidence_grid):
            for screen_x, evidence in enumerate(evidence_row):
                black_score = evidence.score(Stone.BLACK)
                white_score = evidence.score(Stone.WHITE)
                logical_x, logical_y = self._screen_to_logical(screen_x, screen_y)
                strongest = max(black_score, white_score)
                previous = self.board[logical_y][logical_x]
                stone_threshold = self.threshold * 0.82 if model_active else self.threshold
                ambiguous = (
                    evidence.unknown >= 0.48
                    or (
                        model_active
                        and strongest >= evidence.empty - 0.04
                        and abs(black_score - white_score) < 0.085
                    )
                )
                if model_active and ambiguous:
                    if previous in (Stone.BLACK, Stone.WHITE):
                        previous_score = evidence.score(previous)
                        other = Stone.WHITE if previous == Stone.BLACK else Stone.BLACK
                        # An overlay, glare or the edge of a neighbouring
                        # stone must not erase an already-confirmed stone. A
                        # colour change is impossible without first capturing
                        # it, which is validated from a candidate move below.
                        other_score = evidence.score(other)
                        if previous_score >= max(
                            self.threshold * 0.45,
                            other_score - 0.10,
                            evidence.empty - 0.08,
                        ):
                            observed_rows[logical_y][logical_x] = previous
                            observed_scores.append(max(0.35, previous_score))
                        elif evidence.empty >= 0.55 and evidence.empty >= previous_score + 0.12:
                            # A previously committed hover ghost may disappear
                            # without a legal capture. Two stable full-board
                            # frames will reconcile that correction; keeping it
                            # UNKNOWN here would block recovery forever.
                            observed_rows[logical_y][logical_x] = Stone.EMPTY
                            observed_scores.append(evidence.empty)
                        else:
                            observed_rows[logical_y][logical_x] = Stone.UNKNOWN
                            observed_scores.append(0.0)
                            unknown_points += 1
                    elif strongest < stone_threshold + 0.08 or evidence.empty >= strongest - 0.07:
                        # UNKNOWN is commonly produced at coordinate labels,
                        # star points and beside glossy stones. For a point
                        # previously confirmed empty, keep it empty unless a
                        # stone has clear positive evidence; the legal-move
                        # candidate path still detects that new stone.
                        observed_rows[logical_y][logical_x] = Stone.EMPTY
                        observed_scores.append(max(0.30, evidence.empty))
                    else:
                        observed_rows[logical_y][logical_x] = Stone.UNKNOWN
                        observed_scores.append(0.0)
                        unknown_points += 1
                elif strongest < stone_threshold or (
                    model_active and strongest < evidence.empty + 0.045
                ):
                    if evidence.change >= 0.62:
                        observed_rows[logical_y][logical_x] = Stone.UNKNOWN
                        observed_scores.append(0.0)
                        unknown_points += 1
                    else:
                        observed_rows[logical_y][logical_x] = Stone.EMPTY
                        observed_scores.append(
                            evidence.empty if model_active else max(0.0, 1.0 - evidence.change)
                        )
                elif abs(black_score - white_score) < 0.10:
                    observed_rows[logical_y][logical_x] = Stone.UNKNOWN
                    observed_scores.append(0.0)
                    unknown_points += 1
                elif black_score > white_score:
                    observed_rows[logical_y][logical_x] = Stone.BLACK
                    observed_scores.append(min(1.0, black_score + abs(black_score - white_score)))
                    if self.board[logical_y][logical_x] == Stone.EMPTY:
                        unexpected_stones += 1
                else:
                    observed_rows[logical_y][logical_x] = Stone.WHITE
                    observed_scores.append(min(1.0, white_score + abs(black_score - white_score)))
                    if self.board[logical_y][logical_x] == Stone.EMPTY:
                        unexpected_stones += 1
        observed_board = tuple(tuple(row) for row in observed_rows)
        observed_confidence = float(np.mean(observed_scores)) if observed_scores else 0.0
        # This provisional value is refined after comparing the observation
        # with the predicted result of a normal single move.
        frame_valid = unexpected_stones <= 1 and unknown_points <= max(8, self.size)

        candidates.sort(reverse=True)
        legal_candidates: list[tuple[float, int, int, tuple[tuple[Stone, ...], ...]]] = []
        for score, x, y in candidates:
            try:
                predicted = legal_move_result(
                    self.board,
                    Move(x, y, expected_color, self.size),
                    self.board_history,
                )
            except ValueError:
                continue
            legal_candidates.append((score, x, y, predicted))
            if len(legal_candidates) >= 2:
                break
        if legal_candidates:
            best_score, best_x, best_y, predicted_board = legal_candidates[0]
            second_score = legal_candidates[1][0] if len(legal_candidates) > 1 else 0.0
            best = Move(best_x, best_y, expected_color, self.size)
        else:
            best_score, best_x, best_y, predicted_board, second_score, best = (
                0.0,
                0,
                0,
                self.board,
                0.0,
                None,
            )

        exact_single_transition = (
            best is not None
            and unknown_points == 0
            and predicted_board == observed_board
        )
        predicted_captures = tuple(
            (x, y, self.board[y][x])
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x] in (Stone.BLACK, Stone.WHITE)
            and predicted_board[y][x] == Stone.EMPTY
        )
        # Captures are a consequence of one legal move, not a collection of
        # independent visual deletions. Some clients animate removed stones or
        # leave a short-lived shadow, so the intersection classifier can still
        # report the old group after the new capturing stone is already clear.
        # In the unambiguous one-new-stone case the Go state machine is the
        # authority for removals and a stale full-board snapshot must not win.
        rule_based_capture = (
            best is not None
            and bool(predicted_captures)
            and unexpected_stones == 1
        )
        predicted_capture_points = {(x, y) for x, y, _ in predicted_captures}
        marker_candidates: list[tuple[float, int, int, Stone]] = []
        for screen_y, feature_row in enumerate(current_features):
            for screen_x, feature in enumerate(feature_row):
                logical_x, logical_y = self._screen_to_logical(screen_x, screen_y)
                color = absolute_board[logical_y][logical_x]
                if color in (Stone.BLACK, Stone.WHITE):
                    marker_candidates.append(
                        (feature.marker_strength, logical_x, logical_y, color)
                    )
        marker_candidates.sort(reverse=True)
        snapshot_last_move: Move | None = None
        if marker_candidates:
            marker_score, marker_x, marker_y, marker_color = marker_candidates[0]
            second_marker_score = marker_candidates[1][0] if len(marker_candidates) > 1 else 0.0
            if marker_score >= 0.72 and marker_score >= second_marker_score + 0.10:
                snapshot_last_move = Move(marker_x, marker_y, marker_color, self.size)
                if track_stability:
                    self.marker_capable = True

        # A hover preview is normally a translucent copy of a stone. ONNX may
        # classify its outline correctly, but its centre/ring luma contrast is
        # materially weaker than real stones of the same colour. A genuine
        # newly played stone is also commonly identified by the client's red
        # latest-move marker, which is an immediate solidity override.
        reference_contrasts: dict[Stone, list[float]] = {
            Stone.BLACK: [],
            Stone.WHITE: [],
        }
        for logical_y in range(self.size):
            for logical_x in range(self.size):
                color = self.board[logical_y][logical_x]
                if color not in reference_contrasts:
                    continue
                screen_x, screen_y = self._logical_to_screen(logical_x, logical_y)
                evidence = evidence_grid[screen_y][screen_x]
                if absolute_board[logical_y][logical_x] == color:
                    reference_contrasts[color].append(abs(evidence.delta_luma))

        def looks_like_solid_stone(x: int, y: int, color: Stone) -> bool:
            screen_x, screen_y = self._logical_to_screen(x, y)
            evidence = evidence_grid[screen_y][screen_x]
            feature = current_features[screen_y][screen_x]
            if feature.marker_strength >= 0.64:
                return True
            contrast = abs(evidence.delta_luma)
            references = reference_contrasts[color]
            if len(references) >= 2:
                median_reference = float(np.median(references))
                return contrast >= max(18.0, median_reference * 0.76)
            # Opening positions may not yet contain two same-colour reference
            # stones. Use a deliberately strict absolute fallback; clients
            # with a latest-move dot take the marker path above.
            if self.marker_capable:
                minimum = 110.0 if color == Stone.BLACK else 55.0
            else:
                minimum = 105.0 if color == Stone.BLACK else 50.0
            return contrast >= minimum

        hover_previews = tuple(
            Move(x, y, absolute_board[y][x], self.size)
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x] == Stone.EMPTY
            and absolute_board[y][x] in (Stone.BLACK, Stone.WHITE)
            and not looks_like_solid_stone(x, y, absolute_board[y][x])
        )
        hover_points = {(move.x, move.y) for move in hover_previews}
        candidate_is_hover = best is not None and (best.x, best.y) in hover_points
        # A mouse preview somewhere else on the board must not block a clear,
        # legal move. Only the candidate itself is subject to the hover gate;
        # unrelated previews are removed from the unexpected-stone count.
        effective_unexpected_stones = max(0, unexpected_stones - len(hover_points))
        # A cursor preview elsewhere on the board is not part of the game
        # state. Remove those points from the recovery snapshot instead of
        # disabling full-position recovery globally whenever the mouse happens
        # to remain inside the board.
        if reconciled_board and hover_points:
            snapshot_rows = [list(row) for row in reconciled_board]
            for hover_x, hover_y in hover_points:
                snapshot_rows[hover_y][hover_x] = self.board[hover_y][hover_x]
            recovery_board = tuple(tuple(row) for row in snapshot_rows)
        else:
            recovery_board = reconciled_board
        board_changed = bool(recovery_board) and recovery_board != self.board
        stale_capture_snapshot = (
            rule_based_capture
            and bool(recovery_board)
            and all(
                recovery_board[y][x] == predicted_board[y][x]
                or (
                    (x, y) in predicted_capture_points
                    and recovery_board[y][x] == self.board[y][x]
                )
                for y in range(self.size)
                for x in range(self.size)
            )
        )
        # Track every complete changed board, including a visually exact
        # single move. The normal legal-move path gets priority, but if its
        # score gate remains marginal the second identical frame can still
        # resynchronize the full position instead of waiting indefinitely.
        snapshot_candidate = (
            track_stability
            and board_changed
            and reconciled_confidence >= 0.55
            and not stale_capture_snapshot
        )
        exact_reconciled_transition = (
            exact_single_transition
            and bool(recovery_board)
            and predicted_board == recovery_board
        )
        requires_snapshot = snapshot_candidate and not exact_reconciled_transition
        snapshot_stable_frames = 2 if snapshot_candidate else 0
        snapshot_board = recovery_board if snapshot_candidate else ()
        frame_valid = (
            (effective_unexpected_stones <= 1 or snapshot_candidate)
            and unknown_points <= max(8, self.size)
        )

        observed_stone_count = sum(
            value in (Stone.BLACK, Stone.WHITE)
            for row in (snapshot_board or absolute_board)
            for value in row
        )
        removed_confirmed = [
            (x, y, self.board[y][x])
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x] in (Stone.BLACK, Stone.WHITE)
            and (snapshot_board or absolute_board)[y][x] not in (self.board[y][x], Stone.UNKNOWN)
        ]
        added_observed = [
            (x, y, (snapshot_board or absolute_board)[y][x])
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x] == Stone.EMPTY
            and (snapshot_board or absolute_board)[y][x] in (Stone.BLACK, Stone.WHITE)
        ]
        relocated_same_move = (
            len(removed_confirmed) == 1
            and len(added_observed) == 1
            and removed_confirmed[0][2] == added_observed[0][2]
        )
        if observed_stone_count == 0:
            # A cleared board is a new game even if the previous game ended
            # with White to move.
            snapshot_next_color = Stone.BLACK
        elif snapshot_last_move is not None:
            snapshot_next_color = (
                Stone.WHITE if snapshot_last_move.color == Stone.BLACK else Stone.BLACK
            )
        elif relocated_same_move:
            # Correcting a hover false-positive moves the same already-counted
            # colour to its real coordinate; it must not toggle the turn again.
            snapshot_next_color = self.next_color
        elif effective_unexpected_stones % 2 == 1:
            snapshot_next_color = (
                Stone.WHITE if expected_color == Stone.BLACK else Stone.BLACK
            )
        else:
            snapshot_next_color = self.next_color
        opposite_candidates.sort(reverse=True)
        opposite_score, opposite_x, opposite_y = (
            opposite_candidates[0] if opposite_candidates else (0.0, 0, 0)
        )
        opposite_best = (
            Move(opposite_x, opposite_y, opposite_color, self.size) if opposite_candidates else None
        )
        known_point_matches: list[bool] = []
        for logical_y in range(self.size):
            for logical_x in range(self.size):
                old_stone = self.board[logical_y][logical_x]
                predicted_stone = predicted_board[logical_y][logical_x]
                if old_stone == Stone.EMPTY:
                    continue
                screen_x, screen_y = self._logical_to_screen(logical_x, logical_y)
                evidence = evidence_grid[screen_y][screen_x]
                if predicted_stone == Stone.EMPTY:
                    # A previously known stone may disappear only when the
                    # candidate move legally captures it. Once that condition
                    # is proven, ignore stale pixels/animations at the removed
                    # intersections and validate the surviving board instead.
                    if rule_based_capture:
                        continue
                    known_point_matches.append(
                        max(evidence.score(Stone.BLACK), evidence.score(Stone.WHITE))
                        < self.threshold * 0.78
                    )
                else:
                    other_color = Stone.WHITE if predicted_stone == Stone.BLACK else Stone.BLACK
                    known_score = evidence.score(predicted_stone)
                    known_point_matches.append(
                        known_score >= self.threshold * 0.82
                        and known_score >= evidence.score(other_color) - 0.04
                    )
        board_agreement = sum(known_point_matches) / len(known_point_matches) if known_point_matches else 1.0
        fingerprint = (best_x, best_y, expected_color)
        separated = best_score - second_score >= 0.035 or best_score >= 0.78
        board_consistent = (
            board_agreement >= 0.82
            and frame_valid
            and not candidate_is_hover
        )
        exact_board_fast_path = (
            exact_single_transition
            and not candidate_is_hover
            and best_score >= self.threshold * 0.82
            and best_score - opposite_score >= 0.06
            and board_agreement >= 0.90
            and unexpected_stones == 1
            and unknown_points == 0
        )
        isolated_stone_fast_path = (
            not candidate_is_hover
            and
            best_score >= max(0.84, self.threshold + 0.16)
            and best_score - second_score >= 0.10
            and best_score - opposite_score >= 0.16
            and board_agreement >= 0.94
            and unexpected_stones == 1
            and unknown_points <= 1
        )
        rule_capture_fast_path = (
            rule_based_capture
            and not candidate_is_hover
            and best_score >= self.threshold * 0.82
            and separated
            and best_score - opposite_score >= 0.06
            and board_agreement >= 0.90
            and unknown_points <= max(2, len(predicted_captures))
        )
        fast_accepted = (
            track_stability
            and best is not None
            and frame_valid
            and (exact_board_fast_path or isolated_stone_fast_path or rule_capture_fast_path)
        )
        if fast_accepted:
            # A visually isolated, legal stone with a large score margin does
            # not benefit from waiting for an identical second frame. Medium
            # confidence moves retain the normal multi-frame confirmation.
            self.pending = fingerprint
            self.pending_frames = self.stable_required
        elif (
            track_stability
            and best_score >= self.threshold * 0.84
            and separated
            and board_consistent
        ):
            if self.pending == fingerprint:
                self.pending_frames += 1
            else:
                self.pending = fingerprint
                self.pending_frames = 1
        elif track_stability:
            self.pending = None
            self.pending_frames = 0
        stable_frames = self.pending_frames if track_stability else 0
        required_frames = (
            self.stable_required
            if fast_accepted or best_score >= self.threshold
            else self.stable_required + 1
        )
        accepted = best if track_stability and stable_frames >= required_frames else None
        # Invalid full-board states must not leak a plausible-looking point to
        # the UI.  Keep the evidence grid for diagnostics, but publish no move.
        publishable = (
            frame_valid
            and best_score >= self.threshold * 0.84
            and separated
            and board_consistent
        )
        published_best = best if publishable else None
        published_best_score = best_score if publishable else 0.0
        published_second_score = second_score if publishable else 0.0
        comparison_board = reconciled_board or absolute_board
        reconciliation_differences = sum(
            comparison_board[y][x] != Stone.UNKNOWN
            and comparison_board[y][x] != self.board[y][x]
            for y in range(self.size)
            for x in range(self.size)
        )
        result = ScanAnalysis(
            expected_color,
            tuple(tuple(row) for row in score_rows),
            evidence_grid,
            published_best,
            published_best_score,
            published_second_score,
            stable_frames,
            accepted,
            opposite_best,
            opposite_score,
            board_agreement,
            observed_board,
            observed_confidence,
            unexpected_stones,
            unknown_points,
            frame_valid,
            fast_accepted,
            snapshot_board,
            snapshot_next_color,
            snapshot_last_move,
            snapshot_stable_frames,
            hover_previews,
            absolute_board,
            reconciliation_differences,
        )
        if track_stability:
            self.last_analysis = result
        return result

    def commit(self, move: Move) -> None:
        if move.color != self.next_color:
            raise ValueError("落子颜色与当前行棋方不一致")
        if move.board_size != self.size:
            move = Move(move.x, move.y, move.color, self.size)
        self.board = legal_move_result(self.board, move, self.board_history)
        self.move_history.append(move)
        self.board_history.append(self.board)
        self.move_count += 1
        self._next_color = Stone.WHITE if move.color == Stone.BLACK else Stone.BLACK
        self.next_color_history.append(self._next_color)
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []

    def reconcile_snapshot(
        self,
        candidate: tuple[tuple[Stone, ...], ...],
        last_move: Move | None,
    ) -> Move:
        """Append a verified recovery without discarding authoritative history."""
        if (
            len(candidate) != self.size
            or any(len(row) != self.size for row in candidate)
            or any(value == Stone.UNKNOWN for row in candidate for value in row)
        ):
            raise ValueError("整盘恢复候选必须是完整已知棋盘")
        if candidate in self.board_history:
            raise ValueError("整盘恢复候选造成重复局面，已拒绝")

        transition = diff_boards(self.board, candidate)
        if not transition.changed:
            raise ValueError("整盘恢复候选没有局面变化")
        if transition.move is not None:
            predicted = legal_move_result(
                self.board,
                transition.move,
                self.board_history,
            )
            if predicted != candidate:
                raise ValueError("整盘恢复候选不是合法单步局面")
            self.commit(transition.move)
            return transition.move

        if (
            last_move is None
            or last_move.is_pass
            or last_move.color not in (Stone.BLACK, Stone.WHITE)
            or not (0 <= last_move.x < self.size and 0 <= last_move.y < self.size)
            or candidate[last_move.y][last_move.x] != last_move.color
        ):
            raise ValueError("多手整盘恢复缺少可信最后落子标记")
        if last_move.board_size != self.size:
            last_move = Move(last_move.x, last_move.y, last_move.color, self.size)

        self.board = candidate
        self.move_history.append(last_move)
        self.board_history.append(candidate)
        self.move_count += 1
        self._next_color = Stone.WHITE if last_move.color == Stone.BLACK else Stone.BLACK
        self.next_color_history.append(self._next_color)
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []
        self.last_analysis = None
        self.marker_capable = True
        return last_move

    def commit_pass(self, color: Stone | None = None) -> Move:
        color = color or self.next_color
        move = Move.pass_turn(color, self.size)
        self.move_history.append(move)
        self.board_history.append(self.board)
        self.move_count += 1
        self._next_color = Stone.WHITE if color == Stone.BLACK else Stone.BLACK
        self.next_color_history.append(self._next_color)
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []
        return move

    def set_next_color(self, color: Stone) -> None:
        """Correct side-to-move without inventing a visual board change.

        A still image cannot determine turn order after captures, handicap
        setup, passes, or when monitoring starts midway through a game. Keep
        this explicit correction in the same state machine that validates the
        next observed stone so QiDao and vision cannot diverge again.
        """
        if color not in (Stone.BLACK, Stone.WHITE):
            raise ValueError("下一手只能设置为黑方或白方")
        self._next_color = color
        if self.next_color_history:
            self.next_color_history[-1] = color
        else:
            self.next_color_history = [color]
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []

    def undo(self) -> Move | None:
        if not self.move_history:
            return None
        removed = self.move_history.pop()
        self.board_history.pop()
        self.board = self.board_history[-1]
        self.move_count -= 1
        if len(self.next_color_history) > 1:
            self.next_color_history.pop()
        self._next_color = self.next_color_history[-1]
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []
        self.last_analysis = None
        return removed

    def cancel_pending(self) -> None:
        self.pending = None
        self.pending_frames = 0
        self.pending_snapshot = None
        self.pending_snapshot_frames = 0
        self.absolute_observations = []

    def debug_image(self, current: WarpedBoard, title: str) -> np.ndarray:
        image = current.image.copy()
        analysis = self.last_analysis
        for logical_y in range(self.size):
            for logical_x in range(self.size):
                screen_x, screen_y = self._logical_to_screen(logical_x, logical_y)
                cx, cy = current.intersections[screen_y][screen_x]
                stone = self.board[logical_y][logical_x]
                if stone == Stone.BLACK:
                    color, thickness = (20, 20, 20), 3
                elif stone == Stone.WHITE:
                    color, thickness = (245, 245, 245), 3
                else:
                    score = analysis.scores[logical_y][logical_x] if analysis else 0.0
                    color = (30, int(80 + 170 * score), int(255 * (1 - score)))
                    thickness = 1 if score < self.threshold else 3
                cv2.circle(image, (cx, cy), max(5, int(current.spacing * 0.20)), color, thickness, cv2.LINE_AA)
        cv2.rectangle(image, (0, 0), (image.shape[1], 34), (8, 15, 22), -1)
        suffix = ""
        if analysis and analysis.best:
            suffix = (
                f"  candidate={analysis.best.vertex} score={analysis.best_score:.2f}"
                f" stable={analysis.stable_frames} board={analysis.board_agreement:.0%}"
                f" grid={self.last_grid_score:.0%} extra={analysis.unexpected_stones}"
                f" unknown={analysis.unknown_points}"
            )
        cv2.putText(image, title + suffix, (12, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (94, 240, 211), 1, cv2.LINE_AA)
        return image
