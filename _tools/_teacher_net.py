"""验证教师端上传页也能用网络摄像头（没有设备 token，用登录 cookie 代抓）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _net_cam_demo import BASE, CAM_URL, PIN, SHOTS, start_fake_camera  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    server = start_fake_camera()
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1180, "height": 900}, locale="zh-CN")
        login = context.request.post(
            f"{BASE}/api/login",
            data=json.dumps({"pin": PIN}),
            headers={"Content-Type": "application/json"},
        )
        assert login.ok, login.text()

        page = context.new_page()
        page.on("console", lambda msg: errors.append(f"[console] {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
        page.goto(f"{BASE}/teacher", wait_until="networkidle")
        page.click('[data-tab="library"]')
        page.click("text=拍照 / 截图 / 粘贴")
        page.wait_for_selector(".src-mask", state="visible")
        choices = page.eval_on_selector_all(".src-choice .src-label", "els => els.map(e => e.textContent)")
        path = SHOTS / "15_teacher_picker.png"
        page.screenshot(path=str(path))

        page.click('[data-kind="net"]')
        page.fill("#src-net-url", CAM_URL)
        page.click('[data-src="net-connect"]')
        page.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('连上了')", timeout=15000
        )
        page.click("#src-confirm")
        page.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('已取得图片')", timeout=20000
        )
        path2 = SHOTS / "16_teacher_net_captured.png"
        page.screenshot(path=str(path2))
        print(json.dumps({"choices": choices, "hint": page.inner_text("#src-hint"),
                          "shots": [str(path), str(path2)], "console_errors": errors}, ensure_ascii=False, indent=2))
        browser.close()
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
