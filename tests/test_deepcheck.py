"""深度自检的契约测试：结构完整、stub 下不误判、并发时给 busy、接口要登录。

真实引擎的验证不在这里——stub 只能证明"链路和判定逻辑对"，
能不能真跑通由 `_tools/recheck.py deep` 在真机上验证。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services import deepcheck


def test_card_is_png():
    data = deepcheck.test_card()
    assert data.startswith(b"\x89PNG"), "测试卡片必须是 PNG（OCR/VLM 按图片解码）"
    assert len(data) > 5000


def test_deep_check_shape():
    report = deepcheck.run_deep_check()
    assert report["scope"] == "deep"
    assert report["verdict"] in {"pass", "block"}
    assert [item["name"] for item in report["checks"]] == [
        "OCR 识字",
        "VLM 看画面",
        "LLM 生成",
        "TTS 出声",
        "ASR 转写",
    ]
    assert all(item["status"] in {"ok", "warn", "fail"} for item in report["checks"])
    # stub 环境五条链路都走桩实现，应该是"有提示但不拦截"
    assert report["verdict"] == "pass"
    assert report["summary"]


def test_deep_check_busy():
    assert deepcheck.DEEP_LOCK.acquire(blocking=False)
    try:
        report = deepcheck.run_deep_check()
    finally:
        deepcheck.DEEP_LOCK.release()
    assert report["verdict"] == "busy"
    assert "还在跑" in report["summary"]


def test_deep_check_api(teacher):
    response = teacher.post("/api/teacher/selfcheck/deep")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scope"] == "deep"
    assert len(data["checks"]) == 5


def test_deep_check_requires_login():
    # conftest 的 client 是会话级且已登录，这里用独立实例验证权限
    with TestClient(app) as fresh:
        response = fresh.post("/api/teacher/selfcheck/deep")
    assert response.status_code == 401
