from __future__ import annotations

import torch
from torch import nn


class ConvBlock(nn.Sequential):
    def __init__(self, in_channels: int, out_channels: int, stride: int = 1):
        super().__init__(
            nn.Conv2d(
                in_channels,
                out_channels,
                kernel_size=3,
                stride=stride,
                padding=1,
                bias=False,
            ),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )


class TinyYoloBoardLocator(nn.Module):
    """A small, single-class YOLO-style detector for one selected Go board.

    The output is ``[N, 5, 10, 10]``.  Channel zero is objectness.  The other
    channels encode centre offsets inside the winning grid cell and width /
    height relative to the input image.  A selected screen region is expected
    to contain at most one primary board, so anchors and NMS are unnecessary.
    """

    input_size = 320
    grid_size = 10

    def __init__(self):
        super().__init__()
        self.backbone = nn.Sequential(
            ConvBlock(3, 16, 2),
            ConvBlock(16, 24, 2),
            ConvBlock(24, 40, 2),
            ConvBlock(40, 72, 2),
            ConvBlock(72, 112, 2),
            ConvBlock(112, 128),
        )
        self.head = nn.Sequential(
            nn.Conv2d(128, 80, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(80),
            nn.ReLU(inplace=True),
            nn.Conv2d(80, 5, kernel_size=1),
        )

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.head(self.backbone(image))


class IntersectionClassifier(nn.Module):
    """Classify a normalized grid intersection as empty/black/white/unknown."""

    input_size = 48
    class_names = ("empty", "black", "white", "unknown")

    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            ConvBlock(3, 16),
            nn.MaxPool2d(2),
            ConvBlock(16, 28),
            nn.MaxPool2d(2),
            ConvBlock(28, 48),
            nn.MaxPool2d(2),
            ConvBlock(48, 64),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64 * 6 * 6, 96),
            nn.ReLU(inplace=True),
            nn.Dropout(0.12),
            nn.Linear(96, len(self.class_names)),
        )

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.classifier(self.features(image))
