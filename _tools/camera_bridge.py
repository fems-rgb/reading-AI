"""把电脑摄像头（或一路 RTSP 流）变成局域网可访问的 HTTP 摄像头。

背景：机器人上的取图面板，网络摄像头只认「一个返回 JPEG 的 http 地址」。
电脑的 USB / 内置摄像头只能被本机程序打开，ESP32-CAM 之外的老式机器人摄像头
也常常只给 RTSP。这个脚本在本机拉流、用 HTTP 吐出来，于是它们都能接进来：

    # 本机默认摄像头
    python _tools/camera_bridge.py
    # 外接 USB 摄像头（通常是 1）
    python _tools/camera_bridge.py --index 1 --size 1280x720
    # 只支持 RTSP 的机器人/网络摄像头
    python _tools/camera_bridge.py --url rtsp://192.168.1.60:554/stream1

启动后终端会打印抓帧地址，在取图面板里选「网络摄像头」把它填进去即可。

接口：
    GET /capture   → 一张 JPEG（抓帧用，推荐）
    GET /shot.jpg  → 同上（兼容手机 IP Webcam 的习惯写法）
    GET /stream    → MJPEG 流（只给预览用；抓帧仍走 /capture）
    GET /          → 自检页：显示设备、分辨率、可用地址

只依赖 OpenCV，不向园外发任何请求。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from http import server

try:
    import cv2
except ImportError:  # pragma: no cover - 只在没装依赖时触发
    sys.exit("这个工具需要 OpenCV：pip install opencv-python")


def lan_ip() -> str:
    """取本机在局域网里的地址（UDP connect 不会真的发包）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 9))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Camera:
    """一路视频源：可以是本机摄像头序号，也可以是 RTSP/HTTP 流地址。"""

    def __init__(self, source, size: tuple[int, int], fps: int):
        self.source = source
        self.size = size
        self.fps = fps
        self.lock = threading.Lock()
        self.cap = None
        self.fails = 0
        self._open()

    def _open(self) -> None:
        if isinstance(self.source, int) and sys.platform.startswith("win"):
            cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)  # Windows 上打开快很多
        else:
            cap = cv2.VideoCapture(self.source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if not cap.isOpened():
            cap.release()
            self.cap = None
            return
        self.cap = cap
        self.fails = 0

    def _read(self):
        if self.cap is None:
            self._open()
            if self.cap is None:
                return None
        for _ in range(3):  # USB 摄像头刚打开的前两帧经常是空的
            ok, frame = self.cap.read()
            if ok and frame is not None:
                self.fails = 0
                return frame
        self.fails += 1
        if self.fails >= 5:  # 拔了/被别的程序抢走：关掉，下一拍重开
            self.cap.release()
            self.cap = None
        return None

    def jpeg(self) -> bytes | None:
        with self.lock:
            frame = self._read()
        if frame is None:
            return None
        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return buffer.tobytes() if ok else None

    def describe(self) -> str:
        if self.cap is None:
            return "未打开"
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return f"{width}x{height}"


class Handler(server.BaseHTTPRequestHandler):
    camera: Camera
    base_url: str = ""
    server_version = "CameraBridge/1.0"

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/capture", "/shot.jpg", "/snapshot"):
            self._jpeg()
        elif path == "/stream":
            self._stream()
        elif path in ("/", "/index.html"):
            self._index()
        else:
            self.send_error(404, "only /capture /shot.jpg /stream")

    def _jpeg(self) -> None:
        body = self.camera.jpeg()
        if not body:
            self.send_error(503, "camera not ready")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        gap = 1.0 / max(1, min(self.camera.fps, 10))
        try:
            while True:
                body = self.camera.jpeg()
                if not body:
                    time.sleep(0.5)
                    continue
                head = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                )
                self.wfile.write(head + body + b"\r\n")
                time.sleep(gap)
        except (BrokenPipeError, ConnectionResetError):
            return  # 面板关掉了，正常结束

    def _index(self) -> None:
        base = self.base_url
        html = f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摄像头桥接</title>
<body style="font-family:system-ui;margin:24px;line-height:1.7">
<h3>摄像头桥接已就绪</h3>
<p>设备：{self.camera.describe()}　帧率上限：{self.camera.fps}</p>
<p>抓帧地址（填进取图面板）：<code>{base}/capture</code></p>
<p>预览地址（只给 &lt;img&gt; 看）：<code>{base}/stream</code></p>
<p><img src="/stream" alt="预览" style="max-width:100%;border-radius:12px;background:#111"></p>
</body>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: D401 - 终端只留启动信息，别刷屏
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="把电脑摄像头/RTSP 流变成局域网 HTTP 摄像头")
    parser.add_argument("--index", type=int, default=None, help="本机摄像头序号（0 默认，外接 USB 常是 1）")
    parser.add_argument("--url", default="", help="也可以直接给流地址，如 rtsp://192.168.1.60:554/stream1")
    parser.add_argument("--port", type=int, default=8081, help="监听端口，默认 8081")
    parser.add_argument("--size", default="1280x720", help="采集分辨率，如 1280x720")
    parser.add_argument("--fps", type=int, default=10, help="抓帧/MJPEG 的帧率上限，默认 10")
    args = parser.parse_args()

    try:
        width, height = (int(part) for part in args.size.lower().split("x"))
    except ValueError:
        parser.error("--size 要写成 1280x720 这种格式")

    source = args.url.strip() if args.url.strip() else (args.index if args.index is not None else 0)
    camera = Camera(source, (width, height), args.fps)
    if camera.cap is None:
        hint = "RTSP 地址不通" if isinstance(source, str) else f"打不开摄像头 {source}：可能被别的程序占用，或者没有这台设备（试 --index 1）"
        sys.exit(hint)

    base = f"http://{lan_ip()}:{args.port}"
    Handler.camera = camera
    Handler.base_url = base
    httpd = server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"视频源 {source} 已打开：{camera.describe()}，帧率上限 {args.fps}")
    print(f"抓帧地址 {base}/capture   （在机器人取图面板里选「网络摄像头」填这个）")
    print(f"预览地址 {base}/stream")
    print("Ctrl+C 结束。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        if camera.cap is not None:
            camera.cap.release()


if __name__ == "__main__":
    main()
