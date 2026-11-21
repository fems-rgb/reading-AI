"""离线守卫：任何公网地址都必须在发请求之前被拦下。"""

from __future__ import annotations

import pytest

from app import guard


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9101/ocr",
        "http://localhost:8000/healthz",
        "http://10.0.0.8:9102/v1/chat/completions",
        "http://192.168.1.50:9103/v1",
        "http://172.16.5.9:9104/asr",
        "http://gpu-box.local:9102/v1",
    ],
)
def test_private_addresses_are_allowed(url):
    assert guard.audit_url(url).allowed is True


@pytest.mark.parametrize(
    "url",
    [
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "http://8.8.8.8/ocr",
        "https://api.openai.com/v1/chat/completions",
        "ftp://127.0.0.1/ocr",
    ],
)
def test_public_addresses_are_blocked(url):
    audit = guard.audit_url(url)
    assert audit.allowed is False
    assert audit.reason


def test_check_url_raises_and_names_the_reason():
    with pytest.raises(guard.OfflineViolation) as info:
        guard.check_url("https://example.com/v1")
    assert "离线守卫" in str(info.value)


def test_safe_urlopen_blocks_before_network():
    with pytest.raises(guard.OfflineViolation):
        guard.safe_urlopen("https://dashscope.aliyuncs.com/compatible-mode/v1")


def test_configured_endpoints_stay_inside():
    for audit in guard.audit_configured_endpoints():
        assert audit.allowed is True, audit