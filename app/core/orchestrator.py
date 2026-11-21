"""A8 中央编排：显式状态机 + 事件总线 + 翻页打断。

IDLE → CAPTURING → ANALYZING → QUESTIONING → LISTENING → RESPONDING → LISTENING

- 每个会话一个 SessionRuntime，`session_id → runtime` 字典隔离多设备，
  修掉原 y.zip 里 `_voice_session` 单实例串线的问题；
- 翻页立即取消上一页的延迟提问与超时看门狗；
- 不让 LLM 决定调哪个工具，LLM 只在 A3/A4/A7/A10 生成文本。
"""

from __future__ import annotations

import asyncio
import base64
import io
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .. import db
from ..config import get_settings
from ..services import speech
from ..services.metrics import Tracer
from ..services.selfcheck import check_session
from ..utils import now, truncate
from .pipeline import PageRequest, analyze_page, build_weekly_report, generate_advice, handle_answer

STATE_LABELS = {
    "IDLE": "休息中",
    "CAPTURING": "准备中",
    "ANALYZING": "识别中",
    "QUESTIONING": "正在提问",
    "LISTENING": "在听你说",
    "RESPONDING": "在回应",
}


class EventBus:
    """进程内 pub/sub。教师端实时视图、机器人端都从这里取事件。"""

    def __init__(self) -> None:
        self._subscribers: set[tuple[asyncio.Queue, str | None]] = set()
        self.history: deque[dict] = deque(maxlen=120)

    def subscribe(self, session_id: str | None = None) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add((queue, session_id))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        for item in list(self._subscribers):
            if item[0] is queue:
                self._subscribers.discard(item)

    def publish(self, event: str, data: dict, *, session_id: str = "", keep: bool = True) -> None:
        payload = {"type": "event", "event": event, "session_id": session_id, "data": data, "ts": now()}
        if keep:
            self.history.append(payload)
        dead = []
        for queue, scope in list(self._subscribers):
            if scope and scope != session_id:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)

    def recent(self, session_id: str | None = None, limit: int = 40) -> list[dict]:
        items = [item for item in self.history if not session_id or item["session_id"] == session_id]
        return items[-limit:]


bus = EventBus()
RUNTIMES: dict[str, "SessionRuntime"] = {}


