from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable


BOARD_SIZE = 19
GO_COLUMNS = "ABCDEFGHJKLMNOPQRST"


class Stone(IntEnum):
    EMPTY = 0
    BLACK = 1
    WHITE = 2
    UNKNOWN = 3

    @property
    def label(self) -> str:
        return {
            Stone.EMPTY: "空",
            Stone.BLACK: "黑",
            Stone.WHITE: "白",
            Stone.UNKNOWN: "未知",
        }[self]


def empty_board(size: int = BOARD_SIZE) -> tuple[tuple[Stone, ...], ...]:
    return tuple(tuple(Stone.EMPTY for _ in range(size)) for _ in range(size))


def normalize_board(rows: Iterable[Iterable[int | Stone]], size: int | None = None) -> tuple[tuple[Stone, ...], ...]:
    board = tuple(tuple(Stone(value) for value in row) for row in rows)
    size = size or len(board)
    if size not in (9, 13, 19) or len(board) != size or any(len(row) != size for row in board):
        raise ValueError("棋盘必须是 9×9、13×13 或 19×19")
    return board


def board_to_json(board: tuple[tuple[Stone, ...], ...]) -> list[list[int]]:
    return [[int(value) for value in row] for row in board]


def vertex_name(x: int, y: int, size: int = BOARD_SIZE) -> str:
    """Return a human Go coordinate (top-left array coordinate -> e.g. Q16)."""
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError("坐标超出棋盘")
    return f"{GO_COLUMNS[x]}{size - y}"


@dataclass(frozen=True)
class Move:
    x: int
    y: int
    color: Stone
    board_size: int = BOARD_SIZE
    is_pass: bool = False

    @property
    def vertex(self) -> str:
        if self.is_pass:
            return "PASS"
        return vertex_name(self.x, self.y, self.board_size)

    @classmethod
    def pass_turn(cls, color: Stone, board_size: int = BOARD_SIZE) -> "Move":
        return cls(-1, -1, color, board_size, True)

    def to_json(self) -> dict[str, int | str | bool]:
        return {
            "x": self.x,
            "y": self.y,
            "color": int(self.color),
            "vertex": self.vertex,
            "boardSize": self.board_size,
            "pass": self.is_pass,
        }


@dataclass(frozen=True)
class BoardTransition:
    added: tuple[Move, ...] = ()
    removed: tuple[Move, ...] = ()
    changed: tuple[tuple[int, int], ...] = ()

    @property
    def is_single_move(self) -> bool:
        if len(self.added) != 1:
            return False
        move = self.added[0]
        return all(removed.color != move.color for removed in self.removed)

    @property
    def move(self) -> Move | None:
        return self.added[0] if self.is_single_move else None


def diff_boards(
    before: tuple[tuple[Stone, ...], ...],
    after: tuple[tuple[Stone, ...], ...],
) -> BoardTransition:
    added: list[Move] = []
    removed: list[Move] = []
    changed: list[tuple[int, int]] = []
    size = len(before)
    for y in range(size):
        for x in range(size):
            old, new = before[y][x], after[y][x]
            if old == new or new == Stone.UNKNOWN:
                continue
            changed.append((x, y))
            if old in (Stone.BLACK, Stone.WHITE):
                removed.append(Move(x, y, old, size))
            if new in (Stone.BLACK, Stone.WHITE):
                added.append(Move(x, y, new, size))
    return BoardTransition(tuple(added), tuple(removed), tuple(changed))


def merge_unknown(
    previous: tuple[tuple[Stone, ...], ...],
    current: tuple[tuple[Stone, ...], ...],
) -> tuple[tuple[Stone, ...], ...]:
    return tuple(
        tuple(previous[y][x] if current[y][x] == Stone.UNKNOWN else current[y][x] for x in range(len(previous)))
        for y in range(len(previous))
    )


def _neighbors(x: int, y: int, size: int = BOARD_SIZE):
    for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
        if 0 <= nx < size and 0 <= ny < size:
            yield nx, ny


def _group_and_liberties(board: list[list[Stone]], x: int, y: int) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    color = board[y][x]
    group = {(x, y)}
    liberties: set[tuple[int, int]] = set()
    stack = [(x, y)]
    while stack:
        px, py = stack.pop()
        for nx, ny in _neighbors(px, py, len(board)):
            value = board[ny][nx]
            if value == Stone.EMPTY:
                liberties.add((nx, ny))
            elif value == color and (nx, ny) not in group:
                group.add((nx, ny))
                stack.append((nx, ny))
    return group, liberties


