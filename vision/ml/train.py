from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as functional
from torch import nn
from torch.utils.data import DataLoader, Dataset

from .manifest import write_model_manifest
from .models import IntersectionClassifier, TinyYoloBoardLocator


@dataclass(frozen=True)
class DetectorMetrics:
    mean_iou: float
    recall_iou50: float
    recall_iou75: float
    blank_rejection: float
    samples: int


@dataclass(frozen=True)
class ClassifierMetrics:
    accuracy: float
    macro_f1: float
    per_class_f1: dict[str, float]
    confusion: list[list[int]]
    samples: int


class DetectorDataset(Dataset):
    def __init__(self, root: Path, split: str):
        self.image_dir = root / "detector" / "images" / split
        self.label_dir = root / "detector" / "labels" / split
        self.images = sorted(self.image_dir.glob("*.jpg"))
        if not self.images:
            raise ValueError(f"没有找到定位训练图像：{self.image_dir}")

    def __len__(self) -> int:
        return len(self.images)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        path = self.images[index]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"无法读取训练图像：{path}")
        image = cv2.cvtColor(
            cv2.resize(
                image,
                (TinyYoloBoardLocator.input_size, TinyYoloBoardLocator.input_size),
                interpolation=cv2.INTER_AREA,
            ),
            cv2.COLOR_BGR2RGB,
        )
        image_tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1))).float() / 255.0
        label_path = self.label_dir / f"{path.stem}.txt"
        text = label_path.read_text(encoding="utf-8").strip()
        if not text:
            return image_tensor, torch.zeros(4, dtype=torch.float32), torch.tensor(False)
        fields = text.split()
        if len(fields) != 5:
            raise ValueError(f"YOLO 标签格式错误：{label_path}")
        return image_tensor, torch.tensor([float(value) for value in fields[1:]], dtype=torch.float32), torch.tensor(True)


class ClassifierDataset(Dataset):
    def __init__(self, root: Path, split: str):
        archive = np.load(root / "intersections" / f"{split}.npz")
        self.images = archive["images"]
        self.labels = archive["labels"]

    def __len__(self) -> int:
        return int(self.labels.shape[0])

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        image = self.images[index].astype(np.float32) / 255.0
        mean = np.mean(image, axis=(0, 1), keepdims=True)
        standard_deviation = np.maximum(np.std(image, axis=(0, 1), keepdims=True), 0.08)
        normalized = (image - mean) / standard_deviation
        tensor = torch.from_numpy(np.ascontiguousarray(normalized.transpose(2, 0, 1)))
        return tensor, torch.tensor(int(self.labels[index]), dtype=torch.long)


def choose_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def detector_loss(output: torch.Tensor, boxes: torch.Tensor, present: torch.Tensor) -> torch.Tensor:
    batch, _, grid_h, grid_w = output.shape
    object_target = torch.zeros((batch, grid_h, grid_w), dtype=output.dtype, device=output.device)
    positive_indices = torch.nonzero(present, as_tuple=False).flatten()
    if positive_indices.numel():
        positive_boxes = boxes[positive_indices]
        grid_x = torch.clamp((positive_boxes[:, 0] * grid_w).long(), 0, grid_w - 1)
        grid_y = torch.clamp((positive_boxes[:, 1] * grid_h).long(), 0, grid_h - 1)
        object_target[positive_indices, grid_y, grid_x] = 1.0
    object_bce = functional.binary_cross_entropy_with_logits(output[:, 0], object_target, reduction="none")
    positive_mask = object_target > 0.5
    negative_mask = ~positive_mask
    positive_loss = object_bce[positive_mask].mean() if positive_mask.any() else output.sum() * 0.0
    negative_loss = object_bce[negative_mask].mean()
    object_loss = positive_loss + negative_loss * 0.18
    if not positive_indices.numel():
        return object_loss
    encoded = []
    predictions = []
    for batch_index in positive_indices.tolist():
        cx, cy, width, height = boxes[batch_index]
        gx = min(grid_w - 1, int(float(cx) * grid_w))
        gy = min(grid_h - 1, int(float(cy) * grid_h))
        encoded.append(torch.stack((cx * grid_w - gx, cy * grid_h - gy, width, height)))
        predictions.append(output[batch_index, 1:5, gy, gx])
    target_boxes = torch.stack(encoded)
    predicted_boxes = torch.sigmoid(torch.stack(predictions))
    box_loss = functional.smooth_l1_loss(predicted_boxes, target_boxes, beta=0.04)
    return object_loss + box_loss * 7.0


