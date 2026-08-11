from __future__ import annotations

import math
import os
import selectors
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .ml_inference import BoardLocator


CAPTURE_IO_TIMEOUT = 2.0
CAPTURE_CLEANUP_TIMEOUT = 2.0
MAX_CAPTURE_BYTES = 128 * 1024 * 1024


class CaptureError(RuntimeError):
    pass


class BoardTrackingError(CaptureError):
    pass


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    @classmethod
    def from_json(cls, value) -> "Point":
        return cls(float(value[0]), float(value[1]))

    def to_json(self) -> list[float]:
        return [round(self.x, 2), round(self.y, 2)]


@dataclass(frozen=True)
class Quad:
    """Four outer intersections in screen coordinates: TL, TR, BR, BL."""

    top_left: Point
    top_right: Point
    bottom_right: Point
    bottom_left: Point

    @classmethod
    def from_json(cls, value) -> "Quad | None":
        if not value or len(value) != 4:
            return None
        quad = cls(*(Point.from_json(point) for point in value))
        quad.validate()
        return quad

    def to_json(self) -> list[list[float]]:
        return [point.to_json() for point in self.points]

    @property
    def points(self) -> tuple[Point, Point, Point, Point]:
        return self.top_left, self.top_right, self.bottom_right, self.bottom_left

    def validate(self) -> None:
        sides = []
        points = self.points
        for index in range(4):
            first, second = points[index], points[(index + 1) % 4]
            sides.append(math.hypot(second.x - first.x, second.y - first.y))
        if min(sides) < 140:
            raise ValueError("拖选的棋盘矩形太小，请重新拖选")
        polygon_area = 0.0
        for index, point in enumerate(points):
            following = points[(index + 1) % 4]
            polygon_area += point.x * following.y - following.x * point.y
        if abs(polygon_area) < 30_000:
            raise ValueError("拖选的棋盘矩形面积不正确")

    def bounds(self, margin: int = 5) -> tuple[int, int, int, int]:
        xs = [point.x for point in self.points]
        ys = [point.y for point in self.points]
        left = math.floor(min(xs)) - margin
        top = math.floor(min(ys)) - margin
        right = math.ceil(max(xs)) + margin
        bottom = math.ceil(max(ys)) + margin
        return left, top, right - left, bottom - top

    def screen_point(self, x: int, y: int, size: int, rotation: int = 0) -> Point:
        if rotation == 180:
            x, y = size - 1 - x, size - 1 - y
        tx, ty = x / (size - 1), y / (size - 1)
        tl, tr, br, bl = self.points
        px = (
            (1 - tx) * (1 - ty) * tl.x
            + tx * (1 - ty) * tr.x
            + tx * ty * br.x
            + (1 - tx) * ty * bl.x
        )
        py = (
            (1 - tx) * (1 - ty) * tl.y
            + tx * (1 - ty) * tr.y
            + tx * ty * br.y
            + (1 - tx) * ty * bl.y
        )
        return Point(px, py)

    def translated(self, dx: float, dy: float) -> "Quad":
        return Quad(*(Point(point.x + dx, point.y + dy) for point in self.points))

    def interpolated(self, other: "Quad", factor: float) -> "Quad":
        """Interpolate corresponding corners without changing their order."""
        factor = max(0.0, min(1.0, factor))
        return Quad(
            *(
                Point(
                    first.x + (second.x - first.x) * factor,
                    first.y + (second.y - first.y) * factor,
                )
                for first, second in zip(self.points, other.points)
            )
        )


@dataclass(frozen=True)
class WarpedBoard:
    image: np.ndarray
    intersections: tuple[tuple[tuple[int, int], ...], ...]
    spacing: int
    margin: int

    def pil_image(self) -> Image.Image:
        return Image.fromarray(cv2.cvtColor(self.image, cv2.COLOR_BGR2RGB))


