"""耗时与调用埋点（规范 A9：离线版算力治理）。

没有 token 费，但要盯住"调用次数 / 延迟 / 失败率 / 高耗时页"，
这些数据同时喂给教师端运维面板和 AI 自检的系统自检。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Iterator

from .. import db
from ..utils import short_hash

# 一页处理会记 7~8 条；攒够一次批量写，避免每条都 connect/commit（实测单条 10ms）。
# 不足 FLUSH_EVERY 条的尾巴由调用方 aflush() 收尾，例如 pipeline 的 finally。
FLUSH_EVERY = 12


class Tracer:
    """一次页面处理链路共用一个 trace_id。"""

    def __init__(self, session_id: str = "", page_no: int = 0) -> None:
        self.session_id = session_id
        self.page_no = page_no
        self.trace_id = short_hash(f"{session_id}:{page_no}:{time.time()}", 10)
        self._buffer: list[dict] = []

    def record(
        self,
        stage: str,
        latency_ms: float,
        *,
        provider: str = "",
        model: str = "",
        ok: bool = True,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str = "",
    ) -> None:
        self._buffer.append(
            {
                "trace_id": self.trace_id,
                "session_id": self.session_id,
                "page_no": self.page_no,
                "stage": stage,
                "provider": provider,
                "model": model,
                "latency_ms": round(latency_ms, 1),
                "ok": 1 if ok else 0,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": error[:300],
            }
        )
        if len(self._buffer) >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> int:
        """同步落库（批写）；埋点写入失败不能影响主链路。"""
        if not self._buffer:
            return 0
        rows, self._buffer = self._buffer, []
        try:
            return db.add_metrics(rows)
        except Exception:
            return 0

    async def aflush(self) -> int:
        """异步落库：把那次 connect/commit 挪到线程，不卡事件循环。"""
        if not self._buffer:
            return 0
        return await asyncio.to_thread(self.flush)

    @contextmanager
    def timed(self, stage: str) -> Iterator[dict]:
        """用法：with tracer.timed("A1_ocr") as slot: ... slot["ok"]=False"""
        slot: dict = {"ok": True, "error": "", "tokens_in": 0, "tokens_out": 0}
        started = time.perf_counter()
        try:
            yield slot
        except Exception as exc:  # 记录后继续抛，交给上层降级
            slot["ok"] = False
            slot["error"] = str(exc)
            raise
        finally:
            self.record(
                stage,
                (time.perf_counter() - started) * 1000,
                provider=slot.get("provider", ""),
                model=slot.get("model", ""),
                ok=bool(slot["ok"]),
                tokens_in=int(slot.get("tokens_in") or 0),
                tokens_out=int(slot.get("tokens_out") or 0),
                error=str(slot.get("error") or ""),
            )


def today_summary() -> dict:
    from ..utils import today_start

    summary = db.metrics_summary(today_start())
    summary["slowest"] = db.slowest_pages(today_start(), 10)
    return summary