from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

import cv2
import numpy as np

from go_vision.adaptive_vision import (
    AdaptiveBoardTracker,
    BoardAlignmentError,
    PointEvidence,
    PositionRecognition,
)
from go_vision.capture import CaptureError, Point, Quad, WarpedBoard
from go_vision.model import Move, Stone, empty_board
from vision_service import VisionService, snapshot_verification_agrees


def synthetic_board(size: int = 19, stones=(), marker=None) -> WarpedBoard:
    spacing = 42 if size == 19 else 48 if size == 13 else 54
    margin = spacing
    extent = spacing * (size - 1)
    canvas_size = extent + margin * 2 + 1
    image = np.full((canvas_size, canvas_size, 3), (78, 151, 211), dtype=np.uint8)
    for index in range(size):
        point = margin + index * spacing
        cv2.line(image, (margin, point), (margin + extent, point), (28, 42, 53), 1)
        cv2.line(image, (point, margin), (point, margin + extent), (28, 42, 53), 1)
    for x, y, color in stones:
        center = (margin + x * spacing, margin + y * spacing)
        fill = (17, 20, 23) if color == Stone.BLACK else (238, 240, 243)
        cv2.circle(image, center, round(spacing * 0.45), fill, -1, cv2.LINE_AA)
        cv2.circle(image, center, round(spacing * 0.45), (8, 8, 8), 1, cv2.LINE_AA)
    if marker is not None:
        x, y = marker
        center = (margin + x * spacing, margin + y * spacing)
        cv2.circle(image, center, round(spacing * 0.11), (20, 30, 240), -1, cv2.LINE_AA)
    intersections = tuple(
        tuple((margin + x * spacing, margin + y * spacing) for x in range(size))
        for y in range(size)
    )
    return WarpedBoard(image, intersections, spacing, margin)


