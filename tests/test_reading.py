"""自由提问 / 阅读档案 / 用眼休息：走真实接口和真库，不用假数据。"""

from __future__ import annotations

import asyncio
import time

from conftest import page_jpeg

from app.core.orchestrator import SessionRuntime
from app.core.pipeline import _clean_free_answer


def _start(client) -> tuple[dict, dict]:
    child = client.get("/api/teacher/children").json()["items"][0]
    book = client.get("/api/teacher/books").json()["items"][0]
    started = client.post(
        "/api/teacher/sessions", json={"child_id": child["id"], "book_id": book["id"]}
    ).json()
    return started, child


def test_clean_free_answer_rejects_unsafe():
    assert _clean_free_answer("小兔子被鬼抓走了？") == ""
    assert _clean_free_answer("This is a rabbit running.") == ""
    text = _clean_free_answer("因为小兔子想放风筝。它跑得很快。第三句不要。")
    assert "风筝" in text and "第三句" not in text


def test_free_question_answers_and_lands_in_profile(teacher):
    """孩子问一句 → 当场回答（带音频）→ 阅读档案里能查到这条提问。"""
    client = teacher
    started, child = _start(client)
    session_id, token = started["session_id"], started["token"]
    captured = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    )
    assert captured.status_code == 200, captured.text

    asked = client.post(
        f"/api/device/{session_id}/free",
        json={"token": token, "text": "小兔子为什么在草地上跑呀？"},
    )
    assert asked.status_code == 200, asked.text
    data = asked.json()
    assert data["ok"] is True
    assert data["question"] == "小兔子为什么在草地上跑呀？"
    assert data["answer"]
    assert data["mode"] in {"rule", "gate", "llm"}
    assert (data.get("audio") or {}).get("url", "").startswith("/media/tts/")

    profile = client.get(f"/api/teacher/reading/{child['id']}").json()
    assert profile["free_questions"] >= 1
    assert any(item["question"] == data["question"] for item in profile["recent_questions"])
    assert profile["pages"] >= 1
    assert profile["books_read"] >= 1


def test_free_question_before_page_is_refused(teacher):
    client = teacher
    started, _child = _start(client)
    reply = client.post(
        f"/api/device/{started['session_id']}/free",
        json={"token": started["token"], "text": "这是什么呀？"},
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["ok"] is False
    assert "拍一页" in body["human_hint"]


def test_free_question_requires_device_token(teacher):
    client = teacher
    started, _child = _start(client)
    reply = client.post(
        f"/api/device/{started['session_id']}/free",
        json={"token": "wrong-token", "text": "这是什么呀？"},
    )
    assert reply.status_code == 403


def test_free_question_carries_previous_round(monkeypatch, teacher):
    """追问时把上一轮问答带进 prompt：不是单轮问答机。"""
    import app.services.llm as llm_module

    prompts: list[str] = []

    class _FakeLLM:
        enabled = True

        def chat_json(self, system, user, temperature=0.0):
            prompts.append(user)
            return {"answer": "因为它想放风筝。"}, {"provider": "fake"}

    monkeypatch.setattr(llm_module, "LocalLLM", _FakeLLM)

    client = teacher
    started, _child = _start(client)
    session_id, token = started["session_id"], started["token"]
    captured = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    )
    assert captured.status_code == 200, captured.text

    first = client.post(
        f"/api/device/{session_id}/free", json={"token": token, "text": "小兔子在做什么呀？"}
    )
    assert first.json()["ok"] is True
    second = client.post(
        f"/api/device/{session_id}/free", json={"token": token, "text": "它为什么跑呀？"}
    )
    assert second.json()["ok"] is True
    # 第二轮 prompt 里必须能看到第一轮的问题和回答，否则追问会断片
    assert "小兔子在做什么呀？" in prompts[-1]
    assert "因为它想放风筝" in prompts[-1]  # 出口闸门会去掉句末标点，这里只看内容
    assert second.json().get("turns_used") == 1


def test_distance_reminder_is_fixed_text_and_counted(monkeypatch):
    """距离提醒只接受固定文案；次数真实计数，传别的 kind 一律拒绝。"""
    monkeypatch.setattr(
        "app.core.orchestrator.speech.synthesize",
        lambda text, voice: {"url": "/media/tts/test-distance.wav", "mode": "stub"},
    )
    runtime = SessionRuntime(
        session={"id": "test-remind"},
        child={"id": 1, "name": "测试", "age_months": 60},
        book={"id": 1, "title": "测试绘本", "language": "zh"},
        pages=[{"page_no": 1, "status": "ready"}],
    )
    result = asyncio.run(runtime.remind("distance"))
    assert result["ok"] is True
    assert "坐远一点" in result["text"]
    assert (result.get("audio") or {}).get("url")
    assert runtime.distance_reminders == 1

    other = asyncio.run(runtime.remind("孩子你往后退一点"))
    assert other["ok"] is False and "只会提醒" in other["human_hint"]
    assert runtime.distance_reminders == 1


def test_distance_reminder_requires_device_token(teacher):
    client = teacher
    started, _child = _start(client)
    reply = client.post(
        f"/api/device/{started['session_id']}/remind",
        json={"token": "wrong-token", "kind": "distance"},
    )
    assert reply.status_code == 403


def test_eye_rest_fires_after_configured_minutes(monkeypatch):
    """用眼提醒看的是真实经过的时间：满 20 分钟触发一次，紧接着不重复打扰。"""
    from app.config import reset_settings_cache

    monkeypatch.setenv("EYE_REST_MINUTES", "20")
    reset_settings_cache()
    runtime = SessionRuntime(
        session={"id": "test-eye-rest"},
        child={"id": 1, "name": "测试", "age_months": 60},
        book={"id": 1, "title": "测试绘本", "language": "zh"},
        pages=[{"page_no": 1, "status": "ready"}],
    )
    runtime._started_at = time.monotonic() - 21 * 60
    try:
        assert asyncio.run(runtime._maybe_eye_rest()) is True
        assert runtime.eye_rests == 1
        assert asyncio.run(runtime._maybe_eye_rest()) is False
    finally:
        reset_settings_cache()
