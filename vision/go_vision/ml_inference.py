from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


MODEL_ROOT = Path(__file__).resolve().parents[1] / "models"


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-values))


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=1, keepdims=True)
    exponential = np.exp(np.clip(shifted, -30.0, 30.0))
    return exponential / np.maximum(1e-8, np.sum(exponential, axis=1, keepdims=True))


@dataclass(frozen=True)
class BoardDetection:
    confidence: float
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


class BoardLocator:
    """OpenCV-DNN inference for QiDao's single-board YOLO-style ONNX model."""

    input_size = 320

    def __init__(self, path: Path):
        self.path = path
        self.net = cv2.dnn.readNetFromONNX(str(path))

    @classmethod
    def load_default(cls) -> "BoardLocator | None":
        path = MODEL_ROOT / "board_locator.onnx"
        if not path.is_file():
            return None
        try:
            return cls(path)
        except cv2.error:
            return None

    def detect(self, image: np.ndarray) -> BoardDetection | None:
        if image is None or image.size == 0:
            return None
        blob = cv2.dnn.blobFromImage(
            image,
            scalefactor=1.0 / 255.0,
            size=(self.input_size, self.input_size),
            swapRB=True,
            crop=False,
        )
        self.net.setInput(blob)
        output = np.asarray(self.net.forward())
        if output.ndim != 4 or output.shape[0] != 1 or output.shape[1] != 5:
            return None
        objectness = _sigmoid(output[0, 0])
        flat_index = int(np.argmax(objectness))
        grid_y, grid_x = np.unravel_index(flat_index, objectness.shape)
        confidence = float(objectness[grid_y, grid_x])
        if confidence < 0.45:
            return None
        encoded = _sigmoid(output[0, 1:5, grid_y, grid_x])
        grid_height, grid_width = objectness.shape
        center_x = (grid_x + float(encoded[0])) / grid_width
        center_y = (grid_y + float(encoded[1])) / grid_height
        width = float(encoded[2])
        height = float(encoded[3])
        pixel_height, pixel_width = image.shape[:2]
        left = round((center_x - width / 2) * pixel_width)
        top = round((center_y - height / 2) * pixel_height)
        right = round((center_x + width / 2) * pixel_width)
        bottom = round((center_y + height / 2) * pixel_height)
        left = max(0, min(pixel_width - 1, left))
        top = max(0, min(pixel_height - 1, top))
        right = max(left + 1, min(pixel_width, right))
        bottom = max(top + 1, min(pixel_height, bottom))
        if min(right - left, bottom - top) < 90:
            return None
        return BoardDetection(confidence, left, top, right, bottom)


class IntersectionONNXClassifier:
    """Batched empty/black/white/unknown classification for all grid points."""

    input_size = 48
    classes = ("empty", "black", "white", "unknown")

    def __init__(self, path: Path):
        self.path = path
        self.net = cv2.dnn.readNetFromONNX(str(path))

    @classmethod
    def load_default(cls) -> "IntersectionONNXClassifier | None":
        path = MODEL_ROOT / "intersection_classifier.onnx"
        if not path.is_file():
            return None
        try:
            return cls(path)
        except cv2.error:
            return None

    @staticmethod
    def _patch(padded: np.ndarray, cx: int, cy: int, radius: int) -> np.ndarray:
        px, py = cx + radius, cy + radius
        patch = padded[py - radius : py + radius + 1, px - radius : px + radius + 1]
        return cv2.resize(patch, (IntersectionONNXClassifier.input_size, IntersectionONNXClassifier.input_size), interpolation=cv2.INTER_AREA)

    def classify(
        self,
        image: np.ndarray,
        intersections: tuple[tuple[tuple[int, int], ...], ...],
        spacing: int,
    ) -> np.ndarray | None:
        radius = max(8, round(spacing * 0.62))
        # Padding the whole board 361 times used to cost more than the ONNX
        # network itself. Build it once, then slice every intersection patch.
        padded = cv2.copyMakeBorder(
            image,
            radius,
            radius,
            radius,
            radius,
            cv2.BORDER_REFLECT_101,
        )
        patches = [
            self._patch(padded, cx, cy, radius)
            for row in intersections
            for cx, cy in row
        ]
        if not patches:
            return None
        # BGR→RGB is a channel view; invoking cvtColor 361 times adds avoidable
        # Python/OpenCV call overhead on every live frame.
        rgb = np.stack(patches)[..., ::-1].astype(np.float32) / 255.0
        mean = np.mean(rgb, axis=(1, 2), keepdims=True)
        standard_deviation = np.maximum(np.std(rgb, axis=(1, 2), keepdims=True), 0.08)
        normalized = (rgb - mean) / standard_deviation
        blob = np.ascontiguousarray(normalized.transpose(0, 3, 1, 2), dtype=np.float32)
        self.net.setInput(blob)
        logits = np.asarray(self.net.forward())
        if logits.ndim > 2:
            logits = logits.reshape(logits.shape[0], -1)
        if logits.shape != (len(patches), len(self.classes)):
            return None
        probabilities = _softmax(logits.astype(np.float32))
        size = len(intersections)
        return probabilities.reshape(size, size, len(self.classes))


def model_metadata() -> dict:
    path = MODEL_ROOT / "vision_models.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
