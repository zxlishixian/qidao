from __future__ import annotations

import io
import json
import os
import selectors
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


VISION_DIR = Path(__file__).resolve().parents[1]


def read_json_line(stream, timeout: float = 5.0):
    return read_json_line_before(stream, time.monotonic() + timeout)


def read_json_line_before(stream, deadline: float):
    if time.monotonic() >= deadline:
        raise TimeoutError("timed out waiting for vision protocol output")
    buffer = getattr(stream, "_qidao_deadline_buffer", bytearray())
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for vision protocol output")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            if not selector.select(remaining):
                raise TimeoutError("timed out waiting for vision protocol output")
        chunk = os.read(stream.fileno(), 64 * 1024)
        if not chunk:
            raise EOFError("vision service closed stdout")
        buffer.extend(chunk)

    line, _, remainder = buffer.partition(b"\n")
    setattr(stream, "_qidao_deadline_buffer", bytearray(remainder))
    return json.loads(line)


def wait_for_json_event(stream, event: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        message = read_json_line_before(stream, deadline)
        if message.get("event") == event:
            return message


class VisionProtocolTests(unittest.TestCase):
    def test_event_wait_has_one_absolute_deadline(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        stopped = threading.Event()

        def emit_unrelated_events() -> None:
            emit_until = time.monotonic() + 0.3
            try:
                while not stopped.is_set() and time.monotonic() < emit_until:
                    os.write(write_fd, b'{"event":"scan"}\n')
                    time.sleep(0.005)
            finally:
                os.close(write_fd)

        writer = threading.Thread(target=emit_unrelated_events)
        writer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                wait_for_json_event(stream, "running", timeout=0.05)
        finally:
            elapsed = time.monotonic() - started
            stopped.set()
            writer.join(timeout=1.0)
            stream.close()

        self.assertFalse(writer.is_alive())
        self.assertLess(elapsed, 0.2)

    def test_maximal_position_is_one_complete_json_line(self) -> None:
        from vision_service import VisionService

        read_fd, write_fd = os.pipe()
        service = VisionService.__new__(VisionService)
        service._output_lock = threading.Lock()
        service._start_output_writer(write_fd)
        assert service._output_thread is not None
        output_thread = service._output_thread
        self.assertFalse(output_thread.daemon)
        os.set_blocking(read_fd, False)
        board = [[2 for _ in range(19)] for _ in range(19)]
        try:
            service.emit(
                "position",
                board=board,
                observedBoard=board,
                moveNumber=999999999,
                scanSequence=999999999,
                positionSequence=999999999,
                nextPlayer="W",
                confidence=0.9999,
                captureMs=99999.9,
                recognitionMs=99999.9,
                verificationMs=99999.9,
                confirmation="grid-snapshot",
                turnCorrected=True,
                recognizer="OpenCV + ONNX intersection classifier",
                locator="OpenCV exact grid + ONNX coarse locator",
            )
            self.assertTrue(service._output_barrier(timeout=1.0))
            chunks = []
            while True:
                try:
                    chunks.append(os.read(read_fd, 64 * 1024))
                except BlockingIOError:
                    break
            encoded = b"".join(chunks)
        finally:
            service._close_output_writer()
            os.close(read_fd)
            os.close(write_fd)

        self.assertFalse(output_thread.is_alive())
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(json.loads(encoded)["board"], board)

    def test_oversized_event_cannot_leave_a_partial_json_line(self) -> None:
        from vision_service import VisionService

        read_fd, write_fd = os.pipe()
        service = VisionService.__new__(VisionService)
        service._output_lock = threading.Lock()
        service._start_output_writer(write_fd)
        os.set_blocking(read_fd, False)
        try:
            service.emit("error", message="x" * (128 * 1024))
            service.emit("pong")
            self.assertTrue(service._output_barrier(timeout=1.0))
            chunks = []
            while True:
                try:
                    chunks.append(os.read(read_fd, 64 * 1024))
                except BlockingIOError:
                    break
            encoded = b"".join(chunks)
        finally:
            service._close_output_writer()
            os.close(read_fd)
            os.close(write_fd)

        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(json.loads(encoded), {"event": "pong"})

    def test_output_barrier_closes_blocked_writer(self) -> None:
        from vision_service import VisionService

        read_fd, write_fd = os.pipe()
        service = VisionService.__new__(VisionService)
        service._output_lock = threading.Lock()
        service._start_output_writer(write_fd)
        assert service._output_thread is not None
        output_thread = service._output_thread
        try:
            for _ in range(24):
                service.emit("warning", message="x" * 8000)
            self.assertFalse(service._output_barrier(timeout=0.05))
        finally:
            service._close_output_writer()
            os.close(read_fd)
            try:
                os.close(write_fd)
            except OSError:
                pass

        self.assertIsNone(service._output_fd)
        self.assertFalse(output_thread.is_alive())

    def test_output_abort_never_leaves_partial_json_record(self) -> None:
        from vision_service import VisionService

        read_fd, write_fd = os.pipe()
        service = VisionService.__new__(VisionService)
        service._output_lock = threading.Lock()
        service._start_output_writer(write_fd)
        board = [[2 for _ in range(19)] for _ in range(19)]
        try:
            for sequence in range(80):
                service.emit(
                    "position",
                    board=board,
                    observedBoard=board,
                    moveNumber=sequence,
                    scanSequence=sequence,
                    positionSequence=sequence,
                    nextPlayer="W",
                    confidence=0.9999,
                    captureMs=99999.9,
                    recognitionMs=99999.9,
                    verificationMs=99999.9,
                    confirmation="grid-snapshot",
                    turnCorrected=True,
                )
            time.sleep(0.1)
            self.assertFalse(service._output_barrier(timeout=0.05))
        finally:
            service._close_output_writer()
            try:
                os.close(write_fd)
            except OSError:
                pass

        chunks = []
        while chunk := os.read(read_fd, 64 * 1024):
            chunks.append(chunk)
        os.close(read_fd)
        encoded = b"".join(chunks)

        self.assertTrue(encoded)
        trailing = encoded.rsplit(b"\n", 1)[-1]
        self.assertTrue(
            encoded.endswith(b"\n"),
            f"abort left {len(trailing)} bytes of an unterminated JSON record",
        )
        for record in encoded.splitlines():
            self.assertEqual(json.loads(record)["event"], "position")

    def test_writer_closes_when_controller_cleanup_fails(self) -> None:
        from vision_service import VisionService

        read_fd, write_fd = os.pipe()
        service = VisionService.__new__(VisionService)
        service.controller = SimpleNamespace(
            probe_capture=lambda: None,
            close=lambda: (_ for _ in ()).throw(RuntimeError("controller close failed")),
        )
        service.config = SimpleNamespace(interval=0.01)
        service.region = None
        service.tracker = None
        service.running = False
        service.closed = False
        service._condition = threading.Condition()
        service._state_lock = threading.RLock()
        service._output_lock = threading.Lock()
        service._start_output_writer(write_fd)
        assert service._output_thread is not None
        output_thread = service._output_thread
        try:
            with mock.patch.object(sys, "stdin", io.StringIO("")):
                with self.assertRaisesRegex(RuntimeError, "controller close failed"):
                    service.run()
            self.assertFalse(output_thread.is_alive())
        finally:
            if output_thread.is_alive():
                service._abort_output()
            service._close_output_writer()
            os.close(read_fd)
            try:
                os.close(write_fd)
            except OSError:
                pass

    def test_stop_and_shutdown_ignore_stdout_backpressure(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(VISION_DIR / "vision_service.py"), "--demo"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            self.assertEqual(read_json_line(process.stdout)["event"], "ready")

            # Each invalid three-byte command produces a much larger JSON error.
            # The whole input fits in the stdin pipe while stdout exceeds 64 KiB.
            process.stdin.write(
                "{}\n" * 1600
                + json.dumps({"command": "stop"}) + "\n"
                + json.dumps({"command": "shutdown"}) + "\n"
            )
            process.stdin.flush()
            try:
                exit_code = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.fail("stop/shutdown blocked behind undrained stdout")
            self.assertEqual(exit_code, 0)
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            if process.stdout is not None:
                process.stdout.close()

    def test_shutdown_exits_without_waiting_for_stdin_eof(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(VISION_DIR / "vision_service.py"), "--demo"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            self.assertEqual(read_json_line(process.stdout)["event"], "ready")
            process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
            process.stdin.flush()
            try:
                exit_code = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.fail("shutdown waited for stdin EOF")
            self.assertEqual(exit_code, 0)
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_demo_service_handshake_and_configuration(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(VISION_DIR / "vision_service.py"), "--demo"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            ready = read_json_line(process.stdout)
            self.assertEqual(ready["event"], "ready")
            self.assertEqual(ready["protocol"], 1)
            self.assertTrue(ready["captureReady"])

            quad = [[0, 0], [800, 0], [800, 800], [0, 800]]
            process.stdin.write(json.dumps({"command": "configure", "quad": quad, "size": 19}) + "\n")
            process.stdin.flush()
            configured = read_json_line(process.stdout)
            self.assertEqual(configured["event"], "configured")
            self.assertTrue(configured["calibrated"])

            process.stdin.write(json.dumps({"command": "baseline"}) + "\n")
            process.stdin.flush()
            self.assertEqual(read_json_line(process.stdout)["event"], "status")
            baseline = read_json_line(process.stdout)
            self.assertEqual(baseline["event"], "baseline")
            self.assertGreater(baseline["gridScore"], 0.4)
            self.assertEqual(len(baseline["observedBoard"]), 19)
            self.assertEqual(baseline["moveNumber"], 0)
            self.assertEqual(baseline["nextPlayer"], "B")

            process.stdin.write(json.dumps({"command": "start"}) + "\n")
            process.stdin.flush()
            running = read_json_line(process.stdout)
            self.assertEqual(running["event"], "running")
            self.assertTrue(running["running"])
            self.assertEqual(running["trackingMode"], "tracking")
            self.assertEqual(len(running["quad"]), 4)

            scan = None
            for _ in range(6):
                event = read_json_line(process.stdout)
                if event["event"] == "scan":
                    scan = event
                    break
            self.assertIsNotNone(scan)
            assert scan is not None
            self.assertEqual(len(scan["observedBoard"]), 19)
            self.assertEqual(len(scan["confirmedBoard"]), 19)
            self.assertEqual(scan["moveNumber"], 0)
            self.assertEqual(scan["nextPlayer"], "B")

            process.stdin.write(json.dumps({"command": "stop"}) + "\n")
            process.stdin.flush()
            wait_for_json_event(process.stdout, "running")

            # The UI's “Re-recognize Board” action reuses the live tracker's latest
            # corrected quad and templates. It must refresh the complete position
            # without another locator pass or drag-selection interaction.
            process.stdin.write(json.dumps({"command": "rebaseline"}) + "\n")
            process.stdin.flush()
            self.assertEqual(read_json_line(process.stdout)["event"], "status")
            refreshed = read_json_line(process.stdout)
            self.assertEqual(refreshed["event"], "baseline")
            self.assertEqual(len(refreshed["board"]), 19)
            self.assertEqual(refreshed["moveNumber"], 0)

            process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
            process.stdin.flush()
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
