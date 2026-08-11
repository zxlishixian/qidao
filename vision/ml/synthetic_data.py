from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


CLASS_NAMES = ("empty", "black", "white", "unknown")
BOARD_SIZES = (9, 13, 19)
IMAGE_SIZE = 640
PATCH_SIZE = 48


@dataclass(frozen=True)
class Theme:
    board: tuple[int, int, int]
    line: tuple[int, int, int]
    texture: float


THEMES = {
    "train": (
        Theme((202, 160, 96), (42, 35, 28), 11.0),
        Theme((225, 184, 119), (48, 42, 35), 8.0),
        Theme((174, 128, 73), (28, 31, 31), 13.0),
        Theme((191, 170, 126), (46, 48, 48), 6.0),
        Theme((141, 161, 119), (35, 45, 38), 8.0),
    ),
    "val": (
        Theme((214, 148, 82), (35, 33, 32), 15.0),
        Theme((167, 171, 165), (35, 38, 42), 5.0),
    ),
    "test": (
        Theme((230, 195, 139), (60, 47, 36), 12.0),
        Theme((118, 139, 150), (27, 31, 35), 9.0),
    ),
}


def _textured_background(
    height: int,
    width: int,
    theme: Theme,
    rng: np.random.Generator,
) -> np.ndarray:
    yy, xx = np.mgrid[:height, :width]
    base = np.empty((height, width, 3), dtype=np.float32)
    phase = rng.uniform(0, math.tau)
    grain = (
        np.sin(xx / rng.uniform(15.0, 42.0) + phase) * theme.texture
        + np.sin(xx / rng.uniform(65.0, 150.0) + phase * 0.7) * theme.texture * 0.65
        + np.sin(yy / rng.uniform(90.0, 230.0)) * theme.texture * 0.25
    )
    noise = rng.normal(0.0, max(1.0, theme.texture * 0.17), (height, width))
    light = grain + noise + (xx / max(1, width) - 0.5) * rng.uniform(-10.0, 10.0)
    for channel, value in enumerate(theme.board):
        base[..., channel] = value + light * (0.76 + channel * 0.12)
    return np.clip(base, 0, 255).astype(np.uint8)


def _star_indices(size: int) -> tuple[int, ...]:
    if size == 19:
        return 3, 9, 15
    if size == 13:
        return 3, 6, 9
    return 2, 4, 6