class CurrentPositionRecognitionTests(unittest.TestCase):
    def test_stop_waits_for_inflight_scan_before_returning(self) -> None:
        analyze_started = threading.Event()
        release_analyze = threading.Event()
        stop_returned = threading.Event()
        move = Move(3, 3, Stone.BLACK, 19)

        class FakeTracker:
            size = 19
            threshold = 0.61
            recognition_backend = "fake"

            def __init__(self) -> None:
                self.board = empty_board(19)
                self.board_history = [self.board]
                self.move_count = 0
                self.next_color = Stone.BLACK

            def can_skip_unchanged_frame(self, _warped) -> bool:
                return False

            def analyze(self, _warped):
                analyze_started.set()
                if not release_analyze.wait(timeout=2.0):
                    raise TimeoutError("test did not release analyze")
                return SimpleNamespace(
                    accepted=move,
                    board_agreement=1.0,
                    color_mismatch_likely=False,
                    confidence=0.99,
                    fast_accepted=True,
                    opposite_best=None,
                )

            def commit(self, accepted: Move) -> None:
                rows = [list(row) for row in self.board]
                rows[accepted.y][accepted.x] = accepted.color
                self.board = tuple(tuple(row) for row in rows)
                self.move_count += 1
                self.next_color = Stone.WHITE

        tracker = FakeTracker()
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = SimpleNamespace(
            capture=lambda: object(),
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            current_quad=quad,
            consecutive_failures=0,
            last_capture_used_fallback=False,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        service.config = SimpleNamespace(interval=0.01)
        service.running = True
        service.closed = False
        service._condition = threading.Condition()
        service._state_lock = threading.RLock()
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        service.snapshot_relock_candidate = None
        service.snapshot_relock_failures = 0
        service.scan_sequence = 0
        service.position_sequence = 0
        service.unacked_position_payload = None
        service.last_position_emit_at = 0.0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        monitor = threading.Thread(target=service.monitor, daemon=True)

        def stop_service() -> None:
            service.set_running(False)
            stop_returned.set()

        stopper = threading.Thread(
            target=stop_service,
            daemon=True,
        )
        try:
            monitor.start()
            self.assertTrue(analyze_started.wait(timeout=1.0))
            stopper.start()
            self.assertFalse(
                stop_returned.wait(timeout=0.1),
                "stop returned while a scan could still commit a position",
            )

            release_analyze.set()
            self.assertTrue(stop_returned.wait(timeout=1.0))
            positions_at_stop = sum(event == "position" for event, _ in events)
            move_count_at_stop = tracker.move_count
            time.sleep(0.05)

            self.assertEqual(positions_at_stop, 1)
            self.assertEqual(sum(event == "position" for event, _ in events), positions_at_stop)
            self.assertEqual(tracker.move_count, move_count_at_stop)
        finally:
            release_analyze.set()
            with service._state_lock:
                service.running = False
                service.closed = True
            with service._condition:
                service._condition.notify_all()
            stopper.join(timeout=1.0)
            monitor.join(timeout=1.0)

    def test_stop_waits_for_inflight_scan_exception_handling(self) -> None:
        handler_started = threading.Event()
        release_handler = threading.Event()
        stop_returned = threading.Event()
        mutations = []

        class Region:
            tracking_mode = "tracking"
            match_score = 1.0
            anchor_score = 1.0
            consecutive_failures = 0
            last_capture_used_fallback = False
            current_quad = Quad(
                Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800)
            )
            controller = SimpleNamespace(locator_backend="test", locator_confidence=1.0)

            def mark_alignment_failure(self, message: str) -> None:
                handler_started.set()
                if not release_handler.wait(timeout=2.0):
                    raise TimeoutError("test did not release exception handler")
                mutations.append(message)

        service = VisionService.__new__(VisionService)
        service.tracker = SimpleNamespace(last_grid_score=0.0)
        service.region = Region()
        service.config = SimpleNamespace(interval=0.01)
        service.running = True
        service.closed = False
        service._condition = threading.Condition()
        service._state_lock = threading.RLock()
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        def fail_scan() -> None:
            raise BoardAlignmentError("test alignment failure")

        def stop_service() -> None:
            service.set_running(False)
            stop_returned.set()

        service.scan_once = fail_scan
        monitor = threading.Thread(target=service.monitor, daemon=True)
        stopper = threading.Thread(target=stop_service, daemon=True)
        try:
            monitor.start()
            self.assertTrue(handler_started.wait(timeout=1.0))
            stopper.start()
            self.assertFalse(
                stop_returned.wait(timeout=0.1),
                "stop returned while failed-scan handling could still publish",
            )

            release_handler.set()
            self.assertTrue(stop_returned.wait(timeout=1.0))
            mutations_at_stop = list(mutations)
            warnings_at_stop = sum(event == "warning" for event, _ in events)
            time.sleep(0.05)

            self.assertEqual(mutations_at_stop, ["test alignment failure"])
            self.assertEqual(mutations, mutations_at_stop)
            self.assertEqual(warnings_at_stop, 1)
            self.assertEqual(sum(event == "warning" for event, _ in events), warnings_at_stop)
        finally:
            release_handler.set()
            with service._state_lock:
                service.running = False
                service.closed = True
            with service._condition:
                service._condition.notify_all()
            stopper.join(timeout=1.0)
            monitor.join(timeout=1.0)

    def test_turn_correction_preserves_position_and_accepts_correct_color(self) -> None:
        initial = synthetic_board(stones=((3, 3, Stone.BLACK),))
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        original_board = tracker.board
        original_move_count = tracker.move_count

        tracker.set_next_color(Stone.BLACK)

        self.assertEqual(tracker.board, original_board)
        self.assertEqual(tracker.move_count, original_move_count)
        self.assertEqual(tracker.next_color, Stone.BLACK)

        current = synthetic_board(
            stones=((3, 3, Stone.BLACK), (15, 15, Stone.BLACK)),
            marker=(15, 15),
        )
        tracker.analyze(current)
        result = tracker.analyze(current)
        self.assertIsNotNone(result.accepted)
        self.assertEqual(result.accepted.color, Stone.BLACK)

    def test_service_turn_correction_emits_authoritative_position(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 19)
        tracker.commit(Move(3, 3, Stone.BLACK, 19))
        original_board = tracker.board
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = SimpleNamespace(
            current_quad=Quad(
                Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800)
            ),
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            consecutive_failures=0,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
        )
        service.scan_sequence = 4
        service.position_sequence = 0
        service.transition_candidate = (15, 15, Stone.WHITE)
        service.transition_candidate_frames = 1
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.set_next_player("B")

        self.assertEqual(tracker.board, original_board)
        self.assertEqual(tracker.move_count, 1)
        self.assertEqual(tracker.next_color, Stone.BLACK)
        self.assertIsNone(service.transition_candidate)
        self.assertEqual(service.transition_candidate_frames, 0)
        self.assertEqual([event for event, _ in events], ["position"])
        self.assertEqual(events[0][1]["nextPlayer"], "B")
        self.assertEqual(events[0][1]["confirmation"], "turn-correction")
        self.assertTrue(events[0][1]["turnCorrected"])

    def test_service_waits_for_explicit_turn_correction_before_opposite_move(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        current = synthetic_board(
            stones=((3, 3, Stone.WHITE),),
            marker=(3, 3),
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = SimpleNamespace(
            capture=lambda: current,
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            current_quad=quad,
            consecutive_failures=0,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()

        self.assertFalse(any(event == "position" for event, _ in events))
        self.assertTrue(any(event == "warning" for event, _ in events))
        self.assertEqual(tracker.move_history, [])
        self.assertEqual(tracker.board_history, [empty_board(19)])
        self.assertEqual(tracker.move_count, 0)
        self.assertEqual(tracker.next_color, Stone.BLACK)

    def test_service_opposite_retry_failure_preserves_tracker_state(self) -> None:
        empty = synthetic_board()
        current = synthetic_board(
            stones=((3, 3, Stone.WHITE),),
            marker=(3, 3),
        )
        tracker = AdaptiveBoardTracker(empty, 19)
        first_analysis = tracker.analyze(current, track_stability=False)
        self.assertTrue(first_analysis.color_mismatch_likely)
        original_analyze = tracker.analyze
        analyze_calls = 0

        def fail_opposite_retry(*args, **kwargs):
            nonlocal analyze_calls
            analyze_calls += 1
            if analyze_calls == 1:
                return first_analysis
            raise ValueError("反色复核失败")

        tracker.analyze = fail_opposite_retry
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = SimpleNamespace(
            capture=lambda: current,
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            current_quad=quad,
            consecutive_failures=0,
            last_capture_used_fallback=False,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))
        original_move_history = list(tracker.move_history)
        original_board_history = list(tracker.board_history)
        original_move_count = tracker.move_count
        original_next_color = tracker.next_color

        service.scan_once()

        tracker.analyze = original_analyze
        self.assertEqual(tracker.move_history, original_move_history)
        self.assertEqual(tracker.board_history, original_board_history)
        self.assertEqual(tracker.move_count, original_move_count)
        self.assertEqual(tracker.next_color, original_next_color)
        self.assertTrue(any(event == "warning" for event, _ in events))

    def test_service_emits_every_consecutive_move_without_manual_trigger(self) -> None:
        """The monitor-facing scan path must advance on every new frame.

        This is the regression contract for the real user workflow: after the
        baseline, Black and White can play in the external client without any
        QiDao button click between the two position events.
        """
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        tracker.bootstrap(tracker.recognize_position(empty))
        frames = iter(
            (
                synthetic_board(stones=((3, 3, Stone.BLACK),), marker=(3, 3)),
                synthetic_board(
                    stones=((3, 3, Stone.BLACK), (15, 15, Stone.WHITE)),
                    marker=(15, 15),
                ),
            )
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = SimpleNamespace(
            capture=lambda: next(frames),
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            current_quad=quad,
            consecutive_failures=0,
            last_capture_used_fallback=False,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        service.scan_sequence = 0
        service.position_sequence = 0
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        service.unacked_position_payload = None
        service.last_position_emit_at = 0.0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()
        service.scan_once()

        positions = [payload for event, payload in events if event == "position"]
        self.assertEqual(len(positions), 2)
        self.assertEqual([payload["positionSequence"] for payload in positions], [1, 2])
        self.assertEqual(positions[0]["board"][3][3], int(Stone.BLACK))
        self.assertEqual(positions[0]["nextPlayer"], "W")
        self.assertEqual(positions[1]["board"][15][15], int(Stone.WHITE))
        self.assertEqual(positions[1]["nextPlayer"], "B")
        self.assertEqual(tracker.move_count, 2)

    def test_service_legal_transition_watchdog_waits_for_explicit_turn_correction(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        current = synthetic_board(stones=((3, 3, Stone.WHITE),), marker=(3, 3))
        empty_evidence = PointEvidence(0.0, 0.0, 0.0, 0.0, 0.0, empty=1.0)
        marginal_white = PointEvidence(0.01, 0.58, 0.70, 0.70, 70.0, empty=0.08)
        evidences = [
            [marginal_white if (x, y) == (3, 3) else empty_evidence for x in range(19)]
            for y in range(19)
        ]
        tracker._evidence_grid = lambda _image, _features: (
            tuple(tuple(row) for row in evidences),
            True,
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = SimpleNamespace(
            capture=lambda: current,
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            current_quad=quad,
            consecutive_failures=0,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()
        service.scan_once()

        self.assertEqual([event for event, _ in events], ["scan", "warning"])
        self.assertEqual(tracker.board[3][3], Stone.EMPTY)
        self.assertEqual(tracker.move_history, [])
        self.assertEqual(tracker.next_color, Stone.BLACK)

    def test_service_commits_on_grid_verified_tracking_fallback(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        current = synthetic_board(stones=((3, 3, Stone.BLACK),), marker=(3, 3))
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = SimpleNamespace(
            capture=lambda: current,
            tracking_mode="fallback",
            match_score=0.12,
            anchor_score=0.10,
            current_quad=quad,
            consecutive_failures=1,
            last_capture_used_fallback=True,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
            mark_analysis_success=lambda: None,
        )
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()

        self.assertEqual([event for event, _ in events], ["position"])
        self.assertEqual(tracker.board[3][3], Stone.BLACK)

    def test_service_blocks_stable_false_snapshot_when_full_grid_disagrees(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        tracker.bootstrap(tracker.recognize_position(empty))
        false_frame = synthetic_board(
            stones=((3, 3, Stone.BLACK), (15, 15, Stone.WHITE)),
            marker=(15, 15),
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))

        class Region:
            tracking_mode = "tracking"
            match_score = 1.0
            anchor_score = 1.0
            current_quad = quad
            consecutive_failures = 0
            last_capture_used_fallback = False
            controller = SimpleNamespace(locator_backend="test", locator_confidence=1.0)

            def __init__(self) -> None:
                self.relock_count = 0
                self.alignment_errors = []

            def capture(self):
                return false_frame

            def relock_for_snapshot(self):
                self.relock_count += 1
                return empty

            def mark_analysis_success(self):
                return None

            def mark_alignment_failure(self, message):
                self.alignment_errors.append(message)

        region = Region()
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        service.scan_sequence = 0
        service.position_sequence = 0
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()
        tracker.quick_frame_signature = None
        service.scan_once()

        self.assertEqual(region.relock_count, 1)
        self.assertTrue(region.alignment_errors)
        self.assertFalse(any(event == "position" for event, _ in events))
        self.assertTrue(any(event == "warning" for event, _ in events))
        self.assertTrue(all(value == Stone.EMPTY for row in tracker.board for value in row))

    def test_service_rejects_multi_move_snapshot_without_last_move_marker(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        current = synthetic_board(
            stones=((3, 3, Stone.WHITE), (15, 15, Stone.WHITE)),
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))

        class Region:
            tracking_mode = "tracking"
            match_score = 1.0
            anchor_score = 1.0
            current_quad = quad
            consecutive_failures = 0
            last_capture_used_fallback = False
            controller = SimpleNamespace(locator_backend="test", locator_confidence=1.0)

            def capture(self):
                return current

            def relock_for_snapshot(self):
                return current

            def mark_analysis_success(self):
                return None

            def mark_alignment_failure(self, _message):
                return None

        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = Region()
        service.scan_sequence = 0
        service.position_sequence = 0
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        service.unacked_position_payload = None
        service.last_position_emit_at = 0.0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()
        tracker.quick_frame_signature = None
        service.scan_once()

        self.assertFalse(any(event == "position" for event, _ in events))
        self.assertTrue(any(event == "warning" for event, _ in events))
        self.assertEqual(tracker.board, empty_board(19))
        self.assertEqual(tracker.move_history, [])
        self.assertEqual(tracker.board_history, [empty_board(19)])
        self.assertEqual(tracker.next_color_history, [Stone.BLACK])
        self.assertEqual(tracker.move_count, 0)
        self.assertEqual(tracker.next_color, Stone.BLACK)

    def test_service_uses_wide_double_grid_recovery_after_repeated_strict_failure(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        tracker.bootstrap(tracker.recognize_position(empty))
        current = synthetic_board(
            stones=((3, 3, Stone.BLACK), (15, 15, Stone.WHITE)),
            marker=(15, 15),
        )
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))

        class Region:
            tracking_mode = "tracking"
            match_score = 1.0
            anchor_score = 1.0
            current_quad = quad
            consecutive_failures = 0
            last_capture_used_fallback = False
            controller = SimpleNamespace(locator_backend="test", locator_confidence=1.0)

            def __init__(self) -> None:
                self.strict_relocks = 0
                self.wide_relocks = 0
                self.alignment_errors = []

            def capture(self):
                return current

            def relock_for_snapshot(self):
                self.strict_relocks += 1
                raise CaptureError("周期网格校正跳变 81.5 px")

            def relock_for_manual_baseline(self):
                self.wide_relocks += 1
                return current

            def mark_analysis_success(self):
                return None

            def mark_alignment_failure(self, message):
                self.alignment_errors.append(message)

        region = Region()
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        service.scan_sequence = 0
        service.position_sequence = 0
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        service.snapshot_relock_candidate = None
        service.snapshot_relock_failures = 0
        service.unacked_position_payload = None
        service.last_position_emit_at = 0.0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        # cancel_pending() deliberately restarts temporal board voting after a
        # failed destructive verification, so each strict attempt needs two
        # stable frames. The second identical failure must automatically take
        # the safe wide/double-grid path instead of rejecting forever.
        for _ in range(4):
            tracker.quick_frame_signature = None
            service.scan_once()

        positions = [payload for event, payload in events if event == "position"]
        self.assertEqual(region.strict_relocks, 2)
        self.assertEqual(region.wide_relocks, 1)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["confirmation"], "grid-snapshot")
        self.assertEqual(tracker.board[3][3], Stone.BLACK)
        self.assertEqual(tracker.board[15][15], Stone.WHITE)
        self.assertEqual(service.snapshot_relock_failures, 0)

    def test_snapshot_verification_treats_unknown_as_abstention(self) -> None:
        confirmed = tuple(tuple(Stone.EMPTY for _ in range(9)) for _ in range(9))
        candidate_rows = [list(row) for row in confirmed]
        candidate_rows[2][2] = Stone.BLACK
        candidate_rows[6][6] = Stone.WHITE
        candidate_rows[7][7] = Stone.BLACK
        candidate = tuple(tuple(row) for row in candidate_rows)
        verified_rows = [list(row) for row in candidate]
        verified_rows[7][7] = Stone.UNKNOWN
        verified_rows[4][4] = Stone.UNKNOWN
        verified = tuple(tuple(row) for row in verified_rows)

        self.assertTrue(snapshot_verification_agrees(confirmed, candidate, verified))

        contradictory_rows = [list(row) for row in verified]
        contradictory_rows[3][3] = Stone.BLACK
        contradictory = tuple(tuple(row) for row in contradictory_rows)
        self.assertFalse(snapshot_verification_agrees(confirmed, candidate, contradictory))

    def test_snapshot_verification_rejects_unknown_deletion(self) -> None:
        confirmed = [list(row) for row in empty_board(9)]
        confirmed[3][3] = Stone.WHITE
        candidate = [row[:] for row in confirmed]
        candidate[3][3] = Stone.EMPTY
        candidate[4][4] = Stone.BLACK
        verified = [row[:] for row in candidate]
        verified[3][3] = Stone.UNKNOWN
        self.assertFalse(snapshot_verification_agrees(
            tuple(map(tuple, confirmed)), tuple(map(tuple, candidate)), tuple(map(tuple, verified))
        ))

    def test_snapshot_verification_requires_strict_majority_for_even_additions(self) -> None:
        confirmed = empty_board(9)
        candidate = [list(row) for row in confirmed]
        candidate[2][2] = Stone.BLACK
        candidate[6][6] = Stone.WHITE
        verified = [list(row) for row in candidate]
        verified[6][6] = Stone.UNKNOWN
        self.assertFalse(snapshot_verification_agrees(
            confirmed, tuple(map(tuple, candidate)), tuple(map(tuple, verified))
        ))

    def test_service_resyncs_two_missed_moves_and_capture_with_unrelated_unknown(self) -> None:
        before = synthetic_board(
            size=9,
            stones=(
                (1, 1, Stone.WHITE),
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (1, 2, Stone.BLACK),
                (7, 7, Stone.WHITE),
            ),
            marker=(7, 7),
        )
        tracker = AdaptiveBoardTracker(before, 9)
        tracker.bootstrap(tracker.recognize_position(before))
        original_board = tracker.board
        original_move_count = tracker.move_count
        after = synthetic_board(
            size=9,
            stones=(
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (1, 2, Stone.BLACK),
                (2, 1, Stone.BLACK),
                (7, 7, Stone.WHITE),
                (5, 5, Stone.WHITE),
            ),
            marker=(5, 5),
        )
        verified = tracker.recognize_position(after)
        verified_rows = [list(row) for row in verified.board]
        verified_rows[4][4] = Stone.UNKNOWN
        verified_with_unknown = PositionRecognition(
            tuple(tuple(row) for row in verified_rows),
            verified.confidence,
            1,
            verified.next_color,
            verified.last_move,
            verified.point_scores,
        )
        tracker.recognize_position = lambda _frame: verified_with_unknown
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))

        class Region:
            tracking_mode = "tracking"
            match_score = 1.0
            anchor_score = 1.0
            current_quad = quad
            consecutive_failures = 0
            last_capture_used_fallback = False
            controller = SimpleNamespace(locator_backend="test", locator_confidence=1.0)

            def capture(self):
                return after

            def relock_for_snapshot(self):
                return after

            def mark_analysis_success(self):
                return None

            def mark_alignment_failure(self, _message):
                raise AssertionError("compatible unrelated UNKNOWN verification was rejected")

        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = Region()
        service.scan_sequence = 0
        service.position_sequence = 0
        service.transition_candidate = None
        service.transition_candidate_frames = 0
        service.unacked_position_payload = None
        service.last_position_emit_at = 0.0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        service.scan_once()
        tracker.quick_frame_signature = None
        service.scan_once()

        positions = [payload for event, payload in events if event == "position"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["confirmation"], "grid-snapshot")
        self.assertEqual(tracker.board[1][1], Stone.EMPTY)
        self.assertEqual(tracker.board[1][2], Stone.BLACK)
        self.assertEqual(tracker.board[5][5], Stone.WHITE)
        self.assertEqual(tracker.board_history, [original_board, tracker.board])
        self.assertEqual(len(tracker.move_history), 1)
        self.assertEqual(
            (tracker.move_history[-1].x, tracker.move_history[-1].y, tracker.move_history[-1].color),
            (5, 5, Stone.WHITE),
        )
        self.assertEqual(tracker.move_count, original_move_count + 1)
        self.assertEqual(tracker.next_color, Stone.BLACK)

    def test_unchanged_gate_skips_idle_frames_but_never_a_new_stone(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        self.assertFalse(tracker.can_skip_unchanged_frame(empty))
        self.assertTrue(tracker.can_skip_unchanged_frame(empty))

        changed = synthetic_board(stones=((3, 3, Stone.BLACK),))
        self.assertFalse(tracker.can_skip_unchanged_frame(changed))
        tracker.pending = (3, 3, Stone.BLACK)
        self.assertFalse(tracker.can_skip_unchanged_frame(changed))

    def test_unchanged_gate_accumulates_a_slow_fade_against_last_analysis(self) -> None:
        empty = synthetic_board()
        tracker = AdaptiveBoardTracker(empty, 19)
        self.assertFalse(tracker.can_skip_unchanged_frame(empty))

        first_image = empty.image.copy()
        first_image[100:132, 100:132] = np.clip(
            first_image[100:132, 100:132].astype(np.int16) - 2,
            0,
            255,
        ).astype(np.uint8)
        first = WarpedBoard(first_image, empty.intersections, empty.spacing, empty.margin)
        self.assertTrue(tracker.can_skip_unchanged_frame(first))

        second_image = empty.image.copy()
        second_image[100:132, 100:132] = np.clip(
            second_image[100:132, 100:132].astype(np.int16) - 10,
            0,
            255,
        ).astype(np.uint8)
        second = WarpedBoard(second_image, empty.intersections, empty.spacing, empty.margin)
        self.assertFalse(tracker.can_skip_unchanged_frame(second))

    def test_bootstraps_non_empty_position_and_detects_next_move(self) -> None:
        initial = synthetic_board(
            stones=((15, 3, Stone.BLACK), (14, 12, Stone.WHITE)),
            marker=(14, 12),
        )
        tracker = AdaptiveBoardTracker(initial, 19)
        recognition = tracker.recognize_position(initial)
        self.assertEqual(recognition.board[3][15], Stone.BLACK)
        self.assertEqual(recognition.board[12][14], Stone.WHITE)
        self.assertEqual(recognition.next_color, Stone.BLACK)
        self.assertEqual(recognition.unknown_points, 0)
        self.assertIsNotNone(recognition.last_move)
        self.assertEqual((recognition.last_move.x, recognition.last_move.y), (14, 12))

        tracker.bootstrap(recognition)
        unchanged = tracker.analyze(initial)
        self.assertIsNone(unchanged.best)
        self.assertIsNone(unchanged.accepted)
        current = synthetic_board(
            stones=(
                (15, 3, Stone.BLACK),
                (14, 12, Stone.WHITE),
                (3, 3, Stone.BLACK),
            ),
            marker=(3, 3),
        )
        tracker.analyze(current)
        result = tracker.analyze(current)
        self.assertIsNotNone(result.accepted)
        self.assertEqual((result.accepted.x, result.accepted.y), (3, 3))

    def test_empty_star_points_are_not_stones_without_empty_baseline(self) -> None:
        current = synthetic_board()
        tracker = AdaptiveBoardTracker(current, 19)
        recognition = tracker.recognize_position(current)
        self.assertTrue(all(value == Stone.EMPTY for row in recognition.board for value in row))
        self.assertEqual(recognition.unknown_points, 0)

    def test_low_grid_visibility_is_diagnostic_not_a_scan_blocker(self) -> None:
        current = synthetic_board(stones=((3, 3, Stone.BLACK),))
        tracker = AdaptiveBoardTracker(synthetic_board(), 19)
        tracker.grid_alignment_score = lambda _image: 0.05

        score = tracker.ensure_alignment(current)

        self.assertEqual(score, 0.05)
        self.assertEqual(tracker.last_grid_score, 0.05)

    def test_full_frame_detector_does_not_publish_one_frame_mass_change(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        changed = synthetic_board(
            stones=((3, 3, Stone.BLACK), (4, 4, Stone.BLACK), (10, 10, Stone.WHITE))
        )
        result = tracker.analyze(changed)
        self.assertFalse(result.frame_valid)
        self.assertIsNone(result.best)
        self.assertEqual(result.snapshot_board, ())
        self.assertEqual(result.snapshot_stable_frames, 0)
        self.assertTrue(all(value == Stone.EMPTY for row in tracker.board for value in row))
        self.assertGreaterEqual(result.unexpected_stones, 3)

    def test_two_rapid_moves_trigger_direct_snapshot_resync(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        current = synthetic_board(
            stones=((3, 3, Stone.BLACK), (15, 15, Stone.WHITE)),
            marker=(15, 15),
        )

        first = tracker.analyze(current)
        result = tracker.analyze(current)

        self.assertEqual(first.snapshot_board, ())
        self.assertTrue(result.frame_valid)
        self.assertIsNone(result.accepted)
        self.assertEqual(result.snapshot_board[3][3], Stone.BLACK)
        self.assertEqual(result.snapshot_board[15][15], Stone.WHITE)
        self.assertEqual(result.snapshot_next_color, Stone.BLACK)
        self.assertIsNotNone(result.snapshot_last_move)
        self.assertEqual(
            (result.snapshot_last_move.x, result.snapshot_last_move.y),
            (15, 15),
        )

    def test_arbitrarily_many_missed_moves_trigger_direct_snapshot_resync(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        stones = (
            (3, 3, Stone.BLACK),
            (15, 15, Stone.WHITE),
            (3, 15, Stone.BLACK),
            (15, 3, Stone.WHITE),
            (9, 9, Stone.BLACK),
            (9, 3, Stone.WHITE),
            (9, 15, Stone.BLACK),
        )
        current = synthetic_board(stones=stones, marker=(9, 15))

        tracker.analyze(current)
        result = tracker.analyze(current)

        self.assertEqual(result.snapshot_stable_frames, 2)
        self.assertEqual(
            sum(value in (Stone.BLACK, Stone.WHITE) for row in result.snapshot_board for value in row),
            len(stones),
        )
        self.assertEqual(result.snapshot_next_color, Stone.WHITE)

    def test_clear_legal_move_is_accepted_in_one_frame(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        current = synthetic_board(stones=((3, 3, Stone.BLACK),), marker=(3, 3))

        result = tracker.analyze(current)

        self.assertTrue(result.fast_accepted)
        self.assertIsNotNone(result.accepted)
        self.assertEqual((result.accepted.x, result.accepted.y), (3, 3))

    def test_accepted_move_publishes_captured_group_as_empty(self) -> None:
        # Black has already surrounded all but one liberty of a two-stone white
        # group. Playing C18 captures the group. The visual state machine must
        # publish the complete rule result, not merely overlay the new stone.
        before = synthetic_board(
            stones=(
                (1, 1, Stone.WHITE),
                (1, 2, Stone.WHITE),
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (0, 2, Stone.BLACK),
                (1, 3, Stone.BLACK),
                (2, 2, Stone.BLACK),
                (12, 12, Stone.WHITE),
                (13, 13, Stone.WHITE),
                (14, 14, Stone.WHITE),
            ),
            marker=(14, 14),
        )
        tracker = AdaptiveBoardTracker(before, 19)
        tracker.bootstrap(tracker.recognize_position(before))
        after = synthetic_board(
            stones=(
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (0, 2, Stone.BLACK),
                (1, 3, Stone.BLACK),
                (2, 2, Stone.BLACK),
                (2, 1, Stone.BLACK),
                (12, 12, Stone.WHITE),
                (13, 13, Stone.WHITE),
                (14, 14, Stone.WHITE),
            ),
            marker=(2, 1),
        )

        result = tracker.analyze(after)
        if result.accepted is None:
            result = tracker.analyze(after)

        self.assertIsNotNone(result.accepted)
        self.assertEqual((result.accepted.x, result.accepted.y), (2, 1))
        tracker.commit(result.accepted)
        self.assertEqual(tracker.board[1][1], Stone.EMPTY)
        self.assertEqual(tracker.board[2][1], Stone.EMPTY)
        self.assertEqual(tracker.board[1][2], Stone.BLACK)

    def test_stale_visual_stone_cannot_override_rule_based_capture(self) -> None:
        before = synthetic_board(
            stones=(
                (1, 1, Stone.WHITE),
                (1, 2, Stone.WHITE),
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (0, 2, Stone.BLACK),
                (1, 3, Stone.BLACK),
                (2, 2, Stone.BLACK),
                (12, 12, Stone.WHITE),
                (13, 13, Stone.WHITE),
                (14, 14, Stone.WHITE),
            ),
            marker=(14, 14),
        )
        tracker = AdaptiveBoardTracker(before, 19)
        tracker.bootstrap(tracker.recognize_position(before))
        after = synthetic_board(
            stones=(
                (1, 0, Stone.BLACK),
                (0, 1, Stone.BLACK),
                (0, 2, Stone.BLACK),
                (1, 3, Stone.BLACK),
                (2, 2, Stone.BLACK),
                (2, 1, Stone.BLACK),
                (12, 12, Stone.WHITE),
                (13, 13, Stone.WHITE),
                (14, 14, Stone.WHITE),
            ),
            marker=(2, 1),
        )

        empty = PointEvidence(0.0, 0.0, 0.0, 0.0, 0.0, empty=1.0)
        black = PointEvidence(0.99, 0.01, 0.99, 0.9, -110.0, empty=0.01)
        white = PointEvidence(0.01, 0.99, 0.99, 0.9, 90.0, empty=0.01)
        after_stones = {
            (1, 0): black,
            (0, 1): black,
            (0, 2): black,
            (1, 3): black,
            (2, 2): black,
            (2, 1): black,
            (12, 12): white,
            (13, 13): white,
            (14, 14): white,
        }
        evidences = [
            [after_stones.get((x, y), empty) for x in range(19)]
            for y in range(19)
        ]
        # Reproduce a real-screen classifier lag: the new capturing stone is
        # clear, but both vacated points still look like the old white group.
        evidences[1][1] = white
        evidences[2][1] = white
        tracker._evidence_grid = lambda _image, _features: (
            tuple(tuple(row) for row in evidences),
            True,
        )

        result = tracker.analyze(after)
        if result.accepted is None:
            result = tracker.analyze(after)

        self.assertIsNotNone(result.accepted)
        self.assertEqual((result.accepted.x, result.accepted.y), (2, 1))
        tracker.commit(result.accepted)
        self.assertEqual(tracker.board[1][1], Stone.EMPTY)
        self.assertEqual(tracker.board[2][1], Stone.EMPTY)
        self.assertEqual(tracker.board[1][2], Stone.BLACK)

    def test_complete_legal_board_accepts_marginal_model_stone_in_one_frame(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        current = synthetic_board(stones=((3, 3, Stone.BLACK),))
        empty = PointEvidence(0.0, 0.0, 0.0, 0.0, 0.0, empty=1.0)
        marginal_black = PointEvidence(0.58, 0.01, 0.70, 0.70, -140.0, empty=0.08)
        evidences = [
            [marginal_black if (x, y) == (3, 3) else empty for x in range(19)]
            for y in range(19)
        ]
        tracker._evidence_grid = lambda _image, _features: (
            tuple(tuple(row) for row in evidences),
            True,
        )

        result = tracker.analyze(current)

        self.assertLess(result.best_score, tracker.threshold)
        self.assertTrue(result.fast_accepted)
        self.assertIsNotNone(result.accepted)
        self.assertEqual((result.accepted.x, result.accepted.y), (3, 3))

    def test_radial_colour_recovers_white_stone_rejected_by_onnx(self) -> None:
        current = synthetic_board(
            stones=((3, 3, Stone.BLACK), (5, 2, Stone.WHITE)),
        )
        tracker = AdaptiveBoardTracker(current, 19)
        probabilities = np.zeros((19, 19, 4), dtype=np.float32)
        probabilities[..., 0] = 1.0
        probabilities[3, 3] = (0.04, 0.82, 0.05, 0.09)
        # Reproduce the real compact-browser failure: ONNX sees a glossy white
        # stone as UNKNOWN, while its centre/ring colour remains decisive.
        probabilities[2, 5] = (0.08, 0.06, 0.16, 0.70)

        class FixedClassifier:
            def classify(self, _image, _intersections, _spacing):
                return probabilities

        tracker.intersection_classifier = FixedClassifier()
        recognition = tracker.recognize_position(current)

        self.assertEqual(recognition.board[3][3], Stone.BLACK)
        self.assertEqual(recognition.board[2][5], Stone.WHITE)

    def test_repeated_low_confidence_empty_cannot_delete_confirmed_stone(self) -> None:
        initial = synthetic_board(stones=((3, 3, Stone.BLACK),))
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        blank = synthetic_board()
        strong_empty = PointEvidence(0.0, 0.0, 0.0, 0.0, 0.0, empty=1.0)
        weak_empty = PointEvidence(0.10, 0.01, 0.1, 0.1, 0.0, empty=0.12)
        evidences = [
            [weak_empty if (x, y) == (3, 3) else strong_empty for x in range(19)]
            for y in range(19)
        ]
        tracker._evidence_grid = lambda _image, _features: (
            tuple(tuple(row) for row in evidences),
            True,
        )

        results = [tracker.analyze(blank) for _ in range(5)]

        self.assertTrue(all(result.snapshot_board == () for result in results))
        self.assertEqual(tracker.board[3][3], Stone.BLACK)

    def test_translucent_hover_stone_is_never_committed(self) -> None:
        initial = synthetic_board(
            stones=((15, 3, Stone.BLACK), (14, 12, Stone.WHITE)),
            marker=(14, 12),
        )
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        hovered = synthetic_board(
            stones=((15, 3, Stone.BLACK), (14, 12, Stone.WHITE)),
        )
        spacing = hovered.spacing
        center = (spacing + 3 * spacing, spacing + 3 * spacing)
        overlay = hovered.image.copy()
        cv2.circle(
            overlay,
            center,
            round(spacing * 0.45),
            (17, 20, 23),
            -1,
            cv2.LINE_AA,
        )
        hovered_image = cv2.addWeighted(overlay, 0.45, hovered.image, 0.55, 0)
        hovered = WarpedBoard(
            hovered_image,
            hovered.intersections,
            hovered.spacing,
            hovered.margin,
        )

        results = [tracker.analyze(hovered) for _ in range(5)]

        self.assertTrue(all(result.accepted is None for result in results))
        self.assertTrue(all(result.snapshot_board == () for result in results))
        self.assertEqual(
            [(move.x, move.y) for move in results[-1].hover_previews],
            [(3, 3)],
        )
        self.assertEqual(tracker.board[3][3], Stone.EMPTY)

    def test_unrelated_hover_does_not_block_a_real_legal_move(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        current = synthetic_board(stones=((3, 3, Stone.BLACK),))
        spacing = current.spacing
        hover_center = (spacing + 10 * spacing, spacing + 10 * spacing)
        overlay = current.image.copy()
        cv2.circle(
            overlay,
            hover_center,
            round(spacing * 0.45),
            (238, 240, 243),
            -1,
            cv2.LINE_AA,
        )
        image = cv2.addWeighted(overlay, 0.40, current.image, 0.60, 0)
        with_hover = WarpedBoard(image, current.intersections, spacing, current.margin)

        first = tracker.analyze(with_hover)
        second = tracker.analyze(with_hover)

        self.assertEqual([(move.x, move.y) for move in first.hover_previews], [(10, 10)])
        self.assertIsNone(first.accepted)
        self.assertIsNotNone(second.accepted)
        self.assertEqual((second.accepted.x, second.accepted.y), (3, 3))

    def test_unrelated_hover_does_not_block_full_position_recovery(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 19)
        current = synthetic_board(
            stones=((3, 3, Stone.BLACK), (15, 15, Stone.WHITE)),
            marker=(15, 15),
        )
        spacing = current.spacing
        hover_center = (spacing + 10 * spacing, spacing + 10 * spacing)
        overlay = current.image.copy()
        cv2.circle(
            overlay,
            hover_center,
            round(spacing * 0.45),
            (17, 20, 23),
            -1,
            cv2.LINE_AA,
        )
        image = cv2.addWeighted(overlay, 0.40, current.image, 0.60, 0)
        with_hover = WarpedBoard(image, current.intersections, spacing, current.margin)

        first = tracker.analyze(with_hover)
        second = tracker.analyze(with_hover)

        self.assertEqual(first.snapshot_board, ())
        self.assertTrue(second.snapshot_board)
        self.assertEqual(second.snapshot_board[3][3], Stone.BLACK)
        self.assertEqual(second.snapshot_board[15][15], Stone.WHITE)
        self.assertEqual(second.snapshot_board[10][10], Stone.EMPTY)

    def test_false_move_is_relocated_to_stable_real_position(self) -> None:
        initial = synthetic_board()
        tracker = AdaptiveBoardTracker(initial, 19)
        tracker.bootstrap(tracker.recognize_position(initial))
        tracker.commit(Move(3, 3, Stone.BLACK, 19))
        corrected = synthetic_board(
            stones=((15, 15, Stone.BLACK),),
        )

        first = tracker.analyze(corrected)
        result = tracker.analyze(corrected)

        self.assertEqual(first.snapshot_board, ())
        self.assertEqual(result.snapshot_board[3][3], Stone.EMPTY)
        self.assertEqual(result.snapshot_board[15][15], Stone.BLACK)
        self.assertEqual(result.snapshot_next_color, Stone.WHITE)

    def test_full_board_consensus_recovers_multiple_kinds_of_state_error(self) -> None:
        wrong = synthetic_board(
            stones=(
                (3, 3, Stone.BLACK),
                (4, 4, Stone.WHITE),
                (5, 5, Stone.BLACK),
                (10, 10, Stone.WHITE),
            ),
            marker=(10, 10),
        )
        tracker = AdaptiveBoardTracker(wrong, 19)
        tracker.bootstrap(tracker.recognize_position(wrong))
        correct = synthetic_board(
            stones=(
                (3, 3, Stone.BLACK),
                (4, 4, Stone.WHITE),
                (10, 10, Stone.BLACK),
                (15, 15, Stone.BLACK),
                (14, 14, Stone.WHITE),
            ),
            marker=(14, 14),
        )
        expected = tracker.recognize_position(correct).board

        first = tracker.analyze(correct)
        result = tracker.analyze(correct)

        self.assertEqual(first.snapshot_board, ())
        self.assertEqual(result.snapshot_board, expected)
        self.assertGreaterEqual(result.reconciliation_differences, 4)
        self.assertEqual(result.snapshot_next_color, Stone.BLACK)
        self.assertIsNotNone(result.snapshot_last_move)
        self.assertEqual(
            (result.snapshot_last_move.x, result.snapshot_last_move.y),
            (14, 14),
        )

    def test_recovery_uses_per_point_consensus_across_a_noisy_frame(self) -> None:
        wrong = synthetic_board(stones=((3, 3, Stone.BLACK),))
        tracker = AdaptiveBoardTracker(wrong, 19)
        tracker.bootstrap(tracker.recognize_position(wrong))
        correct = synthetic_board(
            stones=((15, 15, Stone.BLACK), (14, 14, Stone.WHITE)),
            marker=(14, 14),
        )
        noisy = synthetic_board(
            stones=(
                (15, 15, Stone.BLACK),
                (14, 14, Stone.WHITE),
                (9, 9, Stone.BLACK),
            ),
            marker=(14, 14),
        )
        expected = tracker.recognize_position(correct).board

        first = tracker.analyze(correct)
        middle = tracker.analyze(noisy)
        result = tracker.analyze(correct)

        self.assertEqual(first.snapshot_board, ())
        # The two real changed points agree across the first two frames. The
        # unrelated one-frame false stone must no longer delay that recovery.
        self.assertEqual(middle.snapshot_board, expected)
        self.assertEqual(result.snapshot_board, expected)
        self.assertEqual(result.snapshot_board[9][9], Stone.EMPTY)

    def test_recovery_does_not_require_confident_unchanged_intersections(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 19)
        observed = [list(row) for row in tracker.board]
        observed[3][3] = Stone.BLACK
        observed_board = tuple(tuple(row) for row in observed)
        point_confidences = tuple(
            tuple(0.92 if (x, y) == (3, 3) else 0.05 for x in range(19))
            for y in range(19)
        )
        tracker.absolute_observations = [
            (observed_board, point_confidences),
            (observed_board, point_confidences),
        ]

        consensus, confidence = tracker._absolute_consensus()

        self.assertEqual(consensus[3][3], Stone.BLACK)
        self.assertTrue(all(
            consensus[y][x] == Stone.EMPTY
            for y in range(19)
            for x in range(19)
            if (x, y) != (3, 3)
        ))
        self.assertGreaterEqual(confidence, 0.90)

    def test_service_replays_position_until_qidao_acknowledges_it(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 19)
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = SimpleNamespace(
            current_quad=quad,
            tracking_mode="tracking",
            match_score=1.0,
            anchor_score=1.0,
            consecutive_failures=0,
            controller=SimpleNamespace(locator_backend="test", locator_confidence=1.0),
        )
        service = VisionService.__new__(VisionService)
        service.tracker = tracker
        service.region = region
        service.scan_sequence = 7
        service.position_sequence = 0
        events = []
        service.emit = lambda event, **payload: events.append((event, payload))

        move = Move(3, 3, Stone.BLACK, 19)
        tracker.commit(move)
        service.emit_position(move, 0.9)
        service.last_position_emit_at = 0.0
        service.replay_unacknowledged_position()

        positions = [payload for event, payload in events if event == "position"]
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0]["positionSequence"], positions[1]["positionSequence"])
        self.assertTrue(positions[1]["replayed"])

        service.acknowledge_position(positions[0]["positionSequence"])
        service.last_position_emit_at = 0.0
        service.replay_unacknowledged_position()
        self.assertEqual(len([event for event, _ in events if event == "position"]), 2)


if __name__ == "__main__":
    unittest.main()
