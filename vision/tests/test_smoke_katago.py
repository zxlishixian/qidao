from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class KataGoSmokeTests(unittest.TestCase):
    def test_half_line_respects_timeout_and_reaps_engine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qidao-smoke-katago-") as temporary:
            temporary_path = Path(temporary)
            fake_engine = temporary_path / "fake-katago"
            pid_file = temporary_path / "fake.pid"
            fake_engine.write_text(
                """#!/bin/sh
printf '%s' "$$" > "$FAKE_KATAGO_PID_FILE"
printf '%s' '{"id":"qidao-screen-smoke"'
exec sleep 60
""",
                encoding="utf-8",
            )
            fake_engine.chmod(0o755)
            environment = os.environ.copy()
            environment["FAKE_KATAGO_PID_FILE"] = str(pid_file)
            started = time.monotonic()
            fake_pid = None
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "tools/smoke_katago.py"),
                        "--katago",
                        str(fake_engine),
                        "--model",
                        str(temporary_path / "model.bin.gz"),
                        "--config",
                        str(temporary_path / "analysis.cfg"),
                        "--timeout",
                        "2.0",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5.0,
                    env=environment,
                    check=False,
                )
                elapsed = time.monotonic() - started
                if pid_file.exists():
                    fake_pid = int(pid_file.read_text(encoding="utf-8"))
                self.assertEqual(result.returncode, 1)
                self.assertIn("KataGo analysis timed out", result.stderr)
                self.assertLess(elapsed, 4.0)
                self.assertIsNotNone(
                    fake_pid,
                    f"fake PID file missing; stdout={result.stdout!r} stderr={result.stderr!r}",
                )
                assert fake_pid is not None
                self.assertFalse(process_is_alive(fake_pid))
            finally:
                if fake_pid is None and pid_file.exists():
                    fake_pid = int(pid_file.read_text(encoding="utf-8"))
                if fake_pid is not None and process_is_alive(fake_pid):
                    os.kill(fake_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
