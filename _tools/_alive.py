"""探活 + 关键路由检查，顺便验证 favicon 不再是 404。"""
from __future__ import annotations

import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=10)
for path in ("/healthz", "/favicon.ico", "/device-check", "/static/robot.js", "/static/app.css"):
    try:
        response = client.get(path)
        print(f"{path:20s} {response.status_code} {response.headers.get('content-type', '')} {len(response.content)}B")
    except Exception as exc:  # noqa: BLE001
        print(f"{path:20s} FAIL {exc}")