def normalize_snapshot_captures(
    board: tuple[tuple[Stone, ...], ...],
    last_mover: Stone | None = None,
) -> tuple[tuple[Stone, ...], ...]:
    """Remove zero-liberty groups from a complete visual snapshot.

    A screen classifier can keep a captured stone for several frames because
    of a fade animation, shadow, or a confident historical false positive.
    Such a board is impossible under the standard Go rules used by QiDao. If
    the mover is known, captured opponent groups are removed first so a legal
    capturing group is not mistaken for suicide before its new liberties are
    exposed. Any remaining zero-liberty groups are stale snapshot artifacts.
    """
    normalized = normalize_board(board)
    if any(value == Stone.UNKNOWN for row in normalized for value in row):
        return normalized
    mutable = [list(row) for row in normalized]

    def dead_groups(color: Stone | None = None) -> list[set[tuple[int, int]]]:
        seen: set[tuple[int, int]] = set()
        dead: list[set[tuple[int, int]]] = []
        for y in range(len(mutable)):
            for x in range(len(mutable)):
                value = mutable[y][x]
                if value not in (Stone.BLACK, Stone.WHITE) or (x, y) in seen:
                    continue
                group, liberties = _group_and_liberties(mutable, x, y)
                seen.update(group)
                if not liberties and (color is None or value == color):
                    dead.append(group)
        return dead

    def remove(groups: list[set[tuple[int, int]]]) -> None:
        for group in groups:
            for x, y in group:
                mutable[y][x] = Stone.EMPTY

    if last_mover in (Stone.BLACK, Stone.WHITE):
        opponent = Stone.WHITE if last_mover == Stone.BLACK else Stone.BLACK
        remove(dead_groups(opponent))

    # Re-evaluate after the opponent is removed. This preserves a capturing
    # group whose only liberties are the points just vacated by that capture.
    while remaining := dead_groups():
        remove(remaining)
    return normalize_board(mutable, len(mutable))


def play_move(
    board: tuple[tuple[Stone, ...], ...], move: Move
) -> tuple[tuple[Stone, ...], ...]:
    """Apply a normal move and captures. Full ko history is tracked by the engine layer."""
    if move.color not in (Stone.BLACK, Stone.WHITE):
        raise ValueError("只能落黑棋或白棋")
    if move.is_pass:
        return board
    if not (0 <= move.x < len(board) and 0 <= move.y < len(board)):
        raise ValueError("坐标超出棋盘")
    if board[move.y][move.x] != Stone.EMPTY:
        raise ValueError(f"{move.vertex} 已经有棋子")
    mutable = [list(row) for row in board]
    size = len(mutable)
    if move.board_size != size:
        move = Move(move.x, move.y, move.color, size)
    mutable[move.y][move.x] = move.color
    opponent = Stone.WHITE if move.color == Stone.BLACK else Stone.BLACK
    for nx, ny in _neighbors(move.x, move.y, size):
        if mutable[ny][nx] != opponent:
            continue
        group, liberties = _group_and_liberties(mutable, nx, ny)
        if not liberties:
            for gx, gy in group:
                mutable[gy][gx] = Stone.EMPTY
    own_group, own_liberties = _group_and_liberties(mutable, move.x, move.y)
    if not own_liberties:
        raise ValueError(f"{move.vertex} 是自杀着")
    return normalize_board(mutable, size)


def legal_move_result(
    board: tuple[tuple[Stone, ...], ...],
    move: Move,
    position_history: Iterable[tuple[tuple[Stone, ...], ...]] = (),
) -> tuple[tuple[Stone, ...], ...]:
    """Apply a move and reject repeated board states (positional superko).

    Screen pixels are observations, never authority.  A newly observed stone
    reaches QiDao only when this rule transition succeeds; captures are
    computed by the state machine rather than inferred as independent visual
    deletions.
    """
    result = play_move(board, move)
    if not move.is_pass and any(result == previous for previous in position_history):
        raise ValueError(f"{move.vertex} 造成重复局面（劫争/全局同形），已拒绝")
    return result
