#!/usr/bin/env bash
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

python3 - <<'PY'
import re
import tomllib
from pathlib import Path

workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
repository_audit = workflow.split("  repository-audit:\n", 1)[1].split("\n  python-vision:\n", 1)[0]
if "fetch-depth: 0" not in repository_audit:
    raise SystemExit("repository-audit checkout 必须使用 fetch-depth: 0")

jobs = ("repository-audit", "python-vision", "rust-core", "swift-boundary", "macos-app-build")
for index, job in enumerate(jobs):
    section = workflow.split(f"  {job}:\n", 1)[1]
    if index + 1 < len(jobs):
        section = section.split(f"\n  {jobs[index + 1]}:\n", 1)[0]
    if not re.search(r"^    timeout-minutes: [1-9][0-9]*$", section, re.MULTILINE):
        raise SystemExit(f"{job} 必须设置 timeout-minutes")

for requirements in (Path("vision/requirements.txt"), Path("vision/requirements-training.txt")):
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if line and not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", line):
            raise SystemExit(f"{requirements} 必须只包含精确版本 pin：{line}")

toolchain = tomllib.loads(Path("rust-toolchain.toml").read_text(encoding="utf-8"))["toolchain"]
if toolchain != {"channel": "1.97.1", "profile": "minimal", "components": ["rustfmt"]}:
    raise SystemExit("rust-toolchain.toml 必须固定项目 Rust 1.97.1 和 rustfmt")
if "rustup toolchain install 1.97.1 --profile minimal --component rustfmt" not in workflow:
    raise SystemExit("Rust CI 必须安装项目固定的 1.97.1 + rustfmt toolchain")

print("CI/toolchain policy verification passed")
PY
