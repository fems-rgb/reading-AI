"""对真实运行中的服务跑一遍完整链路（不是 TestClient）。

用法：先 python run.py，再 python _tools\\live_smoke.py

真实部署是空库（不会铺示例孩子/绘本），所以本脚本用自己的账号建一套
"烟测"数据，跑完把烟测孩子连同会话/问答/建议/周报一起删掉。
"""

import io
import json
import os
import sys
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SEED_DEMO", "0")
from app.config import get_settings  # noqa: E402

SETTINGS = get_settings()
BASE = f"http://127.0.0.1:{SETTINGS.port}"
# 报告落盘：复核脚本只看这份证据，不信控制台输出
SHOTS = Path(__file__).resolve().parent / "shots"

CHILD_NAME = "烟测小朋友"


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


def blank_jpeg() -> bytes:
    """占位页：只为了满足"绘本至少一页"，不预识别，让设备拍照时走真实 OCR/VLM。"""
    image = Image.new("RGB", (900, 1200), (250, 250, 250))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=180)
    health = client.get("/healthz").json()
    print("healthz:", health)
    facts: dict = {"health": health, "pages": {}}

    for path in ("/", "/teacher", "/static/app.css", "/static/teacher.js", "/static/robot.js"):
        response = client.get(path)
        print(f"{path:22s} -> {response.status_code} ({len(response.content)} bytes)")
        facts["pages"][path] = {"status": response.status_code, "bytes": len(response.content)}
        assert response.status_code == 200, path

    login = client.post("/api/login", json={"pin": SETTINGS.teacher_pin})
    assert login.status_code == 200, login.text
    print("login:", login.json())

    overview = client.get("/api/teacher/overview").json()
    print("overview:", {k: overview[k] for k in ("children", "pages_today", "answered_today", "selfcheck_blocked", "demo_mode")})
    print("自检统计:", overview["selfcheck"])

    # 自建烟测数据：孩子 → 绘本 → 一页占位页
    created_child = client.post(
        "/api/teacher/children",
        json={
            "name": CHILD_NAME,
            "age_years": 4.5,
            "class_name": "烟测班",
            "guardian_name": "烟测家长",
            "guardian_consent": True,
        },
    )
    assert created_child.status_code == 200, created_child.text
    child = next(
        item for item in client.get("/api/teacher/children").json()["items"] if item["name"] == CHILD_NAME
    )
    book_resp = client.post("/api/teacher/books", json={"title": "烟测绘本", "language": "zh"})
    assert book_resp.status_code == 200, book_resp.text
    book_id = book_resp.json()["id"]
    uploaded = client.post(
        f"/api/teacher/books/{book_id}/pages",
        data={"page_no": 1},
        files={"file": ("page.jpg", blank_jpeg(), "image/jpeg")},
    )
    print("占位页:", uploaded.status_code, uploaded.json().get("status"))
    print("烟测孩子:", child["id"], "| 烟测绘本:", book_id)

    created = client.post(
        "/api/teacher/sessions", json={"child_id": child["id"], "book_id": book_id}
    ).json()
    session_id, token = created["session_id"], created["token"]
    print("session:", session_id, "robot:", created["robot_url"])
    facts.update(session_id=session_id, child_id=child["id"],
                 parent_token=child["parent_token"], robots=created["robot_url"])

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
    modes = {"ocr": captured["facts"].get("ocr_mode"), "vlm": captured["facts"].get("vlm_mode")}
    print("  识别模式:", modes)
    facts.update(captured_ok=captured["ok"], question=captured["question"]["question"],
                 source=captured["source"], latency_ms=captured["latency_ms"],
                 facts_modes=modes,
                 checker_summary=captured["checker"]["summary"],
                 checker_question=captured["checker"]["question"])

    snapshot = client.get(f"/api/device/{session_id}", params={"token": token}).json()
    print("设备快照状态:", snapshot["snapshot"]["state_label"], "| 事件数:", len(snapshot["history"]))

    answer = client.post(
        f"/api/device/{session_id}/answer", json={"text": "风筝", "token": token}
    ).json()
    print("回答 ->", answer["answer_text"], "|", answer["reply"])
    facts["answer_mode"] = answer.get("mode")

    ended = client.post(f"/api/teacher/sessions/{session_id}/end", json={}).json()
    print("结束：页数", ended["pages"], "| 建议", len(ended["advice"]), "条 | 会话自检", ended["session_check"]["verdict"])
    facts["ended"] = {"pages": ended["pages"], "advice": len(ended["advice"]),
                      "session_check": ended["session_check"]["verdict"]}

    report = client.post(f"/api/teacher/weekly/{child['id']}", json={})
    if report.status_code == 200:
        data = report.json()
        print("周报:", data["payload"]["headline"], "| 自检", data["selfcheck"]["score"])
        client.post(f"/api/teacher/reports/{data['report_id']}/publish", json={})
        facts["weekly"] = {"report_id": data["report_id"], "headline": data["payload"]["headline"],
                           "score": data["selfcheck"]["score"]}
    else:
        print("周报生成失败:", report.status_code, report.text)
        facts["weekly"] = None

    parent = client.get(f"/api/parent/{child['parent_token']}").json()
    print("家长端:", "已发布" if parent.get("published") else parent.get("human_hint"))
    facts["parent_published"] = bool(parent.get("published"))

    demo = client.get("/api/teacher/selfcheck/demo").json()
    print("内容闸门用例：")
    for case in demo["cases"]:
        print(f"  - {case['name']:12s} {case['report']['verdict']:5s} {case['report']['score']:5.1f} | {case['report']['summary']}")
    facts["gate_cases"] = [{"name": case["name"], "verdict": case["report"]["verdict"]}
                           for case in demo["cases"]]

    system = client.get("/api/teacher/selfcheck/system").json()
    print("运行自检:", system["verdict"], system["summary"])
    for item in system["checks"]:
        print(f"  [{item['status']:4s}] {item['name']} {item['detail']}")

    audit = client.get("/api/teacher/audit").json()
    print("出站审计:", audit["human_hint"])
    facts.update(system_verdict=system["verdict"], audit_hint=audit["human_hint"])

    # 清理烟测孩子（连带会话/问答/建议/周报）；烟测绘本留在库里，可手工忽略
    admin = client.post("/api/login", json={"pin": SETTINGS.admin_pin})
    if admin.status_code == 200:
        removed = client.delete(f"/api/teacher/children/{child['id']}")
        print("清理烟测孩子:", removed.status_code, removed.json() if removed.status_code == 200 else "")
        facts["cleaned"] = removed.status_code == 200
    else:
        print("管理员登录失败，烟测孩子没有清理:", admin.status_code)
        facts["cleaned"] = False

    client.close()
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / "live_smoke.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
