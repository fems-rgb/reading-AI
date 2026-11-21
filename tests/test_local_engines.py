"""本机引擎相关的小逻辑：摄像头地址解析、中文校验、提示词入库与升级。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("SEED_DEMO", "0")

from app import db  # noqa: E402
from app.services import camera  # noqa: E402
from app.services.brain import _has_chinese  # noqa: E402
from run import _model_missing  # noqa: E402


@pytest.mark.parametrize(
    "url,expected",
    [
        ("camera:0", 0),
        ("camera:1", 1),
        ("CAMERA:3", 3),
        ("  camera:2  ", 2),
        ("camera:9", 9),
        ("camera:10", None),  # 超过 MAX_INDEX，当作不是本机摄像头
        ("camera:", None),
        ("camera:-1", None),
        ("http://192.168.1.50/capture", None),
        ("rtsp://192.168.1.51/stream", None),
        ("", None),
    ],
)
def test_camera_index(url, expected):
    assert camera.camera_index(url) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("小兔子拿着风筝", True),
        ("What color is the kite?", False),
        ("中文 English 混排", True),
        ("", False),
    ],
)
def test_has_chinese(text, expected):
    assert _has_chinese(text) is expected


@pytest.mark.parametrize(
    "models,model,expected",
    [
        (None, "qwen2.5:3b", False),  # 拿不到清单就不猜
        ({"qwen2.5:3b", "deepseek-r1:8b"}, "qwen2.5:3b", False),
        ({"deepseek-r1:8b"}, "qwen2.5:3b", True),
        ({"qwen2.5:3b"}, "qwen2.5", True),  # 不带 tag 按 latest 算，3b 不算
        ({"qwen2.5:latest"}, "qwen2.5", False),
    ],
)
def test_model_missing(models, model, expected):
    assert _model_missing(models, model) is expected


def test_prompts_are_chinese_and_versioned(client):
    """提示词入库后要能读到，且明确要求中文；重复初始化不再加版本。"""
    db.init_db()
    first = db.get_prompt("A4_question")
    assert first and "简体中文" in first["body"]
    db.init_db()
    second = db.get_prompt("A4_question")
    assert second["version"] == first["version"]
    assert second["body"] == first["body"]
