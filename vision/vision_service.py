#!/usr/bin/env python3
"""JSON-lines screen-board recognition service for the QiDao UI.

The service owns screen capture and computer-vision state.  It never starts
KataGo and never mutates QiDao's game tree directly. Stable legal transitions
and temporally confirmed full-board snapshots are emitted to stdout; Swift
decides how to reconcile them with the authoritative QiDao position.
"""

from __future__ import annotations

import json
import os
import queue
import select
import stat
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from go_vision.adaptive_vision import AdaptiveBoardTracker, BoardAlignmentError, PositionRecognition
from go_vision.capture import (
    BoardRegionTracker,
    BoardTrackingError,
    CaptureError,
    Quad,
    ScreenController,
)
from go_vision.model import Move, Stone, board_to_json, diff_boards, legal_move_result


OUTPUT_BARRIER_TIMEOUT = 0.25
MAX_OUTPUT_LINE_BYTES = 16 * 1024


@dataclass
class ServiceConfig:
    quad: Quad | None = None
    size: int = 19
    rotation: int = 0
    threshold: float = 0.61
    stable_frames: int = 2
    interval: float = 0.12


def consensus_position(
    recognitions: list[PositionRecognition],
    size: int,
) -> PositionRecognition:
    """Combine a short capture burst using votes and trimmed probabilities.

    Full-board equality is too strict for animated clients: tracking can move
    by a subpixel and flip a handful of marginal intersections in every frame.
    A strict vote remains authoritative when it exists. If conservative
    single-frame classification returned UNKNOWN repeatedly, fuse its raw
    empty/black/white evidence instead of failing the entire board.
    """
    if len(recognitions) < 3:
        raise ValueError("建立棋盘局面至少需要三帧图像")

    required_votes = len(recognitions) // 2 + 1
    rows: list[list[Stone]] = []
    fused_score_rows: list[list[tuple[float, float, float]]] = []
    unknown_points = 0
    has_point_scores = all(
        len(recognition.point_scores) == size
        and all(len(row) == size for row in recognition.point_scores)
        for recognition in recognitions
    )

    def trimmed_mean(values: list[float]) -> float:
        ordered = sorted(values)
        if len(ordered) >= 5:
            ordered = ordered[1:-1]
        return sum(ordered) / max(1, len(ordered))

    for y in range(size):
        row: list[Stone] = []
        fused_score_row: list[tuple[float, float, float]] = []
        for x in range(size):
            votes = Counter(recognition.board[y][x] for recognition in recognitions)
            # UNKNOWN is an abstention, not a board state that can win a vote.
            votes.pop(Stone.UNKNOWN, None)
            if not votes:
                winner, count = Stone.UNKNOWN, 0
            else:
                winner, count = votes.most_common(1)[0]
            if count < required_votes:
                if has_point_scores:
                    states = (Stone.EMPTY, Stone.BLACK, Stone.WHITE)
                    fused = [
                        trimmed_mean([recognition.point_scores[y][x][index] for recognition in recognitions])
                        + 0.12 * votes.get(state, 0) / len(recognitions)
                        for index, state in enumerate(states)
                    ]
                    winner_index = max(range(3), key=fused.__getitem__)
                    winner = states[winner_index]
                    # Empty is the safe state for a previously unknown point
                    # unless one stone colour has positive, repeatable evidence.
                    # This removes the common UNKNOWN halo around stones while
                    # preserving real stones that the model saw consistently.
                    if winner != Stone.EMPTY:
                        other_index = 2 if winner == Stone.BLACK else 1
                        repeated = votes.get(winner, 0) >= 2
                        stone_clear = (
                            fused[winner_index] >= fused[0] - 0.025
                            and fused[winner_index] >= fused[other_index] + 0.025
                        )
                        if not repeated and not stone_clear:
                            winner = Stone.EMPTY
                    fused_score_row.append(tuple(float(value) for value in fused))
                else:
                    winner = Stone.UNKNOWN
                    fused_score_row.append((0.0, 0.0, 0.0))
            elif has_point_scores:
                fused_score_row.append(
                    tuple(
                        trimmed_mean([recognition.point_scores[y][x][index] for recognition in recognitions])
                        for index in range(3)
                    )
                )
            else:
                fused_score_row.append((0.0, 0.0, 0.0))
            if winner == Stone.UNKNOWN:
                unknown_points += 1
            row.append(winner)
        rows.append(row)
        fused_score_rows.append(fused_score_row)

    board = tuple(tuple(row) for row in rows)
    next_player_votes = Counter(recognition.next_color for recognition in recognitions)
    next_color = next_player_votes.most_common(1)[0][0]

    move_votes = Counter(
        (move.x, move.y, move.color)
        for recognition in recognitions
        if (move := recognition.last_move) is not None
    )
    last_move = None
    if move_votes:
        (move_x, move_y, move_color), move_count = move_votes.most_common(1)[0]
        if move_count >= 2 and board[move_y][move_x] == move_color:
            last_move = Move(move_x, move_y, move_color, size)

    confidence = sum(recognition.confidence for recognition in recognitions) / len(recognitions)
    return PositionRecognition(
        board,
        confidence,
        unknown_points,
        next_color,
        last_move,
        tuple(tuple(row) for row in fused_score_rows),
    )


