from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from go_vision import capture as capture_module
from go_vision.capture import BoardRegionTracker, CaptureError, Point, Quad, ScreenController
from vision_service import VisionService


class RecordingScreenController(ScreenController):
    def _capture_region(self, bounds: tuple[int, int, int, int]) -> np.ndarray:
        self.capture_requests.append(bounds)
        return super()._capture_region(bounds)

    def _ensure_capture_process(self):
        previous = self._capture_process
        process = super()._ensure_capture_process()
        if process is not previous:
            self.started_processes.append(process)
            self.helper_started.set()
        return process

    def _stop_capture_process(self, cleanup_deadline: float | None = None) -> None:
        process = self._capture_process
        if cleanup_deadline is None:
            super()._stop_capture_process()
        else:
            super()._stop_capture_process(cleanup_deadline)
        if process is not None:
            self.stopped_processes.append(process)


class CaptureProcessTests(unittest.TestCase):
    @staticmethod
    def _helper(directory: str, body: str) -> Path:
        helper = Path(directory) / "fake-screen-tool"
        helper.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "import time\n"
            + body,
            encoding="utf-8",
        )
        helper.chmod(0o755)
        return helper

    @staticmethod
    def _controller(helper: Path) -> RecordingScreenController:
        controller = RecordingScreenController.__new__(RecordingScreenController)
        controller.tool = helper
        controller._capture_process = None
        controller._capture_lock = threading.Lock()
        controller.capture_requests = []
        controller.started_processes = []
        controller.stopped_processes = []
        controller.helper_started = threading.Event()
        return controller

    @staticmethod
    def _tracking_region(controller: RecordingScreenController) -> BoardRegionTracker:
        quad = Quad(Point(0, 0), Point(800, 0), Point(800, 800), Point(0, 800))
        region = BoardRegionTracker.__new__(BoardRegionTracker)
        region.controller = controller
        region.size = 19
        region.current_quad = quad
        region.anchor_quad = quad
        region.match_score = 1.0
        region.anchor_score = 1.0
        region.last_shift = Point(0.0, 0.0)
        region.tracking_mode = "tracking"
        region.consecutive_failures = 1
        region.frame_index = 59
        region.last_reanchor_frame = 0
        region.last_tracking_error = ""
        region.force_recovery = False
        region.alignment_failures = 0
        region.last_capture_used_fallback = False
        region.last_recovery_attempt_frame = -1000
        region.template_margin = 8
        region.grid_spacing_points = 800 / 18
        return region

    def _assert_reaped(self, controller: RecordingScreenController) -> None:
        self.assertIsNone(controller._capture_process)
        self.assertTrue(controller.stopped_processes)
        self.assertTrue(all(process.poll() is not None for process in controller.stopped_processes))

    def _assert_capture_error(
        self,
        body: str,
        *,
        expected_message: str,
        io_timeout: float = 1.5,
        cleanup_timeout: float = 2.0,
        max_elapsed: float = 1.8,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(self._helper(directory, body))
            started = time.monotonic()
            try:
                capture_error: Exception | None = None
                with (
                    mock.patch.object(
                        capture_module,
                        "CAPTURE_IO_TIMEOUT",
                        io_timeout,
                        create=True,
                    ),
                    mock.patch.object(
                        capture_module,
                        "CAPTURE_CLEANUP_TIMEOUT",
                        cleanup_timeout,
                        create=True,
                    ),
                ):
                    try:
                        controller._capture_region((0, 0, 1, 1))
                    except Exception as error:
                        capture_error = error
                    else:
                        self.fail("malformed helper response was accepted")
                self.assertIsInstance(capture_error, CaptureError)
                assert capture_error is not None
                self.assertIn(expected_message, str(capture_error))
                self.assertLess(time.monotonic() - started, max_elapsed)
                self._assert_reaped(controller)

                close_started = time.monotonic()
                controller.close()
                self.assertLess(time.monotonic() - close_started, 0.5)
            finally:
                controller.close()

    def test_capture_times_out_and_reaps_silent_helper(self) -> None:
        self._assert_capture_error(
            "sys.stdin.buffer.readline()\n"
            "time.sleep(1.0)\n",
            expected_message="超时",
            io_timeout=0.2,
            max_elapsed=0.8,
        )

    def test_capture_times_out_while_helper_stops_reading_a_full_stdin_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(
                self._helper(
                    directory,
                    "time.sleep(5.0)\n",
                )
            )
            process = controller._ensure_capture_process()
            assert process.stdin is not None
            stdin_fd = process.stdin.fileno()
            original_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
            fcntl.fcntl(stdin_fd, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
            try:
                while True:
                    os.write(stdin_fd, b"x" * 4096)
            except BlockingIOError:
                pass
            finally:
                fcntl.fcntl(stdin_fd, fcntl.F_SETFL, original_flags)

            result: list[Exception] = []

            def capture() -> None:
                try:
                    controller._capture_region((0, 0, 1, 1))
                except Exception as error:
                    result.append(error)

            worker = threading.Thread(target=capture, daemon=True)
            started = time.monotonic()
            try:
                with (
                    mock.patch.object(capture_module, "CAPTURE_IO_TIMEOUT", 0.2),
                    mock.patch.object(capture_module, "CAPTURE_CLEANUP_TIMEOUT", 0.2),
                ):
                    worker.start()
                    worker.join(timeout=0.8)

                blocked = worker.is_alive()
                if blocked:
                    process.kill()
                    process.wait(timeout=1.0)
                    worker.join(timeout=1.0)
                self.assertFalse(blocked, "capture blocked while writing its helper request")
                self.assertFalse(worker.is_alive(), "capture thread survived helper cleanup")
                self.assertEqual(len(result), 1)
                self.assertIsInstance(result[0], CaptureError)
                self.assertIn("超时", str(result[0]))
                self.assertLess(time.monotonic() - started, 0.8)
                self._assert_reaped(controller)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1.0)
                worker.join(timeout=1.0)
                controller.close()

    def test_capture_cleanup_kills_uncooperative_helper_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            controller = self._controller(
                self._helper(
                    directory,
                    "import signal\n"
                    "from pathlib import Path\n"
                    "signal.signal(signal.SIGTERM, lambda *_: None)\n"
                    f"Path({str(ready)!r}).touch()\n"
                    "sys.stdin.buffer.readline()\n"
                    "sys.stdout.buffer.write(b'BAD RESPONSE\\n')\n"
                    "sys.stdout.buffer.flush()\n"
                    "time.sleep(5.0)\n",
                )
            )
            try:
                controller._ensure_capture_process()
                deadline = time.monotonic() + 5.0
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "helper did not install its SIGTERM handler")

                started = time.monotonic()
                with (
                    mock.patch.object(capture_module, "CAPTURE_IO_TIMEOUT", 0.5),
                    mock.patch.object(
                        capture_module,
                        "CAPTURE_CLEANUP_TIMEOUT",
                        0.2,
                        create=True,
                    ),
                ):
                    with self.assertRaises(CaptureError):
                        controller._capture_region((0, 0, 1, 1))
                self.assertLess(time.monotonic() - started, 0.8)
                self.assertEqual(
                    len(controller.started_processes),
                    1,
                    "cleanup failure restarted a helper inside the same request",
                )
                self._assert_reaped(controller)
            finally:
                controller.close()

    def test_stop_shares_one_deadline_across_all_tracking_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(
                self._helper(
                    directory,
                    "while sys.stdin.buffer.readline():\n"
                    "    time.sleep(1.0)\n",
                )
            )
            controller.demo = False
            controller.board_locator = None
            controller.locator_confidence = 0.0
            region = self._tracking_region(controller)

            service = VisionService.__new__(VisionService)
            service.controller = controller
            service.region = region
            service.tracker = SimpleNamespace(last_grid_score=0.0)
            service.config = SimpleNamespace(interval=0.01)
            service.running = True
            service.closed = False
            service._condition = threading.Condition()
            service._state_lock = threading.RLock()
            service.scan_sequence = 0
            service.emit = lambda *_args, **_kwargs: None
            monitor = threading.Thread(target=service.monitor, daemon=True)
            stop_elapsed = []

            def stop_service() -> None:
                started = time.monotonic()
                service.set_running(False)
                stop_elapsed.append(time.monotonic() - started)

            stopper = threading.Thread(target=stop_service, daemon=True)
            try:
                with (
                    mock.patch.object(capture_module, "CAPTURE_IO_TIMEOUT", 0.2),
                    mock.patch.object(
                        capture_module,
                        "CAPTURE_CLEANUP_TIMEOUT",
                        0.2,
                        create=True,
                    ),
                ):
                    monitor.start()
                    self.assertTrue(controller.helper_started.wait(timeout=1.0))
                    stopper.start()
                    stopper.join(timeout=1.5)

                self.assertFalse(stopper.is_alive(), "stop exceeded one capture-cycle deadline")
                self.assertEqual(len(controller.capture_requests), 4)
                self.assertEqual(
                    len(controller.started_processes),
                    1,
                    "expired fallback captures started fresh helpers",
                )
                self.assertLess(stop_elapsed[0], 0.5)
                self._assert_reaped(controller)
            finally:
                with service._state_lock:
                    service.running = False
                    service.closed = True
                with service._condition:
                    service._condition.notify_all()
                stopper.join(timeout=1.0)
                monitor.join(timeout=1.0)
                controller.close()

    def test_fast_failure_starts_only_one_helper_per_capture_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(
                self._helper(
                    directory,
                    "while sys.stdin.buffer.readline():\n"
                    "    sys.stdout.buffer.write(b'BAD RESPONSE\\n')\n"
                    "    sys.stdout.buffer.flush()\n",
                )
            )
            controller.demo = False
            controller.board_locator = None
            controller.locator_confidence = 0.0
            region = self._tracking_region(controller)

            try:
                with (
                    mock.patch.object(capture_module, "CAPTURE_IO_TIMEOUT", 10.0),
                    mock.patch.object(capture_module, "CAPTURE_CLEANUP_TIMEOUT", 0.2),
                ):
                    with controller.capture_cycle():
                        with self.assertRaises(CaptureError):
                            region.capture()

                    self.assertEqual(len(controller.capture_requests), 4)
                    self.assertEqual(
                        len(controller.started_processes),
                        1,
                        "one failed scan cycle restarted its helper",
                    )
                    self.assertEqual(len(controller.stopped_processes), 1)
                    self._assert_reaped(controller)

                    with controller.capture_cycle():
                        with self.assertRaises(CaptureError):
                            controller._capture_region((0, 0, 1, 1))

                self.assertEqual(
                    len(controller.started_processes),
                    2,
                    "a fresh capture cycle could not start one new helper",
                )
                self.assertEqual(len(controller.stopped_processes), 2)
                self._assert_reaped(controller)
            finally:
                controller.close()

    def test_capture_rejects_oversized_payload_and_reaps_helper(self) -> None:
        self._assert_capture_error(
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'RAW 1 1 134217732 134217732\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(1.0)\n",
            expected_message="无效 BGRA 尺寸",
        )

    def test_capture_rejects_overlong_header_and_reaps_helper(self) -> None:
        self._assert_capture_error(
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'9' * 4097)\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(1.0)\n",
            expected_message="响应头过长",
        )

    def test_capture_rejects_negative_legacy_payload_and_reaps_helper(self) -> None:
        self._assert_capture_error(
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'-1\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(1.0)\n",
            expected_message="无效图像长度",
        )

    def test_capture_rejects_inconsistent_raw_payload_and_reaps_helper(self) -> None:
        self._assert_capture_error(
            "sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'RAW 1 1 4 3\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(1.0)\n",
            expected_message="无效 BGRA 尺寸",
        )

    def test_capture_rejects_non_finite_bounds_and_reaps_helper(self) -> None:
        for value in ("inf", "-inf", "nan"):
            with self.subTest(value=value):
                self._assert_capture_error(
                    "sys.stdin.buffer.readline()\n"
                    f"sys.stdout.buffer.write(b'RAW 1 1 4 4 {value} 0 1 1\\n' "
                    "+ bytes((1, 2, 3, 255)))\n"
                    "sys.stdout.buffer.flush()\n"
                    "time.sleep(1.0)\n",
                    expected_message="无效屏幕范围",
                    io_timeout=5.0,
                    max_elapsed=6.0,
                )

    def test_capture_preserves_valid_raw_frame_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = self._helper(
                directory,
                "while sys.stdin.buffer.readline():\n"
                "    sys.stdout.buffer.write(b'RAW 1 1 4 4\\n' + bytes((1, 2, 3, 255)))\n"
                "    sys.stdout.buffer.flush()\n",
            )
            controller = self._controller(helper)
            try:
                with mock.patch.object(capture_module, "CAPTURE_IO_TIMEOUT", 5.0):
                    image = controller._capture_region((0, 0, 1, 1))
                np.testing.assert_array_equal(image, np.array([[[1, 2, 3]]], dtype=np.uint8))
            finally:
                controller.close()
            self._assert_reaped(controller)


if __name__ == "__main__":
    unittest.main()
