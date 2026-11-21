"""极简身份的会话：教师/管理员用口令登录，家长用一次性链接。

幼儿园场景不该让老师记账号密码；一个 4-6 位口令 + 签名 Cookie 就够，
权限校验一律在接口层做（规范 §6：不靠前端隐藏）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from .config import get_settings

COOKIE_NAME = "rc_session"
ROLE_TEACHER = "teacher"
ROLE_ADMIN = "admin"
MAX_AGE = 60 * 60 * 12


def _sign(payload: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def issue(role: str) -> str:
    expires = int(time.time()) + MAX_AGE
    payload = f"{role}|{expires}"
    token = f"{payload}|{_sign(payload)}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def verify(token: str | None) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        role, expires, signature = raw.split("|")
    except Exception:
        return None
    if _sign(f"{role}|{expires}") != signature:
        return None
    if int(expires) < int(time.time()):
        return None
    return role if role in {ROLE_TEACHER, ROLE_ADMIN} else None


def check_pin(pin: str) -> str | None:
    settings = get_settings()
    pin = (pin or "").strip()
    if pin and pin == settings.admin_pin:
        return ROLE_ADMIN
    if pin and pin == settings.teacher_pin:
        return ROLE_TEACHER
    return None


def role_of(request: Request) -> str | None:
    return verify(request.cookies.get(COOKIE_NAME))


def require_teacher(request: Request) -> str:
    role = role_of(request)
    if not role:
        raise HTTPException(status_code=401, detail="请先用口令登录教师端")
    return role


def require_admin(request: Request) -> str:
    role = role_of(request)
    if role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="这一步需要管理员口令")
    return role


def parent_url(token: str) -> str:
    return f"/parent/{token}"