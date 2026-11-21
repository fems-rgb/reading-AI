"""验证「翻页自动拍」：伪造一个会自己翻页的摄像头流，全程不点快门。

预期：进入摄像头预览 -> 1.5s 暖机 -> 画面切换（翻页）-> 自动抓拍 -> 1.5s 后自动提交
-> 服务端识别 -> 出现提问。脚本只点两下：拍一页、选摄像头。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_tools" / "shots"
BASE = "http://127.0.0.1:8000"
PIN = "2026"

# 每 2.5 秒在 A/B 两页之间切换，模拟孩子翻页
FAKE_CAMERA = """
(() => {
  const canvas = document.createElement('canvas');
  canvas.width = 720; canvas.height = 960;
  const ctx = canvas.getContext('2d');
  let page = 0;
  function paint() {
    const bg = page === 0 ? '#e7dfc9' : '#dce7d4';
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, 720, 960);
    ctx.fillStyle = '#2b2b30';
    ctx.font = 'bold 64px sans-serif';
    ctx.fillText(page === 0 ? '第 1 页' : '第 2 页', 70, 140);
    ctx.font = '36px sans-serif';
    ctx.fillText(page === 0 ? '小兔子在草地上放风筝。' : '风筝飞得比大树还高。', 70, 230);
    ctx.fillStyle = page === 0 ? '#de5846' : '#3f7fbf';
    ctx.beginPath();
    ctx.arc(360, 560, 150, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 48px sans-serif';
    ctx.fillText(page === 0 ? 'A' : 'B', 336, 580);
    for (let i = 0; i < 6; i += 1) {
      ctx.fillStyle = 'rgba(40,40,44,.65)';
      ctx.fillRect(70, 760 + i * 22, 560 - i * 40, 8);
    }
  }
  paint();
  setInterval(() => { page = 1 - page; paint(); }, 2500);

  const stream = canvas.captureStream(15);
  // 每次都返回新流：capture.js 会先 probe 一次权限并停掉 track，
  // 共用同一个流会把正式预览的轨道一起停掉（画面只剩半帧）
  navigator.mediaDevices.getUserMedia = async () => canvas.captureStream(15);
  navigator.mediaDevices.enumerateDevices = async () => ([
    {kind: 'videoinput', deviceId: 'fake-cam', label: '模拟摄像头（会自动翻页）', groupId: 'fake'}
  ]);
})();
"""


def post_json(api, path: str, payload: dict):
    return api.post(
        f"{BASE}{path}",
        data=json.dumps(payload, ensure_ascii=False),
        headers={"Content-Type": "application/json"},
    )


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    report: dict = {"shots": [], "console_errors": []}
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            permissions=["camera", "microphone"],
        )
        api = context.request
        assert post_json(api, "/api/login", {"pin": PIN}).ok
        children = api.get(f"{BASE}/api/teacher/children").json()["items"]
        books = api.get(f"{BASE}/api/teacher/books").json()["items"]
        child = next(item for item in children if item["name"] == "小雨")
        created = post_json(
            api, "/api/teacher/sessions", {"child_id": child["id"], "book_id": books[0]["id"]}
        ).json()
        session_id = created["session_id"]
        robot_url = BASE + created["robot_url"]
        for item in api.get(f"{BASE}/api/teacher/sessions").json()["items"]:
            if item["id"] != session_id:
                post_json(api, f"/api/teacher/sessions/{item['id']}/end", {})

        page = context.new_page()
        page.on(
            "console",
            lambda msg: errors.append(f"[console] {msg.text}") if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(f"[pageerror] {exc}"))
        page.add_init_script(FAKE_CAMERA)

        page.goto(robot_url, wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('conn-text').textContent.trim() === '已连接'", timeout=20000
        )

        # 只看两次点击：拍一页 + 选摄像头，后面全靠自动
        page.click("#main-btn")
        page.wait_for_selector(".src-mask", state="visible")
        auto_checked = page.is_checked("#src-auto")
        report["auto_flip_default_on"] = auto_checked
        page.click('[data-kind="camera"]')
        page.wait_for_function(
            "(() => { const v = document.getElementById('src-video'); return v && v.videoWidth > 0; })()",
            timeout=20000,
        )
        print("摄像头已开，等待自动检测翻页…")

        # 等 hint 出现「检测到翻页」（自动抓拍那一刻）
        page.wait_for_function(
            "document.getElementById('src-hint').textContent.includes('检测到翻页')", timeout=30000
        )
        report["auto_hint"] = page.inner_text("#src-hint")
        report["auto_preview"] = page.is_visible("#src-preview")
        path = SHOTS / "08_flip_auto.png"
        page.screenshot(path=str(path))
        report["shots"].append(str(path))
        print("自动拍:", report["auto_hint"])

        # 什么都不点，等它自己提交并进入提问
        page.wait_for_function(
            "(() => { const s = document.getElementById('stage'); return !!(s.querySelector('.question') || s.querySelector('.hint')); })()",
            timeout=60000,
        )
        page.wait_for_timeout(500)
        report["stage_after_auto"] = page.inner_text("#stage")
        report["state_label"] = page.inner_text("#state-text")
        path = SHOTS / "09_flip_result.png"
        page.screenshot(path=str(path))
        report["shots"].append(str(path))
        print("自动提交后:", report["stage_after_auto"].replace("\n", " / "))

        post_json(api, f"/api/teacher/sessions/{session_id}/end", {})
        report["console_errors"] = errors
        browser.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