def snapshot_verification_agrees(
    confirmed: tuple[tuple[Stone, ...], ...],
    candidate: tuple[tuple[Stone, ...], ...],
    verified: tuple[tuple[Stone, ...], ...],
) -> bool:
    """Accept a temporally stable snapshot when a relock has no contradiction.

    Dense real positions routinely leave a few unchanged intersections UNKNOWN
    in one independently relocked frame. UNKNOWN may abstain there, but never
    authorizes deleting or recolouring a confirmed stone. Reject every known
    contradiction, require explicit proof for every destructive change, and
    require a strict majority of newly added stones to be seen independently.
    """
    transition = diff_boards(confirmed, candidate)
    if not transition.changed:
        return False

    for y in range(len(candidate)):
        for x in range(len(candidate)):
            value = verified[y][x]
            if value != Stone.UNKNOWN and value != candidate[y][x]:
                return False

    for x, y in transition.changed:
        before = confirmed[y][x]
        after = candidate[y][x]
        if before in (Stone.BLACK, Stone.WHITE) and before != after:
            if verified[y][x] != after:
                return False

    if transition.added:
        independently_confirmed = sum(
            verified[move.y][move.x] == move.color
            for move in transition.added
        )
        return independently_confirmed * 2 > len(transition.added)

    known_changed = sum(
        verified[y][x] == candidate[y][x]
        for x, y in transition.changed
    )
    required = max(1, (len(transition.changed) * 3 + 4) // 5)
    return known_changed >= required


class VisionService:
    def __init__(self, root: Path, demo: bool = False):
        self.root = root
        self.controller = ScreenController(root, demo=demo)
        bundled_tool = Path(__file__).resolve().parent / "screen-tool"
        if bundled_tool.is_file():
            self.controller.tool = bundled_tool
        self.config = ServiceConfig()
        self.region: BoardRegionTracker | None = None
        self.tracker: AdaptiveBoardTracker | None = None
        self.running = False
        self.closed = False
        self._condition = threading.Condition()
        self._state_lock = threading.RLock()
        self._output_lock = threading.Lock()
        try:
            output_fd = sys.stdout.fileno()
        except (AttributeError, OSError, ValueError):
            output_fd = None
        self._start_output_writer(output_fd)
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self.transition_candidate: tuple[int, int, Stone] | None = None
        self.transition_candidate_frames = 0
        self.snapshot_relock_candidate: tuple[tuple[Stone, ...], ...] | None = None
        self.snapshot_relock_failures = 0
        self.scan_sequence = 0
        self.position_sequence = 0
        self.unacked_position_payload: dict[str, Any] | None = None
        self.last_position_emit_at = 0.0
        self.last_acked_position_sequence = 0

    def tracking_payload(self) -> dict[str, Any]:
        if self.region is None:
            return {}
        return {
            "quad": self.region.current_quad.to_json(),
            "trackingMode": self.region.tracking_mode,
            "trackingScore": round(self.region.match_score, 4),
            "anchorScore": round(self.region.anchor_score, 4),
            "trackingFailures": self.region.consecutive_failures,
            "trackingFallback": bool(
                getattr(self.region, "last_capture_used_fallback", False)
            ),
            "gridScore": round(
                getattr(self.tracker, "last_grid_score", 0.0),
                4,
            ) if self.tracker is not None else 0.0,
        }

    def _start_output_writer(self, output_fd: int | None) -> None:
        self._output_fd: int | None = None
        self._output_queue: queue.Queue[bytes | threading.Event | None] = queue.Queue(
            maxsize=256
        )
        self._output_stop = threading.Event()
        self._output_thread: threading.Thread | None = None
        if output_fd is None:
            return
        try:
            if not stat.S_ISFIFO(os.fstat(output_fd).st_mode):
                return
            os.set_blocking(output_fd, False)
        except OSError:
            return
        self._output_fd = output_fd
        self._output_thread = threading.Thread(
            target=self._write_output,
            name="vision-output",
            daemon=False,
        )
        self._output_thread.start()

    def _write_output(self) -> None:
        output_poller = select.kqueue() if hasattr(select, "kqueue") else None
        try:
            while not self._output_stop.is_set():
                try:
                    item = self._output_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    return
                if isinstance(item, threading.Event):
                    item.set()
                    continue
                with self._output_lock:
                    output_fd = self._output_fd
                    if output_fd is None:
                        return
                if output_poller is not None and not self._wait_for_pipe_capacity(
                    output_poller,
                    output_fd,
                    len(item),
                ):
                    return
                with self._output_lock:
                    if self._output_stop.is_set() or self._output_fd != output_fd:
                        return
                    try:
                        written = os.write(output_fd, item)
                    except BlockingIOError:
                        written = 0
                    except OSError:
                        self._output_fd = None
                        self._output_stop.set()
                        return
                if written != len(item):
                    # The Darwin capacity preflight makes this unreachable for
                    # the single writer. On another platform, never append the
                    # remainder of a record after a partial nonblocking write.
                    self._abort_output()
                    return
        finally:
            if output_poller is not None:
                output_poller.close()

    def _wait_for_pipe_capacity(self, poller, output_fd: int, byte_count: int) -> bool:
        change = select.kevent(
            output_fd,
            filter=select.KQ_FILTER_WRITE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        )
        while not self._output_stop.is_set():
            try:
                events = poller.control([change], 1, 0.05)
            except OSError:
                return False
            if events:
                event = events[0]
                if event.flags & select.KQ_EV_ERROR:
                    return False
                if event.data >= byte_count:
                    return True
            self._output_stop.wait(0.01)
        return False

    def _abort_output(self) -> None:
        self._output_stop.set()
        with self._output_lock:
            output_fd = self._output_fd
            self._output_fd = None
            if output_fd is not None:
                try:
                    if sys.stdout.fileno() == output_fd:
                        sys.stdout.close()
                    else:
                        os.close(output_fd)
                except (AttributeError, OSError, ValueError):
                    pass
        output_thread = self._output_thread
        if output_thread is not None and output_thread is not threading.current_thread():
            output_thread.join(timeout=OUTPUT_BARRIER_TIMEOUT)

    def _output_barrier(self, timeout: float = OUTPUT_BARRIER_TIMEOUT) -> bool:
        output_thread = getattr(self, "_output_thread", None)
        if output_thread is None:
            return True
        if self._output_stop.is_set() or not output_thread.is_alive():
            return False
        reached = threading.Event()
        try:
            self._output_queue.put_nowait(reached)
        except queue.Full:
            self._abort_output()
            return False
        if reached.wait(timeout=timeout):
            return True
        self._abort_output()
        return False

    def _close_output_writer(self) -> None:
        output_thread = getattr(self, "_output_thread", None)
        if output_thread is None:
            return
        if not self._output_barrier():
            return
        self._output_stop.set()
        try:
            self._output_queue.put_nowait(None)
        except queue.Full:
            self._abort_output()
            return
        output_thread.join(timeout=OUTPUT_BARRIER_TIMEOUT)
        if output_thread.is_alive():
            self._abort_output()
        self._output_thread = None

    def emit(self, event: str, **payload: Any) -> None:
        message = {"event": event, **payload}
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        output_thread = getattr(self, "_output_thread", None)
        if output_thread is not None:
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_OUTPUT_LINE_BYTES or self._output_stop.is_set():
                return
            try:
                self._output_queue.put_nowait(encoded)
            except queue.Full:
                pass
            return
        with self._output_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def configure(self, command: dict[str, Any]) -> None:
        quad = Quad.from_json(command.get("quad"))
        size = int(command.get("size", self.config.size))
        rotation = int(command.get("rotation", self.config.rotation))
        if size not in (9, 13, 19):
            raise ValueError("棋盘规格只能是 9、13 或 19")
        if rotation not in (0, 180):
            raise ValueError("棋盘方向只能是 0° 或 180°")
        self.config = ServiceConfig(
            quad=quad,
            size=size,
            rotation=rotation,
            threshold=max(0.45, min(0.90, float(command.get("threshold", 0.61)))),
            stable_frames=max(2, min(6, int(command.get("stableFrames", 2)))),
            # Live analysis promises a sub-second board update. Slower polling
            # intervals cannot satisfy that contract even when recognition is
            # instantaneous, so keep the configurable range in real-time.
            interval=max(0.10, min(0.25, float(command.get("interval", 0.12)))),
        )
        self.running = False
        self.region = None
        self.tracker = None
        self.transition_candidate = None
        self.transition_candidate_frames = 0
        self.reset_snapshot_relock_retry()
        self.scan_sequence = 0
        self.position_sequence = 0
        self.unacked_position_payload = None
        self.last_position_emit_at = 0.0
        self.last_acked_position_sequence = 0
        self.emit("configured", size=size, rotation=rotation, calibrated=quad is not None)

    def capture_baseline(self, reuse_calibration: bool = False) -> None:
        if self.config.quad is None:
            raise ValueError("请先拖选实战棋盘")
        self.emit(
            "status",
            phase="baseline",
            message=(
                "正在快速重新识别当前完整局面…"
                if reuse_calibration and self.region is not None and self.tracker is not None
                else "正在定位网格并识别当前完整局面…"
            ),
        )
        if reuse_calibration and self.region is not None and self.tracker is not None:
            # Manual re-recognition is authoritative recovery, not an ordinary
            # periodic correction. The remembered quad may itself be stale, so
            # let the region perform two consistent wide full-grid fits before
            # replacing its corners and templates. Automatic tracking keeps
            # the stricter one-period anti-jump guard.
            region = self.region
            tracker = self.tracker
            recognitions = [
                tracker.recognize_position(region.relock_for_manual_baseline())
            ]
        else:
            region = BoardRegionTracker(self.controller, self.config.quad, self.config.size)
            tracker = AdaptiveBoardTracker(
                region.reference,
                self.config.size,
                rotation=self.config.rotation,
                threshold=self.config.threshold,
                stable_required=self.config.stable_frames,
            )
            recognitions = [tracker.recognize_position(region.reference)]

        # Three frames are sufficient for a strict majority and keep manual
        # re-recognition comfortably below the previous multi-second wait.
        # Only ambiguous boards pay for two additional recovery frames.
        for _ in range(2):
            time.sleep(0.055)
            current = tracker.recognize_position(region.capture())
            recognitions.append(current)

        confirmed = consensus_position(recognitions, self.config.size)
        if confirmed.unknown_points:
            for _ in range(2):
                time.sleep(0.055)
                recognitions.append(tracker.recognize_position(region.capture()))
            confirmed = consensus_position(recognitions, self.config.size)
        if confirmed.unknown_points:
            raise ValueError(
                f"多帧投票后仍有 {confirmed.unknown_points} 个交叉点无法确认；"
                "请确保棋盘完整可见并移开鼠标后重试"
            )
        tracker.bootstrap(confirmed)
        self.region = region
        self.tracker = tracker
        self.transition_candidate = None
        self.transition_candidate_frames = 0
        self.reset_snapshot_relock_retry()
        self.scan_sequence = 0
        self.position_sequence = 0
        self.unacked_position_payload = None
        self.last_position_emit_at = 0.0
        self.last_acked_position_sequence = 0
        black_count = sum(value == Stone.BLACK for row in tracker.board for value in row)
        white_count = sum(value == Stone.WHITE for row in tracker.board for value in row)
        self.emit(
            "baseline",
            **self.tracking_payload(),
            board=board_to_json(tracker.board),
            observedBoard=board_to_json(tracker.board),
            observedConfidence=round(confirmed.confidence, 4),
            moveNumber=tracker.move_count,
            nextPlayer="B" if tracker.next_color == Stone.BLACK else "W",
            lastMove=confirmed.last_move.to_json() if confirmed.last_move else None,
            recognizer=tracker.recognition_backend,
            locator=region.controller.locator_backend,
            message=(
                f"当前局面已载入：黑 {black_count} 子，白 {white_count} 子；"
                f"{tracker.recognition_backend}"
            ),
        )

    def set_running(self, value: bool) -> None:
        with self._state_lock:
            if value and (self.region is None or self.tracker is None):
                raise ValueError("请先采集空棋盘基准")
            self.running = value
            with self._condition:
                self._condition.notify_all()
            if not value:
                self._output_barrier()
            self.emit("running", running=value, **self.tracking_payload())

    def reset_snapshot_relock_retry(self) -> None:
        """Forget a failed automatic whole-board verification attempt."""
        self.snapshot_relock_candidate = None
        self.snapshot_relock_failures = 0

    def note_snapshot_relock_failure(
        self,
        candidate: tuple[tuple[Stone, ...], ...],
    ) -> int:
        """Count strict relock failures only while the visual board agrees.

        A different candidate starts a fresh attempt. This prevents unrelated
        noisy frames from accumulating enough failures to authorize the wider
        recovery path.
        """
        if candidate == getattr(self, "snapshot_relock_candidate", None):
            self.snapshot_relock_failures = (
                getattr(self, "snapshot_relock_failures", 0) + 1
            )
        else:
            self.snapshot_relock_candidate = candidate
            self.snapshot_relock_failures = 1
        return self.snapshot_relock_failures

    def pass_turn(self) -> None:
        if self.tracker is None:
            raise ValueError("尚未建立识别状态")
        move = self.tracker.commit_pass()
        self.reset_snapshot_relock_retry()
        self.emit_position(move, 1.0)

    def set_next_player(self, value: str) -> None:
        if self.tracker is None:
            raise ValueError("尚未建立识别状态")
        normalized = value.strip().upper()
        if normalized not in ("B", "W"):
            raise ValueError("下一手只能设置为 B 或 W")
        color = Stone.BLACK if normalized == "B" else Stone.WHITE
        self.tracker.set_next_color(color)
        self.transition_candidate = None
        self.transition_candidate_frames = 0
        self.reset_snapshot_relock_retry()
        self.emit_position(
            None,
            1.0,
            {
                "confirmation": "turn-correction",
                "turnCorrected": True,
            },
        )

    def undo(self) -> None:
        if self.tracker is None:
            raise ValueError("尚未建立识别状态")
        removed = self.tracker.undo()
        self.reset_snapshot_relock_retry()
        self.emit(
            "undo",
            removed=removed.to_json() if removed else None,
            moveNumber=self.tracker.move_count,
            nextPlayer="B" if self.tracker.next_color == Stone.BLACK else "W",
            board=board_to_json(self.tracker.board),
            observedBoard=board_to_json(self.tracker.board),
        )

    def emit_position(
        self,
        move: Move | None,
        confidence: float,
        performance: dict[str, Any] | None = None,
    ) -> None:
        assert self.tracker is not None
        assert self.region is not None
        self.position_sequence = getattr(self, "position_sequence", 0) + 1
        payload = dict(
            board=board_to_json(self.tracker.board),
            observedBoard=board_to_json(self.tracker.board),
            lastMove=move.to_json() if move else None,
            moveNumber=self.tracker.move_count,
            nextPlayer="B" if self.tracker.next_color == Stone.BLACK else "W",
            confidence=round(confidence, 4),
            scanSequence=getattr(self, "scan_sequence", 0),
            positionSequence=self.position_sequence,
            **self.tracking_payload(),
            **(performance or {}),
            recognizer=self.tracker.recognition_backend,
            locator=self.region.controller.locator_backend,
            locatorConfidence=round(self.region.controller.locator_confidence, 4),
        )
        # A pipe write only proves delivery to macOS, not that QiDao actually
        # applied the position to its analysis tree. Keep the newest position
        # until Swift acknowledges the exact sequence; a stalled/inactive UI
        # then receives an idempotent replay instead of waiting for a click.
        self.unacked_position_payload = payload
        self.last_position_emit_at = time.monotonic()
        self.emit("position", **payload)

    def acknowledge_position(self, sequence: int) -> None:
        if sequence <= 0:
            return
        self.last_acked_position_sequence = max(
            getattr(self, "last_acked_position_sequence", 0),
            sequence,
        )
        pending = getattr(self, "unacked_position_payload", None)
        if pending is not None and int(pending.get("positionSequence", 0)) <= sequence:
            self.unacked_position_payload = None

    def replay_unacknowledged_position(self) -> None:
        pending = getattr(self, "unacked_position_payload", None)
        if pending is None:
            return
        now = time.monotonic()
        if now - getattr(self, "last_position_emit_at", 0.0) < 0.40:
            return
        self.last_position_emit_at = now
        replay = dict(pending)
        replay["replayed"] = True
        self.emit("position", **replay)

    def emit_analysis_scan(
        self,
        analysis,
        capture_ms: float,
        recognition_ms: float,
        scan_ms: float,
    ) -> None:
        """Publish diagnostics only while a position is still pending.

        Committed positions bypass this event so their smaller, authoritative
        `position` message reaches the Swift main actor before any preview UI.
        """
        assert self.tracker is not None
        self.emit(
            "scan",
            scanSequence=getattr(self, "scan_sequence", 0),
            candidate=analysis.best.to_json() if analysis.best else None,
            observedBoard=board_to_json(analysis.absolute_board or analysis.observed_board),
            confirmedBoard=board_to_json(self.tracker.board),
            moveNumber=self.tracker.move_count,
            nextPlayer="B" if self.tracker.next_color == Stone.BLACK else "W",
            confidence=round(analysis.confidence, 4),
            stableFrames=analysis.stable_frames,
            boardAgreement=round(analysis.board_agreement, 4),
            observedConfidence=round(analysis.observed_confidence, 4),
            unexpectedStones=analysis.unexpected_stones,
            unknownPoints=analysis.unknown_points,
            frameValid=analysis.frame_valid,
            fastAccepted=analysis.fast_accepted,
            snapshotReady=bool(analysis.snapshot_board),
            snapshotStableFrames=analysis.snapshot_stable_frames,
            hoverPreviews=[move.to_json() for move in analysis.hover_previews],
            reconciliationDifferences=analysis.reconciliation_differences,
            captureMs=round(capture_ms, 1),
            recognitionMs=round(recognition_ms, 1),
            scanMs=round(scan_ms, 1),
            **self.tracking_payload(),
        )

    def observed_legal_transition(self, analysis) -> Move | None:
        """Recover one legal move directly from the absolute visual board.

        This is deliberately independent of the expected-turn candidate score.
        It prevents a correct, stable screen transition from waiting forever
        because an old turn inference or a marginal model score stayed below a
        global gate. Hover coordinates are restored to their confirmed values
        before the Go-rule comparison.
        """
        assert self.tracker is not None
        if not analysis.absolute_board:
            return None
        observed = [list(row) for row in analysis.absolute_board]
        for hover in analysis.hover_previews:
            observed[hover.y][hover.x] = self.tracker.board[hover.y][hover.x]
        observed_board = tuple(tuple(row) for row in observed)
        transition = diff_boards(self.tracker.board, observed_board)
        move = transition.move
        if move is None:
            return None
        try:
            predicted = legal_move_result(
                self.tracker.board,
                move,
                self.tracker.board_history,
            )
        except ValueError:
            return None

        for y in range(self.tracker.size):
            for x in range(self.tracker.size):
                visual = observed_board[y][x]
                if visual == Stone.UNKNOWN or visual == predicted[y][x]:
                    continue
                # A client can leave a captured stone visible during its fade
                # animation. Once the newly added stone proves the legal
                # capture, the rules engine is authoritative for that removal.
                if (
                    self.tracker.board[y][x] in (Stone.BLACK, Stone.WHITE)
                    and predicted[y][x] == Stone.EMPTY
                    and visual == self.tracker.board[y][x]
                ):
                    continue
                return None
        return move

    def scan_once(self) -> None:
        capture_cycle = getattr(getattr(self, "controller", None), "capture_cycle", None)
        if capture_cycle is None:
            self._scan_once()
            return
        with capture_cycle():
            self._scan_once()

    def _scan_once(self) -> None:
        assert self.region is not None
        assert self.tracker is not None
        self.scan_sequence = getattr(self, "scan_sequence", 0) + 1
        scan_started = time.perf_counter()
        warped = self.region.capture()
        capture_finished = time.perf_counter()
        if (
            getattr(self, "transition_candidate", None) is None
            and self.tracker.can_skip_unchanged_frame(warped)
        ):
            scan_finished = time.perf_counter()
            capture_ms = (capture_finished - scan_started) * 1000.0
            self.emit(
                "scan",
                scanSequence=self.scan_sequence,
                moveNumber=self.tracker.move_count,
                nextPlayer="B" if self.tracker.next_color == Stone.BLACK else "W",
                captureMs=round(capture_ms, 1),
                recognitionMs=round((scan_finished - capture_finished) * 1000.0, 1),
                scanMs=round((scan_finished - scan_started) * 1000.0, 1),
                unchanged=True,
                **self.tracking_payload(),
            )
            return
        analysis = self.tracker.analyze(warped)
        fallback_geometry_stable = False
        if getattr(self.region, "last_capture_used_fallback", False):
            # Template correlation measures changing board appearance. When it
            # is weak, validate the actual grid at the last confirmed quad
            # instead of stopping recognition. A moved/misaligned board shifts
            # the 19 expected line positions and fails this independent gate.
            grid_score = self.tracker.ensure_alignment(warped)
            fallback_geometry_stable = grid_score >= max(
                0.16,
                self.tracker.baseline_grid_score * 0.48,
            )
        turn_corrected = False
        opposite = analysis.opposite_best
        if analysis.color_mismatch_likely and opposite is not None:
            candidate_is_hover = any(
                move.x == opposite.x and move.y == opposite.y
                for move in analysis.hover_previews
            )
            marker_confirms = (
                analysis.snapshot_last_move is not None
                and analysis.snapshot_last_move.x == opposite.x
                and analysis.snapshot_last_move.y == opposite.y
                and analysis.snapshot_last_move.color == opposite.color
            )
            try:
                legal_move_result(self.tracker.board, opposite, self.tracker.board_history)
                legal_opposite = True
            except ValueError:
                legal_opposite = False
            if (
                legal_opposite
                and analysis.unexpected_stones == 1
                and (marker_confirms or not candidate_is_hover)
            ):
                self.tracker.quick_frame_signature = None
                try:
                    self.tracker.analyze(
                        warped,
                        expected_color=opposite.color,
                    )
                except (BoardAlignmentError, ValueError):
                    self.tracker.cancel_pending()
                    self.emit(
                        "warning",
                        message="反色复核未通过；请显式 pass 或 setNextPlayer 后继续识别",
                        **self.tracking_payload(),
                    )
                    return
                self.tracker.cancel_pending()
                self.emit(
                    "warning",
                    message="检测到与当前行棋方相反的落子；请显式 pass 或 setNextPlayer 后继续识别",
                    **self.tracking_payload(),
                )
                return
        recognition_finished = time.perf_counter()
        capture_ms = (capture_finished - scan_started) * 1000.0
        recognition_ms = (recognition_finished - capture_finished) * 1000.0
        scan_ms = (recognition_finished - scan_started) * 1000.0
        if analysis.accepted is None:
            observed_move = self.observed_legal_transition(analysis)
            fingerprint = (
                (observed_move.x, observed_move.y, observed_move.color)
                if observed_move is not None
                else None
            )
            if fingerprint is not None and fingerprint == getattr(self, "transition_candidate", None):
                self.transition_candidate_frames = getattr(self, "transition_candidate_frames", 0) + 1
            elif fingerprint is not None:
                self.transition_candidate = fingerprint
                self.transition_candidate_frames = 1
            else:
                self.transition_candidate = None
                self.transition_candidate_frames = 0
            if observed_move is not None and self.transition_candidate_frames >= 2:
                if observed_move.color != self.tracker.next_color:
                    self.transition_candidate = None
                    self.transition_candidate_frames = 0
                    self.tracker.cancel_pending()
                    self.emit(
                        "warning",
                        message="检测到与当前行棋方相反的落子；请显式 pass 或 setNextPlayer 后继续识别",
                        **self.tracking_payload(),
                    )
                    return
                self.tracker.commit(observed_move)
                self.transition_candidate = None
                self.transition_candidate_frames = 0
                self.reset_snapshot_relock_retry()
                self.region.mark_analysis_success()
                self.emit_position(
                    observed_move,
                    max(analysis.confidence, 0.72),
                    {
                        "captureMs": round(capture_ms, 1),
                        "recognitionMs": round(recognition_ms, 1),
                        "verificationMs": 0.0,
                        "confirmation": "legal-transition",
                        "turnCorrected": observed_move.color != analysis.expected_color,
                    },
                )
                return
        else:
            self.transition_candidate = None
            self.transition_candidate_frames = 0
        if analysis.accepted is None and analysis.snapshot_board:
            snapshot_geometry_stable = (
                (
                    self.region.tracking_mode in ("tracking", "reanchored", "recovered", "verified")
                    and self.region.match_score >= 0.50
                    and self.region.anchor_score >= 0.42
                )
                or (
                    fallback_geometry_stable
                    and analysis.board_agreement >= 0.86
                )
            )
            if not snapshot_geometry_stable:
                self.emit_analysis_scan(analysis, capture_ms, recognition_ms, scan_ms)
                self.region.mark_alignment_failure(
                    "整盘视觉状态发生变化，但四角定位尚未通过复核"
                )
                self.emit(
                    "warning",
                    message="检测到整盘差异，正在先复核棋盘四角；尚未覆盖 QiDao",
                    **self.tracking_payload(),
                )
                return
            # A multi-point snapshot is powerful enough to replace QiDao's
            # entire position, including removals and colour corrections. Do
            # not authorize that replacement from template correlation alone:
            # a slightly stretched quad can remain stable for many frames and
            # classify coordinate labels as a strip of stones. Refit the full
            # grid, recognize it independently, and require the same board.
            verification_started = time.perf_counter()
            try:
                verified_frame = self.region.relock_for_snapshot()
                verified_position = self.tracker.recognize_position(verified_frame)
            except (CaptureError, BoardAlignmentError, ValueError) as error:
                self.tracker.cancel_pending()
                failures = self.note_snapshot_relock_failure(analysis.snapshot_board)
                if failures < 2:
                    self.region.mark_alignment_failure(str(error))
                    self.emit(
                        "warning",
                        message="严格网格复核未通过，正在对同一整盘差异再次确认；尚未覆盖 QiDao",
                        snapshotRelockFailures=failures,
                        **self.tracking_payload(),
                    )
                    return

                # The strict relock intentionally rejects a displacement near
                # one grid period. If the remembered quad is already stale,
                # that guard can reject the same correct board forever. After
                # the *same* temporally stable snapshot fails twice, perform
                # the controlled wide recovery used by manual re-recognition:
                # it requires two independent full-grid fits to agree before
                # mutating the quad. Board contents are still independently
                # checked below before QiDao is updated.
                try:
                    verified_frame = self.region.relock_for_manual_baseline()
                    verified_position = self.tracker.recognize_position(verified_frame)
                except (CaptureError, BoardAlignmentError, ValueError) as wide_error:
                    self.region.mark_alignment_failure(str(wide_error))
                    self.emit(
                        "warning",
                        message="宽范围双网格复核仍未通过，已保留当前 QiDao 棋盘并继续自动恢复",
                        snapshotRelockFailures=failures,
                        **self.tracking_payload(),
                    )
                    return
            self.reset_snapshot_relock_retry()
            if not snapshot_verification_agrees(
                self.tracker.board,
                analysis.snapshot_board,
                verified_position.board,
            ):
                self.tracker.cancel_pending()
                self.reset_snapshot_relock_retry()
                self.region.mark_alignment_failure(
                    "完整网格复核发现与临时局面矛盾的已知交叉点"
                )
                self.emit(
                    "warning",
                    message="完整网格复核发现已知交叉点冲突；错误局面已拦截，正在按真实网格恢复",
                    verificationMs=round(
                        (time.perf_counter() - verification_started) * 1000.0,
                        1,
                    ),
                    **self.tracking_payload(),
                )
                return
            # Preserve the authoritative rule history. A single explainable
            # transition still goes through normal Go rules; a larger missed
            # burst requires a trusted last-move marker and becomes one
            # undoable recovery step instead of a new bootstrap baseline.
            try:
                reconciled_move = self.tracker.reconcile_snapshot(
                    analysis.snapshot_board,
                    analysis.snapshot_last_move,
                )
            except ValueError as error:
                self.tracker.cancel_pending()
                self.reset_snapshot_relock_retry()
                self.region.mark_alignment_failure(str(error))
                self.emit(
                    "warning",
                    message=f"整盘恢复未提交：{error}；请显式 pass 或 setNextPlayer 后继续识别",
                    **self.tracking_payload(),
                )
                return
            self.reset_snapshot_relock_retry()
            self.region.mark_analysis_success()
            self.emit_position(
                reconciled_move,
                analysis.observed_confidence,
                {
                    "captureMs": round(capture_ms, 1),
                    "recognitionMs": round(recognition_ms, 1),
                    "verificationMs": round(
                        (time.perf_counter() - verification_started) * 1000.0,
                        1,
                    ),
                    "confirmation": "grid-snapshot",
                    "turnCorrected": turn_corrected,
                },
            )
            return
        self.region.mark_analysis_success()
        if analysis.accepted is None:
            self.emit_analysis_scan(analysis, capture_ms, recognition_ms, scan_ms)
            return

        stable_geometry = (
            (
                self.region.tracking_mode in ("tracking", "reanchored", "recovered")
                and self.region.match_score >= 0.45
                and self.region.anchor_score >= 0.38
                and analysis.board_agreement >= 0.90
            )
            or (
                fallback_geometry_stable
                and analysis.board_agreement >= 0.90
            )
        )
        if stable_geometry:
            # The state machine already verified a single legal change. A
            # second full-grid fit was the largest per-move latency and also
            # caused overlay jumps. Reserve it for weak geometry only.
            self.tracker.commit(analysis.accepted)
            self.reset_snapshot_relock_retry()
            self.emit_position(
                analysis.accepted,
                analysis.confidence,
                {
                    "captureMs": round(capture_ms, 1),
                    "recognitionMs": round(recognition_ms, 1),
                    "verificationMs": 0.0,
                    "confirmation": "single-frame" if analysis.fast_accepted else "temporal",
                    "turnCorrected": turn_corrected,
                },
            )
            return

        # A second, independent full-grid fit prevents one shifted template
        # match from turning Q16 into another coordinate.
        verification_started = time.perf_counter()
        verified = self.region.reanchor()
        verification = self.tracker.analyze(
            verified,
            expected_color=analysis.accepted.color,
            track_stability=False,
        )
        same_move = (
            verification.best is not None
            and verification.best.x == analysis.accepted.x
            and verification.best.y == analysis.accepted.y
            and verification.best.color == analysis.accepted.color
        )
        minimum = max(self.tracker.threshold, analysis.best_score * 0.84)
        if not same_move or verification.best_score < minimum or verification.board_agreement < 0.82:
            self.tracker.cancel_pending()
            self.emit(
                "rejected",
                message="重新定位后坐标不一致，本帧已丢弃",
                first=analysis.accepted.to_json(),
                verified=verification.best.to_json() if verification.best else None,
            )
            return
        self.tracker.commit(analysis.accepted)
        self.reset_snapshot_relock_retry()
        self.emit_position(
            analysis.accepted,
            min(analysis.confidence, verification.confidence),
            {
                "captureMs": round(capture_ms, 1),
                "recognitionMs": round(recognition_ms, 1),
                "verificationMs": round((time.perf_counter() - verification_started) * 1000.0, 1),
                "confirmation": "grid",
                "turnCorrected": turn_corrected,
            },
        )

    def monitor(self) -> None:
        while not self.closed:
            with self._condition:
                if not self.running:
                    self._condition.wait(timeout=0.25)
                    continue
            started = time.monotonic()
            # Re-identification replaces the tracker and region. Keep scan
            # recovery in the same lifecycle barrier so stop cannot return
            # while an old failure is still mutating or publishing state.
            with self._state_lock:
                try:
                    if not self.running:
                        continue
                    self.scan_once()
                    self.replay_unacknowledged_position()
                except (BoardTrackingError, BoardAlignmentError) as error:
                    if isinstance(error, BoardAlignmentError) and self.region is not None:
                        self.region.mark_alignment_failure(str(error))
                    self.emit("warning", message=str(error), **self.tracking_payload())
                except Exception as error:
                    # One bad screen frame must not tear down a long-running live
                    # game. Command/configuration errors are still reported as
                    # fatal by execute(), but the monitor loop always retries.
                    if self.region is not None:
                        self.region.mark_alignment_failure(str(error))
                    self.emit(
                        "warning",
                        message=f"单帧识别失败，正在自动恢复：{error}",
                        **self.tracking_payload(),
                    )
            elapsed = time.monotonic() - started
            time.sleep(max(0.01, self.config.interval - elapsed))

    def execute(self, command: dict[str, Any]) -> None:
        action = command.get("command")
        if action == "configure":
            with self._state_lock:
                self.configure(command)
        elif action == "baseline":
            with self._state_lock:
                self.capture_baseline()
        elif action == "rebaseline":
            with self._state_lock:
                self.capture_baseline(reuse_calibration=True)
        elif action == "start":
            self.set_running(True)
        elif action == "stop":
            self.set_running(False)
        elif action == "pass":
            with self._state_lock:
                self.pass_turn()
        elif action == "setNextPlayer":
            with self._state_lock:
                self.set_next_player(str(command.get("color", "")))
        elif action == "undo":
            with self._state_lock:
                self.undo()
        elif action == "ackPosition":
            with self._state_lock:
                self.acknowledge_position(int(command.get("sequence", 0)))
        elif action == "shutdown":
            with self._state_lock:
                self.running = False
                self.closed = True
                with self._condition:
                    self._condition.notify_all()
                self._output_barrier()
        elif action == "ping":
            self.emit("pong")
        else:
            raise ValueError(f"未知命令：{action}")

    def run(self) -> int:
        worker = threading.Thread(target=self.monitor, name="vision-monitor", daemon=True)
        worker.start()
        # Do not infer readiness from a settings switch. Exercise the exact
        # signed helper and ScreenCaptureKit path used for real board frames.
        try:
            self.controller.probe_capture()
            self.emit("ready", protocol=1, captureReady=True)
        except Exception as error:
            self.emit("ready", protocol=1, captureReady=False, message=f"真实录屏测试失败：{error}")
        for raw in sys.stdin:
            if self.closed:
                break
            try:
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError("命令必须是 JSON 对象")
                self.execute(command)
                if self.closed:
                    break
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                self.emit("error", message=str(error))
        with self._state_lock:
            self.running = False
            self.closed = True
            with self._condition:
                self._condition.notify_all()
        worker.join(timeout=2.0)
        try:
            self.controller.close()
        finally:
            self._close_output_writer()
        return 0


def main() -> int:
    service_root = Path(__file__).resolve().parents[1]
    return VisionService(service_root, demo="--demo" in sys.argv).run()


if __name__ == "__main__":
    raise SystemExit(main())
