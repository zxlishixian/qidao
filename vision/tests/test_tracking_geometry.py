from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from go_vision.capture import BoardRegionTracker, BoardTrackingError, Point, Quad, ScreenController
from go_vision.ml_inference import BoardDetection


class MovingBoardController:
    """Deterministic virtual screen used to detect long-term tracker drift."""

    demo = False
    locator_confidence = 0.0

    def __init__(self) -> None:
        self.left = 250
        self.top = 180
        self.extent = 540
        self.size = 19
        self.stones: list[tuple[int, int, tuple[int, int, int]]] = []

    @property
    def quad(self) -> Quad:
        return Quad(
            Point(self.left, self.top),
            Point(self.left + self.extent, self.top),
            Point(self.left + self.extent, self.top + self.extent),
            Point(self.left, self.top + self.extent),
        )

    def move(self, dx: int, dy: int) -> None:
        self.left += dx
        self.top += dy

    def _screen(self) -> np.ndarray:
        image = np.full((1100, 1400, 3), (42, 48, 54), dtype=np.uint8)
        margin = 42
        cv2.rectangle(
            image,
            (self.left - margin, self.top - margin),
            (self.left + self.extent + margin, self.top + self.extent + margin),
            (92, 158, 205),
            -1,
        )
        spacing = self.extent / (self.size - 1)
        for index in range(self.size):
            position_x = round(self.left + index * spacing)
            position_y = round(self.top + index * spacing)
            cv2.line(image, (position_x, self.top), (position_x, self.top + self.extent), (26, 31, 35), 1)
            cv2.line(image, (self.left, position_y), (self.left + self.extent, position_y), (26, 31, 35), 1)
        # Non-periodic outside features anchor the absolute grid coordinate.
        cv2.circle(image, (self.left - 24, self.top - 23), 8, (25, 210, 245), -1)
        cv2.rectangle(
            image,
            (self.left + self.extent + 14, self.top + 8),
            (self.left + self.extent + 31, self.top + 29),
            (220, 80, 45),
            -1,
        )
        for x, y, color in self.stones:
            center = (round(self.left + x * spacing), round(self.top + y * spacing))
            cv2.circle(image, center, 14, color, -1, cv2.LINE_AA)
        return image

    def _capture_region(self, bounds: tuple[int, int, int, int]) -> np.ndarray:
        left, top, width, height = bounds
        return self._screen()[top : top + height, left : left + width].copy()

    def locate_grid_quad(self, raw: np.ndarray, bounds: tuple[int, int, int, int], size: int) -> Quad:
        return self.quad

    def capture_board(self, quad: Quad, size: int):
        bounds = quad.bounds()
        return ScreenController.warp_image(self._capture_region(bounds), bounds, quad, size)

    @staticmethod
    def warp_image(raw, bounds, quad, size):
        return ScreenController.warp_image(raw, bounds, quad, size)


