#!/bin/bash

# Script to build Rust core and copy bindings to SwiftUI project

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$PROJECT_DIR/qidao-core"
SWIFT_DIR="$PROJECT_DIR/QiDao/QiDao/Core"

echo "Building Rust core..."
cd "$CORE_DIR"
cargo build --locked --release

echo "Generating bindings..."
cargo run --locked --features=uniffi/cli --bin uniffi-bindgen -- generate --library "$CORE_DIR/target/release/libqidao_core.dylib" --language swift --out-dir "$CORE_DIR/out"

echo "Copying files to SwiftUI project..."
mkdir -p "$SWIFT_DIR/qidao_coreFFI"
cp "$CORE_DIR/target/release/libqidao_core.a" "$SWIFT_DIR/"
cp "$CORE_DIR/out/qidao_core.swift" "$SWIFT_DIR/"
cp "$CORE_DIR/out/qidao_coreFFI.h" "$SWIFT_DIR/qidao_coreFFI/"
cp "$CORE_DIR/out/qidao_coreFFI.modulemap" "$SWIFT_DIR/qidao_coreFFI/module.modulemap"

echo "Done! Please ensure Xcode project is configured to link libqidao_core.a and include the modulemap path."
