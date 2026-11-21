"""真实落地链路：USB 摄像头实拍照片 → 老师上传成绘本页 → 预识别 → 校对 → 开新会话。

输出写 _tools/_e2e_real_out.txt（UTF-8），robot 链接写 _tools/shots/real_session.json。
"""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

settings = get_settings()
base = f"http://127.0.0.1:{settings.port}"
shots = ROOT / "_tools" / "shots"
lines: list[str] = []


def log(text: str = "") -> None:
    lines.append(str(text))


# 老师人工校对的内容，依据是实拍照片本身（人躺床上看书，卧室）
CURATED = {
    "raw_text": "",
    "faithful_description": "人躺在床上看书。",
    "read_aloud_script": "人在床上，看书。",
    "keywords": ["人", "书", "床"],
    "characters": ["人"],
    "objects": ["书", "床", "枕头"],
    "scene": "卧室",
    "relations": ["人躺在床上看书"],
}

with httpx.Client(base_url=base, timeout=240) as client:
    login = client.post("/api/login", json={"pin": settings.teacher_pin})
    log(f"login: {login.status_code}")

    books = client.get("/api/teacher/books").json()["items"]
    book = next((item for item in books if item["title"] == "实拍绘本"), None)
    if book is None:
        created = client.post("/api/teacher/books", json={"title": "实拍绘本", "language": "zh"})
        log(f"create book: {created.status_code} {created.text}")
        books = client.get("/api/teacher/books").json()["items"]
        book = next(item for item in books if item["title"] == "实拍绘本")
    log(f"book: id={book['id']} pages={[(p['page_no'], p['status']) for p in book.get('pages', [])]}")

    image = (shots / "camera0.jpg").read_bytes()
    uploaded = client.post(
        f"/api/teacher/books/{book['id']}/pages",
        data={"page_no": 1},
        files={"file": ("camera.jpg", image, "image/jpeg")},
    )
    log(f"upload: {uploaded.status_code}")
    payload = uploaded.json()
    log(json.dumps(payload, ensure_ascii=False, indent=2))

    page_id = payload.get("page_id")
    if payload.get("status") != "ready":
        patched = client.patch(f"/api/teacher/pages/{page_id}", json=CURATED)
        log(f"teacher review: {patched.status_code} {patched.text}")

    books = client.get("/api/teacher/books").json()["items"]
    book = next(item for item in books if item["id"] == book["id"])
    log(f"book after review: {[(p['page_no'], p['status']) for p in book.get('pages', [])]}")
    log(f"page image: {(book.get('pages') or [{}])[0].get('image', '')}")

    children = client.get("/api/teacher/children").json()["items"]
    child = next((item for item in children if item["name"] == "实拍小朋友"), None)
    if child is None:
        client.post(
            "/api/teacher/children",
            json={
                "name": "实拍小朋友",
                "age_years": 5,
                "class_name": "实拍班",
                "guardian_name": "实拍家长",
                "guardian_consent": True,
            },
        )
        children = client.get("/api/teacher/children").json()["items"]
        child = next(item for item in children if item["name"] == "实拍小朋友")
    log(f"child: id={child['id']} {child['name']}")

    session = client.post(
        "/api/teacher/sessions", json={"child_id": child["id"], "book_id": book["id"]}
    ).json()
    log(f"session: {json.dumps({k: session[k] for k in ('session_id', 'token', 'robot_url')}, ensure_ascii=False)}")

    (shots / "real_session.json").write_text(
        json.dumps(
            {
                "robot_url": base + session["robot_url"],
                "session_id": session["session_id"],
                "book_id": book["id"],
                "page_id": page_id,
                "teacher_pin": settings.teacher_pin,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    system = client.get("/api/teacher/selfcheck/system").json()
    for check in system.get("checks", []):
        if check["id"] in {"library", "vlm", "llm", "camera"}:
            log(f"selfcheck [{check['status']}] {check['name']}: {check.get('detail', '')}")

(ROOT / "_tools" / "_e2e_real_out.txt").write_text("\n".join(lines), encoding="utf-8")
print("done")
