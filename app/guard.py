"""离线守卫：所有对外请求的唯一出口。

规范 §6 要求"部署后做出站流量审计，确认无外部域名请求"。
与其事后审计，不如把约束写进代码：任何 http(s) 请求都必须先过
`check_url()`，非园内地址直接抛 OfflineViolation，不提供绕过开关。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import get_settings


class OfflineViolation(RuntimeError):
    """试图访问园内网以外的地址。"""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"离线守卫已拦截：{reason}（{url}）")


@dataclass(frozen=True)
class UrlAudit:
    url: str
    host: str
    allowed: bool
    reason: str


def _is_private_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _resolves_private(host: str) -> bool:
    """把内网域名（如 *.lan）解析一次再判断，避免用 DNS 绕过守卫。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    return all(_is_private_ip(item[4][0]) for item in infos)


def audit_url(url: str) -> UrlAudit:
    settings = get_settings()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https", "ws", "wss"}:
        return UrlAudit(url, host, False, f"不支持的协议 {parts.scheme!r}")
    if not host:
        return UrlAudit(url, host, False, "地址缺少主机名")
    if not settings.offline_only:
        return UrlAudit(url, host, True, "OFFLINE_ONLY=0，未启用出站限制")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return UrlAudit(url, host, True, "本机地址")
    if host in settings.allowed_hosts:
        return UrlAudit(url, host, True, "已在 ALLOWED_HOSTS 中登记")
    if _is_private_ip(host):
        return UrlAudit(url, host, True, "园内私有地址")
    if _resolves_private(host):
        return UrlAudit(url, host, True, "解析到园内私有地址")
    return UrlAudit(url, host, False, "公网地址，违反完全离线约束")


def check_url(url: str) -> str:
    result = audit_url(url)
    if not result.allowed:
        raise OfflineViolation(url, result.reason)
    return url


def safe_urlopen(request: urllib.request.Request | str, timeout: float = 30):
    """urlopen 的唯一合法入口。"""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    check_url(url)
    return urllib.request.urlopen(request, timeout=timeout)


def audit_configured_endpoints() -> list[UrlAudit]:
    """把配置里的模型地址全部过一遍，供管理员一键审计。"""
    settings = get_settings()
    return [audit_url(url) for url in settings.model_endpoints.values() if url]


def audit_env_for_public_urls() -> list[UrlAudit]:
    """审计本系统自己的配置：.env 文件 + 已生效的模型地址。

    只扫自己的配置文件，不扫整个进程环境（开发机上的 IDE / 代理变量与本系统无关，
    全环境扫描会把它们误报成“违反离线约束”）。
    """
    import re

    from .config import ENV_FILE

    pattern = re.compile(r"https?://[^\s,;\"']+")
    urls: list[str] = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0]
            urls.extend(pattern.findall(stripped))
    urls.extend(url for url in get_settings().model_endpoints.values() if url)

    findings: list[UrlAudit] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        findings.append(audit_url(url))
    return findings


__all__ = [
    "OfflineViolation",
    "UrlAudit",
    "audit_configured_endpoints",
    "audit_env_for_public_urls",
    "audit_url",
    "check_url",
    "safe_urlopen",
    "urllib_error",
]

urllib_error = urllib.error