class ScreenController:
    def __init__(self, root: Path, demo: bool = False):
        self.root = root
        self.demo = demo
        workspace_tool = root / ".build" / "screen-tool"
        bundled_tool = root / "vision" / "screen-tool"
        # Source runs use <repo>/.build/screen-tool; the signed app service is
        # rooted at Contents/Resources and keeps its sealed helper beside the
        # Python package under Resources/vision. Prefer the bundled identity
        # there so TCC permission and the raw-frame protocol stay consistent.
        self.tool = bundled_tool if bundled_tool.is_file() else workspace_tool
        self.board_locator = BoardLocator.load_default()
        self.locator_confidence = 0.0
        self._capture_process: subprocess.Popen | None = None
        self._capture_lock = threading.Lock()
        self._capture_cycle_deadline: float | None = None
        self._capture_cleanup_deadline: float | None = None

    @contextmanager
    def capture_cycle(self):
        if getattr(self, "_capture_cycle_deadline", None) is not None:
            yield
            return
        self._capture_cycle_deadline = time.monotonic() + CAPTURE_IO_TIMEOUT
        self._capture_cleanup_deadline = None
        try:
            yield
        finally:
            self._capture_cycle_deadline = None
            self._capture_cleanup_deadline = None

    @property
    def locator_backend(self) -> str:
        return (
            "OpenCV 精确网格 + ONNX 粗框校验"
            if self.board_locator is not None
            else "OpenCV 精确网格定位"
        )

    def permissions(self, request: bool = False) -> dict[str, bool]:
        if self.demo:
            return {"screen": True}
        command = [str(self.tool), "permissions"] + (["request"] if request else [])
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        try:
            import json

            return json.loads(result.stdout)
        except Exception:
            return {"screen": False}

    def probe_capture(self) -> None:
        """Prove that the actual capture helper can return screen pixels.

        A TCC switch or CGPreflight result is not sufficient: a differently
        signed child process can still be denied.  This uses the exact helper
        and API used by normal board frames and validates the produced image.
        """
        if self.demo:
            return
        image = self._capture_region((0, 0, 8, 8))
        if image.shape[0] < 2 or image.shape[1] < 2:
            raise CaptureError("录屏组件返回了无效图像")

    def _capture_region(self, bounds: tuple[int, int, int, int]) -> np.ndarray:
        if not self.tool.is_file():
            raise CaptureError("原生屏幕采集组件不存在，请重新运行 run.command")
        cycle_deadline = getattr(self, "_capture_cycle_deadline", None)
        deadline = cycle_deadline or time.monotonic() + CAPTURE_IO_TIMEOUT
        with self._capture_lock:
            cleanup_deadline = getattr(self, "_capture_cleanup_deadline", None)
            if cycle_deadline is not None and cleanup_deadline is not None:
                raise CaptureError("当前屏幕采集周期已经失败")
            last_error = "未知截屏错误"
            for _ in range(2):
                if time.monotonic() >= deadline:
                    last_error = "等待原生采集流超时"
                    break
                try:
                    process = self._ensure_capture_process()
                    assert process.stdin is not None
                    assert process.stdout is not None
                    request = " ".join(str(value) for value in bounds) + "\n"
                    with selectors.DefaultSelector() as selector:
                        self._write_all(
                            process.stdin,
                            selector,
                            deadline,
                            request.encode("utf-8"),
                        )
                        selector.register(process.stdout, selectors.EVENT_READ)
                        header, buffered = self._read_header(
                            process.stdout,
                            selector,
                            deadline,
                        )
                        fields = header.strip().split()
                        if fields and fields[0] == b"RAW" and len(fields) in (5, 9):
                            pixel_width = int(fields[1])
                            pixel_height = int(fields[2])
                            bytes_per_row = int(fields[3])
                            byte_count = int(fields[4])
                            if (
                                pixel_width <= 0
                                or pixel_height <= 0
                                or bytes_per_row <= 0
                                or byte_count <= 0
                                or byte_count > MAX_CAPTURE_BYTES
                                or bytes_per_row < pixel_width * 4
                                or byte_count != bytes_per_row * pixel_height
                            ):
                                raise CaptureError("原生采集流返回了无效 BGRA 尺寸")
                            payload = self._read_exact(
                                process.stdout,
                                selector,
                                deadline,
                                byte_count,
                                buffered,
                            )
                            rows = np.frombuffer(payload, dtype=np.uint8).reshape(
                                pixel_height,
                                bytes_per_row,
                            )
                            bgra = rows[:, : pixel_width * 4].reshape(
                                pixel_height,
                                pixel_width,
                                4,
                            )
                            image = np.ascontiguousarray(bgra[..., :3])
                            if len(fields) == 9:
                                try:
                                    actual_bounds = tuple(float(value) for value in fields[5:9])
                                    if not all(math.isfinite(value) for value in actual_bounds):
                                        raise CaptureError("原生采集流返回了无效屏幕范围")
                                    image = self._restore_requested_capture(
                                        image,
                                        bounds,
                                        actual_bounds,
                                    )
                                except CaptureError:
                                    raise
                                except Exception as error:
                                    raise CaptureError(
                                        "原生采集流返回了无效屏幕范围"
                                    ) from error
                            return image

                        # Backward compatibility with a helper from an older app
                        # bundle while an update is being installed.
                        if len(fields) != 1:
                            raise CaptureError("原生采集流返回了无效响应头")
                        byte_count = int(fields[0])
                        if byte_count <= 0 or byte_count > MAX_CAPTURE_BYTES:
                            raise CaptureError("原生采集流返回了无效图像长度")
                        payload = self._read_exact(
                            process.stdout,
                            selector,
                            deadline,
                            byte_count,
                            buffered,
                        )
                        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if image is None or image.size == 0:
                            raise CaptureError("无法解码原生采集帧")
                        return image
                except (BrokenPipeError, OSError, ValueError, CaptureError) as error:
                    last_error = str(error)
                    if cleanup_deadline is None:
                        cleanup_deadline = time.monotonic() + CAPTURE_CLEANUP_TIMEOUT
                        if cycle_deadline is not None:
                            self._capture_cleanup_deadline = cleanup_deadline
                    self._stop_capture_process(cleanup_deadline)
                    # A later monitor cycle may retry with fresh budgets. Do
                    # not start another helper after this request spent any of
                    # its one explicit cleanup budget.
                    break
            raise CaptureError(f"无法读取屏幕：{last_error}")

    def _ensure_capture_process(self) -> subprocess.Popen:
        process = self._capture_process
        if process is not None and process.poll() is None:
            return process
        self._stop_capture_process()
        process = subprocess.Popen(
            [str(self.tool), "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._capture_process = process
        assert process.stdin is not None
        os.set_blocking(process.stdin.fileno(), False)
        return process

    @staticmethod
    def _write_all(stream, selector, deadline: float, data: bytes) -> None:
        selector.register(stream, selectors.EVENT_WRITE)
        sent = 0
        try:
            while sent < len(data):
                timeout = deadline - time.monotonic()
                if timeout <= 0 or not selector.select(timeout):
                    raise CaptureError("写入原生采集流请求超时")
                try:
                    written = os.write(stream.fileno(), data[sent:])
                except BlockingIOError:
                    continue
                except InterruptedError:
                    continue
                except BrokenPipeError as error:
                    raise CaptureError("原生采集流请求管道已关闭") from error
                except OSError as error:
                    raise CaptureError("无法写入原生采集流请求") from error
                if written == 0:
                    raise CaptureError("原生采集流请求管道意外停止")
                sent += written
        finally:
            try:
                selector.unregister(stream)
            except (KeyError, ValueError):
                pass

    @staticmethod
    def _read_available(stream, selector, deadline: float, byte_count: int) -> bytes:
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not selector.select(timeout):
            raise CaptureError("等待原生采集流超时")
        chunk = os.read(stream.fileno(), byte_count)
        if not chunk:
            raise CaptureError("原生采集流意外结束")
        return chunk

    @classmethod
    def _read_header(cls, stream, selector, deadline: float) -> tuple[bytes, bytes]:
        data = bytearray()
        while True:
            newline = data.find(b"\n")
            if newline >= 0:
                return bytes(data[: newline + 1]), bytes(data[newline + 1 :])
            if len(data) >= 4096:
                raise CaptureError("原生采集流响应头过长")
            data.extend(
                cls._read_available(
                    stream,
                    selector,
                    deadline,
                    4096 - len(data),
                )
            )

    @classmethod
    def _read_exact(
        cls,
        stream,
        selector,
        deadline: float,
        byte_count: int,
        buffered: bytes = b"",
    ) -> bytes:
        if len(buffered) > byte_count:
            raise CaptureError("原生采集流返回了不一致的图像长度")
        chunks = [buffered]
        remaining = byte_count - len(buffered)
        while remaining:
            chunk = cls._read_available(
                stream,
                selector,
                deadline,
                min(remaining, 64 * 1024),
            )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _restore_requested_capture(
        image: np.ndarray,
        requested: tuple[int, int, int, int],
        actual: tuple[float, float, float, float],
    ) -> np.ndarray:
        """Restore pixels clipped by ScreenCaptureKit at a display edge.

        SCScreenshotManager silently intersects an off-screen request with the
        active display. The old code still mapped the smaller returned bitmap
        across the original point rectangle, stretching every grid coordinate
        during recovery near a screen edge. Pad the missing area instead so
        pixel-to-point geometry remains invariant.
        """
        request_left, request_top, request_width, request_height = requested
        actual_left, actual_top, actual_width, actual_height = actual
        if actual_width <= 0 or actual_height <= 0:
            raise CaptureError("原生采集流返回了无效屏幕范围")
        scale_x = image.shape[1] / actual_width
        scale_y = image.shape[0] / actual_height
        target_width = max(1, round(request_width * scale_x))
        target_height = max(1, round(request_height * scale_y))
        left = max(0, round((actual_left - request_left) * scale_x))
        top = max(0, round((actual_top - request_top) * scale_y))
        right = max(0, target_width - left - image.shape[1])
        bottom = max(0, target_height - top - image.shape[0])
        if left == 0 and top == 0 and right == 0 and bottom == 0:
            return image
        restored = cv2.copyMakeBorder(
            image,
            top,
            bottom,
            left,
            right,
            cv2.BORDER_REPLICATE,
        )
        # Rounding at fractional display boundaries can differ by one pixel.
        return restored[:target_height, :target_width]

    def _stop_capture_process(self, cleanup_deadline: float | None = None) -> None:
        process = self._capture_process
        self._capture_process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        reap_error: subprocess.TimeoutExpired | None = None
        if process.poll() is None:
            process.terminate()
            deadline = cleanup_deadline or time.monotonic() + CAPTURE_CLEANUP_TIMEOUT
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=min(1.0, remaining / 2.0))
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    # Keep ownership so close() can retry; never silently lose
                    # a killed child that has not yet been reaped.
                    self._capture_process = process
                    reap_error = error
        if process.stdout is not None:
            process.stdout.close()
        if reap_error is not None:
            raise CaptureError("屏幕采集进程回收超时") from reap_error

    def close(self) -> None:
        with self._capture_lock:
            self._stop_capture_process()

    @staticmethod
    def _geometry(size: int) -> tuple[int, int, int, int, tuple[tuple[tuple[int, int], ...], ...]]:
        spacing = 42 if size == 19 else 48 if size == 13 else 54
        margin = spacing
        grid_extent = spacing * (size - 1)
        canvas_size = grid_extent + margin * 2 + 1
        intersections = tuple(
            tuple((margin + x * spacing, margin + y * spacing) for x in range(size)) for y in range(size)
        )
        return spacing, margin, grid_extent, canvas_size, intersections

    @classmethod
    def demo_board(cls, size: int) -> WarpedBoard:
        spacing, margin, grid_extent, canvas_size, intersections = cls._geometry(size)
        image = np.full((canvas_size, canvas_size, 3), (145, 181, 211), dtype=np.uint8)
        for index in range(size):
            position = margin + index * spacing
            cv2.line(image, (margin, position), (margin + grid_extent, position), (38, 45, 48), 1)
            cv2.line(image, (position, margin), (position, margin + grid_extent), (38, 45, 48), 1)
        return WarpedBoard(image, intersections, spacing, margin)

    @classmethod
    def warp_image(
        cls,
        raw: np.ndarray,
        bounds: tuple[int, int, int, int],
        quad: Quad,
        size: int,
    ) -> WarpedBoard:
        """Perspective-normalize a captured region or an offline screenshot."""
        left, top, width_points, height_points = bounds
        pixel_height, pixel_width = raw.shape[:2]
        scale_x, scale_y = pixel_width / width_points, pixel_height / height_points
        source = np.float32(
            [[(point.x - left) * scale_x, (point.y - top) * scale_y] for point in quad.points]
        )
        spacing, margin, grid_extent, canvas_size, intersections = cls._geometry(size)
        destination = np.float32(
            [
                [margin, margin],
                [margin + grid_extent, margin],
                [margin + grid_extent, margin + grid_extent],
                [margin, margin + grid_extent],
            ]
        )
        transform = cv2.getPerspectiveTransform(source, destination)
        warped = cv2.warpPerspective(
            raw,
            transform,
            (canvas_size, canvas_size),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return WarpedBoard(warped, intersections, spacing, margin)

    def capture_board(self, quad: Quad, size: int) -> WarpedBoard:
        if self.demo:
            return self.demo_board(size)
        bounds = quad.bounds()
        raw = self._capture_region(bounds)
        return self.warp_image(raw, bounds, quad, size)

    @staticmethod
    def _periodic_grid_axis(
        response: np.ndarray,
        line_count: int,
        target_spacing: float | None = None,
    ) -> tuple[float, float, float]:
        """Find an evenly spaced run of grid lines in a one-dimensional response."""
        length = int(response.shape[0])
        if length < line_count * 5:
            raise CaptureError("框选区域太小，无法识别完整棋盘")
        response = cv2.GaussianBlur(response.astype(np.float32).reshape(1, -1), (1, 1), 0).ravel()
        median = float(np.median(response))
        scale = float(np.percentile(response, 95) - median)
        normalized = (response - median) / max(1e-5, scale)
        # The earlier implementation sliced five pixels inside three nested
        # Python loops (span/start/line), making a verification refit take
        # seconds.  A 1-D dilation is exactly the same local max operation;
        # NumPy then scores every possible start for one span in a batch.
        local_max = cv2.dilate(
            normalized.reshape(1, -1),
            np.ones((1, 5), dtype=np.uint8),
        ).ravel()
        best: tuple[float, float, float] | None = None
        min_span = max(line_count - 1, round(length * 0.55))
        max_span = min(length - 1, round(length * 0.99))
        if target_spacing is not None:
            target_span = target_spacing * (line_count - 1)
            # The first axis may cover only half of a deliberately generous
            # screenshot while the orthogonal axis already tells us the real
            # grid spacing. Do not retain the 55%-of-image lower bound here:
            # it made the square-guided pass unable to recover such a board.
            min_span = max(line_count - 1, round(target_span * 0.91))
            max_span = min(length - 1, round(target_span * 1.09))
            if min_span > max_span:
                raise CaptureError("没有找到与另一方向等距的完整棋盘网格")
        line_indices = np.arange(line_count, dtype=np.float32)
        for span in range(min_span, max_span + 1):
            spacing = span / (line_count - 1)
            if spacing < 6:
                continue
            max_start = max(0, length - 1 - span)
            start_step = 1 if max_start < 90 else 2
            starts = np.arange(0, max_start + 1, start_step, dtype=np.float32)
            sample_points = np.rint(
                starts[:, None] + line_indices[None, :] * spacing
            ).astype(np.int32)
            sample_points = np.clip(sample_points, 0, length - 1)
            scores = np.mean(local_max[sample_points], axis=1)
            scores += 0.035 * span / length
            if target_spacing is not None:
                scores -= 0.18 * abs(spacing - target_spacing) / max(1.0, target_spacing)
            best_index = int(np.argmax(scores))
            score = float(scores[best_index])
            if best is None or score > best[2]:
                start = float(starts[best_index])
                best = (start, start + span, score)
        if best is None or best[2] < 0.20:
            raise CaptureError("框选区域内没有找到规则棋盘网格，请把整个棋盘框在矩形中")
        return best

    @classmethod
    def detect_grid_quad(
        cls,
        raw: np.ndarray,
        bounds: tuple[int, int, int, int],
        size: int,
    ) -> Quad:
        """Fit the outer grid intersections inside a user-dragged rectangle."""
        if raw is None or raw.size == 0:
            raise CaptureError("框选区域截图为空")
        gray = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY).astype(np.float32)

        def responses(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            vertical_blur = cv2.GaussianBlur(image, (9, 1), 0)
            horizontal_blur = cv2.GaussianBlur(image, (1, 9), 0)
            vertical = np.mean(np.abs(image - vertical_blur), axis=0)
            horizontal = np.mean(np.abs(image - horizontal_blur), axis=1)
            return vertical, horizontal

        def fit_square_axes(
            vertical_response: np.ndarray,
            horizontal_response: np.ndarray,
        ) -> tuple[float, float, float, float]:
            x = cls._periodic_grid_axis(vertical_response, size)
            y = cls._periodic_grid_axis(horizontal_response, size)
            candidates = [(x, y)]
            spacing_x = (x[1] - x[0]) / max(1, size - 1)
            spacing_y = (y[1] - y[0]) / max(1, size - 1)
            try:
                candidates.append(
                    (cls._periodic_grid_axis(vertical_response, size, spacing_y), y)
                )
            except CaptureError:
                pass
            try:
                candidates.append(
                    (x, cls._periodic_grid_axis(horizontal_response, size, spacing_x))
                )
            except CaptureError:
                pass

            def pair_score(candidate) -> float:
                axis_x, axis_y = candidate
                candidate_spacing_x = (axis_x[1] - axis_x[0]) / max(1, size - 1)
                candidate_spacing_y = (axis_y[1] - axis_y[0]) / max(1, size - 1)
                square_error = abs(
                    math.log(candidate_spacing_x / max(1e-5, candidate_spacing_y))
                )
                # A Go board is square in screen clients. A slightly stronger
                # run of wood grain or coordinate glyphs must not beat a pair
                # of mutually consistent horizontal and vertical grid periods.
                return axis_x[2] + axis_y[2] - 2.2 * square_error

            best_x, best_y = max(candidates, key=pair_score)
            return best_x[0], best_x[1], best_y[0], best_y[1]

        vertical, horizontal = responses(gray)
        x0, x1, y0, y1 = fit_square_axes(vertical, horizontal)

        # Refine each axis while averaging only across the detected grid body.
        yi0, yi1 = max(0, int(y0)), min(gray.shape[0], int(y1) + 1)
        xi0, xi1 = max(0, int(x0)), min(gray.shape[1], int(x1) + 1)
        vertical, _ = responses(gray[yi0:yi1, :])
        _, horizontal = responses(gray[:, xi0:xi1])
        x0, x1, y0, y1 = fit_square_axes(vertical, horizontal)

        spacing_x = (x1 - x0) / max(1, size - 1)
        spacing_y = (y1 - y0) / max(1, size - 1)
        spacing_ratio = spacing_x / max(1e-5, spacing_y)
        if not 0.82 <= spacing_ratio <= 1.22:
            raise CaptureError(
                "检测到的横纵网格间距不一致；请重新框住完整的正方形棋盘"
            )

        left, top, width_points, height_points = bounds
        scale_x = width_points / raw.shape[1]
        scale_y = height_points / raw.shape[0]
        screen_left = left + x0 * scale_x
        screen_right = left + x1 * scale_x
        screen_top = top + y0 * scale_y
        screen_bottom = top + y1 * scale_y
        quad = Quad(
            Point(screen_left, screen_top),
            Point(screen_right, screen_top),
            Point(screen_right, screen_bottom),
            Point(screen_left, screen_bottom),
        )
        quad.validate()
        return quad

    def locate_grid_quad(
        self,
        raw: np.ndarray,
        bounds: tuple[int, int, int, int],
        size: int,
    ) -> Quad:
        """Cross-check a full-grid fit with YOLO, or use YOLO only as recovery.

        The periodic fit determines exact intersections.  A neural box is
        never allowed to crop a valid full grid merely because its objectness
        is high; synthetic-domain detectors can be confidently wrong.
        """
        full_grid: Quad | None = None
        full_grid_error: Exception | None = None
        try:
            full_grid = self.detect_grid_quad(raw, bounds, size)
        except (CaptureError, ValueError) as error:
            full_grid_error = error
        if self.board_locator is None:
            self.locator_confidence = 0.0
            if full_grid is not None:
                return full_grid
            assert full_grid_error is not None
            raise full_grid_error
        detection = self.board_locator.detect(raw)
        if detection is None:
            self.locator_confidence = 0.0
            if full_grid is not None:
                return full_grid
            assert full_grid_error is not None
            raise full_grid_error

        def agrees_with_detection(grid: Quad) -> bool:
            left, top, width_points, height_points = bounds
            pixel_height, pixel_width = raw.shape[:2]
            grid_left, grid_top, grid_width, grid_height = grid.bounds(margin=0)
            grid_box = (
                (grid_left - left) * pixel_width / width_points,
                (grid_top - top) * pixel_height / height_points,
                (grid_left + grid_width - left) * pixel_width / width_points,
                (grid_top + grid_height - top) * pixel_height / height_points,
            )
            intersection_left = max(grid_box[0], detection.left)
            intersection_top = max(grid_box[1], detection.top)
            intersection_right = min(grid_box[2], detection.right)
            intersection_bottom = min(grid_box[3], detection.bottom)
            intersection_area = max(0.0, intersection_right - intersection_left) * max(
                0.0, intersection_bottom - intersection_top
            )
            grid_area = max(1.0, (grid_box[2] - grid_box[0]) * (grid_box[3] - grid_box[1]))
            detection_area = max(1.0, detection.width * detection.height)
            overlap = intersection_area / max(1.0, grid_area + detection_area - intersection_area)
            grid_center_x = (grid_box[0] + grid_box[2]) * 0.5
            grid_center_y = (grid_box[1] + grid_box[3]) * 0.5
            detection_center_x = (detection.left + detection.right) * 0.5
            detection_center_y = (detection.top + detection.bottom) * 0.5
            center_error = max(
                abs(detection_center_x - grid_center_x) / max(1.0, grid_box[2] - grid_box[0]),
                abs(detection_center_y - grid_center_y) / max(1.0, grid_box[3] - grid_box[1]),
            )
            size_error = max(
                abs(detection.width - (grid_box[2] - grid_box[0])) / max(1.0, grid_box[2] - grid_box[0]),
                abs(detection.height - (grid_box[3] - grid_box[1])) / max(1.0, grid_box[3] - grid_box[1]),
            )
            return overlap >= 0.70 and center_error <= 0.10 and size_error <= 0.18

        if full_grid is not None and agrees_with_detection(full_grid):
            self.locator_confidence = detection.confidence
            return full_grid

        # A wide user selection or recovery search can contain another
        # 19-periodic structure: wood grain, coordinate labels or an adjacent
        # UI panel. If the global periodic optimum disagrees with the coarse
        # neural board region, fit a second complete grid inside that region.
        # The neural box never becomes the final geometry; all four returned
        # edges still come from the independently measured 19 grid lines.
        padding = round(max(detection.width, detection.height) * 0.15)
        pixel_height, pixel_width = raw.shape[:2]
        x0 = max(0, detection.left - padding)
        y0 = max(0, detection.top - padding)
        x1 = min(pixel_width, detection.right + padding)
        y1 = min(pixel_height, detection.bottom + padding)
        cropped = raw[y0:y1, x0:x1]
        left, top, width_points, height_points = bounds
        cropped_bounds = (
            left + x0 * width_points / pixel_width,
            top + y0 * height_points / pixel_height,
            (x1 - x0) * width_points / pixel_width,
            (y1 - y0) * height_points / pixel_height,
        )
        try:
            neural_grid = self.detect_grid_quad(cropped, cropped_bounds, size)
            if agrees_with_detection(neural_grid):
                self.locator_confidence = detection.confidence
                return neural_grid
        except (CaptureError, ValueError):
            pass

        # The bundled ONNX locator currently has synthetic-domain metrics
        # only. On the supplied real Galaxy screenshot it was off by up to
        # 195 px despite reporting 98% objectness. A valid full-line fit remains
        # authoritative when the coarse model cannot independently reproduce
        # it; its confidence is deliberately discarded.
        self.locator_confidence = 0.0
        if full_grid is not None:
            return full_grid
        assert full_grid_error is not None
        raise full_grid_error

class BoardRegionTracker:
    """Long-lived board tracker with fast motion tracking and grid recovery.

    Template matching keeps normal frames inexpensive.  It is deliberately
    not the source of truth, however: a periodic full-grid fit removes slow
    drift, and a progressively wider recovery fit can reacquire a board after
    a larger window move or a temporary obstruction.  ``current_quad`` is
    therefore always the latest four-corner grid estimate that the UI can
    render as its live tracking frame.
    """

    # Template tracking already validates every frame. A foreground full-grid
    # refit is substantially slower, so run it less often and immediately when
    # confidence weakens instead of injecting a latency spike every few seconds.
    PERIODIC_REANCHOR_FRAMES = 60

    def __init__(self, controller: ScreenController, selection: Quad, size: int):
        self.controller = controller
        self.size = size
        self.match_score = 1.0
        self.anchor_score = 1.0
        self.last_shift = Point(0.0, 0.0)
        self.tracking_mode = "calibrated"
        self.consecutive_failures = 0
        self.frame_index = 0
        self.last_reanchor_frame = 0
        self.last_tracking_error = ""
        self.force_recovery = False
        self.alignment_failures = 0
        self.last_capture_used_fallback = False
        self.last_recovery_attempt_frame = -1000
        if controller.demo:
            self.template_margin = 8
            self.current_quad = selection
            self.anchor_quad = selection
            self.reference = controller.demo_board(size)
            self.template = self.reference.image
            self.anchor_template = self.template.copy()
            self.tracking_pixels_per_point = 1.0
            _, _, width, height = selection.bounds(margin=0)
            self.grid_spacing_points = min(width, height) / max(1, size - 1)
            return

        # Keep a small outside margin so the outer grid lines are not clipped
        # when a saved, already-corrected rectangle is calibrated again.
        selection_bounds = selection.bounds(margin=8)
        selection_raw = controller._capture_region(selection_bounds)
        self.current_quad = controller.locate_grid_quad(selection_raw, selection_bounds, size)
        self.anchor_quad = self.current_quad
        _, _, grid_width, grid_height = self.current_quad.bounds(margin=0)
        grid_spacing = min(grid_width, grid_height) / max(1, size - 1)
        self.grid_spacing_points = grid_spacing
        # Include coordinate labels and the outer wood border. Those features
        # are not periodic like grid intersections, so they anchor absolute
        # coordinates when the window moves by one or more grid spacings.
        self.template_margin = int(max(18, min(38, round(grid_spacing * 1.45))))
        template_bounds = self.current_quad.bounds(margin=self.template_margin)
        template_raw = controller._capture_region(template_bounds)
        self.reference = controller.warp_image(template_raw, template_bounds, self.current_quad, size)
        # Normalize tracking images by macOS screen *points*, not by the raw
        # screenshot pixel count. A search rectangle can cross a Retina/non-
        # Retina display boundary or be clipped at a display edge, and
        # ScreenCaptureKit may then return a different backing scale. Applying
        # the template's raw-pixel scale to that image made a physically larger
        # search area smaller than the template and raised a false size error.
        self.tracking_pixels_per_point = min(
            1.0,
            360.0 / max(template_bounds[2], template_bounds[3]),
        )
        self.template = self._tracking_image(template_raw, template_bounds)
        self.anchor_template = self.template.copy()

    def _tracking_image(
        self,
        image: np.ndarray,
        point_bounds: tuple[int, int, int, int],
    ) -> np.ndarray:
        target_width = max(8, round(point_bounds[2] * self.tracking_pixels_per_point))
        target_height = max(8, round(point_bounds[3] * self.tracking_pixels_per_point))
        if image.shape[1] != target_width or image.shape[0] != target_height:
            image = cv2.resize(
                image,
                (target_width, target_height),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (3, 3), 0)

    def _fast_capture(self) -> WarpedBoard:
        if self.controller.demo:
            self.match_score = 1.0
            self.anchor_score = 1.0
            self.last_shift = Point(0.0, 0.0)
            self.tracking_mode = "tracking"
            self.last_capture_used_fallback = False
            return self.controller.demo_board(self.size)

        _, _, width, height = self.current_quad.bounds(margin=0)
        search_margin = int(max(28, min(80, min(width, height) * 0.10)))
        search_bounds = self.current_quad.bounds(margin=search_margin + self.template_margin)
        raw = self.controller._capture_region(search_bounds)
        search = self._tracking_image(raw, search_bounds)
        if search.shape[0] < self.template.shape[0] or search.shape[1] < self.template.shape[1]:
            # A fully visible grid is still usable when the OS clips the
            # outside tracking margin. Do not prevent initial synchronization;
            # use the calibrated quad for this frame and retry tracking later.
            self.match_score = 0.0
            self.anchor_score = 0.0
            self.last_shift = Point(0.0, 0.0)
            self.tracking_mode = "degraded"
            self.last_capture_used_fallback = True
            return self.controller.capture_board(self.current_quad, self.size)
        adaptive_result = cv2.matchTemplate(search, self.template, cv2.TM_CCOEFF_NORMED)
        anchor_result = cv2.matchTemplate(search, self.anchor_template, cv2.TM_CCOEFF_NORMED)

        expected_bounds = self.current_quad.bounds(margin=self.template_margin)
        pixels_per_point_x = search.shape[1] / search_bounds[2]
        pixels_per_point_y = search.shape[0] / search_bounds[3]
        expected_x = (expected_bounds[0] - search_bounds[0]) * pixels_per_point_x
        expected_y = (expected_bounds[1] - search_bounds[1]) * pixels_per_point_y
        yy, xx = np.indices(adaptive_result.shape, dtype=np.float32)
        distance = np.sqrt((xx - expected_x) ** 2 + (yy - expected_y) ** 2)
        radius = max(1.0, search_margin * (pixels_per_point_x + pixels_per_point_y) * 0.5)
        combined = anchor_result * 0.68 + adaptive_result * 0.32
        scored = combined - 0.085 * distance / radius
        _, _, _, location = cv2.minMaxLoc(scored)
        location_x, location_y = self._subpixel_peak(scored, location)
        raw_score = float(combined[location[1], location[0]])
        anchor_score = float(anchor_result[location[1], location[0]])
        # Publish the actual failed scores as well. Keeping the previous 90%+
        # values made the recovery state machine repeatedly take the wrong
        # branch and hid the reason from the UI.
        self.match_score = raw_score
        self.anchor_score = anchor_score
        if raw_score < 0.30 or anchor_score < 0.24:
            raise BoardTrackingError(
                f"棋盘自动跟踪失败（综合 {raw_score:.0%}，锚点 {anchor_score:.0%}）；"
                "请确认没有被大面积遮挡，或重新拖选"
            )

        matched_left = search_bounds[0] + location_x / pixels_per_point_x
        matched_top = search_bounds[1] + location_y / pixels_per_point_y
        dx = matched_left - expected_bounds[0]
        dy = matched_top - expected_bounds[1]
        maximum_fast_shift = max(6.0, self.grid_spacing_points * 0.48)
        if abs(dx) > maximum_fast_shift or abs(dy) > maximum_fast_shift:
            # A Go grid is periodic, so template matching can produce a strong
            # score exactly one intersection away. Never install such a jump
            # directly. The full-grid recovery path must first confirm all four
            # outer intersections.
            raise BoardTrackingError(
                f"局部跟踪疑似跳格（{dx:.1f}, {dy:.1f} px），正在用完整网格复核"
            )
        # Resolve the match as an absolute displacement from the last
        # full-grid anchor. The previous implementation added every residual
        # to ``current_quad``. A stable 1-2 px bias from Retina downsampling,
        # stones or a last-move marker was therefore integrated into a random
        # walk and the visible frame slowly left the real board.
        dx, dy = self._apply_absolute_fast_match(
            matched_left,
            matched_top,
            pixels_per_point_x,
            pixels_per_point_y,
        )
        self.match_score = raw_score
        self.anchor_score = anchor_score
        self.last_shift = Point(dx, dy)
        self.tracking_mode = "tracking"
        self.last_capture_used_fallback = False
        matched_patch = search[
            location[1] : location[1] + self.template.shape[0],
            location[0] : location[0] + self.template.shape[1],
        ]
        # Adapt to newly added stones and last-move markers slowly. Geometry is
        # always resolved from ``anchor_quad`` above, so this appearance model
        # can no longer move the reference frame cumulatively.
        if matched_patch.shape == self.template.shape and raw_score >= 0.48 and anchor_score >= 0.40:
            self.template = cv2.addWeighted(self.template, 0.88, matched_patch, 0.12, 0)
        return self.controller.warp_image(raw, search_bounds, self.current_quad, self.size)

    def _apply_absolute_fast_match(
        self,
        matched_left: float,
        matched_top: float,
        pixels_per_point_x: float,
        pixels_per_point_y: float,
    ) -> tuple[float, float]:
        """Install a local match without accumulating stationary noise.

        ``matched_left``/``matched_top`` describe the on-screen origin of the
        template.  Relating that origin to the last independently fitted grid
        makes every frame an absolute measurement. Repeating the same biased
        measurement can move the frame once, but never once per scan.
        """
        anchor_bounds = self.anchor_quad.bounds(margin=self.template_margin)
        measured = self.anchor_quad.translated(
            matched_left - anchor_bounds[0],
            matched_top - anchor_bounds[1],
        )
        current_center = self._quad_center(self.current_quad, self.size)
        measured_center = self._quad_center(measured, self.size)
        correction_x = measured_center.x - current_center.x
        correction_y = measured_center.y - current_center.y

        # The subpixel parabola is clipped to 0.75 tracker pixels and image
        # resizing contributes a little more uncertainty. A 1.15-pixel dead
        # zone removes that noise. Slow, genuine movement is still detected as
        # an absolute displacement once it crosses the threshold.
        dead_zone_x = 1.15 / max(0.1, pixels_per_point_x)
        dead_zone_y = 1.15 / max(0.1, pixels_per_point_y)
        applied_x = 0.0 if abs(correction_x) <= dead_zone_x else correction_x
        applied_y = 0.0 if abs(correction_y) <= dead_zone_y else correction_y
        self.current_quad = self.current_quad.translated(applied_x, applied_y)
        return applied_x, applied_y

    def _capture_at_last_verified_quad(self, *, recovering: bool) -> WarpedBoard:
        """Keep recognition alive while an independent relock is pending.

        A low template correlation is not proof that the board moved. Stones,
        hover markers and client animations all alter appearance while the
        calibrated grid often remains exactly where it was. The recognition
        layer performs a grid-position check on this fallback frame before it
        is allowed to commit any state.
        """
        self.last_shift = Point(0.0, 0.0)
        self.last_capture_used_fallback = True
        self.tracking_mode = "fallback"
        return self.controller.capture_board(self.current_quad, self.size)

    @staticmethod
    def _subpixel_peak(response: np.ndarray, location: tuple[int, int]) -> tuple[float, float]:
        """Refine an integer template peak with a local quadratic fit."""
        x, y = location

        def offset(before: float, center: float, after: float) -> float:
            denominator = before - 2.0 * center + after
            if abs(denominator) < 1e-6:
                return 0.0
            return max(-0.75, min(0.75, 0.5 * (before - after) / denominator))

        dx = 0.0
        dy = 0.0
        if 0 < x < response.shape[1] - 1:
            dx = offset(float(response[y, x - 1]), float(response[y, x]), float(response[y, x + 1]))
        if 0 < y < response.shape[0] - 1:
            dy = offset(float(response[y - 1, x]), float(response[y, x]), float(response[y + 1, x]))
        return x + dx, y + dy

    def capture(self) -> WarpedBoard:
        """Capture one normalized board frame while maintaining lock.

        The common path follows small translations with template matching.
        Every few seconds (or after confidence weakens) a full grid is fitted
        again.  A failed fast match immediately switches to a progressively
        wider recovery search instead of forcing the user to stop monitoring.
        """
        self.frame_index += 1
        if self.controller.demo:
            return self._fast_capture()

        if self.force_recovery:
            # Recovery used to be sticky: once an appearance check failed,
            # every future frame ran only the slower full-grid search. A dense
            # but stationary board could therefore remain in recovery forever
            # even though its adaptive and anchor templates still matched at
            # 95%+. Always give the inexpensive local lock the first chance to
            # recover normal real-time scanning.
            try:
                warped = self._fast_capture()
                self.consecutive_failures = 0
                self.alignment_failures = 0
                self.last_tracking_error = ""
                self.force_recovery = False
                self.tracking_mode = "tracking"
                return warped
            except (BoardTrackingError, CaptureError, ValueError) as error:
                self.consecutive_failures = max(1, self.consecutive_failures + 1)
                self.last_tracking_error = str(error)
                if self.frame_index - self.last_recovery_attempt_frame >= 4:
                    self.last_recovery_attempt_frame = self.frame_index
                    try:
                        return self.recover()
                    except (BoardTrackingError, CaptureError, ValueError) as recovery_error:
                        self.last_tracking_error = str(recovery_error)
                return self._capture_at_last_verified_quad(recovering=True)

        reanchor_due = self.frame_index - self.last_reanchor_frame >= self.PERIODIC_REANCHOR_FRAMES
        confidence_weak = self.match_score < 0.42 or self.anchor_score < 0.34
        weak_reanchor_due = confidence_weak and self.frame_index - self.last_reanchor_frame >= 8
        if reanchor_due or weak_reanchor_due:
            try:
                return self.periodic_reanchor()
            except (BoardTrackingError, CaptureError, ValueError) as error:
                # The board may be dense enough to hide several grid lines.
                # Keep the inexpensive tracker alive and only enter recovery
                # if that independent path also fails.
                self.last_tracking_error = str(error)
                # Do not run the expensive full-grid fit on every 120 ms frame
                # after one rejected correction.
                self.last_reanchor_frame = self.frame_index

        try:
            warped = self._fast_capture()
            self.consecutive_failures = 0
            self.last_tracking_error = ""
            self.force_recovery = False
            return warped
        except (BoardTrackingError, CaptureError, ValueError) as error:
            self.consecutive_failures += 1
            self.last_tracking_error = str(error)
            if self.consecutive_failures < 2:
                return self._capture_at_last_verified_quad(recovering=False)
            if self.frame_index - self.last_recovery_attempt_frame >= 4:
                self.last_recovery_attempt_frame = self.frame_index
                try:
                    return self.recover()
                except (BoardTrackingError, CaptureError, ValueError) as recovery_error:
                    self.last_tracking_error = str(recovery_error)
            return self._capture_at_last_verified_quad(recovering=True)

    @staticmethod
    def _quad_center(quad: Quad, size: int) -> Point:
        middle = (size - 1) // 2
        return quad.screen_point(middle, middle, size)

    def _install_detected_quad(
        self,
        detected: Quad,
        raw: np.ndarray,
        capture_bounds: tuple[int, int, int, int],
        *,
        refresh_anchor: bool,
        mode: str,
        blend_factor: float = 1.0,
    ) -> WarpedBoard:
        old_center = self._quad_center(self.current_quad, self.size)
        installed = self.current_quad.interpolated(detected, blend_factor)
        new_center = self._quad_center(installed, self.size)
        self.last_shift = Point(new_center.x - old_center.x, new_center.y - old_center.y)
        self.current_quad = installed
        self.tracking_mode = mode
        self.last_reanchor_frame = self.frame_index
        self.consecutive_failures = 0
        self.last_tracking_error = ""
        self.force_recovery = False
        self.alignment_failures = 0
        self.last_capture_used_fallback = False

        # A successful geometric refit is an absolute-coordinate anchor, so
        # it is safe to refresh both templates. This is essential in long
        # games: an immutable empty-board image eventually becomes less like
        # the real board than a wrong nearby patch.
        template_bounds = installed.bounds(margin=self.template_margin)
        left, top, width_points, height_points = capture_bounds
        scale_x = raw.shape[1] / max(1, width_points)
        scale_y = raw.shape[0] / max(1, height_points)
        x0 = max(0, round((template_bounds[0] - left) * scale_x))
        y0 = max(0, round((template_bounds[1] - top) * scale_y))
        x1 = min(raw.shape[1], round((template_bounds[0] + template_bounds[2] - left) * scale_x))
        y1 = min(raw.shape[0], round((template_bounds[1] + template_bounds[3] - top) * scale_y))
        patch = raw[y0:y1, x0:x1]
        if patch.size:
            refreshed = self._tracking_image(patch, template_bounds)
            self.template = refreshed
            if refresh_anchor:
                self.anchor_template = refreshed.copy()
                self.anchor_quad = installed

        # Grid fitting has no template correlation score. Use a conservative
        # geometric confidence, optionally raised by the independent YOLO
        # locator when it agreed with the grid.
        geometric_score = max(0.68, float(self.controller.locator_confidence))
        self.match_score = geometric_score
        self.anchor_score = geometric_score
        return self.controller.warp_image(raw, capture_bounds, installed, self.size)

    def mark_alignment_failure(self, message: str) -> None:
        """Use hysteresis before treating one weak grid frame as lost."""
        self.alignment_failures += 1
        required = 3 if min(self.match_score, self.anchor_score) >= 0.42 else 2
        self.force_recovery = self.alignment_failures >= required
        self.consecutive_failures = max(1, self.consecutive_failures)
        self.tracking_mode = "recovering" if self.force_recovery else "degraded"
        self.last_tracking_error = message
        if self.force_recovery:
            self.match_score *= 0.72
            self.anchor_score *= 0.72

    def mark_analysis_success(self) -> None:
        self.alignment_failures = 0
        self.force_recovery = False
        if (
            self.tracking_mode == "degraded"
            and not self.last_capture_used_fallback
            and self.match_score >= 0.30
            and self.anchor_score >= 0.24
        ):
            self.tracking_mode = "tracking"
        self.last_tracking_error = ""

    def _refit(
        self,
        *,
        search_margin: int,
        maximum_scale_change: float,
        refresh_anchor: bool,
        mode: str,
        maximum_center_shift: float | None = None,
        update_tracking: bool = True,
        blend_factor: float = 1.0,
    ) -> WarpedBoard:
        _, _, width, height = self.current_quad.bounds(margin=0)
        search_bounds = self.current_quad.bounds(margin=search_margin)
        raw = self.controller._capture_region(search_bounds)
        detected = self.controller.locate_grid_quad(raw, search_bounds, self.size)
        _, _, detected_width, detected_height = detected.bounds(margin=0)
        scale_change = max(
            abs(detected_width - width) / max(1, width),
            abs(detected_height - height) / max(1, height),
        )
        if scale_change > maximum_scale_change:
            raise BoardTrackingError(
                f"自动重定位发现棋盘缩放变化 {scale_change:.0%}，暂不采用该位置"
            )
        old_center = self._quad_center(self.current_quad, self.size)
        new_center = self._quad_center(detected, self.size)
        center_shift = math.hypot(new_center.x - old_center.x, new_center.y - old_center.y)
        if maximum_center_shift is not None and center_shift > maximum_center_shift:
            raise BoardTrackingError(
                f"周期网格校正跳变 {center_shift:.1f} px，已保留当前稳定位置"
            )
        if (
            abs(new_center.x - old_center.x) > search_margin * 0.98
            or abs(new_center.y - old_center.y) > search_margin * 0.98
        ):
            raise BoardTrackingError("自动重定位结果位于搜索范围边缘，正在扩大范围复核")
        if not update_tracking:
            return self.controller.warp_image(raw, search_bounds, detected, self.size)
        return self._install_detected_quad(
            detected,
            raw,
            search_bounds,
            refresh_anchor=refresh_anchor,
            mode=mode,
            blend_factor=blend_factor,
        )

    def periodic_reanchor(self) -> WarpedBoard:
        """Remove accumulated template drift without interrupting monitoring."""
        if self.controller.demo:
            return self.controller.demo_board(self.size)
        _, _, width, height = self.current_quad.bounds(margin=0)
        margin = int(max(58, min(150, min(width, height) * 0.22)))
        return self._refit(
            search_margin=margin,
            maximum_scale_change=0.18,
            refresh_anchor=True,
            mode="reanchored",
            maximum_center_shift=max(8.0, self.grid_spacing_points * 0.72),
            blend_factor=1.0,
        )

    def relock_for_manual_baseline(self) -> WarpedBoard:
        """Authoritatively reacquire the grid with two wide, consistent fits.

        Automatic reanchoring must reject a displacement close to one grid
        period because a periodic line pattern can otherwise move every stone
        to the neighbouring coordinate.  That same guard cannot be used for
        an explicit re-recognition or an escalated automatic snapshot recovery:
        by then, the remembered quad may itself be the stale value that needs
        replacing.

        Search more widely and fit the complete grid twice without mutating
        tracker state.  Only two geometrically consistent candidates may
        replace the quad and both appearance templates.  This keeps the
        anti-jump protection on the automatic path while allowing a manual
        recovery to escape an already drifted lock.
        """
        if self.controller.demo:
            self.tracking_mode = "recovered"
            return self.controller.demo_board(self.size)

        _, _, current_width, current_height = self.current_quad.bounds(margin=0)
        board_extent = min(current_width, current_height)
        search_margin = int(max(110, min(360, board_extent * 0.40)))
        search_bounds = self.current_quad.bounds(margin=search_margin)

        def detect_candidate() -> tuple[Quad, np.ndarray]:
            raw = self.controller._capture_region(search_bounds)
            detected = self.controller.locate_grid_quad(raw, search_bounds, self.size)
            _, _, detected_width, detected_height = detected.bounds(margin=0)
            scale_change = max(
                abs(detected_width - current_width) / max(1, current_width),
                abs(detected_height - current_height) / max(1, current_height),
            )
            if scale_change > 0.35:
                raise BoardTrackingError(
                    f"宽范围重新识别发现棋盘缩放变化 {scale_change:.0%}；"
                    "请确认棋盘完整可见，必要时重新框选"
                )
            old_center = self._quad_center(self.current_quad, self.size)
            new_center = self._quad_center(detected, self.size)
            if (
                abs(new_center.x - old_center.x) > search_margin * 0.98
                or abs(new_center.y - old_center.y) > search_margin * 0.98
            ):
                raise BoardTrackingError(
                    "宽范围重新识别结果位于搜索范围边缘；请保持棋盘无遮挡后重试"
                )
            return detected, raw

        first, _ = detect_candidate()
        second, second_raw = detect_candidate()
        first_center = self._quad_center(first, self.size)
        second_center = self._quad_center(second, self.size)
        center_disagreement = math.hypot(
            second_center.x - first_center.x,
            second_center.y - first_center.y,
        )
        corner_disagreement = max(
            math.hypot(second_point.x - first_point.x, second_point.y - first_point.y)
            for first_point, second_point in zip(first.points, second.points)
        )
        center_tolerance = max(5.0, self.grid_spacing_points * 0.18)
        corner_tolerance = max(7.0, self.grid_spacing_points * 0.24)
        if center_disagreement > center_tolerance or corner_disagreement > corner_tolerance:
            raise BoardTrackingError(
                "宽范围重新识别的两次完整网格定位不一致"
                f"（中心 {center_disagreement:.1f} px，角点 {corner_disagreement:.1f} px）；"
                "已保留原位置，请保持棋盘静止后重试"
            )

        confirmed = first.interpolated(second, 0.5)
        return self._install_detected_quad(
            confirmed,
            second_raw,
            search_bounds,
            refresh_anchor=True,
            mode="recovered",
            blend_factor=1.0,
        )

    def relock_for_snapshot(self) -> WarpedBoard:
        """Refit all outer intersections before trusting a whole-board state.

        Template matching is intentionally optimized for inexpensive frame-to-
        frame translation.  A small scale/corner error can nevertheless stay
        highly correlated while moving the outer sampling patches onto row or
        column labels.  That failure produces a very characteristic strip of
        false stones and, because it is stable, temporal voting alone cannot
        reject it.  Full-position replacement and manual re-recognition are
        rare enough to pay for an independent full-grid fit and install it
        without interpolation.
        """
        if self.controller.demo:
            return self.controller.demo_board(self.size)
        _, _, width, height = self.current_quad.bounds(margin=0)
        board_extent = min(width, height)
        search_margin = int(max(72, min(180, board_extent * 0.24)))
        return self._refit(
            search_margin=search_margin,
            maximum_scale_change=0.24,
            refresh_anchor=True,
            mode="verified",
            maximum_center_shift=max(8.0, self.grid_spacing_points * 0.72),
            update_tracking=True,
            blend_factor=1.0,
        )

    def recover(self) -> WarpedBoard:
        """Progressively widen the full-grid search after tracking is lost."""
        if self.controller.demo:
            return self.controller.demo_board(self.size)
        _, _, width, height = self.current_quad.bounds(margin=0)
        board_extent = min(width, height)
        growth = 0.10 * min(5, max(1, self.consecutive_failures))
        margin = int(max(100, min(340, board_extent * (0.24 + growth))))
        self.tracking_mode = "recovering"
        try:
            return self._refit(
                search_margin=margin,
                maximum_scale_change=0.30,
                refresh_anchor=True,
                mode="recovered",
            )
        except (CaptureError, ValueError) as error:
            self.last_tracking_error = str(error)
            raise BoardTrackingError(
                f"正在自动找回棋盘（第 {self.consecutive_failures} 次，搜索 ±{margin} px）；"
                "请暂时保持棋盘无遮挡"
            ) from error

    def reanchor(self) -> WarpedBoard:
        """Independently refit the full grid before committing a detected move."""
        if self.controller.demo:
            return self.controller.demo_board(self.size)
        _, _, width, height = self.current_quad.bounds(margin=0)
        search_margin = int(max(52, min(115, min(width, height) * 0.18)))
        return self._refit(
            search_margin=search_margin,
            maximum_scale_change=0.065,
            refresh_anchor=False,
            mode="verified",
            maximum_center_shift=max(4.0, self.grid_spacing_points * 0.48),
            update_tracking=False,
        )
