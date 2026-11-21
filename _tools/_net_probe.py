"""诊断：网络摄像头连接后 img 为什么没有画面。打印提示文案、img 状态、代理请求结果。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _demo_shot import wait_connected  # noqa: E402
from _net_cam_demo import BASE, CAM_URL, PIN, SHOTS, start_fake_camera  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    server = start_fake_camera()
    events: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1100, "height": 900}, locale="zh-CN")
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
        child = next(item for item in children if item["name"] == "小雨")
        created = post_json(
            "/api/teacher/sessions", {"child_id": child["id"], "book_id": books[0]["id"]}
        ).json()
        robot_url = BASE + created["robot_url"]

        page = context.new_page()
        page.on("console", lambda msg: events.append(f"[console:{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: events.append(f"[pageerror] {exc}"))
        page.on("requestfailed", lambda req: events.append(f"[failed] {req.url} {req.failure}"))
        page.goto(robot_url, wait_until="domcontentloaded")
        wait_connected(page)

        page.click("#main-btn")
        page.wait_for_selector(".src-mask", state="visible")
        page.click('[data-kind="net"]')
        page.wait_for_selector("#src-net", state="visible")
        page.fill("#src-net-url", CAM_URL)
        page.click('[data-src="net-connect"]')
        page.wait_for_timeout(4000)

        snapshot = page.evaluate(
            """() => {
              const img = document.getElementById('src-net-img');
              const net = document.getElementById('src-net');
              const stage = document.getElementById('src-stage');
              const robot = window.ROBOT || {};
              return {
                hint: document.getElementById('src-hint').textContent,
                note: document.getElementById('src-note').textContent,
                netDisplay: net ? net.style.display : 'missing',
                stageDisplay: stage ? stage.style.display : 'missing',
                img: img ? {hidden: img.hidden, src: img.getAttribute('src'), w: img.naturalWidth, h: img.naturalHeight, complete: img.complete} : null,
                localStorage: localStorage.getItem('reading_ai.net_cams'),
                robot: {sessionId: robot.sessionId, token: (robot.token || '').slice(0, 6) + '…'},
                savedOptions: document.getElementById('src-net-list').innerHTML.slice(0, 200),
              };
            }"""
        )

        proxy = page.evaluate(
            """async (url) => {
              const robot = window.ROBOT || {};
              const q = new URLSearchParams({session_id: robot.sessionId || '', token: robot.token || '', url, t: String(Date.now())});
              try {
                const r = await fetch('/api/camera/snapshot?' + q.toString(), {cache: 'no-store'});
                const text = await r.text();
                return {status: r.status, type: r.headers.get('content-type'), head: text.slice(0, 120)};
              } catch (error) { return {error: String(error)}; }
            }""",
            CAM_URL,
        )

        path = SHOTS / "dbg_net.png"
        page.screenshot(path=str(path), full_page=True)
        print(json.dumps({"snapshot": snapshot, "proxy": proxy, "events": events[-12:]}, ensure_ascii=False, indent=2))
        post_json(f"/api/teacher/sessions/{created['session_id']}/end", {})
        browser.close()
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