def decode_detector(output: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    objectness = torch.sigmoid(output[:, 0])
    batch, grid_h, grid_w = objectness.shape
    flat_index = objectness.reshape(batch, -1).argmax(dim=1)
    grid_y = torch.div(flat_index, grid_w, rounding_mode="floor")
    grid_x = flat_index % grid_w
    confidences = objectness[torch.arange(batch, device=output.device), grid_y, grid_x]
    raw_boxes = output[torch.arange(batch, device=output.device), 1:5, grid_y, grid_x]
    encoded = torch.sigmoid(raw_boxes)
    boxes = torch.stack(
        (
            (grid_x + encoded[:, 0]) / grid_w,
            (grid_y + encoded[:, 1]) / grid_h,
            encoded[:, 2],
            encoded[:, 3],
        ),
        dim=1,
    )
    return confidences, boxes


def box_iou(first: torch.Tensor, second: torch.Tensor) -> torch.Tensor:
    first_corners = torch.stack(
        (
            first[:, 0] - first[:, 2] / 2,
            first[:, 1] - first[:, 3] / 2,
            first[:, 0] + first[:, 2] / 2,
            first[:, 1] + first[:, 3] / 2,
        ),
        dim=1,
    )
    second_corners = torch.stack(
        (
            second[:, 0] - second[:, 2] / 2,
            second[:, 1] - second[:, 3] / 2,
            second[:, 0] + second[:, 2] / 2,
            second[:, 1] + second[:, 3] / 2,
        ),
        dim=1,
    )
    top_left = torch.maximum(first_corners[:, :2], second_corners[:, :2])
    bottom_right = torch.minimum(first_corners[:, 2:], second_corners[:, 2:])
    intersection_size = torch.clamp(bottom_right - top_left, min=0)
    intersection = intersection_size[:, 0] * intersection_size[:, 1]
    first_area = first[:, 2] * first[:, 3]
    second_area = second[:, 2] * second[:, 3]
    return intersection / torch.clamp(first_area + second_area - intersection, min=1e-6)


@torch.inference_mode()
def evaluate_detector(model: nn.Module, loader: DataLoader, device: torch.device) -> DetectorMetrics:
    model.eval()
    ious: list[float] = []
    blank_ok = 0
    blank_count = 0
    for images, targets, present in loader:
        images = images.to(device)
        output = model(images)
        confidence, predicted = decode_detector(output)
        present_device = present.to(device).bool()
        if present_device.any():
            values = box_iou(predicted[present_device], targets.to(device)[present_device])
            ious.extend(float(value) for value in values.cpu())
        missing = ~present_device
        if missing.any():
            blank_count += int(missing.sum())
            blank_ok += int((confidence[missing] < 0.45).sum())
    values = np.asarray(ious, dtype=np.float32)
    return DetectorMetrics(
        mean_iou=float(values.mean()) if len(values) else 0.0,
        recall_iou50=float(np.mean(values >= 0.50)) if len(values) else 0.0,
        recall_iou75=float(np.mean(values >= 0.75)) if len(values) else 0.0,
        blank_rejection=blank_ok / blank_count if blank_count else 1.0,
        samples=len(loader.dataset),
    )


@torch.inference_mode()
def evaluate_classifier(model: nn.Module, loader: DataLoader, device: torch.device) -> ClassifierMetrics:
    model.eval()
    count = len(IntersectionClassifier.class_names)
    confusion = np.zeros((count, count), dtype=np.int64)
    for images, targets in loader:
        predicted = model(images.to(device)).argmax(dim=1).cpu().numpy()
        actual = targets.numpy()
        for expected, observed in zip(actual, predicted, strict=True):
            confusion[int(expected), int(observed)] += 1
    f1_values: list[float] = []
    per_class: dict[str, float] = {}
    for index, name in enumerate(IntersectionClassifier.class_names):
        true_positive = confusion[index, index]
        false_positive = confusion[:, index].sum() - true_positive
        false_negative = confusion[index, :].sum() - true_positive
        denominator = 2 * true_positive + false_positive + false_negative
        f1 = float(2 * true_positive / denominator) if denominator else 0.0
        per_class[name] = f1
        f1_values.append(f1)
    total = int(confusion.sum())
    return ClassifierMetrics(
        accuracy=float(np.trace(confusion) / total) if total else 0.0,
        macro_f1=float(np.mean(f1_values)),
        per_class_f1=per_class,
        confusion=confusion.tolist(),
        samples=total,
    )


def train_detector(
    data_root: Path,
    checkpoint_root: Path,
    device: torch.device,
    epochs: int,
    batch_size: int,
) -> tuple[TinyYoloBoardLocator, DetectorMetrics, DetectorMetrics]:
    train_loader = DataLoader(DetectorDataset(data_root, "train"), batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(DetectorDataset(data_root, "val"), batch_size=batch_size, num_workers=0)
    test_loader = DataLoader(DetectorDataset(data_root, "test"), batch_size=batch_size, num_workers=0)
    model = TinyYoloBoardLocator().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=2.2e-3, weight_decay=2e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs), eta_min=1e-5)
    best_score = -math.inf
    best_path = checkpoint_root / "board_locator.pt"
    for epoch in range(1, epochs + 1):
        model.train()
        losses = []
        for images, boxes, present in train_loader:
            optimizer.zero_grad(set_to_none=True)
            output = model(images.to(device))
            loss = detector_loss(output, boxes.to(device), present.to(device).bool())
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        scheduler.step()
        metrics = evaluate_detector(model, val_loader, device)
        score = metrics.mean_iou + metrics.recall_iou75 + metrics.blank_rejection * 0.25
        if score > best_score:
            best_score = score
            torch.save(model.state_dict(), best_path)
        print(
            f"detector epoch {epoch:02d}/{epochs} loss={np.mean(losses):.4f} "
            f"val_iou={metrics.mean_iou:.3f} r75={metrics.recall_iou75:.3f} "
            f"blank={metrics.blank_rejection:.3f}",
            flush=True,
        )
    model.load_state_dict(torch.load(best_path, map_location=device, weights_only=True))
    return model, evaluate_detector(model, val_loader, device), evaluate_detector(model, test_loader, device)