@dataclass
class SessionRuntime:
    session: dict
    child: dict
    book: dict
    pages: list[dict]
    state: str = "IDLE"
    page_no: int = 0
    paused: bool = False
    current: dict | None = None
    last_turn: dict | None = None
    last_thumb: dict = field(default_factory=dict)  # 最近一页的小图，提问/回答时给孩子留个画面参照
    history: deque = field(default_factory=lambda: deque(maxlen=3))
    tts_urls: set[str] = field(default_factory=set)  # 本场产生的播放缓存，结束时清掉
    _tasks: set[asyncio.Task] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closed: bool = False

    # ---------- 构造 ----------

    @classmethod
    def load(cls, session_id: str) -> "SessionRuntime | None":
        runtime = RUNTIMES.get(session_id)
        if runtime:
            return runtime
        session = db.get_session(session_id)
        if not session or session["status"] != "active":
            return None
        child = db.get_child(session["child_id"])
        book = db.get_book(session["book_id"])
        if not child or not book:
            return None
        pages = db.list_pages(book["id"])
        runtime = cls(session=session, child=child, book=book, pages=pages)
        for turn in db.list_turns(session["id"])[-3:]:
            runtime.history.append(
                {
                    "question": turn.get("question", ""),
                    "closed": len(turn.get("question", "")) <= 12 and "吗？" in turn.get("question", ""),
                    "answer": turn.get("answer_text", ""),
                    "understood": turn.get("understood", ""),
                }
            )
        RUNTIMES[session_id] = runtime
        return runtime

    # ---------- 事件 ----------

    def _emit(self, event: str, data: dict, *, keep: bool = True) -> None:
        payload = {"state": self.state, "state_label": STATE_LABELS.get(self.state, ""), **data}
        bus.publish(event, payload, session_id=self.session["id"], keep=keep)

    def snapshot(self) -> dict:
        return {
            "session_id": self.session["id"],
            "child": {"id": self.child["id"], "name": self.child["name"], "age_months": self.child["age_months"]},
            "book": {"id": self.book["id"], "title": self.book["title"], "language": self.book["language"]},
            "state": self.state,
            "state_label": STATE_LABELS.get(self.state, ""),
            "page_no": self.page_no,
            "pages_total": len(self.pages),
            "paused": self.paused,
            "current": self.current,
            "last_turn": self.last_turn,
            "last_thumb": self.last_thumb,
        }

    # ---------- 任务管理 ----------

    def _spawn(self, factory: Callable[[], Awaitable], delay: float = 0) -> asyncio.Task:
        """传工厂函数而不是协程对象：任务被取消时不会留下未 await 的协程。"""
        async def runner():
            try:
                if delay:
                    await asyncio.sleep(delay)
                await factory()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # 任何后台异常都要让老师看到人话提示
                self._emit("error", {"human_hint": "这一步没成功，我们再来一次好吗？", "detail": str(exc)[:120]})

        task = asyncio.get_running_loop().create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ---------- 主流程 ----------

    async def start(self) -> dict:
        if not self.pages:
            self._emit("error", {"human_hint": "这本绘本还没有页面，请老师先在绘本库里补上。"})
            return {"ok": False, "human_hint": "这本绘本还没有页面"}
        db.update_session(self.session["id"], state="IDLE")
        self._emit("started", {"session": self.snapshot()})
        return {"ok": True, "session": self.snapshot()}

    async def capture(self, page_no: int, image_bytes: bytes) -> dict:
        async with self._lock:
            if self._closed:
                return {"ok": False, "human_hint": "这次共读已经结束了。"}
            settings = get_settings()
            if page_no > settings.page_limit_per_session:
                return {"ok": False, "human_hint": "已经读了很多页啦，休息一下吧。"}
            # 翻页打断：取消上一页的延迟提问与超时
            self._cancel_tasks()
            self.page_no = page_no
            self.state = "CAPTURING"
            self._emit("state", {"page_no": page_no})
            self.state = "ANALYZING"
            self._emit("state", {"page_no": page_no})

            library_page = next((page for page in self.pages if page["page_no"] == page_no), None)
            tracer = Tracer(self.session["id"], page_no)
            # 缩略图与识别并行；PIL 解码放到线程，不阻塞其他会话
            thumb_task = asyncio.create_task(asyncio.to_thread(_thumbnail, image_bytes))
            result = await analyze_page(
                PageRequest(
                    session_id=self.session["id"],
                    page_no=page_no,
                    child=self.child,
                    book=self.book,
                    library_page=library_page,
                    image_bytes=image_bytes,
                    history=list(self.history),
                ),
                tracer=tracer,
            )
            if not result.get("ok"):
                self.state = "CAPTURING"
                self._emit(
                    "quality",
                    {
                        "page_no": page_no,
                        "human_hint": result.get("human_hint", ""),
                        "actions": result.get("actions", []),
                        "quality": result.get("quality", {}),
                        "thumbnail": await thumb_task,
                    },
                )
                return result

            self.current = {
                "turn_id": result["turn_id"],
                "page_no": page_no,
                "question": result["question"],
                "summary": result["summary"],
                "facts": result["facts"],
                "checker": result["checker"],
                "source": result["source"],
                "display": result["display"],
            }
            self.last_thumb = await thumb_task
            turn = db.q1("SELECT * FROM turns WHERE id=?", (result["turn_id"],))
            self.last_turn = turn

            self._emit(
                "page",
                {
                    "page_no": page_no,
                    "display": result["display"],
                    "facts": result["facts"],
                    "checker": result["checker"],
                    "source": result["source"],
                    "latency_ms": result["latency_ms"],
                    "thumbnail": self.last_thumb,
                    "question": result["question"],
                },
            )
            db.update_session(self.session["id"], last_page_no=page_no)

            # 留出看画面的时间，再开口提问；连续翻页会取消旧任务
            self.state = "QUESTIONING"
            self._emit("state", {"page_no": page_no})
            self._spawn(lambda: self._ask_after_delay(result["question"]))
            return result

    async def _ask_after_delay(self, question: dict) -> None:
        if self._closed or self.paused:
            return
        # TTS 与“留出看画面的时间”并行：合成耗时不能加在孩子等待上
        audio_task = asyncio.ensure_future(
            asyncio.to_thread(speech.synthesize, question["question"], "")
        )
        try:
            await asyncio.sleep(get_settings().question_delay_seconds)
            if self._closed or self.paused:
                return
            audio = self._remember_audio(await audio_task)
        finally:
            if not audio_task.done():
                audio_task.cancel()
        self.state = "LISTENING"
        self._emit(
            "ask",
            {
                "page_no": self.page_no,
                "question": question,
                "audio": audio,
                "choices": question.get("choices", []),
                "thumbnail": self.last_thumb,
            },
        )
        settings = get_settings()
        self._spawn(lambda: self._listen_timeout("first"), settings.listen_timeout_seconds)
        self._spawn(lambda: self._listen_timeout("second"), settings.listen_timeout_seconds + settings.teacher_timeout_seconds)

    async def _listen_timeout(self, stage: str) -> None:
        if self._closed or self.state != "LISTENING":
            return
        question = (self.current or {}).get("question") or {}
        if stage == "first":
            choices = question.get("choices") or []
            self._emit(
                "timeout",
                {
                    "stage": "icon_choice",
                    "human_hint": "没关系，我们一起看看这里……",
                    "choices": choices,
                },
            )
            return
        self._emit(
            "timeout",
            {"stage": "notify_teacher", "human_hint": "孩子这次没有回答，老师可以过来帮一下。"},
        )

    async def answer(self, text: str = "", audio: bytes = b"") -> dict:
        async with self._lock:
            if self._closed:
                return {"ok": False, "human_hint": "这次共读已经结束了。"}
            if not self.last_turn or not self.current:
                return {"ok": False, "human_hint": "还没有提问呢，先拍一页吧。"}
            self._cancel_tasks()
            self.state = "RESPONDING"
            self._emit("state", {"page_no": self.page_no})
            result = await handle_answer(
                session=self.session,
                child=self.child,
                book=self.book,
                page_no=self.page_no,
                turn=self.last_turn,
                facts=self.current.get("facts") or {},
                answer_text=text,
                audio=audio,
            )
            turn = db.q1("SELECT * FROM turns WHERE id=?", (self.last_turn["id"],))
            self.last_turn = turn
            self.history.append(
                {
                    "question": turn.get("question", ""),
                    "closed": "吗？" in (turn.get("question") or ""),
                    "answer": turn.get("answer_text", ""),
                    "understood": turn.get("understood", ""),
                }
            )
            audio = self._remember_audio(result.get("audio", {}))
            self.state = "LISTENING"
            self._emit(
                "reply",
                {
                    "page_no": self.page_no,
                    "answer_text": result.get("answer_text", ""),
                    "understood": result.get("understood", ""),
                    "action": result.get("action", ""),
                    "reply": result.get("reply", ""),
                    "audio": audio,
                    "asr": result.get("asr", {}),
                },
            )
            return result

    async def control(self, action: str) -> dict:
        if action == "pause":
            self.paused = True
            self._cancel_tasks()
            self.state = "IDLE"
            self._emit("state", {"paused": True})
            return {"ok": True, "state_label": "休息中"}
        if action == "resume":
            self.paused = False
            self.state = "IDLE"
            self._emit("state", {"paused": False})
            return {"ok": True}
        if action == "reask":
            self._cancel_tasks()
            question = (self.current or {}).get("question")
            if not question:
                return {"ok": False, "human_hint": "这一页还没有问题，先拍一页吧。"}
            audio = self._remember_audio(await asyncio.to_thread(speech.synthesize, question["question"], ""))
            self.state = "LISTENING"
            self._emit("ask", {"page_no": self.page_no, "question": question, "audio": audio, "choices": question.get("choices", [])})
            return {"ok": True}
        if action == "skip":
            self._cancel_tasks()
            self.state = "LISTENING"
            self._emit("skipped", {"page_no": self.page_no, "human_hint": "这一页先跳过，翻到下一页吧。"})
            return {"ok": True}
        if action == "teacher_talk":
            self._cancel_tasks()
            self.state = "IDLE"
            self._emit("teacher_talk", {"page_no": self.page_no, "human_hint": "老师来提问啦，我先安静听。"})
            return {"ok": True}
        if action == "end":
            return await self.end()
        return {"ok": False, "human_hint": f"不认识的操作：{action}"}

    def _remember_audio(self, audio: dict | None) -> dict:
        """记下这一句的播放缓存地址，会话结束时统一清掉。"""
        audio = audio or {}
        url = audio.get("url") or ""
        if url:
            self.tts_urls.add(url)
        return audio

    async def end(self) -> dict:
        async with self._lock:
            if self._closed:
                return {"ok": True, "already": True}
            self._cancel_tasks()
            self.state = "IDLE"
            turns = db.list_turns(self.session["id"])
            advice_items, advice_report = await generate_advice(self.session, self.child, turns)
            db.end_session(self.session["id"])
            session_check = check_session(self.session, turns, advice_items, memory_cleared=False)

            payload, report = build_weekly_report(self.child, turns, advice_items)
            report_id = None
            if self.child.get("guardian_consent"):
                from datetime import datetime, timedelta

                expires = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
                report_id = db.add_report(
                    self.child["id"], payload["week_start"], payload["week_end"], payload, expires
                )

            summary = {
                "pages": len(turns),
                "answered": len([turn for turn in turns if turn.get("answer_text")]),
                "advice": advice_items,
                "advice_check": advice_report,
                "session_check": session_check,
                "weekly_report_id": report_id,
                "child": {"id": self.child["id"], "name": self.child["name"]},
            }
            # 工作内存激进释放：清空事实/摘要/历史，只留数据库短记录
            self.current = None
            self.last_turn = None
            self.history.clear()
            # 播放缓存一并清掉（别的并行会话还在用的那句跳过）
            shared: set[str] = set()
            for runtime in RUNTIMES.values():
                if runtime is not self:
                    shared |= runtime.tts_urls
            speech.forget(self.tts_urls, keep=shared)
            self.tts_urls.clear()
            self._closed = True
            check_session(self.session, turns, advice_items, memory_cleared=True)
            RUNTIMES.pop(self.session["id"], None)
            self._emit("ended", summary)
            return {"ok": True, **summary}


def _thumbnail(image_bytes: bytes, max_side: int = 320) -> dict:
    """只在内存里生成一张小缩略图给教师端看，不落盘、不进数据库。"""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=60)
        return {
            "mime": "image/jpeg",
            "b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
    except Exception:
        return {}


def list_active_sessions() -> list[dict]:
    rows = db.q(
        "SELECT s.*, c.name AS child_name, b.title AS book_title FROM sessions s"
        " JOIN children c ON c.id=s.child_id JOIN books b ON b.id=s.book_id"
        " WHERE s.status='active' ORDER BY s.created_at DESC"
    )
    for row in rows:
        runtime = RUNTIMES.get(row["id"])
        row["state"] = runtime.state if runtime else row.get("state")
        row["state_label"] = STATE_LABELS.get(row["state"] or "IDLE", "")
        row["age_months"] = db.get_child(row["child_id"]).get("age_months") if db.get_child(row["child_id"]) else 0
    return rows


def page_display(title: str, text: str) -> str:
    return f"{title}：{truncate(text, 40)}"