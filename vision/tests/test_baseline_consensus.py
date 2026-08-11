from __future__ import annotations

import unittest

from go_vision.adaptive_vision import PositionRecognition
from go_vision.model import Move, Stone, empty_board
from vision_service import consensus_position


class BaselineConsensusTests(unittest.TestCase):
    @staticmethod
    def scored_recognition(
        size: int,
        point: tuple[int, int],
        scores: tuple[float, float, float],
    ) -> PositionRecognition:
        board = [list(row) for row in empty_board(size)]
        x, y = point
        board[y][x] = Stone.UNKNOWN
        point_scores = [
            [(1.0, 0.0, 0.0) for _ in range(size)]
            for _ in range(size)
        ]
        point_scores[y][x] = scores
        return PositionRecognition(
            tuple(tuple(row) for row in board),
            0.82,
            1,
            Stone.WHITE,
            None,
            tuple(tuple(row) for row in point_scores),
        )

    def test_independent_point_noise_does_not_require_identical_frames(self) -> None:
        size = 9
        stable = [list(row) for row in empty_board(size)]
        stable[2][3] = Stone.BLACK
        stable[6][5] = Stone.WHITE

        recognitions: list[PositionRecognition] = []
        for frame in range(7):
            board = [row.copy() for row in stable]
            # Every frame is globally different, but no noisy point gets a
            # majority across the burst.
            board[(frame + 1) % size][(frame * 2 + 1) % size] = Stone.BLACK
            recognitions.append(
                PositionRecognition(
                    tuple(tuple(row) for row in board),
                    0.90,
                    0,
                    Stone.BLACK,
                    Move(5, 6, Stone.WHITE, size),
                )
            )

        self.assertEqual(len({recognition.board for recognition in recognitions}), 7)
        result = consensus_position(recognitions, size)

        self.assertEqual(result.board, tuple(tuple(row) for row in stable))
        self.assertEqual(result.unknown_points, 0)
        self.assertEqual(result.next_color, Stone.BLACK)
        self.assertEqual(result.last_move, Move(5, 6, Stone.WHITE, size))

    def test_point_without_a_strict_majority_remains_unknown(self) -> None:
        size = 9
        recognitions: list[PositionRecognition] = []
        values = [Stone.EMPTY, Stone.EMPTY, Stone.EMPTY, Stone.BLACK, Stone.BLACK, Stone.BLACK, Stone.UNKNOWN]
        for value in values:
            board = [list(row) for row in empty_board(size)]
            board[4][4] = value
            recognitions.append(
                PositionRecognition(tuple(tuple(row) for row in board), 0.8, 0, Stone.BLACK, None)
            )

        result = consensus_position(recognitions, size)
        self.assertEqual(result.board[4][4], Stone.UNKNOWN)
        self.assertEqual(result.unknown_points, 1)

    def test_probability_fusion_recovers_stone_from_unknown_frames(self) -> None:
        size = 9
        recognitions = [
            self.scored_recognition(size, (4, 4), (0.18, 0.70, 0.12))
            for _ in range(7)
        ]

        result = consensus_position(recognitions, size)

        self.assertEqual(result.board[4][4], Stone.BLACK)
        self.assertEqual(result.unknown_points, 0)

    def test_probability_fusion_removes_unknown_halo_as_empty(self) -> None:
        size = 9
        recognitions = [
            self.scored_recognition(size, (5, 4), (0.58, 0.24, 0.18))
            for _ in range(7)
        ]

        result = consensus_position(recognitions, size)

        self.assertEqual(result.board[4][5], Stone.EMPTY)
        self.assertEqual(result.unknown_points, 0)


if __name__ == "__main__":
    unittest.main()
