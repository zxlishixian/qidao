#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_DIR/.build"

mkdir -p "$BUILD_DIR/module-cache"
xcrun clang \
    -fobjc-arc \
    -fmodules-cache-path="$BUILD_DIR/module-cache" \
    "$PROJECT_DIR/vision/native/ScreenTool.m" \
    -o "$BUILD_DIR/screen-tool" \
    -framework Cocoa \
    -framework ApplicationServices \
    -framework ScreenCaptureKit \
    -framework ImageIO \
    -framework UniformTypeIdentifiers

echo "Vision capture helper built at $BUILD_DIR/screen-tool"
