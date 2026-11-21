"""真拍一帧本机 USB 摄像头：登录教师端 → /api/camera/snapshot?url=camera:0。"""

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

settings = get_settings()
base = f"http://127.0.0.1:{settings.port}"

with httpx.Client(base_url=base, timeout=30) as client:
    login = client.post("/api/login", json={"pin": settings.teacher_pin})
    print("login:", login.status_code)
    shot = client.get("/api/camera/snapshot", params={"url": "camera:0"})
    print("snapshot:", shot.status_code, shot.headers.get("content-type"), len(shot.content), "bytes")
    out = Path(__file__).resolve().parent / "shots" / "camera0.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(shot.content)
    print("saved:", out)
    head = shot.content[:2]
    print("jpeg magic:", head == b"\xff\xd8")
