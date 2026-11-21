"""链路性能基准：进程内直接跑 A0→A11，输出各阶段与端到端的 p50/p95。

为什么要进程内跑：把网络、WebSocket、浏览器都排除掉，只看"这一页到底花了多少算力时间"。
Web 层的开销另有说明（见 README §性能）——本地回环上通常只有几毫秒。

用法：
    python _tools\\bench.py            # 默认 20 次单会话 + 4 并发
    python _tools\\bench.py 50         # 改次数
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="bench_"))
os.environ.setdefault("SECRET_KEY", "bench-secret")
os.environ.setdefault("TEACHER_PIN", "2026")
os.environ.setdefault("ADMIN_PIN", "8642")
# 基准需要一套固定孩子/绘本，显式打开示例数据
os.environ.setdefault("SEED_DEMO", "1")

from PIL import Image, ImageDraw  # noqa: E402

from app import db  # noqa: E402
from app.core.pipeline import PageRequest, analyze_page, handle_answer  # noqa: E402
from app.seed import ensure_seed  # noqa: E402
from app.services.metrics import Tracer  # noqa: E402
from app.utils import short_hash  # noqa: E402

STAGES = [
    "A0_quality",
    "A1_ocr",
    "A2_vlm",
    "A3_summary",
    "A11_summary",
    "A4_ask",
    "A11_question",
    "A7_understand",
    "A6_asr",
    "page_total",
]


def page_jpeg() -> bytes:
    """造一张能过质检的页面：浅底 + 硬边色块（PIL 生成，不依赖素材）。"""
    image = Image.new("RGB", (1200, 1600), (248, 246, 240))
    draw = ImageDraw.Draw(image)
    for row in range(10):
        top = 140 + row * 130
        draw.rectangle([100, top, 1100, top + 60], fill=(40 + row * 12, 80, 120))
    draw.ellipse([300, 500, 900, 1100], outline=(30, 30, 30), width=18)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def make_session(child_id: int, book_id: int, label: str) -> str:
    session_id = short_hash(f"{label}:{time.time()}", 16)
    db.create_session(session_id, child_id, book_id, token=short_hash(session_id, 12))
    return session_id


async def one_page(session_id: str, child: dict, book: dict, page: dict, image: bytes) -> tuple[float, dict, dict]:
    tracer = Tracer(session_id, page["page_no"])
    started = time.perf_counter()
    result = await analyze_page(
        PageRequest(
            session_id=session_id,
            page_no=page["page_no"],
            child=child,
            book=book,
            library_page=page,
            image_bytes=image,
            history=[],
        ),
        tracer=tracer,
    )
    wall = (time.perf_counter() - started) * 1000
    rows = db.q("SELECT stage, latency_ms FROM metrics WHERE trace_id=?", (tracer.trace_id,))
    return wall, {row["stage"]: float(row["latency_ms"]) for row in rows}, result.get("facts") or {}


async def main(rounds: int) -> None:
    db.init_db()
    ensure_seed()
    child = db.list_children()[0]
    book = db.list_books()[0]
    book = db.get_book(book["id"])
    pages = db.list_pages(book["id"])
    if not pages:
        print("没有页面，先 python run.py 生成种子数据。")
        return
    page = pages[0]
    image = page_jpeg()

    # 预热：避免把首次 import / 首次 PIL 解码算进统计
    warm = make_session(child["id"], book["id"], "warm")
    await one_page(warm, child, book, page, image)

    print(f"模式: ocr={os.environ.get('OCR_MODE', 'stub')} llm={os.environ.get('LLM_MODE', 'stub')} "
          f"tts={os.environ.get('TTS_MODE', 'stub')}  轮次: {rounds}\n")

    # 1) 单会话串行
    session_id = make_session(child["id"], book["id"], "serial")
    walls: list[float] = []
    facts: dict = {}
    stages: dict[str, list[float]] = {name: [] for name in STAGES}
    for _ in range(rounds):
        wall, per_stage, facts = await one_page(session_id, child, book, page, image)
        walls.append(wall)
        for name, value in per_stage.items():
            stages.setdefault(name, []).append(value)

    turns = db.list_turns(session_id)

    # 2) 回答链路
    answers: list[float] = []
    for turn in turns[: rounds]:
        started = time.perf_counter()
        await handle_answer(
            session=db.get_session(session_id),
            child=child,
            book=book,
            page_no=turn["page_no"],
            turn=turn,
            facts=facts,
            answer_text="小兔子拿着风筝",
        )
        answers.append((time.perf_counter() - started) * 1000)

    # 3) 4 路并发（模拟 4 台机器人同时拍页）
    sessions = [make_session(child["id"], book["id"], f"conc{i}") for i in range(4)]
    started = time.perf_counter()
    concurrent = await asyncio.gather(
        *[one_page(sid, child, book, page, image) for sid in sessions]
    )
    concurrent_total = (time.perf_counter() - started) * 1000
    concurrent_walls = [wall for wall, _, _ in concurrent]

    print(f"{'阶段':<14}{'p50':>9}{'p95':>9}{'max':>9}   样本")
    for name in STAGES:
        values = stages.get(name) or []
        if not values:
            continue
        print(f"{name:<14}{pct(values, .5):>8.0f}ms{pct(values, .95):>7.0f}ms{max(values):>7.0f}ms   {len(values)}")
    print(f"{'端到端(page)':<12}{pct(walls, .5):>8.0f}ms{pct(walls, .95):>7.0f}ms{max(walls):>7.0f}ms   {len(walls)}")
    print(f"{'回答链路':<14}{pct(answers, .5):>8.0f}ms{pct(answers, .95):>7.0f}ms{max(answers):>7.0f}ms   {len(answers)}")
    print(
        f"4 路并发: 单页 p50 {pct(concurrent_walls, .5):.0f}ms / 整体 {concurrent_total:.0f}ms "
        f"(串行预期 {pct(walls, .5) * 4:.0f}ms)"
    )
    print(f"吞吐: 串行 {1000 / max(pct(walls, .5), 1e-6):.1f} 页/秒，4 并发 {4000 / max(concurrent_total, 1e-6):.1f} 页/秒")


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    asyncio.run(main(rounds))