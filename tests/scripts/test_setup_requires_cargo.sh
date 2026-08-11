#!/usr/bin/env bash
set -eu

source_root=$(cd "$(dirname "$0")/../.." && pwd)
test_root=$(mktemp -d /private/tmp/qidao-setup-cargo.XXXXXX)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/bin"
cp "$source_root/setup.command" "$test_root/setup.command"

marker=$test_root/python-was-called
printf '%s\n' '#!/bin/sh' ': > "$QIDAO_SETUP_TEST_MARKER"' 'exit 99' > "$test_root/bin/python3"
chmod +x "$test_root/bin/python3"

if output=$(PATH="$test_root/bin:/usr/bin:/bin" QIDAO_SETUP_TEST_MARKER="$marker" bash "$test_root/setup.command" 2>&1); then
    printf 'FAIL: setup unexpectedly passed without cargo\n%s\n' "$output" >&2
    exit 1
fi
if [ -e "$marker" ]; then
    printf 'FAIL: setup invoked Python before checking for cargo\n%s\n' "$output" >&2
    exit 1
fi
if ! printf '%s\n' "$output" | grep -E -q 'cargo|Rust'; then
    printf 'FAIL: setup did not print actionable Rust/Cargo guidance\n%s\n' "$output" >&2
    exit 1
fi

printf 'PASS: setup fails before Python work when cargo is unavailable\n'