class TrackingGeometryTests(unittest.TestCase):
    def test_packaged_controller_uses_bundled_signed_capture_helper(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundled = root / "vision" / "screen-tool"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            controller = ScreenController(root, demo=True)
            self.assertEqual(controller.tool, bundled)

    def test_tracking_images_use_screen_point_scale_across_backing_scales(self) -> None:
        tracker = BoardRegionTracker.__new__(BoardRegionTracker)
        tracker.tracking_pixels_per_point = 0.5

        retina_template = np.zeros((1000, 1000, 3), dtype=np.uint8)
        non_retina_search = np.zeros((620, 620, 3), dtype=np.uint8)

        template = tracker._tracking_image(retina_template, (0, 0, 500, 500))
        search = tracker._tracking_image(non_retina_search, (-60, -60, 620, 620))

        self.assertEqual(template.shape, (250, 250))
        self.assertEqual(search.shape, (310, 310))
        self.assertGreaterEqual(search.shape[0], template.shape[0])
        self.assertGreaterEqual(search.shape[1], template.shape[1])

    def test_capture_clipped_at_left_edge_is_padded_without_coordinate_stretch(self) -> None:
        # A 749-point request starting at x=-17 is clipped to the visible 732
        # points by ScreenCaptureKit on a Retina display. The restored bitmap
        # must still represent all 749 requested points at exactly 2 px/point.
        visible = np.full((1496, 1464, 3), 73, dtype=np.uint8)

        restored = ScreenController._restore_requested_capture(
            visible,
            (-17, 57, 749, 748),
            (0.0, 57.0, 732.0, 748.0),
        )

        self.assertEqual(restored.shape, (1496, 1498, 3))
        self.assertTrue(np.all(restored[:, 34] == 73))
        self.assertTrue(np.all(restored[:, -1] == 73))

    def test_capture_clipped_at_top_left_preserves_both_point_scales(self) -> None:
        visible = np.zeros((900, 1100, 3), dtype=np.uint8)
        visible[0, 0] = (11, 22, 33)

        restored = ScreenController._restore_requested_capture(
            visible,
            (-25, -15, 575, 465),
            (0.0, 0.0, 550.0, 450.0),
        )

        self.assertEqual(restored.shape, (930, 1150, 3))
        self.assertTrue(np.array_equal(restored[30, 50], (11, 22, 33)))

    def test_locator_refits_inside_neural_region_when_global_period_is_wrong(self) -> None:
        controller = ScreenController.__new__(ScreenController)
        controller.locator_confidence = 0.0
        controller.board_locator = type(
            "Locator",
            (),
            {"detect": lambda _self, _raw: BoardDetection(0.96, 200, 200, 800, 800)},
        )()
        wrong = Quad(Point(10, 10), Point(490, 10), Point(490, 490), Point(10, 490))
        correct = Quad(Point(100, 100), Point(400, 100), Point(400, 400), Point(100, 400))
        calls = 0

        def detect(_raw, _bounds, _size):
            nonlocal calls
            calls += 1
            return wrong if calls == 1 else correct

        controller.detect_grid_quad = detect

        result = controller.locate_grid_quad(
            np.zeros((1000, 1000, 3), dtype=np.uint8),
            (0, 0, 500, 500),
            19,
        )

        self.assertEqual(result, correct)
        self.assertAlmostEqual(controller.locator_confidence, 0.96)

    def test_grid_detector_prefers_square_period_over_stronger_wide_texture(self) -> None:
        # Keep the real grid below 55% of the broad selection width. This is
        # the failure shape seen when a user includes surrounding client UI.
        image = np.full((1000, 1600, 3), (174, 196, 220), dtype=np.uint8)
        actual_x = [150 + 44 * index for index in range(19)]
        actual_y = [100 + 44 * index for index in range(19)]
        for x in actual_x:
            cv2.line(image, (x, actual_y[0]), (x, actual_y[-1]), (35, 35, 35), 2)
        for y in actual_y:
            cv2.line(image, (actual_x[0], y), (actual_x[-1], y), (35, 35, 35), 2)
        # Strong full-height vertical texture advertises a wider false period.
        for x in (80 + 50 * index for index in range(19)):
            cv2.line(image, (x, 0), (x, image.shape[0] - 1), (5, 5, 5), 3)

        quad = ScreenController.detect_grid_quad(
            image,
            (0, 0, image.shape[1], image.shape[0]),
            19,
        )

        self.assertAlmostEqual(quad.top_left.x, actual_x[0], delta=3.0)
        self.assertAlmostEqual(quad.top_right.x, actual_x[-1], delta=3.0)
        self.assertAlmostEqual(quad.top_left.y, actual_y[0], delta=3.0)
        self.assertAlmostEqual(quad.bottom_left.y, actual_y[-1], delta=3.0)

    @staticmethod
    def tracker_shell() -> BoardRegionTracker:
        tracker = BoardRegionTracker.__new__(BoardRegionTracker)
        tracker.controller = type("Controller", (), {"demo": False})()
        tracker.size = 19
        tracker.current_quad = Quad(
            Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800)
        )
        tracker.match_score = 1.0
        tracker.anchor_score = 1.0
        tracker.tracking_mode = "calibrated"
        tracker.consecutive_failures = 0
        tracker.frame_index = 0
        tracker.last_reanchor_frame = 0
        tracker.last_tracking_error = ""
        tracker.force_recovery = False
        tracker.alignment_failures = 0
        tracker.last_capture_used_fallback = False
        tracker.last_recovery_attempt_frame = -1000
        tracker.grid_spacing_points = 800 / 18
        tracker.template_margin = 30
        tracker.anchor_quad = tracker.current_quad
        return tracker

    def test_repeated_stationary_match_bias_does_not_accumulate_drift(self) -> None:
        tracker = self.tracker_shell()
        original = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        anchor_bounds = tracker.anchor_quad.bounds(margin=tracker.template_margin)

        # A constant resize/template bias used to be added again on every
        # frame. After a minute the overlay could be many intersections away.
        for _ in range(600):
            tracker._apply_absolute_fast_match(
                anchor_bounds[0] + 2.6,
                anchor_bounds[1] - 2.4,
                0.50,
                0.50,
            )

        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        self.assertLess(abs(actual.x - original.x), 3.0)
        self.assertLess(abs(actual.y - original.y), 3.0)

    def test_absolute_fast_match_follows_slow_window_motion(self) -> None:
        tracker = self.tracker_shell()
        anchor_bounds = tracker.anchor_quad.bounds(margin=tracker.template_margin)

        # A one-point-per-frame drag may sit inside the noise threshold for a
        # frame, but the absolute displacement keeps growing and is caught
        # without integrating any measurement error.
        for distance in range(1, 21):
            tracker._apply_absolute_fast_match(
                anchor_bounds[0] + distance,
                anchor_bounds[1] + distance,
                0.50,
                0.50,
            )

        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        self.assertAlmostEqual(actual.x, 420.0, delta=2.1)
        self.assertAlmostEqual(actual.y, 420.0, delta=2.1)

    def test_fast_tracking_failure_switches_to_recovery(self) -> None:
        tracker = self.tracker_shell()
        fallback_frame = object()
        recovered_frame = object()

        def fail_fast():
            raise BoardTrackingError("lost")

        tracker._fast_capture = fail_fast
        tracker._capture_at_last_verified_quad = lambda **_options: fallback_frame
        tracker.recover = lambda: recovered_frame

        first = tracker.capture()
        second = tracker.capture()

        self.assertIs(first, fallback_frame)
        self.assertIs(second, recovered_frame)
        self.assertEqual(tracker.consecutive_failures, 2)

    def test_periodic_reanchor_error_does_not_drop_recognition_frame(self) -> None:
        tracker = self.tracker_shell()
        tracker.frame_index = tracker.PERIODIC_REANCHOR_FRAMES - 1
        fast_frame = object()
        tracker.periodic_reanchor = lambda: (_ for _ in ()).throw(
            BoardTrackingError("dense board hid grid lines")
        )
        tracker._fast_capture = lambda: fast_frame

        result = tracker.capture()

        self.assertIs(result, fast_frame)
        self.assertEqual(tracker.last_reanchor_frame, tracker.frame_index)

    def test_forced_recovery_returns_to_fast_lock_when_template_is_still_valid(self) -> None:
        tracker = self.tracker_shell()
        fast_frame = object()
        fast_called = False
        recover_called = False

        def fast_capture():
            nonlocal fast_called
            fast_called = True
            return fast_frame

        def recover():
            nonlocal recover_called
            recover_called = True
            return object()

        tracker._fast_capture = fast_capture
        tracker.recover = recover
        tracker.mark_alignment_failure("grid moved")
        tracker.mark_alignment_failure("grid moved")
        self.assertFalse(tracker.force_recovery)
        tracker.mark_alignment_failure("grid moved")

        result = tracker.capture()

        self.assertIs(result, fast_frame)
        self.assertTrue(fast_called)
        self.assertFalse(recover_called)
        self.assertFalse(tracker.force_recovery)
        self.assertEqual(tracker.tracking_mode, "tracking")

    def test_successful_analysis_clears_transient_recovery_state(self) -> None:
        tracker = self.tracker_shell()
        tracker.mark_alignment_failure("one weak frame")

        self.assertEqual(tracker.tracking_mode, "degraded")
        self.assertFalse(tracker.force_recovery)

        tracker.mark_analysis_success()

        self.assertEqual(tracker.tracking_mode, "tracking")
        self.assertEqual(tracker.alignment_failures, 0)
        self.assertFalse(tracker.force_recovery)

    def test_recovery_search_expands_after_repeated_failures(self) -> None:
        tracker = self.tracker_shell()
        margins: list[int] = []

        def refit(**options):
            margins.append(options["search_margin"])
            return object()

        tracker._refit = refit
        tracker.consecutive_failures = 1
        tracker.recover()
        tracker.consecutive_failures = 4
        tracker.recover()

        self.assertGreater(margins[1], margins[0])
        self.assertLessEqual(margins[1], 340)

    def test_subpixel_peak_refines_fractional_template_location(self) -> None:
        x = np.arange(7, dtype=np.float32)
        y = np.arange(7, dtype=np.float32)
        xx, yy = np.meshgrid(x, y)
        response = -((xx - 3.35) ** 2 + (yy - 2.60) ** 2)

        refined = BoardRegionTracker._subpixel_peak(response, (3, 3))

        self.assertAlmostEqual(refined[0], 3.35, places=2)
        self.assertAlmostEqual(refined[1], 2.60, places=2)

    def test_moving_board_stays_locked_without_false_recovery_or_drift(self) -> None:
        controller = MovingBoardController()
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        tracker.PERIODIC_REANCHOR_FRAMES = 1000

        maximum_error = 0.0
        for frame in range(30):
            controller.move(3, 2)
            if frame == 10:
                controller.stones.append((15, 3, (18, 20, 22)))
            if frame == 20:
                controller.stones.append((14, 12, (238, 240, 244)))
            tracker.capture()
            expected = BoardRegionTracker._quad_center(controller.quad, 19)
            actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
            maximum_error = max(maximum_error, np.hypot(expected.x - actual.x, expected.y - actual.y))
            self.assertNotEqual(tracker.tracking_mode, "recovering")

        self.assertLess(maximum_error, 3.0)

    def test_one_grid_template_jump_requires_full_grid_recovery(self) -> None:
        controller = MovingBoardController()
        controller.left = 400
        controller.top = 280
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        tracker.PERIODIC_REANCHOR_FRAMES = 1000
        controller.move(30, 0)

        tracker.capture()
        self.assertEqual(tracker.tracking_mode, "fallback")
        fallback_center = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        self.assertGreater(
            np.hypot(
                BoardRegionTracker._quad_center(controller.quad, 19).x - fallback_center.x,
                BoardRegionTracker._quad_center(controller.quad, 19).y - fallback_center.y,
            ),
            20.0,
        )
        tracker.capture()

        expected = BoardRegionTracker._quad_center(controller.quad, 19)
        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        self.assertLess(np.hypot(expected.x - actual.x, expected.y - actual.y), 2.0)
        self.assertEqual(tracker.tracking_mode, "recovered")

    def test_snapshot_relock_replaces_a_stretched_quad_without_blending(self) -> None:
        controller = MovingBoardController()
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        correct = controller.quad
        tracker.current_quad = Quad(
            correct.top_left,
            Point(correct.top_right.x + 30, correct.top_right.y),
            Point(correct.bottom_right.x + 30, correct.bottom_right.y),
            correct.bottom_left,
        )

        tracker.relock_for_snapshot()

        expected = BoardRegionTracker._quad_center(correct, 19)
        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        self.assertLess(np.hypot(expected.x - actual.x, expected.y - actual.y), 0.1)
        self.assertEqual(tracker.tracking_mode, "verified")

    def test_manual_relock_recovers_beyond_periodic_jump_limit(self) -> None:
        controller = MovingBoardController()
        controller.top = 300
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        correct = controller.quad
        tracker.current_quad = tracker.current_quad.translated(90.0, 0.0)
        tracker.anchor_quad = tracker.current_quad

        with self.assertRaises(BoardTrackingError):
            tracker.relock_for_snapshot()

        tracker.relock_for_manual_baseline()

        expected = BoardRegionTracker._quad_center(correct, 19)
        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        anchor = BoardRegionTracker._quad_center(tracker.anchor_quad, 19)
        self.assertLess(np.hypot(expected.x - actual.x, expected.y - actual.y), 0.1)
        self.assertLess(np.hypot(expected.x - anchor.x, expected.y - anchor.y), 0.1)
        self.assertEqual(tracker.tracking_mode, "recovered")

    def test_manual_relock_rejects_inconsistent_full_grid_candidates(self) -> None:
        controller = MovingBoardController()
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        original = tracker.current_quad
        correct = controller.quad
        detections = iter((correct, correct.translated(30.0, 0.0)))
        controller.locate_grid_quad = lambda _raw, _bounds, _size: next(detections)

        with self.assertRaisesRegex(BoardTrackingError, "两次完整网格定位不一致"):
            tracker.relock_for_manual_baseline()

        self.assertEqual(tracker.current_quad, original)
        self.assertEqual(tracker.anchor_quad, original)

    def test_periodic_reanchor_removes_drift_in_one_grid_fit(self) -> None:
        controller = MovingBoardController()
        tracker = BoardRegionTracker(controller, controller.quad, 19)
        tracker.current_quad = tracker.current_quad.translated(11.0, -9.0)
        tracker.anchor_quad = tracker.current_quad

        tracker.periodic_reanchor()

        expected = BoardRegionTracker._quad_center(controller.quad, 19)
        actual = BoardRegionTracker._quad_center(tracker.current_quad, 19)
        anchor = BoardRegionTracker._quad_center(tracker.anchor_quad, 19)
        self.assertLess(np.hypot(expected.x - actual.x, expected.y - actual.y), 0.1)
        self.assertLess(np.hypot(expected.x - anchor.x, expected.y - anchor.y), 0.1)
        self.assertEqual(tracker.tracking_mode, "reanchored")


if __name__ == "__main__":
    unittest.main()
