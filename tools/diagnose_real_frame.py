#!/usr/bin/env python3
"""Exercise live-move detection against a real client screenshot.

The tool locates and normalizes a board inside a caller-supplied crop, then
copies one already-rendered stone to an empty intersection.  This keeps the
client's real theme, shadows, Retina scaling and anti-aliasing while testing
the quick frame gate and the full tracker independently of screen capture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from go_vision.adaptive_vision import AdaptiveBoardTracker
from go_vision.capture import ScreenController, WarpedBoard
from go_vision.model import Stone


def with_copied_stone(
    warped: WarpedBoard,
    source: tuple[int, int],
    destination: tuple[int, int],
) -> WarpedBoard:
    image = warped.image.copy()
    source_center = warped.intersections[source[1]][source[0]]
    destination_center = warped.intersections[destination[1]][destination[0]]
    radius = max(5, round(warped.spacing * 0.47))
    yy, xx = np.ogrid[: image.shape[0], : image.shape[1]]
    mask = (xx - destination_center[0]) ** 2 + (yy - destination_center[1]) ** 2 <= radius**2
    source_y = np.clip(
        np.arange(image.shape[0]) - destination_center[1] + source_center[1],
        0,
        image.shape[0] - 1,
    )
    source_x = np.clip(
        np.arange(image.shape[1]) - destination_center[0] + source_center[0],
        0,
        image.shape[1] - 1,
    )
    copied = image[np.ix_(source_y, source_x)]
    image[mask] = copied[mask]
    return WarpedBoard(image, warped.intersections, warped.spacing, warped.margin)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--crop", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"), required=True)
    parser.add_argument("--size", type=int, default=19)
    args = parser.parse_args()

    screenshot = cv2.imread(str(args.image))
    if screenshot is None:
        raise SystemExit(f"cannot read {args.image}")
    x0, y0, x1, y1 = args.crop
    raw = screenshot[y0:y1, x0:x1]
    bounds = (x0, y0, x1 - x0, y1 - y0)
    quad = ScreenController.detect_grid_quad(raw, bounds, args.size)
    warped = ScreenController.warp_image(raw, bounds, quad, args.size)
    tracker = AdaptiveBoardTracker(warped, args.size, threshold=0.61, stable_required=2)
    recognized = tracker.recognize_position(warped)
    tracker.bootstrap(recognized)

    sources: dict[Stone, tuple[int, int]] = {}
    empties: list[tuple[int, int]] = []
    for y, row in enumerate(recognized.board):
        for x, stone in enumerate(row):
            if stone in (Stone.BLACK, Stone.WHITE):
                sources.setdefault(stone, (x, y))
            elif stone == Stone.EMPTY and 2 <= x < args.size - 2 and 2 <= y < args.size - 2:
                empties.append((x, y))

    print(f"quad={quad.to_json()}")
    print(
        "recognized="
        f"black:{sum(value == Stone.BLACK for row in recognized.board for value in row)} "
        f"white:{sum(value == Stone.WHITE for row in recognized.board for value in row)} "
        f"unknown:{recognized.unknown_points} next:{recognized.next_color.name} "
        f"backend:{tracker.recognition_backend}"
    )
    tracker.can_skip_unchanged_frame(warped)
    print(f"static_skipped={tracker.can_skip_unchanged_frame(warped)}")

    color = tracker.next_color
    source = sources.get(color)
    if source is None or not empties:
        raise SystemExit(f"no rendered {color.name} stone or empty target available")
    destination = empties[len(empties) // 2]
    changed = with_copied_stone(warped, source, destination)
    print(
        f"synthetic_move={color.name}@{destination} source={source} "
        f"quick_gate_skipped={tracker.can_skip_unchanged_frame(changed)}"
    )
    for frame in range(1, 4):
        analysis = tracker.analyze(changed)
        best = None if analysis.best is None else (analysis.best.x, analysis.best.y, analysis.best.color.name)
        accepted = (
            None
            if analysis.accepted is None
            else (analysis.accepted.x, analysis.accepted.y, analysis.accepted.color.name)
        )
        print(
            f"frame={frame} best={best} score={analysis.best_score:.3f} "
            f"accepted={accepted} stable={analysis.stable_frames} "
            f"hover={[(move.x, move.y) for move in analysis.hover_previews]} "
            f"unexpected={analysis.unexpected_stones} unknown={analysis.unknown_points} "
            f"agreement={analysis.board_agreement:.3f}"
        )


if __name__ == "__main__":
    main()
