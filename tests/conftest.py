"""测试前置：先把数据目录指到临时目录，再导入应用（配置是导入时读取的）。

这样跑测试不会碰到 data/app.db 里的演示数据。
"""

from __future__ import annotations

import io
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="reading_ai_test_")
os.environ["DATA_DIR"] = _TMP
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["TEACHER_PIN"] = "2026"
os.environ["ADMIN_PIN"] = "8642"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.main import app  # noqa: E402


def page_jpeg() -> bytes:
    """画一张"清晰的一页"：硬边黑块 + 浅底色，能过拍摄质量检测。"""
    image = Image.new("RGB", (900, 1200), (246, 244, 238))
    draw = ImageDraw.Draw(image)
    for index in range(10):
        top = 120 + index * 90
        width = 520 + (index % 4) * 90
        draw.rectangle([90, top, 90 + width, top + 26], fill=(28, 28, 32))
    draw.ellipse([520, 760, 780, 1020], fill=(255, 213, 79))
    draw.rectangle([120, 780, 420, 1040], fill=(168, 216, 185))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as instance:
        yield instance


@pytest.fixture(scope="session")
def teacher(client):
    """已登录的老师客户端（cookie 由 TestClient 自动保存）。"""
    response = client.post("/api/login", json={"pin": "2026"})
    assert response.status_code == 200, response.text
    return client