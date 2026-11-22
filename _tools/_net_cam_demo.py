"""实拍验证「网络摄像头」通道：本机起一个模拟 ESP32-CAM 的 JPEG 服务，
再用真实浏览器从取图面板连上去、抓帧、提交、拿到提问；顺带验证：

1. 手动连接 → 预览 → 抓帧 → 用这张 → 服务端识别 → 提问（一轮完整链路）；
2. 自动翻页：画面在连上 6 秒后换一页，不点任何按钮，应自动拍下并自动提交；
3. 自检页的「网络摄像头」试连；
4. _tools/camera_bridge.py：用一段本地 AVI 当视频源，验证它能吐出 JPEG；
5. 只给 MJPEG 流的摄像头（/stream）与要登录的摄像头（账号密码写进地址，Basic）。

用法：先 python run.py，再 python _tools\\_net_cam_demo.py
"""

from __future__ import annotations

import base64
import http.server
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _demo_shot import page_image  # noqa: E402  复用同一页合成绘本

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_tools" / "shots"
BASE = "http://127.0.0.1:8000"
# 口令从 .env 读，不能在脚本里写死：真实部署的老师口令不是默认值
sys.path.insert(0, str(ROOT))
from app.config import get_settings  # noqa: E402

SETTINGS = get_settings()
PIN = SETTINGS.teacher_pin
CAM_PORT = 8091
CAM_URL = f"http://127.0.0.1:{CAM_PORT}/capture"
MJPEG_URL = f"http://127.0.0.1:{CAM_PORT}/stream"
BASIC_URL = f"http://cam:secret@127.0.0.1:{CAM_PORT}/auth"

STATE = {"page": 0, "switch_at": 0.0, "hits": 0}


def jpeg_bytes(index: int) -> bytes:
    image = page_image()
    if index % 2 == 1:  # 第二页：换个标题色块，帧差足够大
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        draw.rectangle([60, 430, 520, 700], fill=(120, 150, 210))
        draw.rectangle([560, 470, 680, 590], fill=(255, 240, 120))
    from io import BytesIO

    buffer = BytesIO()
    image.resize((720, 960)).save(buffer, format="JPEG", quality=86)
    return buffer.getvalue()


class FakeEsp32(http.server.BaseHTTPRequestHandler):
    """/capture 返回一张 JPEG；到点后自动换页（给自动翻页用）。"""

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/stream":  # 只给 MJPEG 流的廉价摄像头
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for _ in range(300):
                    body = jpeg_bytes(STATE["page"])
                    head = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(head + body + b"\r\n")
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        if path not in ("/capture", "/shot.jpg", "/auth"):
            self.send_error(404)
            return
        if path == "/auth":  # 要网页登录的摄像头：账号密码写在地址里
            expected = "Basic " + base64.b64encode(b"cam:secret").decode()
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="cam"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if STATE["switch_at"] and time.monotonic() >= STATE["switch_at"]:
            STATE["page"] = 1
        STATE["hits"] += 1
        body = jpeg_bytes(STATE["page"])
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401
        return


def start_fake_camera() -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", CAM_PORT), FakeEsp32)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check_bridge() -> dict:
    """camera_bridge.py 用一段本地 AVI 当源，GET /capture 应拿到 JPEG。"""
    source = SHOTS / "bridge_source.avi"
    SHOTS.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 10, (640, 480))
    frame = cv2.imdecode(np.frombuffer(jpeg_bytes(0), dtype="uint8"), cv2.IMREAD_COLOR)
    probe = cv2.resize(frame, (640, 480))
    for _ in range(120):
        writer.write(probe)
    writer.release()

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "_tools" / "camera_bridge.py"), "--url", str(source), "--port", "8092"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = {"ok": False, "detail": ""}
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8092/capture", timeout=3) as response:
                    body = response.read()
                if response.status == 200 and body[:2] == b"\xff\xd8" and response.headers.get("content-type", "").startswith("image/jpeg"):
                    result = {"ok": True, "detail": f"{len(body)}B jpeg"}
                    break
            except Exception:  # noqa: BLE001 - 启动中，继续等
                time.sleep(0.6)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return result


