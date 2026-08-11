#!/usr/bin/env python3
"""Send one real Analysis Engine query and print the best candidate."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--katago",
        default=os.environ.get("KATAGO_EXECUTABLE", "katago"),
        help="KataGo executable (default: KATAGO_EXECUTABLE or katago on PATH)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("KATAGO_MODEL"),
        help="KataGo model path (required unless KATAGO_MODEL is set)",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("KATAGO_CONFIG", str(root / "katago/analysis.cfg")),
        help="analysis config (default: KATAGO_CONFIG or the repository config)",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if args.model is None:
        parser.error("--model or KATAGO_MODEL is required")

    command = [args.katago, "analysis", "-model", args.model, "-config", args.config]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    query = {
        "id": "qidao-screen-smoke",
        "moves": [],
        "initialStones": [],
        "rules": "chinese",
        "komi": 7.5,
        "boardXSize": 19,
        "boardYSize": 19,
        "analyzeTurns": [0],
        "maxVisits": 8,
        "includeOwnership": False,
        "includePolicy": False,
    }
    process.stdin.write((json.dumps(query, separators=(",", ":")) + "\n").encode())
    process.stdin.flush()

    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + args.timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    stderr_tail: deque[str] = deque(maxlen=12)
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("KataGo exited early: " + "".join(stderr_tail))
            remaining = max(0.0, deadline - time.monotonic())
            for key, _ in selector.select(timeout=min(0.5, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    continue
                buffer = buffers[key.data]
                buffer.extend(chunk)
                if key.data == "stderr":
                    while b"\n" in buffer:
                        line, _, remainder = buffer.partition(b"\n")
                        stderr_tail.append(line.decode(errors="replace") + "\n")
                        buffer[:] = remainder
                    if len(buffer) > 64 * 1024:
                        del buffer[:-64 * 1024]
                    continue
                if len(buffer) > 1024 * 1024:
                    raise RuntimeError("KataGo returned an oversized JSON line")
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer[:] = remainder
                    result = json.loads(line)
                    if result.get("id") != "qidao-screen-smoke" or result.get("error"):
                        raise RuntimeError(str(result))
                    moves = result.get("moveInfos", [])
                    if not moves:
                        raise RuntimeError("KataGo returned no candidate moves")
                    best = max(moves, key=lambda item: item.get("visits", 0))
                    print(json.dumps({
                        "ok": True,
                        "move": best.get("move"),
                        "visits": best.get("visits"),
                        "winrate": best.get("winrate"),
                    }, ensure_ascii=False))
                    return 0
        partial_stderr = buffers["stderr"].decode(errors="replace")
        raise TimeoutError("KataGo analysis timed out: " + "".join(stderr_tail) + partial_stderr)
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"SMOKE FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