def train_classifier(
    data_root: Path,
    checkpoint_root: Path,
    device: torch.device,
    epochs: int,
    batch_size: int,
) -> tuple[IntersectionClassifier, ClassifierMetrics, ClassifierMetrics]:
    train_loader = DataLoader(ClassifierDataset(data_root, "train"), batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(ClassifierDataset(data_root, "val"), batch_size=batch_size, num_workers=0)
    test_loader = DataLoader(ClassifierDataset(data_root, "test"), batch_size=batch_size, num_workers=0)
    model = IntersectionClassifier().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1.8e-3, weight_decay=3e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs), eta_min=1e-5)
    best_score = -math.inf
    best_path = checkpoint_root / "intersection_classifier.pt"
    for epoch in range(1, epochs + 1):
        model.train()
        losses = []
        for images, targets in train_loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(images.to(device))
            loss = functional.cross_entropy(logits, targets.to(device), label_smoothing=0.025)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        scheduler.step()
        metrics = evaluate_classifier(model, val_loader, device)
        if metrics.macro_f1 > best_score:
            best_score = metrics.macro_f1
            torch.save(model.state_dict(), best_path)
        print(
            f"classifier epoch {epoch:02d}/{epochs} loss={np.mean(losses):.4f} "
            f"val_acc={metrics.accuracy:.4f} macro_f1={metrics.macro_f1:.4f}",
            flush=True,
        )
    model.load_state_dict(torch.load(best_path, map_location=device, weights_only=True))
    return model, evaluate_classifier(model, val_loader, device), evaluate_classifier(model, test_loader, device)