def wait_connected(page, timeout: int = 20000) -> None:
    page.wait_for_function(
        "document.getElementById('conn-text').textContent.trim() === '已连接'", timeout=timeout
    )


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    server = start_fake_camera()
    print("模拟 ESP32-CAM:", CAM_URL)
    bridge = check_bridge()
    print("camera_bridge:", bridge)

    report: dict = {"shots": [], "console_errors": [], "bridge": bridge}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 860}, locale="zh-CN")

        def watch(page) -> None:
            page.on(
                "console",
                lambda msg: report["console_errors"].append(f"[console] {msg.text}")
                if msg.type == "error"
                else None,
            )
            page.on("pageerror", lambda exc: report["console_errors"].append(f"[pageerror] {exc}"))

        api = context.request

        def post_json(path: str, payload: dict):
            return api.post(
                f"{BASE}{path}",
                data=json.dumps(payload, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
            )

        assert post_json("/api/login", {"pin": PIN}).ok
        children = api.get(f"{BASE}/api/teacher/children").json()["items"]
        books = api.get(f"{BASE}/api/teacher/books").json()["items"]
        assert books, "库里没有绘本：先在教师端建一本再跑这个验证"
        # 真实部署没有开发期的“小雨”，找不到就自己建一个验证孩子，
        # 跑完连同 4 个会话一起删掉，不给园里留测试数据。
        target = next((item for item in children if item["name"] == "小雨"), None)
        own_child = target is None
        if own_child:
            made = post_json("/api/teacher/children", {
                "name": "网络摄像头验证",
                "age_years": 4.5,
                "class_name": "验证班",
                "guardian_name": "验证家长",
                "guardian_consent": True,
            })
            assert made.ok, made.text()
            children = api.get(f"{BASE}/api/teacher/children").json()["items"]
            target = next(item for item in children if item["name"] == "网络摄像头验证")
        child = target

        def new_session() -> tuple[str, str, str]:
            created = post_json(
                "/api/teacher/sessions", {"child_id": child["id"], "book_id": books[0]["id"]}
            ).json()
            return created["session_id"], created["token"], BASE + created["robot_url"]

        def open_net_panel(page) -> None:
            page.click("#main-btn")
            page.wait_for_selector(".src-mask", state="visible")
            page.click('[data-kind="net"]')
            page.wait_for_selector("#src-net", state="visible")

        # ---- 1. 自检页试连 ----
        session_id, _token, robot_url = new_session()
        check = context.new_page()
        watch(check)
        check.goto(f"{BASE}/device-check", wait_until="networkidle")
        check.fill("#netcam-url", CAM_URL)
        check.click("#netcam-run")
        check.wait_for_function(
            "document.getElementById('netcam-note').textContent.includes('有画面')", timeout=12000
        )
        path = SHOTS / "13_check_netcam.png"
        check.screenshot(path=str(path), full_page=True)
        report["shots"].append(str(path))
        report["check_note"] = check.inner_text("#netcam-note")
        report["check_saved"] = check.evaluate("localStorage.getItem('reading_ai.net_cams')")
        check.close()

        # ---- 2. 机器人端：网络摄像头手动抓帧 ----
        robot = context.new_page()
        watch(robot)
        robot.goto(robot_url, wait_until="domcontentloaded")
        wait_connected(robot)
        open_net_panel(robot)
        robot.fill("#src-net-name", "模拟 ESP32")
        robot.fill("#src-net-url", CAM_URL)
        path = SHOTS / "08_net_config.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))

        robot.click('[data-src="net-connect"]')
        robot.wait_for_function(
            "(() => { const i = document.getElementById('src-net-img'); return i && !i.hidden && i.naturalWidth > 0; })()",
            timeout=15000,
        )
        robot.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('连上了')", timeout=10000
        )
        robot.wait_for_timeout(500)
        path = SHOTS / "09_net_live.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["net_hint"] = robot.inner_text("#src-hint")

        robot.click("#src-confirm")  # 拍下来
        robot.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('已取得图片')", timeout=20000
        )
        path = SHOTS / "10_net_captured.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["captured_hint"] = robot.inner_text("#src-hint")

        robot.click("#src-confirm")  # 用这张
        # 讲稿先于提问：拍完后应先把这一页讲出来（文字上屏 + 音频可取）
        robot.wait_for_function(
            "(() => { const s = document.getElementById('story'); return s && !s.hidden && s.textContent.trim().length > 0; })()",
            timeout=60000,
        )
        robot.wait_for_timeout(300)
        path = SHOTS / "12_story.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["story"] = robot.inner_text("#story").replace("\n", " ")[:160]
        audio_src = robot.evaluate(
            "(() => { try { return (currentAudio && currentAudio.src) || ''; } catch (e) { return ''; } })()"
        )
        report["story_audio"] = audio_src
        if audio_src:
            media = context.request.get(audio_src)
            report["story_audio_ok"] = bool(media.ok and len(media.body()) > 2048)
        robot.wait_for_function(
            "(() => { const s = document.getElementById('stage'); return !!(s.querySelector('.question') || s.querySelector('.hint')); })()",
            timeout=60000,
        )
        robot.wait_for_timeout(600)
        path = SHOTS / "11_net_question.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["question"] = robot.inner_text("#stage").replace("\n", " / ")[:160]

        # ---- 孩子自己提问：🙋 问一句 → 打字 → 说完了 → 回答上屏并合成语音 ----
        robot.click("#free-btn")
        robot.fill("#answer-input", "小兔子为什么在草地上跑呀？")
        robot.click("#answer-send")
        robot.wait_for_function(
            "(() => document.getElementById('stage').textContent.includes('小朋友问'))()",
            timeout=90000,
        )
        robot.wait_for_timeout(400)
        path = SHOTS / "13_free.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["free_answer"] = robot.inner_text("#stage").replace("\n", " / ")[:200]
        report["free_answer_text"] = robot.evaluate(
            "(() => { const e = document.querySelector('#stage .question'); return e ? e.textContent.trim() : ''; })()"
        )
        free_audio = robot.evaluate(
            "(() => { try { return (currentAudio && currentAudio.src) || ''; } catch (e) { return ''; } })()"
        )
        report["free_audio"] = free_audio
        if free_audio:
            media = context.request.get(free_audio)
            report["free_audio_ok"] = bool(media.ok and len(media.body()) > 2048)

        # ---- 追问第二轮：上一轮问答要带进上下文，不能答非所问 ----
        robot.click("#free-btn")
        robot.fill("#answer-input", "它手里拿的是什么呀？")
        robot.click("#answer-send")
        robot.wait_for_function(
            "(() => document.getElementById('stage').textContent.includes('它手里拿的是什么呀'))()",
            timeout=90000,
        )
        robot.wait_for_timeout(400)
        path = SHOTS / "19_free_followup.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["free_followup"] = robot.inner_text("#stage").replace("\n", " / ")[:200]
        report["free_followup_answer"] = robot.evaluate(
            "(() => { const e = document.querySelector('#stage .question'); return e ? e.textContent.trim() : ''; })()"
        )
        follow_audio = robot.evaluate(
            "(() => { try { return (currentAudio && currentAudio.src) || ''; } catch (e) { return ''; } })()"
        )
        report["free_followup_audio"] = follow_audio
        if follow_audio:
            media = context.request.get(follow_audio)
            report["free_followup_audio_ok"] = bool(media.ok and len(media.body()) > 2048)

        # ---- 护眼开关：支持就开，不支持必须说实话，不能假装成功 ----
        robot.click("#guard-btn")
        robot.wait_for_function(
            "(() => { const t = document.getElementById('toast');"
            " return t && !t.hidden && t.textContent.trim().length > 0; })()",
            timeout=10000,
        )
        robot.wait_for_timeout(300)
        path = SHOTS / "20_guard.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["guard_note"] = robot.inner_text("#toast").strip()
        # 真开起来了就关掉，别把摄像头一直占着；开不起来也必须留下真实原因
        if "护眼开着" in report["guard_note"]:
            robot.click("#guard-btn")
            robot.wait_for_function(
                "(() => document.getElementById('toast').textContent.includes('已关'))()", timeout=10000
            )
            report["guard_off"] = robot.inner_text("#toast").strip()

        # ---- 问了就该进档案：用教师端接口回查，确认不是只在前端显示 ----
        profile = api.get(f"{BASE}/api/teacher/reading/{child['id']}").json()
        report["profile_after_free"] = {
            "pages": profile.get("pages"),
            "free_questions": profile.get("free_questions"),
            "recent": [item.get("question") for item in (profile.get("recent_questions") or [])[:2]],
        }
        assert int(profile.get("free_questions") or 0) >= 2, "两轮提问没都进阅读档案"
        robot.close()

        # ---- 3. 自动翻页：换一页、不点按钮，应自动拍并自动提交 ----
        second_id, _token2, second_url = new_session()
        auto = context.new_page()
        watch(auto)
        auto.goto(second_url, wait_until="domcontentloaded")
        wait_connected(auto)
        open_net_panel(auto)
        auto.fill("#src-net-url", CAM_URL)
        auto.click('[data-src="net-connect"]')
        auto.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('连上了')", timeout=15000
        )
        STATE["switch_at"] = time.monotonic() + 6  # 连上 6 秒后换页，模拟人手翻书
        auto.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('检测到翻页')", timeout=40000
        )
        auto.wait_for_timeout(300)
        path = SHOTS / "12_net_autoflip.png"
        auto.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["autoflip_hint"] = auto.inner_text("#src-hint")
        auto.wait_for_function(
            "(() => { const s = document.getElementById('stage'); return !!(s.querySelector('.question') || s.querySelector('.hint')); })()",
            timeout=60000,
        )
        report["autoflip_question"] = auto.inner_text("#stage").replace("\n", " / ")[:160]
        auto.close()

        report["capture_hits"] = STATE["hits"]

        # ---- 4. 只给 MJPEG 流的摄像头：预览直连、抓帧由服务端从流里抠一张 ----
        third_id, _token3, third_url = new_session()
        mjpeg = context.new_page()
        watch(mjpeg)
        mjpeg.goto(third_url, wait_until="domcontentloaded")
        wait_connected(mjpeg)
        open_net_panel(mjpeg)
        mjpeg.fill("#src-net-url", MJPEG_URL)
        mjpeg.click('[data-src="net-connect"]')
        mjpeg.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('连上了')", timeout=20000
        )
        mjpeg.click("#src-confirm")
        mjpeg.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('已取得图片')", timeout=20000
        )
        path = SHOTS / "14_net_mjpeg.png"
        mjpeg.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["mjpeg_hint"] = mjpeg.inner_text("#src-hint")
        mjpeg.close()

        # ---- 5. 要登录的摄像头：账号密码写进地址，预览和抓帧都走服务端 ----
        fourth_id, _token4, fourth_url = new_session()
        basic = context.new_page()
        watch(basic)
        basic.goto(fourth_url, wait_until="domcontentloaded")
        wait_connected(basic)
        open_net_panel(basic)
        basic.fill("#src-net-url", BASIC_URL)
        basic.click('[data-src="net-connect"]')
        basic.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('连上了')", timeout=20000
        )
        # 带凭证的地址浏览器不许 <img> 直连：应直接走服务端代理预览，1.5 秒一帧
        basic.wait_for_function(
            "(() => { const i = document.getElementById('src-net-img'); return i && !i.hidden && i.naturalWidth > 0; })()",
            timeout=20000,
        )
        basic.click("#src-confirm")
        basic.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('已取得图片')", timeout=20000
        )
        path = SHOTS / "17_net_basic.png"
        basic.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["basic_hint"] = basic.inner_text("#src-hint")
        basic.close()

        report["ended"] = [
            post_json(f"/api/teacher/sessions/{session_id}/end", {}).ok,
            post_json(f"/api/teacher/sessions/{second_id}/end", {}).ok,
            post_json(f"/api/teacher/sessions/{third_id}/end", {}).ok,
            post_json(f"/api/teacher/sessions/{fourth_id}/end", {}).ok,
        ]
        if own_child:
            # 管理员口令才能删孩子；删孩子会连带会话/回合，不留验证残留
            if post_json("/api/login", {"pin": SETTINGS.admin_pin}).ok:
                removed = api.delete(f"{BASE}/api/teacher/children/{child['id']}")
                report["cleaned"] = removed.ok
            else:
                report["cleaned"] = False
        browser.close()

    server.shutdown()
    server.server_close()
    # 落盘一份报告：复核脚本只认这份证据，不认控制台自己打印的“通过”
    report_path = SHOTS / "net_cam_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
