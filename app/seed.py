"""开发/演示用示例数据：一支绘本、几个孩子、一次已结束的共读。

真实部署不会执行：``SEED_DEMO=1`` 才会铺（默认 0），园里首次启动是空库，
老师在教师端录入自己班的孩子和绘本。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

from . import db
from .config import get_settings
from .utils import now


def _placeholder(path: Path, palette: tuple[str, str, str], variant: int) -> None:
    """生成一张没有任何文字的色块封面，避免仓库里塞版权图片。"""
    width, height = 900, 1200
    image = Image.new("RGB", (width, height), palette[0])
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, int(height * 0.62), width, height], fill=palette[1])
    draw.ellipse(
        [width * 0.62, height * 0.1, width * 0.86, height * 0.22], fill=palette[2]
    )
    if variant == 1:
        draw.ellipse([width * 0.18, height * 0.55, width * 0.42, height * 0.72], fill="#ffffff")
        draw.ellipse([width * 0.24, height * 0.58, width * 0.30, height * 0.62], fill="#3b3b3b")
        draw.ellipse([width * 0.32, height * 0.58, width * 0.38, height * 0.62], fill="#3b3b3b")
        draw.ellipse([width * 0.56, height * 0.5, width * 0.74, height * 0.62], fill="#ffe082")
        draw.line(
            [width * 0.3, height * 0.68, width * 0.52, height * 0.5], fill="#8d6e63", width=6
        )
    elif variant == 2:
        draw.ellipse([width * 0.15, height * 0.52, width * 0.42, height * 0.72], fill="#8d6e63")
        draw.ellipse([width * 0.52, height * 0.56, width * 0.78, height * 0.74], fill="#ffffff")
        draw.ellipse([width * 0.58, height * 0.6, width * 0.64, height * 0.64], fill="#3b3b3b")
    else:
        draw.ellipse([width * 0.1, height * 0.15, width * 0.3, height * 0.27], fill="#ffcc80")
        draw.ellipse([width * 0.6, height * 0.58, width * 0.84, height * 0.74], fill="#ffffff")
        draw.polygon(
            [(width * 0.64, height * 0.66), (width * 0.8, height * 0.55), (width * 0.8, height * 0.72)],
            fill="#ffcc80",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=88)


def _page_image(book_id: int, page_no: int, variant: int) -> str:
    settings = get_settings()
    folder = settings.library_dir / str(book_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{page_no}.jpg"
    legacy = settings.root / "legacy_y" / "y" / f"test_page{'' if page_no == 1 else '2'}.jpg"
    if legacy.exists() and page_no <= 2:
        path.write_bytes(legacy.read_bytes())
    elif not path.exists():
        palettes = [("#dff0ff", "#a8d8b9", "#ffd54f"), ("#ffe9d6", "#b7e4c7", "#ffab91"), ("#f3e5ff", "#c8e6c9", "#90caf9")]
        _placeholder(path, palettes[(variant - 1) % len(palettes)], variant)
    return f"{book_id}/{path.name}"


def ensure_seed() -> None:
    if not get_settings().seed_demo:
        return
    if db.list_books():
        return

    settings = get_settings()
    token_xiaoyu = uuid.uuid4().hex
    db.create_child("小雨", 54, "中一班", ["zh"], "小雨妈妈", True, token_xiaoyu)
    db.create_child("乐乐", 42, "中一班", ["zh"], "乐乐爸爸", True, uuid.uuid4().hex)
    db.create_child("小虎", 60, "大一班", ["zh"], "", False, uuid.uuid4().hex)
    db.create_child("萌萌", 28, "托班", ["zh"], "萌萌妈妈", True, uuid.uuid4().hex)

    # 《小兔子找风筝》：三层摘要与题库均按"只含画面事实"写好，可被自检直接放行
    book_id = db.create_book("小兔子找风筝", "zh", "友情 / 自然", 36, 72)
    pages = [
        {
            "page_no": 1,
            "raw_text": "小兔子拿着风筝，跑向草地。",
            "characters": ["小兔子"],
            "objects": ["风筝", "草地"],
            "scene": "晴天",
            "relations": ["小兔子拿着风筝在草地上跑"],
            "mood": "开心",
            "keywords": ["小兔子", "风筝", "草地", "跑"],
            "faithful": "画面里有小兔子。小兔子拿着风筝。它在草地上跑。",
            "script": "看呀，小兔子拿着风筝，在草地上跑。",
        },
        {
            "page_no": 2,
            "raw_text": "小熊摔倒了，小兔子去扶他。",
            "characters": ["小熊", "小兔子"],
            "objects": ["草地"],
            "scene": "草地上",
            "relations": ["小熊摔倒在草地上", "小兔子去扶他"],
            "mood": "有点难过又温暖",
            "keywords": ["小熊", "小兔子", "摔倒", "扶"],
            "faithful": "画面里有小熊、小兔子。小熊摔倒了。小兔子去扶他。",
            "script": "看呀，小熊摔倒了，小兔子去扶他。",
        },
        {
            "page_no": 3,
            "raw_text": "天快黑了，它们一起收风筝。",
            "characters": ["小兔子", "小熊"],
            "objects": ["风筝", "草地"],
            "scene": "傍晚的草地",
            "relations": ["它们一起收风筝"],
            "mood": "安静",
            "keywords": ["小兔子", "小熊", "风筝", "收"],
            "faithful": "画面里有小兔子、小熊。天快黑了。它们一起收风筝。",
            "script": "看呀，天快黑了，它们一起收风筝。",
        },
    ]
    page_ids: dict[int, int] = {}
    for item in pages:
        page_id = db.upsert_page(
            book_id,
            item["page_no"],
            image_path=_page_image(book_id, item["page_no"], item["page_no"]),
            raw_text=item["raw_text"],
            faithful_description=item["faithful"],
            read_aloud_script=item["script"],
            keywords=item["keywords"],
            characters=item["characters"],
            objects=item["objects"],
            scene=item["scene"],
            relations=item["relations"],
            mood=item["mood"],
            ocr_confidence=0.96,
            status="ready",
        )
        page_ids[item["page_no"]] = page_id

    bank = [
        (1, "小兔子手里拿着什么呀？", "observe"),
        (1, "你觉得小兔子接下来会去哪里？", "guess"),
        (2, "你觉得小熊心里什么感觉呀？", "empathy"),
        (2, "小兔子在做什么呀？", "observe"),
        (3, "刚才这一页，谁拿着风筝？", "retell"),
    ]
    for page_no, text, qtype in bank:
        db.add_bank_item(page_ids[page_no], text, qtype, 36, 72, source="seed")

    # 英文绘本：验证"语言跟随绘本"
    en_book = db.create_book("The Little Cat", "en", "animals", 36, 72)
    db.upsert_page(
        en_book,
        1,
        image_path=_page_image(en_book, 1, 1),
        raw_text="The little cat sat on the mat.",
        faithful_description="You can see a cat. The cat sits on a mat.",
        read_aloud_script="You can see a cat. The cat sits on a mat.",
        keywords=["cat", "mat"],
        characters=["cat"],
        objects=["mat"],
        scene="a warm room",
        relations=["the cat sits on the mat"],
        ocr_confidence=0.97,
        status="ready",
    )
    db.add_bank_item(
        db.list_pages(en_book)[0]["id"],
        "What is the cat sitting on?",
        "observe",
        36,
        72,
        source="seed",
    )

    # 一次已结束的示例共读，让教师端首屏不是空的
    child = db.get_child_by_parent_token(token_xiaoyu)
    session_id = "demo-session-0001"
    db.create_session(session_id, child["id"], book_id, uuid.uuid4().hex, "demo")
    turn_ids = []
    for page_no, question, answer, understood, reply in (
        (1, "小兔子手里拿着什么呀？", "风筝", "yes", "哇，你看到风筝了，真仔细！"),
        (2, "你觉得小熊心里什么感觉呀？", "", "no", "没关系，我们一起看看这里……"),
    ):
        turn_id = db.add_turn(
            session_id,
            page_no,
            page_ids[page_no],
            question,
            "observe" if page_no == 1 else "empathy",
            {
                "verdict": "pass",
                "score": 100,
                "summary": "全部检查通过",
                "checks": [{"id": "question_grounding", "name": "只问画面里的内容", "status": "ok", "detail": ""}],
                "source": "bank",
                "expected": "风筝" if page_no == 1 else "小熊",
                "band": "4_6",
                "checked_at": now(),
            },
            source="bank",
        )
        db.update_turn(
            turn_id,
            answer_text=answer,
            asr_confidence=0.92 if answer else 0,
            understood=understood,
            action="encourage" if answer else "icon_choice",
            reply=reply,
        )
        turn_ids.append(turn_id)
    db.end_session(session_id)
    db.add_advice(child["id"], session_id, "本次有 1 次没等到回答，建议下次先示范一句。", "waiting", [turn_ids[1]])
    db.add_advice(child["id"], session_id, "孩子能说出「风筝」，可延伸同主题绘本。", "interest", [turn_ids[0]])
    db.add_advice(child["id"], session_id, "本次共读完成 2 页，节奏平稳。", "pace", turn_ids[:1])

    # 一份已发布的周报，家长扫码即可看到（用于演示，不含任何排名或标签）
    payload = {
        "week_start": "2026-11-14",
        "week_end": "2026-11-21",
        "headline": "小雨 这周一起读了 2 页绘本",
        "summary": "共读 2 页，其中 1 页有回应。",
        "highlights": ["「小兔子手里拿着什么呀？」— 孩子说：风筝"],
        "teacher_note": "对风筝很感兴趣，下次可以读同主题绘本。",
        "suggest_home": "在家可以问孩子：今天书里谁最有趣？",
    }
    report_id = db.add_report(child["id"], payload["week_start"], payload["week_end"], payload, "2027-08-31")
    db.publish_report(report_id)

    db.audit("system", "seed_demo", {"children": 4, "books": 2, "session": session_id})
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "seed.json").write_text(
        json.dumps({"child_token": token_xiaoyu, "book_id": book_id}, ensure_ascii=False),
        encoding="utf-8",
    )