def export_onnx(model: nn.Module, example: torch.Tensor, output: Path, dynamic_batch: bool = False) -> None:
    model = model.cpu().eval()
    dynamic_axes = {"image": {0: "batch"}, "output": {0: "batch"}} if dynamic_batch else None
    torch.onnx.export(
        model,
        example,
        output,
        input_names=["image"],
        output_names=["output"],
        dynamic_axes=dynamic_axes,
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_with_opencv(path: Path, input_shape: tuple[int, ...]) -> list[int]:
    net = cv2.dnn.readNetFromONNX(str(path))
    net.setInput(np.zeros(input_shape, dtype=np.float32))
    return list(net.forward().shape)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train and export QiDao screen-board models")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checkpoints", type=Path, required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--detector-epochs", type=int, default=24)
    parser.add_argument("--classifier-epochs", type=int, default=16)
    parser.add_argument("--detector-batch", type=int, default=24)
    parser.add_argument("--classifier-batch", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20260805)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    args.checkpoints.mkdir(parents=True, exist_ok=True)
    device = choose_device(args.device)
    print(f"training device: {device}", flush=True)
    started = time.time()
    detector, detector_val, detector_test = train_detector(
        args.data,
        args.checkpoints,
        device,
        args.detector_epochs,
        args.detector_batch,
    )
    classifier, classifier_val, classifier_test = train_classifier(
        args.data,
        args.checkpoints,
        device,
        args.classifier_epochs,
        args.classifier_batch,
    )
    detector_path = args.output / "board_locator.onnx"
    classifier_path = args.output / "intersection_classifier.onnx"
    export_onnx(
        detector,
        torch.zeros(1, 3, TinyYoloBoardLocator.input_size, TinyYoloBoardLocator.input_size),
        detector_path,
    )
    export_onnx(
        classifier,
        torch.zeros(1, 3, IntersectionClassifier.input_size, IntersectionClassifier.input_size),
        classifier_path,
        dynamic_batch=True,
    )
    detector_shape = validate_with_opencv(
        detector_path,
        (1, 3, TinyYoloBoardLocator.input_size, TinyYoloBoardLocator.input_size),
    )
    classifier_shape = validate_with_opencv(
        classifier_path,
        (3, 3, IntersectionClassifier.input_size, IntersectionClassifier.input_size),
    )
    manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "schema": 1,
        "architecture": "tiny-yolo-board-locator + onnx-intersection-classifier + go-state-machine",
        "trainingSeconds": round(time.time() - started, 2),
        "device": str(device),
        "dataset": manifest,
        "detector": {
            "file": detector_path.name,
            "sha256": file_sha256(detector_path),
            "input": [1, 3, TinyYoloBoardLocator.input_size, TinyYoloBoardLocator.input_size],
            "output": detector_shape,
            "validation": asdict(detector_val),
            "test": asdict(detector_test),
        },
        "intersectionClassifier": {
            "file": classifier_path.name,
            "sha256": file_sha256(classifier_path),
            "classes": list(IntersectionClassifier.class_names),
            "input": ["batch", 3, IntersectionClassifier.input_size, IntersectionClassifier.input_size],
            "output": classifier_shape,
            "validation": asdict(classifier_val),
            "test": asdict(classifier_test),
        },
        "limitations": [
            "Synthetic held-out metrics do not measure arbitrary third-party Go clients.",
            "Real-screen acceptance is also guarded by grid fitting, multi-frame stability and Go legality.",
        ],
    }
    report = write_model_manifest(args.output / "vision_models.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