def _draw_stone(
    image: np.ndarray,
    center: tuple[int, int],
    radius: int,
    color: int,
    rng: np.random.Generator,
) -> None:
    cx, cy = center
    shadow = max(1, round(radius * 0.13))
    cv2.circle(image, (cx + shadow, cy + shadow), radius, (30, 30, 30), -1, cv2.LINE_AA)
    if color == 1:
        outer = rng.integers(4, 22)
        inner = rng.integers(19, 48)
        cv2.circle(image, center, radius, (int(outer),) * 3, -1, cv2.LINE_AA)
        cv2.circle(
            image,
            (cx - radius // 4, cy - radius // 4),
            max(2, round(radius * 0.56)),
            (int(inner),) * 3,
            -1,
            cv2.LINE_AA,
        )
        if rng.random() < 0.65:
            highlight = int(rng.integers(70, 135))
            cv2.circle(
                image,
                (cx - radius // 3, cy - radius // 3),
                max(1, radius // 8),
                (highlight, highlight, highlight),
                -1,
                cv2.LINE_AA,
            )
    else:
        outer = int(rng.integers(180, 223))
        inner = int(rng.integers(232, 255))
        cv2.circle(image, center, radius, (outer, outer, outer), -1, cv2.LINE_AA)
        cv2.circle(
            image,
            (cx - radius // 5, cy - radius // 5),
            max(2, round(radius * 0.67)),
            (inner, inner, inner),
            -1,
            cv2.LINE_AA,
        )
        cv2.circle(image, center, radius, (70, 70, 70), 1, cv2.LINE_AA)


def _draw_marker(
    image: np.ndarray,
    center: tuple[int, int],
    radius: int,
    rng: np.random.Generator,
) -> None:
    choice = int(rng.integers(0, 4))
    marker_color = ((25, 35, 235), (230, 115, 20), (35, 205, 70), (245, 245, 245))[choice]
    if choice == 3:
        cv2.circle(image, center, max(2, radius // 5), marker_color, 2, cv2.LINE_AA)
    elif rng.random() < 0.55:
        cv2.circle(image, center, max(2, radius // 5), marker_color, -1, cv2.LINE_AA)
    else:
        cv2.drawMarker(
            image,
            center,
            marker_color,
            cv2.MARKER_TRIANGLE_UP,
            max(5, radius // 2),
            2,
            cv2.LINE_AA,
        )


def render_board(
    split: str,
    rng: np.random.Generator,
    canvas: int = 520,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    theme = THEMES[split][int(rng.integers(0, len(THEMES[split])))]
    size = int(rng.choice(BOARD_SIZES))
    margin = int(rng.integers(34, 60))
    extent = canvas - 2 * margin
    spacing = extent / (size - 1)
    image = _textured_background(canvas, canvas, theme, rng)
    line = tuple(int(value) for value in theme.line)
    thickness = 1 if spacing < 32 else int(rng.integers(1, 3))
    positions = [round(margin + index * spacing) for index in range(size)]
    for point in positions:
        cv2.line(image, (positions[0], point), (positions[-1], point), line, thickness, cv2.LINE_AA)
        cv2.line(image, (point, positions[0]), (point, positions[-1]), line, thickness, cv2.LINE_AA)
    star_radius = max(2, round(spacing * 0.075))
    for y in _star_indices(size):
        for x in _star_indices(size):
            cv2.circle(image, (positions[x], positions[y]), star_radius, line, -1, cv2.LINE_AA)

    occupancy = rng.uniform(0.0, 0.42)
    coordinates = [(x, y) for y in range(size) for x in range(size)]
    rng.shuffle(coordinates)
    count = min(len(coordinates), round(len(coordinates) * occupancy))
    radius = max(5, round(spacing * rng.uniform(0.40, 0.48)))
    stones: list[tuple[int, int, int]] = []
    for index, (x, y) in enumerate(coordinates[:count]):
        color = 1 if index % 2 == 0 else 2
        if rng.random() < 0.08:
            color = 3 - color
        _draw_stone(image, (positions[x], positions[y]), radius, color, rng)
        stones.append((x, y, color))
    if stones and rng.random() < 0.82:
        x, y, _ = stones[int(rng.integers(0, len(stones)))]
        _draw_marker(image, (positions[x], positions[y]), radius, rng)
    if rng.random() < 0.35:
        # UI candidate overlays on empty points are deliberately common.
        occupied = {(x, y) for x, y, _ in stones}
        empties = [(x, y) for x, y in coordinates if (x, y) not in occupied]
        for x, y in empties[: int(rng.integers(1, 5))]:
            color = tuple(int(value) for value in rng.choice(((35, 175, 245), (70, 210, 80), (230, 120, 25))))
            cv2.circle(image, (positions[x], positions[y]), max(3, radius - 1), color, 2, cv2.LINE_AA)
    return image, (positions[0], positions[0], positions[-1], positions[-1])


def _desktop_background(rng: np.random.Generator) -> np.ndarray:
    base_value = int(rng.integers(22, 232))
    background = np.full((IMAGE_SIZE, IMAGE_SIZE, 3), base_value, dtype=np.uint8)
    tint = rng.integers(-35, 36, 3)
    background = np.clip(background.astype(np.int16) + tint, 0, 255).astype(np.uint8)
    for _ in range(int(rng.integers(5, 16))):
        x0 = int(rng.integers(0, IMAGE_SIZE - 20))
        y0 = int(rng.integers(0, IMAGE_SIZE - 12))
        x1 = min(IMAGE_SIZE - 1, x0 + int(rng.integers(40, 280)))
        y1 = min(IMAGE_SIZE - 1, y0 + int(rng.integers(8, 70)))
        delta = int(rng.integers(-45, 46))
        color = tuple(int(np.clip(base_value + delta + value, 0, 255)) for value in tint)
        cv2.rectangle(background, (x0, y0), (x1, y1), color, -1)
    return background


def render_detector_sample(
    split: str,
    rng: np.random.Generator,
) -> tuple[np.ndarray, tuple[float, float, float, float] | None]:
    desktop = _desktop_background(rng)
    # Blank/covered selections teach objectness to fall below threshold when
    # the board window disappears instead of returning a confident random box.
    if rng.random() < 0.12:
        return desktop, None
    board, grid = render_board(split, rng)
    target_size = float(rng.uniform(350.0, 580.0))
    aspect = float(rng.uniform(0.91, 1.09))
    width = target_size * aspect
    height = target_size / aspect
    cx = float(rng.uniform(width * 0.50, IMAGE_SIZE - width * 0.50))
    cy = float(rng.uniform(height * 0.50, IMAGE_SIZE - height * 0.50))
    jitter = min(width, height) * rng.uniform(0.0, 0.045)
    destination = np.float32(
        [
            [cx - width / 2 + rng.uniform(-jitter, jitter), cy - height / 2 + rng.uniform(-jitter, jitter)],
            [cx + width / 2 + rng.uniform(-jitter, jitter), cy - height / 2 + rng.uniform(-jitter, jitter)],
            [cx + width / 2 + rng.uniform(-jitter, jitter), cy + height / 2 + rng.uniform(-jitter, jitter)],
            [cx - width / 2 + rng.uniform(-jitter, jitter), cy + height / 2 + rng.uniform(-jitter, jitter)],
        ]
    )
    source = np.float32([[0, 0], [board.shape[1] - 1, 0], [board.shape[1] - 1, board.shape[0] - 1], [0, board.shape[0] - 1]])
    transform = cv2.getPerspectiveTransform(source, destination)
    warped = cv2.warpPerspective(board, transform, (IMAGE_SIZE, IMAGE_SIZE), flags=cv2.INTER_LINEAR)
    mask = cv2.warpPerspective(
        np.full(board.shape[:2], 255, dtype=np.uint8),
        transform,
        (IMAGE_SIZE, IMAGE_SIZE),
        flags=cv2.INTER_LINEAR,
    )
    alpha = mask.astype(np.float32)[..., None] / 255.0
    image = np.clip(warped * alpha + desktop * (1.0 - alpha), 0, 255).astype(np.uint8)

    x0, y0, x1, y1 = grid
    grid_points = np.float32([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]])
    transformed = cv2.perspectiveTransform(grid_points, transform)[0]
    left, top = np.min(transformed, axis=0)
    right, bottom = np.max(transformed, axis=0)
    box = (
        float(np.clip((left + right) / (2 * IMAGE_SIZE), 0.0, 1.0)),
        float(np.clip((top + bottom) / (2 * IMAGE_SIZE), 0.0, 1.0)),
        float(np.clip((right - left) / IMAGE_SIZE, 0.0, 1.0)),
        float(np.clip((bottom - top) / IMAGE_SIZE, 0.0, 1.0)),
    )
    if rng.random() < 0.35:
        kernel = int(rng.choice((3, 5)))
        image = cv2.GaussianBlur(image, (kernel, kernel), rng.uniform(0.2, 1.1))
    if rng.random() < 0.28:
        quality = int(rng.integers(55, 92))
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok:
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    return image, box


def render_intersection_patch(label: int, split: str, rng: np.random.Generator) -> np.ndarray:
    theme = THEMES[split][int(rng.integers(0, len(THEMES[split])))]
    source_size = int(rng.integers(58, 82))
    image = _textured_background(source_size, source_size, theme, rng)
    center = (source_size // 2 + int(rng.integers(-2, 3)), source_size // 2 + int(rng.integers(-2, 3)))
    spacing = float(rng.uniform(source_size * 0.62, source_size * 0.91))
    line = tuple(int(value) for value in theme.line)
    thickness = int(rng.integers(1, 3))
    grid_kind = int(rng.integers(0, 9))
    if grid_kind not in (1, 3):
        cv2.line(image, (0, center[1]), (source_size - 1, center[1]), line, thickness, cv2.LINE_AA)
    else:
        direction = 1 if grid_kind == 1 else -1
        cv2.line(image, center, (center[0] + direction * source_size, center[1]), line, thickness, cv2.LINE_AA)
    if grid_kind not in (2, 3):
        cv2.line(image, (center[0], 0), (center[0], source_size - 1), line, thickness, cv2.LINE_AA)
    else:
        direction = 1 if grid_kind == 2 else -1
        cv2.line(image, center, (center[0], center[1] + direction * source_size), line, thickness, cv2.LINE_AA)
    radius = max(9, round(spacing * rng.uniform(0.40, 0.49)))
    if label in (1, 2):
        _draw_stone(image, center, radius, label, rng)
        if rng.random() < 0.34:
            _draw_marker(image, center, radius, rng)
    elif label == 0 and rng.random() < 0.18:
        cv2.circle(image, center, max(2, round(spacing * 0.07)), line, -1, cv2.LINE_AA)
    elif label == 3:
        overlay = image.copy()
        mode = int(rng.integers(0, 5))
        colors = ((30, 75, 240), (245, 155, 30), (40, 210, 75), (190, 55, 190))
        color = colors[int(rng.integers(0, len(colors)))]
        if mode == 0:
            cv2.circle(overlay, center, radius, color, -1, cv2.LINE_AA)
        elif mode == 1:
            cv2.circle(overlay, center, radius, color, max(3, radius // 4), cv2.LINE_AA)
        elif mode == 2:
            cv2.rectangle(overlay, (center[0] - radius, center[1] - radius // 2), (center[0] + radius, center[1] + radius // 2), color, -1)
        elif mode == 3:
            points = np.array(
                [[center[0] - radius, center[1] - radius], [center[0] - radius, center[1] + radius], [center[0] + radius // 2, center[1] + radius // 3]],
                dtype=np.int32,
            )
            cv2.fillPoly(overlay, [points], color, cv2.LINE_AA)
        else:
            cv2.putText(overlay, str(int(rng.integers(1, 99))), (center[0] - radius, center[1] + radius // 3), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        alpha = float(rng.uniform(0.55, 1.0))
        image = cv2.addWeighted(overlay, alpha, image, 1.0 - alpha, 0)

    angle = float(rng.uniform(-4.0, 4.0))
    scale = float(rng.uniform(0.94, 1.07))
    transform = cv2.getRotationMatrix2D(center, angle, scale)
    image = cv2.warpAffine(image, transform, (source_size, source_size), borderMode=cv2.BORDER_REFLECT_101)
    if rng.random() < 0.30:
        image = cv2.GaussianBlur(image, (3, 3), rng.uniform(0.25, 0.9))
    image = cv2.resize(image, (PATCH_SIZE, PATCH_SIZE), interpolation=cv2.INTER_AREA)
    gain = float(rng.uniform(0.78, 1.22))
    offset = float(rng.uniform(-18.0, 18.0))
    return np.clip(image.astype(np.float32) * gain + offset, 0, 255).astype(np.uint8)


def generate_detector(root: Path, split: str, count: int, seed: int) -> None:
    image_dir = root / "detector" / "images" / split
    label_dir = root / "detector" / "labels" / split
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for index in range(count):
        image, box = render_detector_sample(split, rng)
        stem = f"{split}-{index:05d}"
        cv2.imwrite(str(image_dir / f"{stem}.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 92])
        label_text = "" if box is None else "0 " + " ".join(f"{value:.8f}" for value in box) + "\n"
        (label_dir / f"{stem}.txt").write_text(label_text, encoding="utf-8")


def generate_intersections(root: Path, split: str, count: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    labels = np.arange(count, dtype=np.int64) % len(CLASS_NAMES)
    rng.shuffle(labels)
    images = np.empty((count, PATCH_SIZE, PATCH_SIZE, 3), dtype=np.uint8)
    for index, label in enumerate(labels):
        images[index] = cv2.cvtColor(
            render_intersection_patch(int(label), split, rng),
            cv2.COLOR_BGR2RGB,
        )
    target = root / "intersections"
    target.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(target / f"{split}.npz", images=images, labels=labels)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate reproducible synthetic screen-Go training data")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--detector-train", type=int, default=1200)
    parser.add_argument("--detector-val", type=int, default=240)
    parser.add_argument("--detector-test", type=int, default=240)
    parser.add_argument("--intersection-train", type=int, default=12000)
    parser.add_argument("--intersection-val", type=int, default=2400)
    parser.add_argument("--intersection-test", type=int, default=2400)
    parser.add_argument("--seed", type=int, default=20260805)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    counts = {
        "train": (args.detector_train, args.intersection_train),
        "val": (args.detector_val, args.intersection_val),
        "test": (args.detector_test, args.intersection_test),
    }
    for split_index, (split, (detector_count, intersection_count)) in enumerate(counts.items()):
        split_seed = args.seed + split_index * 100_000
        generate_detector(args.output, split, detector_count, split_seed)
        generate_intersections(args.output, split, intersection_count, split_seed + 50_000)
    manifest = {
        "schema": 1,
        "seed": args.seed,
        "detector": {split: values[0] for split, values in counts.items()},
        "intersections": {split: values[1] for split, values in counts.items()},
        "intersectionClasses": list(CLASS_NAMES),
        "boardSizes": list(BOARD_SIZES),
        "note": "Validation and test palettes are disjoint from training palettes.",
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
