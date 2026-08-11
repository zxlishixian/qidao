#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

if ! command -v cargo >/dev/null 2>&1; then
    echo "未找到 Rust/Cargo。请先从 https://rustup.rs/ 安装 rustup，"
    echo "然后运行：rustup toolchain install 1.97.1"
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    python3 -m venv "$PROJECT_DIR/.venv"
fi

"$PYTHON" -m pip install -r "$PROJECT_DIR/vision/requirements.txt"

if ! command -v katago >/dev/null 2>&1; then
    echo "未发现 KataGo。请先运行：brew install katago"
fi

"$PROJECT_DIR/build_core.sh"
"$PROJECT_DIR/build_app.command"
echo
echo "安装完成：$PROJECT_DIR/.build/QiDao.app"
echo "首次运行请在系统设置中授予屏幕录制权限。"
