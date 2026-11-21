"""对真实运行中的服务跑一遍完整链路（不是 TestClient）。

用法：先 python run.py，再 python _tools\\live_smoke.py
"""

import io
import sys

import httpx
from PIL import Image, ImageDraw

BASE = "http://127.0.0.1:8000"


def page_jpeg() -> bytes:
    image = Image.new("RGB", (900, 1200), (246, 244, 238))
    draw = ImageDraw.Draw(image)
    for index in range(10):
        top = 120 + index * 90
        draw.rectangle([90, top, 690, top + 26], fill=(28, 28, 32))
    draw.ellipse([520, 760, 780, 1020], fill=(255, 213, 79))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=30)
    health = client.get("/healthz").json()
    print("healthz:", health)

    for path in ("/", "/teacher", "/static/app.css", "/static/teacher.js", "/static/robot.js"):
        response = client.get(path)
        print(f"{path:22s} -> {response.status_code} ({len(response.content)} bytes)")
        assert response.status_code == 200, path

    login = client.post("/api/login", json={"pin": "2026"})
    assert login.status_code == 200, login.text
    print("login:", login.json())

    overview = client.get("/api/teacher/overview").json()
    print("overview:", {k: overview[k] for k in ("children", "pages_today", "answered_today", "selfcheck_blocked", "demo_mode")})
    print("自检统计:", overview["selfcheck"])

    children = client.get("/api/teacher/children").json()["items"]
    books = client.get("/api/teacher/books").json()["items"]
    child = next(item for item in children if item["name"] == "小雨")
    book = books[0]
    created = client.post(
        "/api/teacher/sessions", json={"child_id": child["id"], "book_id": book["id"]}
    ).json()
    session_id, token = created["session_id"], created["token"]
    print("session:", session_id, "robot:", created["robot_url"])

    robot_page = client.get(created["robot_url"])
    print("robot page:", robot_page.status_code, "len", len(robot_page.text))

    captured = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    ).json()
    print("capture ok:", captured["ok"], "| 提问:", captured["question"]["question"])
    print("  摘要自检:", captured["checker"]["summary"]["verdict"], captured["checker"]["summary"]["score"])
    print("  提问自检:", captured["checker"]["question"]["verdict"], captured["checker"]["question"]["score"])
    print("  来源:", captured["source"], "| 耗时:", captured["latency_ms"], "ms")

    snapshot = client.get(f"/api/device/{session_id}", params={"token": token}).json()
    print("设备快照状态:", snapshot["snapshot"]["state_label"], "| 事件数:", len(snapshot["history"]))

    answer = client.post(
        f"/api/device/{session_id}/answer", json={"text": "风筝", "token": token}
    ).json()
    print("回答 ->", answer["answer_text"], "|", answer["reply"])

    ended = client.post(f"/api/teacher/sessions/{session_id}/end", json={}).json()
    print("结束：页数", ended["pages"], "| 建议", len(ended["advice"]), "条 | 会话自检", ended["session_check"]["verdict"])

    report = client.post(f"/api/teacher/weekly/{child['id']}", json={})
    if report.status_code == 200:
        data = report.json()
        print("周报:", data["payload"]["headline"], "| 自检", data["selfcheck"]["score"])
        client.post(f"/api/teacher/reports/{data['report_id']}/publish", json={})
    else:
        print("周报生成失败:", report.status_code, report.text)

    parent = client.get(f"/api/parent/{child['parent_token']}").json()
    print("家长端:", "已发布" if parent.get("published") else parent.get("human_hint"))

    demo = client.get("/api/teacher/selfcheck/demo").json()
    print("内容闸门演示：")
    for case in demo["cases"]:
        print(f"  - {case['name']:12s} {case['report']['verdict']:5s} {case['report']['score']:5.1f} | {case['report']['summary']}")

    system = client.get("/api/teacher/selfcheck/system").json()
    print("运行自检:", system["verdict"], system["summary"])
    for item in system["checks"]:
        print(f"  [{item['status']:4s}] {item['name']} {item['detail']}")

    audit = client.get("/api/teacher/audit").json()
    print("出站审计:", audit["human_hint"])
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())