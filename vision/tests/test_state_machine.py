from __future__ import annotations

import unittest
from types import SimpleNamespace

import cv2
import numpy as np

from go_vision.adaptive_vision import AdaptiveBoardTracker, PointEvidence
from go_vision.capture import Point, Quad, WarpedBoard
from go_vision.model import (
    Move,
    Stone,
    empty_board,
    legal_move_result,
    normalize_snapshot_captures,
    play_move,
)
from vision_service import VisionService


def synthetic_board(size: int = 9, stones=(), marker=None) -> WarpedBoard:
    spacing = 54
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


class GoStateMachineTests(unittest.TestCase):
    def test_rejects_observed_move_that_repeats_a_position(self) -> None:
        board = empty_board(9)
        move = Move(2, 2, Stone.BLACK, 9)
        repeated = play_move(board, move)
        with self.assertRaisesRegex(ValueError, "重复局面"):
            legal_move_result(board, move, (board, repeated))

    def test_computes_capture_instead_of_trusting_visual_deletions(self) -> None:
        board = [list(row) for row in empty_board(9)]
        board[1][1] = Stone.WHITE
        board[0][1] = Stone.BLACK
        board[1][0] = Stone.BLACK
        board[2][1] = Stone.BLACK
        position = tuple(tuple(row) for row in board)
        result = legal_move_result(position, Move(2, 1, Stone.BLACK, 9), (position,))
        self.assertEqual(result[1][1], Stone.EMPTY)
        self.assertEqual(result[1][2], Stone.BLACK)

    def test_snapshot_removes_stale_zero_liberty_group(self) -> None:
        board = [list(row) for row in empty_board(19)]
        board[14][16] = Stone.WHITE  # R5, matching the reported real position.
        board[13][16] = Stone.BLACK
        board[15][16] = Stone.BLACK
        board[14][15] = Stone.BLACK
        board[14][17] = Stone.BLACK

        normalized = normalize_snapshot_captures(
            tuple(tuple(row) for row in board),
            Stone.WHITE,
        )

        self.assertEqual(normalized[14][16], Stone.EMPTY)
        self.assertEqual(normalized[13][16], Stone.BLACK)
        self.assertEqual(normalized[15][16], Stone.BLACK)

    def test_snapshot_removes_opponent_before_testing_capturing_stone(self) -> None:
        board = [list(row) for row in empty_board(9)]
        board[1][1] = Stone.WHITE
        board[0][1] = Stone.BLACK
        board[1][0] = Stone.BLACK
        board[2][1] = Stone.BLACK
        board[1][2] = Stone.BLACK  # The capturing move.
        # In this raw visual frame both adjacent groups have no liberties. The
        # new black stone lives only after the captured white point is cleared.
        board[0][2] = Stone.WHITE
        board[1][3] = Stone.WHITE
        board[2][2] = Stone.WHITE

        normalized = normalize_snapshot_captures(
            tuple(tuple(row) for row in board),
            Stone.BLACK,
        )

        self.assertEqual(normalized[1][1], Stone.EMPTY)
        self.assertEqual(normalized[1][2], Stone.BLACK)

    def test_analyze_normalizes_capture_using_previous_player(self) -> None:
        warped = synthetic_board()
        tracker = AdaptiveBoardTracker(warped, 9)
        tracker.set_next_color(Stone.WHITE)
        raw_board = [list(row) for row in empty_board(9)]
        raw_board[1][1] = Stone.WHITE
        raw_board[0][1] = Stone.BLACK
        raw_board[1][0] = Stone.BLACK
        raw_board[2][1] = Stone.BLACK
        raw_board[1][2] = Stone.BLACK
        raw_board[0][2] = Stone.WHITE
        raw_board[1][3] = Stone.WHITE
        raw_board[2][2] = Stone.WHITE
        absolute_board = tuple(tuple(row) for row in raw_board)
        point_confidences = tuple(tuple(0.99 for _ in range(9)) for _ in range(9))
        empty_evidence = PointEvidence(0.0, 0.0, 0.0, 0.0, 0.0, empty=1.0)
        evidences = tuple(tuple(empty_evidence for _ in range(9)) for _ in range(9))
        tracker._evidence_grid = lambda _image, _features: (evidences, True)
        tracker._absolute_position_from_evidence = lambda _evidence, _active: (
            absolute_board,
            0.99,
            0,
            point_confidences,
        )

        result = tracker.analyze(warped)

        self.assertEqual(result.expected_color, Stone.WHITE)
        self.assertEqual(result.absolute_board[1][1], Stone.EMPTY)
        self.assertEqual(result.absolute_board[1][2], Stone.BLACK)

    def test_opposite_color_retry_analyzes_the_opposite_expected_color(self) -> None:
        empty = synthetic_board(19)
        current = synthetic_board(
            19,
            stones=((3, 3, Stone.WHITE),),
            marker=(3, 3),
        )
        tracker = AdaptiveBoardTracker(empty, 19)
        first_analysis = tracker.analyze(current, track_stability=False)
        self.assertTrue(first_analysis.color_mismatch_likely)
        expected_colors = []

        def fail_opposite_retry(*args, **kwargs):
            expected_colors.append(kwargs.get("expected_color"))
            if len(expected_colors) == 1:
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
        service.emit = lambda *_args, **_kwargs: None

        service.scan_once()

        self.assertEqual(expected_colors, [None, Stone.WHITE])

    def test_commit_rejects_wrong_color_without_mutating_history(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 9)
        original_move_history = list(tracker.move_history)
        original_board_history = list(tracker.board_history)
        original_next_color_history = list(tracker.next_color_history)
        original_move_count = tracker.move_count
        original_next_color = tracker.next_color

        with self.assertRaisesRegex(ValueError, "落子颜色与当前行棋方不一致"):
            tracker.commit(Move(3, 3, Stone.WHITE, 9))

        self.assertEqual(tracker.move_history, original_move_history)
        self.assertEqual(tracker.board_history, original_board_history)
        self.assertEqual(tracker.next_color_history, original_next_color_history)
        self.assertEqual(tracker.move_count, original_move_count)
        self.assertEqual(tracker.next_color, original_next_color)

    def test_reconcile_snapshot_rejects_historical_position(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 9)
        tracker.commit(Move(0, 0, Stone.BLACK, 9))
        tracker.commit(Move(1, 1, Stone.WHITE, 9))
        historical = tracker.board
        tracker.commit(Move(2, 2, Stone.BLACK, 9))
        original_move_history = list(tracker.move_history)
        original_board_history = list(tracker.board_history)
        original_next_color_history = list(tracker.next_color_history)
        original_move_count = tracker.move_count
        original_next_color = tracker.next_color

        with self.assertRaisesRegex(ValueError, "重复局面"):
            tracker.reconcile_snapshot(
                historical,
                Move(1, 1, Stone.WHITE, 9),
            )

        self.assertEqual(tracker.move_history, original_move_history)
        self.assertEqual(tracker.board_history, original_board_history)
        self.assertEqual(tracker.next_color_history, original_next_color_history)
        self.assertEqual(tracker.move_count, original_move_count)
        self.assertEqual(tracker.next_color, original_next_color)
        self.assertNotEqual(tracker.board, historical)

    def test_reconcile_snapshot_appends_marker_position_and_remains_undoable(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 9)
        before = tracker.board
        candidate_rows = [list(row) for row in before]
        candidate_rows[3][3] = Stone.BLACK
        candidate_rows[5][5] = Stone.WHITE
        candidate = tuple(tuple(row) for row in candidate_rows)
        last_move = Move(5, 5, Stone.WHITE, 9)

        reconciled = tracker.reconcile_snapshot(candidate, last_move)

        self.assertEqual(reconciled, last_move)
        self.assertEqual(tracker.board_history, [before, candidate])
        self.assertEqual(tracker.move_history, [last_move])
        self.assertEqual(tracker.next_color_history, [Stone.BLACK, Stone.BLACK])
        self.assertEqual(tracker.move_count, 1)
        self.assertEqual(tracker.next_color, Stone.BLACK)
        self.assertEqual(tracker.undo(), last_move)
        self.assertEqual(tracker.board, before)
        self.assertEqual(tracker.board_history, [before])
        self.assertEqual(tracker.move_history, [])
        self.assertEqual(tracker.next_color_history, [Stone.BLACK])
        self.assertEqual(tracker.move_count, 0)
        self.assertEqual(tracker.next_color, Stone.BLACK)

    def test_reconcile_snapshot_routes_single_move_through_commit(self) -> None:
        tracker = AdaptiveBoardTracker(synthetic_board(), 9)
        candidate_rows = [list(row) for row in tracker.board]
        candidate_rows[3][3] = Stone.BLACK
        candidate = tuple(tuple(row) for row in candidate_rows)

        move = tracker.reconcile_snapshot(candidate, None)

        self.assertEqual(move, Move(3, 3, Stone.BLACK, 9))
        self.assertEqual(tracker.board, candidate)
        self.assertEqual(tracker.move_history, [move])
        self.assertEqual(tracker.next_color, Stone.WHITE)


if __name__ == "__main__":
    unittest.main()
