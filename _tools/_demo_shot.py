"""实拍演示：用真实浏览器从登录、建会话、设备自检到机器人端拍照提问回答走一遍，产出截图。

无实物摄像头时用 Chromium 的虚拟摄像头（--use-file-for-fake-video-capture），
画面内容是本脚本现场画的一页绘本，用来验证「拍照 -> 识别 -> 提问 -> 回答」整条链路。

用法：先 python run.py，再 python _tools\\_demo_shot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_tools" / "shots"
BASE = "http://127.0.0.1:8000"
PIN = "2026"

TITLE_FONT = r"C:\Windows\Fonts\msyhbd.ttc"
BODY_FONT = r"C:\Windows\Fonts\msyh.ttc"


def page_image() -> Image.Image:
    """画一页像样的绘本：标题、四行正文、太阳、风筝、草地。

    底色刻意压在亮度 232 以下，否则会被质量闸门判成过曝。
    """
    img = Image.new("RGB", (900, 1200), (231, 224, 206))
    draw = ImageDraw.Draw(img)
    draw.rectangle([18, 18, 882, 1182], outline=(196, 184, 162), width=3)
    title = ImageFont.truetype(TITLE_FONT, 56)
    body = ImageFont.truetype(BODY_FONT, 32)
    draw.text((76, 74), "小兔子的风筝", font=title, fill=(28, 28, 32))
    lines = [
        "春天来了，风轻轻地吹。",
        "小兔子带着风筝来到草地。",
        "风筝越飞越高，像一只小鸟。",
        "“再高一点！”小兔子笑着喊。",
    ]
    for index, line in enumerate(lines):
        draw.text((76, 190 + index * 64), line, font=body, fill=(46, 46, 50))
    draw.ellipse([540, 470, 660, 590], fill=(255, 196, 60))
    draw.polygon([(330, 660), (430, 600), (378, 730)], fill=(222, 88, 70))
    draw.line([(383, 730), (366, 812), (388, 868)], fill=(54, 54, 58), width=3)
    draw.rectangle([0, 960, 900, 1200], fill=(176, 196, 132))
    for x0 in range(40, 900, 90):
        draw.line([(x0, 1200), (x0 + 18, 1050)], fill=(158, 180, 116), width=4)
    return img


def make_y4m(img: Image.Image, path: Path, frames: int = 12) -> None:
    """写一个 I420 的 YUV4MPEG2 文件，给 Chromium 当虚拟摄像头画面。"""
    width, height = img.size
    ycbcr = img.convert("YCbCr")
    y_plane = ycbcr.getchannel(0)
    u_plane = ycbcr.getchannel(1).resize((width // 2, height // 2), Image.BILINEAR)
    v_plane = ycbcr.getchannel(2).resize((width // 2, height // 2), Image.BILINEAR)
    payload = y_plane.tobytes() + u_plane.tobytes() + v_plane.tobytes()
    with path.open("wb") as handle:
        handle.write(f"YUV4MPEG2 W{width} H{height} F30:1 Ip A1:1 C420jpeg\n".encode("ascii"))
        for _ in range(frames):
            handle.write(b"FRAME\n")
            handle.write(payload)


def wait_connected(page, timeout: int = 20000) -> None:
    page.wait_for_function(
        "document.getElementById('conn-text').textContent.trim() === '已连接'", timeout=timeout
    )


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    source = page_image()
    source.save(SHOTS / "fake_page.jpg", format="JPEG", quality=92)
    fake_video = SHOTS / "fake_camera.y4m"
    make_y4m(source.resize((720, 960), Image.LANCZOS), fake_video)
    print("虚拟摄像头画面:", fake_video, fake_video.stat().st_size, "bytes")

    report: dict = {"shots": [], "console_errors": []}
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="msedge",
            headless=True,
            args=[
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                f"--use-file-for-fake-video-capture={fake_video.as_posix()}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            permissions=["camera", "microphone"],
        )

        def watch(page) -> None:
            page.on(
                "console",
                lambda msg: errors.append(f"[console] {msg.text}") if msg.type == "error" else None,
            )
            page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))

        api = context.request

        def post_json(path: str, payload: dict):
            # 老版本 playwright 没有 json= 参数，手动带 Content-Type
            return api.post(
                f"{BASE}{path}",
                data=json.dumps(payload, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
            )

        login = post_json("/api/login", {"pin": PIN})
        assert login.ok, login.text()
        children = api.get(f"{BASE}/api/teacher/children").json()["items"]
        books = api.get(f"{BASE}/api/teacher/books").json()["items"]
        child = next(item for item in children if item["name"] == "小雨")
        created = post_json(
            "/api/teacher/sessions", {"child_id": child["id"], "book_id": books[0]["id"]}
        ).json()
        session_id, token = created["session_id"], created["token"]
        robot_url = BASE + created["robot_url"]
        print("会话:", session_id, "|", robot_url)

        # 实拍前清掉之前异常中断留下的进行中会话，避免教师端堆脏数据
        for item in api.get(f"{BASE}/api/teacher/sessions").json()["items"]:
            if item["id"] != session_id:
                post_json(f"/api/teacher/sessions/{item['id']}/end", {})

        # ---- 1. 设备自检页 ----
        check = context.new_page()
        watch(check)
        check.goto(f"{BASE}/device-check", wait_until="networkidle")
        check.wait_for_selector("#caps tbody tr")
        check.click("#cam-start")
        check.wait_for_function(
            "document.getElementById('cam-note').textContent.includes('正在预览')", timeout=15000
        )
        check.click("#mic-start")
        check.click("#spk-play")
        check.click("#net-run")
        check.wait_for_function(
            "document.getElementById('net-note').textContent.includes('往返')", timeout=15000
        )
        check.wait_for_timeout(1500)
        path = SHOTS / "01_device_check.png"
        check.screenshot(path=str(path), full_page=True)
        report["shots"].append(str(path))
        report["device_check_summary"] = check.inner_text("#summary-text")
        report["device_check_net"] = check.inner_text("#net-note")
        report["device_check_cam"] = check.inner_text("#cam-note")
        print("自检页:", report["device_check_summary"], "|", report["device_check_net"])

        # ---- 2. 机器人端：空闲 ----
        robot = context.new_page()
        watch(robot)
        robot.goto(robot_url, wait_until="domcontentloaded")
        wait_connected(robot)
        robot.wait_for_timeout(600)
        path = SHOTS / "02_robot_idle.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["robot_screen"] = robot.evaluate(
            "({screen: document.documentElement.dataset.screen, orientation: document.documentElement.dataset.orientation, tag: document.getElementById('device-tag').textContent})"
        )
        print("机器人端:", report["robot_screen"])

        # ---- 3. 取图：来源面板 ----
        robot.click("#main-btn")
        robot.wait_for_selector(".src-mask", state="visible")
        robot.wait_for_timeout(400)
        path = SHOTS / "03_source_picker.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))

        # ---- 4. 取图：摄像头实时预览 ----
        robot.click('[data-kind="camera"]')
        robot.wait_for_function(
            "(() => { const v = document.getElementById('src-video'); return v && !v.hidden && v.readyState >= 2; })()",
            timeout=20000,
        )
        robot.wait_for_timeout(900)
        path = SHOTS / "04_camera_live.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))

        # ---- 5. 拍下来 -> 用这张 -> 服务端识别 -> 提问 ----
        robot.click("#src-confirm")
        robot.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('已取得图片')", timeout=20000
        )
        robot.wait_for_timeout(300)
        robot.click("#src-confirm")
        robot.wait_for_function(
            "(() => { const s = document.getElementById('stage'); return !!(s.querySelector('.question') || s.querySelector('.hint')); })()",
            timeout=60000,
        )
        robot.wait_for_timeout(600)
        path = SHOTS / "05_question.png"
        robot.screenshot(path=str(path))
        report["shots"].append(str(path))
        report["question_stage"] = robot.inner_text("#stage")
        report["state_label"] = robot.inner_text("#state-text")
        print("提问:", report["question_stage"].replace("\n", " / "))

        # ---- 6. 回答 ----
        if robot.is_visible("#answer-bar"):
            robot.fill("#answer-input", "风筝")
            robot.click("#answer-send")
            robot.wait_for_selector("#stage .answer", timeout=60000)
            robot.wait_for_timeout(500)
            path = SHOTS / "06_answer.png"
            robot.screenshot(path=str(path))
            report["shots"].append(str(path))
            report["answer_stage"] = robot.inner_text("#stage")
            print("回答:", report["answer_stage"].replace("\n", " / "))
        else:
            report["answer_stage"] = "（未出现回答条，可能被质量闸门拦下）"

        # ---- 7. 小屏竖屏（7 寸机器人） ----
        small = browser.new_context(
            viewport={"width": 420, "height": 760},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=2,
            locale="zh-CN",
            permissions=["camera", "microphone"],
        )
        compact = small.new_page()
        watch(compact)
        compact.goto(robot_url, wait_until="domcontentloaded")
        wait_connected(compact)
        compact.wait_for_timeout(700)
        path = SHOTS / "07_compact.png"
        compact.screenshot(path=str(path), full_page=True)
        report["shots"].append(str(path))
        report["compact_screen"] = compact.evaluate(
            "({screen: document.documentElement.dataset.screen, orientation: document.documentElement.dataset.orientation, width: innerWidth})"
        )
        print("小屏:", report["compact_screen"])

        ended = post_json(f"/api/teacher/sessions/{session_id}/end", {})
        report["ended"] = ended.ok
        report["console_errors"] = errors
        browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
