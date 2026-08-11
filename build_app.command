#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_DIR/.build"
APP_DIR="$BUILD_DIR/QiDao.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
MODULE_CACHE="$BUILD_DIR/swift-module-cache"
CORE_DIR="$PROJECT_DIR/QiDao/QiDao/Core"
CORE_FFI_DIR="$CORE_DIR/qidao_coreFFI"
SIGNING_DIR="$PROJECT_DIR/.signing"
KEYCHAIN_PATH="$SIGNING_DIR/QiDaoLocal.keychain-db"
PASSWORD_FILE="$SIGNING_DIR/keychain-password"
SKIP_SIGNING="${QIDAO_SKIP_SIGNING:-0}"

if [ ! -d "$SDK_PATH" ]; then
    echo "缺少兼容的 macOS SDK：$SDK_PATH"
    exit 1
fi

if [ ! -f "$CORE_DIR/libqidao_core.a" ] || [ ! -f "$CORE_DIR/qidao_core.swift" ] || \
   [ ! -f "$CORE_FFI_DIR/qidao_coreFFI.h" ] || [ ! -f "$CORE_FFI_DIR/module.modulemap" ]; then
    echo "Rust Core bindings are missing; building them now..."
    "$PROJECT_DIR/build_core.sh"
fi

if [ "$SKIP_SIGNING" = "1" ]; then
    echo "WARNING: building unsigned because QIDAO_SKIP_SIGNING=1 (CI/build verification only)."
else
    if [ ! -f "$KEYCHAIN_PATH" ] || [ ! -f "$PASSWORD_FILE" ]; then
        echo "缺少稳定的本地签名身份；请先运行 $PROJECT_DIR/setup_signing.command"
        exit 1
    fi
    IFS= read -r QIDAO_KEYCHAIN_PASSWORD < "$PASSWORD_FILE"
    security unlock-keychain -p "$QIDAO_KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
    SIGNING_IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN_PATH" | awk '/QiDao Local Code Signing/ { print $2; exit }')"
    if [ -z "$SIGNING_IDENTITY" ]; then
        echo "签名钥匙串中没有有效的 QiDao Local Code Signing 身份"
        exit 1
    fi
fi

"$PROJECT_DIR/build_vision.sh"
rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$MODULE_CACHE"

find "$PROJECT_DIR/QiDao/QiDao" -name '*.swift' -print0 | xargs -0 swiftc \
    -parse-as-library \
    -sdk "$SDK_PATH" \
    -target arm64-apple-macosx14.0 \
    -module-cache-path "$MODULE_CACHE" \
    -I "$CORE_FFI_DIR" \
    -Xcc -fmodule-map-file="$CORE_FFI_DIR/module.modulemap" \
    "$CORE_DIR/libqidao_core.a" \
    -framework SwiftUI \
    -framework AppKit \
    -framework Foundation \
    -o "$MACOS_DIR/QiDao"

cp "$PROJECT_DIR/app/Info.plist" "$CONTENTS_DIR/Info.plist"
cp -R "$PROJECT_DIR/QiDao/QiDao/Resources/." "$RESOURCES_DIR/"
cp -R "$PROJECT_DIR/QiDao/QiDao/en.lproj" "$RESOURCES_DIR/"
cp -R "$PROJECT_DIR/QiDao/QiDao/zh-Hans.lproj" "$RESOURCES_DIR/"
mkdir -p "$RESOURCES_DIR/katago"
cp "$PROJECT_DIR/katago/analysis.cfg" "$RESOURCES_DIR/katago/"
if [ -f "$PROJECT_DIR/katago/default_model.bin.gz" ]; then
    cp "$PROJECT_DIR/katago/default_model.bin.gz" "$RESOURCES_DIR/katago/"
else
    echo "未打包 KataGo 权重；启动后请在 AI 引擎设置中选择模型文件"
fi
cp "$PROJECT_DIR/katago/NETWORK_LICENSE.md" "$RESOURCES_DIR/katago/"
mkdir -p "$RESOURCES_DIR/vision"
cp "$PROJECT_DIR/vision/vision_service.py" "$RESOURCES_DIR/vision/"
cp "$PROJECT_DIR/vision/requirements.txt" "$RESOURCES_DIR/vision/"
mkdir -p "$RESOURCES_DIR/vision/go_vision"
cp "$PROJECT_DIR"/vision/go_vision/*.py "$RESOURCES_DIR/vision/go_vision/"
if [ -d "$PROJECT_DIR/vision/models" ]; then
    cp -R "$PROJECT_DIR/vision/models" "$RESOURCES_DIR/vision/"
fi
cp "$PROJECT_DIR/.build/screen-tool" "$RESOURCES_DIR/vision/"

if [ "$SKIP_SIGNING" != "1" ]; then
    # A real local signing identity gives TCC a certificate-anchored designated
    # requirement. Never fall back to ad-hoc signing: its cdhash changes on every
    # rebuild and silently invalidates Screen Recording authorization.
    codesign --force --timestamp=none \
        --keychain "$KEYCHAIN_PATH" \
        --sign "$SIGNING_IDENTITY" \
        --identifier "net.paradigmx.QiDao.screen-tool" \
        "$RESOURCES_DIR/vision/screen-tool"
    codesign --force --timestamp=none \
        --keychain "$KEYCHAIN_PATH" \
        --sign "$SIGNING_IDENTITY" \
        --identifier "net.paradigmx.QiDao" \
        "$APP_DIR"
fi
echo "Built: $APP_DIR"
