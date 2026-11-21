"""本机 USB / 内置摄像头直连取图。

机器人端不用再单独跑 camera_bridge：地址写 camera:0 就由服务端开摄像头拍一帧。
句柄按编号缓存（首次打开约 1 秒，之后取帧几十毫秒），只在同一把锁里读，
避免两个浏览器同时抢同一个设备。
"""

from __future__ import annotations

import re
import threading

_HANDLES: dict[int, object] = {}
_LOCK = threading.Lock()
_PATTERN = re.compile(r"^camera:(\d{1,2})$", re.IGNORECASE)

MAX_INDEX = 9


def camera_index(url: str) -> int | None:
    """camera:0 → 0；不是本机摄像头地址就返回 None。"""
    match = _PATTERN.match((url or "").strip())
    if not match:
        return None
    index = int(match.group(1))
    return index if 0 <= index <= MAX_INDEX else None


def capture(index: int) -> bytes:
    """拍一帧 JPEG。失败抛 RuntimeError（由接口转成给老师看的中文提示）。"""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("本机摄像头需要 opencv-python：pip install opencv-python") from exc

    with _LOCK:
        capture_device = _HANDLES.get(index)
        if capture_device is None or not capture_device.isOpened():
            if capture_device is not None:
                capture_device.release()
            # Windows 上用 DirectShow 打开更快；其他系统退回默认后端
            backend = getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY)
            capture_device = cv2.VideoCapture(index, backend)
            capture_device.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            capture_device.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            _HANDLES[index] = capture_device
        frame = None
        for _ in range(4):  # 刚打开的摄像头前几帧常是黑的
            ok, frame = capture_device.read()
            if ok:
                break
        if frame is None:
            _HANDLES.pop(index, None)
            capture_device.release()
            raise RuntimeError(f"camera:{index} 没有画面，检查摄像头是否被别的程序占用")
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("这一帧编码失败，再拍一次")
        return bytes(buffer.tobytes())


def release_all() -> None:
    with _LOCK:
        for device in _HANDLES.values():
            try:
                device.release()
            except Exception:
                pass
        _HANDLES.clear()
