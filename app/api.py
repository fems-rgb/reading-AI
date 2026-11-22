"""HTTP / WebSocket 接口层。

约定：
- 教师/管理员接口一律 `Depends(auth.require_teacher|require_admin)`；
- 家长接口只认一次性随机 token，且只能读自己孩子的已发布周报；
- 设备接口认 session token，设备本身不持有任何模型密钥。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import socket
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import auth, db, guard
from .config import get_settings
from .core import orchestrator
from .core.orchestrator import STATE_LABELS, SessionRuntime, bus, list_active_sessions
from .core.pipeline import analyze_page, build_weekly_report
from .services import selfcheck
from .services import camera as local_camera
from .services import deepcheck
from .services.metrics import today_summary
from .services.vision import OcrService, VlmService
from .utils import age_label, now, today_start, truncate

api = APIRouter(prefix="/api")
ws_api = APIRouter()


# ---------------- 登录 ----------------


@api.post("/login")
async def login(response: Response, payload: dict = Body(...)) -> dict:
    role = auth.check_pin(str(payload.get("pin") or ""))
    if not role:
        raise HTTPException(status_code=401, detail="口令不对，问一下管理员哦。")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue(role),
        max_age=auth.MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    db.audit(role, "login", {})
    return {"ok": True, "role": role}


@api.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@api.get("/me")
async def me(role: str = Depends(auth.require_teacher)) -> dict:
    return {"role": role}


# ---------------- 教师端：概览 ----------------


@api.get("/teacher/overview")
async def overview(role: str = Depends(auth.require_teacher)) -> dict:
    today = today_start()
    children = db.list_children()
    turns_today = db.q("SELECT COUNT(*) AS n FROM turns WHERE created_at>=?", (today,))[0]["n"]
    answered_today = db.q(
        "SELECT COUNT(*) AS n FROM turns WHERE created_at>=? AND TRIM(COALESCE(answer_text,''))<>''",
        (today,),
    )[0]["n"]
    sessions_today = db.q("SELECT COUNT(*) AS n FROM sessions WHERE created_at>=?", (today,))[0]["n"]
    pending_advice = db.q("SELECT COUNT(*) AS n FROM advice WHERE status='pending'")[0]["n"]
    blocked = db.q(
        "SELECT COUNT(*) AS n FROM selfchecks WHERE scope='content' AND verdict!='pass' AND created_at>=?",
        (today,),
    )[0]["n"]
    summary = today_summary()
    return {
        "children": len(children),
        "sessions_today": sessions_today,
        "pages_today": turns_today,
        "answered_today": answered_today,
        "pending_advice": pending_advice,
        "selfcheck_blocked": blocked,
        "selfcheck": db.selfcheck_stats(today),
        "metrics": summary,
        "active_sessions": list_active_sessions(),
        "demo_mode": get_settings().demo_mode,
        "modes": get_settings().model_modes,
        "today": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---------------- 教师端：幼儿与授权 ----------------


@api.get("/teacher/children")
async def children(role: str = Depends(auth.require_teacher)) -> dict:
    items = []
    for child in db.list_children():
        consent = bool(child["guardian_consent"])
        items.append(
            {
                **child,
                "guardian_consent": consent,
                "age_label": age_label(int(child["age_months"])),
                "languages": json.loads(child["languages"] or '["zh"]'),
                "report_ready": bool(db.latest_report(child["id"])) and consent,
            }
        )
    return {"items": items}


@api.post("/teacher/children")
async def create_child(payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="先填一下孩子的名字吧。")
    age_years = float(payload.get("age_years") or 4)
    age_months = int(round(age_years * 12))
    if not 12 <= age_months <= 96:
        raise HTTPException(status_code=400, detail="年龄请填 1 到 8 岁之间。")
    languages = payload.get("languages") or ["zh"]
    child_id = db.create_child(
        name=name,
        age_months=age_months,
        class_name=str(payload.get("class_name") or "小一班"),
        languages=list(languages),
        guardian_name=str(payload.get("guardian_name") or ""),
        guardian_consent=bool(payload.get("guardian_consent")),
        parent_token=uuid.uuid4().hex,
    )
    db.audit(role, "create_child", {"child_id": child_id})
    return {"ok": True, "id": child_id}


@api.post("/teacher/children/{child_id}/consent")
async def set_consent(
    child_id: int, payload: dict = Body(...), role: str = Depends(auth.require_teacher)
) -> dict:
    if not db.get_child(child_id):
        raise HTTPException(status_code=404, detail="找不到这个孩子。")
    db.set_consent(child_id, bool(payload.get("consented")), payload.get("guardian_name"))
    db.audit(role, "set_consent", {"child_id": child_id, "consented": bool(payload.get("consented"))})
    return {"ok": True}


@api.delete("/teacher/children/{child_id}")
async def delete_child(child_id: int, role: str = Depends(auth.require_admin)) -> dict:
    if not db.get_child(child_id):
        raise HTTPException(status_code=404, detail="找不到这个孩子。")
    result = db.delete_child_everything(child_id)
    return {"ok": True, **result}


# ---------------- 教师端：绘本库与题库 ----------------


@api.get("/teacher/books")
async def books(role: str = Depends(auth.require_teacher)) -> dict:
    items = []
    for book in db.list_books():
        pages = db.list_pages(book["id"])
        items.append(
            {
                **book,
                "pages": [
                    {
                        "id": page["id"],
                        "page_no": page["page_no"],
                        "status": page["status"],
                        "faithful_description": page["faithful_description"],
                        "raw_text": truncate(page["raw_text"], 40),
                        "image": f"/media/library/{page['image_path']}" if page["image_path"] else "",
                    }
                    for page in pages
                ],
            }
        )
    return {"items": items}


@api.post("/teacher/books")
async def create_book(payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="先填一下书名吧。")
    book_id = db.create_book(
        title=title,
        language=str(payload.get("language") or "zh"),
        theme=str(payload.get("theme") or ""),
        age_min=int(payload.get("age_min_months") or 36),
        age_max=int(payload.get("age_max_months") or 72),
    )
    db.audit(role, "create_book", {"book_id": book_id})
    return {"ok": True, "id": book_id}


@api.post("/teacher/books/{book_id}/pages")
async def upload_page(
    book_id: int,
    page_no: int = Form(...),
    file: UploadFile = File(...),
    role: str = Depends(auth.require_teacher),
) -> dict:
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="找不到这本绘本。")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="图片是空的。")
    if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
        raise HTTPException(status_code=400, detail="请上传 JPEG 或 PNG 图片。")
    settings = get_settings()
    folder = settings.library_dir / str(book_id)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = "png" if data.startswith(b"\x89PNG") else "jpg"
    filename = f"{page_no}.{suffix}"
    (folder / filename).write_bytes(data)
    relative = f"{book_id}/{filename}"

    # 预识别：能接模型就生成草稿，接不上就标记待校对（不假装识别成功）
    ocr = await asyncio.to_thread(OcrService().read_sync, data)
    vlm = await asyncio.to_thread(VlmService().analyze_sync, data)
    from .services import nlg

    facts = {
        "raw_text": ocr.text,
        "characters": vlm.characters,
        "objects": vlm.objects,
        "relations": vlm.relations,
        "scene": vlm.scene,
        "mood": vlm.mood,
        "keywords": (vlm.characters + vlm.objects)[:5],
    }
    draft = nlg.fuse(facts, 48, book["language"])
    status = "ready" if (ocr.text or vlm.characters) else "needs_review"
    page_id = db.upsert_page(
        book_id,
        page_no,
        image_path=relative,
        raw_text=ocr.text,
        faithful_description=draft["faithful_description"],
        read_aloud_script=draft["read_aloud_script"],
        keywords=draft["keywords"],
        characters=vlm.characters,
        objects=vlm.objects,
        scene=vlm.scene,
        relations=vlm.relations,
        mood=vlm.mood,
        ocr_confidence=ocr.confidence,
        status=status,
    )
    db.audit(role, "upload_page", {"book_id": book_id, "page_no": page_no, "status": status})
    return {
        "ok": True,
        "page_id": page_id,
        "status": status,
        "human_hint": "这一页还需要老师校对一下文字。" if status != "ready" else "这一页已经可以用了。",
        "draft": draft,
    }


@api.patch("/teacher/pages/{page_id}")
async def update_page(page_id: int, payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    page = db.get_page_by_id(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="找不到这一页。")
    allowed = {"raw_text", "faithful_description", "read_aloud_script", "keywords", "characters", "objects", "scene"}
    fields = {key: payload[key] for key in allowed if key in payload}
    if not fields:
        raise HTTPException(status_code=400, detail="没有要保存的内容。")
    fields["status"] = "ready"
    db.upsert_page(page["book_id"], page["page_no"], image_path=page["image_path"], **fields)
    db.audit(role, "update_page", {"page_id": page_id})
    return {"ok": True}


@api.get("/teacher/pages/{page_id}")
async def page_detail(page_id: int, role: str = Depends(auth.require_teacher)) -> dict:
    """单页完整内容：老师校对三层文本时用，列表接口只给截断摘要。"""
    page = db.get_page_by_id(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="找不到这一页。")
    page["image"] = f"/media/library/{page['image_path']}" if page.get("image_path") else ""
    return {"page": page}


@api.get("/teacher/bank")
async def bank(book_id: int, role: str = Depends(auth.require_teacher)) -> dict:
    return {"items": db.list_bank(book_id=book_id)}


@api.post("/teacher/bank")
async def add_bank(payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    page_id = int(payload.get("book_page_id") or 0)
    text = str(payload.get("text") or "").strip()
    if not page_id or not text:
        raise HTTPException(status_code=400, detail="问题和所属页面都要填。")
    item_id = db.add_bank_item(
        page_id,
        text,
        str(payload.get("qtype") or "observe"),
        int(payload.get("age_min_months") or 36),
        int(payload.get("age_max_months") or 72),
        source="teacher",
    )
    db.audit(role, "add_bank", {"item_id": item_id, "page_id": page_id})
    return {"ok": True, "id": item_id}


@api.delete("/teacher/bank/{item_id}")
async def remove_bank(item_id: int, role: str = Depends(auth.require_teacher)) -> dict:
    db.deactivate_bank_item(item_id)
    db.audit(role, "remove_bank", {"item_id": item_id})
    return {"ok": True}


# ---------------- 教师端：会话 ----------------


@api.post("/teacher/sessions")
async def create_session(payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    child = db.get_child(int(payload.get("child_id") or 0))
    book = db.get_book(int(payload.get("book_id") or 0))
    if not child:
        raise HTTPException(status_code=404, detail="找不到这个孩子。")
    if not book:
        raise HTTPException(status_code=404, detail="找不到这本绘本。")
    pages = db.list_pages(book["id"])
    if not pages:
        raise HTTPException(status_code=400, detail="这本绘本还没有页面，先去绘本库补上。")
    session_id = uuid.uuid4().hex
    token = uuid.uuid4().hex
    db.create_session(session_id, child["id"], book["id"], token, str(payload.get("device_id") or ""))
    runtime = SessionRuntime.load(session_id)
    if runtime:
        await runtime.start()
    db.audit(role, "create_session", {"session_id": session_id, "child_id": child["id"], "book_id": book["id"]})
    return {
        "ok": True,
        "session_id": session_id,
        "token": token,
        "robot_url": f"/robot?session={session_id}&token={token}",
        "pages": [{"page_no": page["page_no"], "status": page["status"]} for page in pages],
        "child": {"id": child["id"], "name": child["name"], "age_months": child["age_months"]},
        "book": {"id": book["id"], "title": book["title"], "language": book["language"]},
    }


@api.get("/teacher/sessions")
async def sessions(role: str = Depends(auth.require_teacher)) -> dict:
    return {"items": list_active_sessions()}


@api.get("/teacher/sessions/{session_id}")
async def session_detail(session_id: str, role: str = Depends(auth.require_teacher)) -> dict:
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="找不到这次共读。")
    runtime = orchestrator.RUNTIMES.get(session_id)
    child = db.get_child(session["child_id"])
    book = db.get_book(session["book_id"])
    return {
        "session": session,
        "child": child,
        "book": book,
        "snapshot": runtime.snapshot() if runtime else None,
        "turns": db.list_turns(session_id),
        "advice": db.list_advice(child_id=session["child_id"]),
    }


@api.post("/teacher/sessions/{session_id}/end")
async def end_session(session_id: str, role: str = Depends(auth.require_teacher)) -> dict:
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        db.end_session(session_id)
        return {"ok": True, "human_hint": "这次共读已经结束了。"}
    result = await runtime.end()
    db.audit(role, "end_session", {"session_id": session_id})
    return result


# ---------------- 教学建议 / 周报 ----------------


@api.get("/teacher/advice")
async def advice_list(status: str | None = None, role: str = Depends(auth.require_teacher)) -> dict:
    return {"items": db.list_advice(status=status)}


@api.post("/teacher/advice/{advice_id}/decide")
async def advice_decide(advice_id: int, payload: dict = Body(...), role: str = Depends(auth.require_teacher)) -> dict:
    action = str(payload.get("action") or "")
    if action not in {"accept", "ignore", "edit"}:
        raise HTTPException(status_code=400, detail="只支持 accept / ignore / edit。")
    status = {"accept": "accepted", "ignore": "ignored", "edit": "accepted"}[action]
    db.decide_advice(advice_id, status, str(payload.get("text") or ""))
    db.audit(role, "decide_advice", {"advice_id": advice_id, "action": action})
    return {"ok": True}


def _child_turns(child_id: int, days: int = 7) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return db.q(
        "SELECT t.* FROM turns t JOIN sessions s ON s.id=t.session_id"
        " WHERE s.child_id=? AND t.created_at>=? ORDER BY t.id",
        (child_id, since),
    )


@api.post("/teacher/weekly/{child_id}")
async def generate_weekly(child_id: int, role: str = Depends(auth.require_teacher)) -> dict:
    child = db.get_child(child_id)
    if not child:
        raise HTTPException(status_code=404, detail="找不到这个孩子。")
    if not child["guardian_consent"]:
        raise HTTPException(status_code=403, detail="该幼儿还没有监护人授权，暂不能生成周报。")
    turns = _child_turns(child_id)
    advice_items = db.list_advice(child_id=child_id)
    payload, report = build_weekly_report(child, turns, advice_items)
    if report["verdict"] != "pass":
        raise HTTPException(status_code=422, detail=f"自检未通过：{report['checks']}")
    expires = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")
    report_id = db.add_report(child_id, payload["week_start"], payload["week_end"], payload, expires)
    db.audit(role, "generate_weekly", {"child_id": child_id, "report_id": report_id})
    return {
        "ok": True,
        "report_id": report_id,
        "payload": payload,
        "parent_url": f"/parent/{child['parent_token']}",
        "selfcheck": report,
    }


@api.get("/teacher/reports")
async def reports(role: str = Depends(auth.require_teacher)) -> dict:
    rows = db.q(
        "SELECT r.*, c.name AS child_name, c.parent_token FROM weekly_reports r"
        " JOIN children c ON c.id=r.child_id ORDER BY r.id DESC LIMIT 50"
    )
    return {"items": rows}


@api.post("/teacher/reports/{report_id}/publish")
async def publish_report(report_id: int, role: str = Depends(auth.require_teacher)) -> dict:
    report = db.get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="找不到这份周报。")
    db.publish_report(report_id)
    db.audit(role, "publish_report", {"report_id": report_id})
    return {"ok": True}


# ---------------- 自检 / 运维 ----------------


@api.get("/teacher/selfcheck/system")
async def selfcheck_system(role: str = Depends(auth.require_teacher)) -> dict:
    return selfcheck.run_system_check()


@api.post("/teacher/selfcheck/deep")
async def selfcheck_deep(role: str = Depends(auth.require_teacher)) -> dict:
    """真跑五个引擎（约 20~40 秒）：测试卡片走 OCR/VLM，合成一句话再转写回来。"""
    return await asyncio.to_thread(deepcheck.run_deep_check)


@api.get("/teacher/selfcheck/history")
async def selfcheck_history(scope: str | None = None, role: str = Depends(auth.require_teacher)) -> dict:
    return {"items": db.list_selfchecks(scope=scope, limit=50)}


@api.get("/teacher/selfcheck/stats")
async def selfcheck_stats(role: str = Depends(auth.require_teacher)) -> dict:
    return {
        "today": db.selfcheck_stats(today_start()),
        "week": db.selfcheck_stats((datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")),
    }


@api.get("/teacher/selfcheck/demo")
async def selfcheck_demo(role: str = Depends(auth.require_teacher)) -> dict:
    """内容闸门试跑：不调模型，纯规则跑几条典型坏例子，让老师看见它真会拦。"""
    checker = selfcheck.ContentChecker()
    facts = {
        "raw_text": "小兔子在草地上追风筝。",
        "characters": ["小兔子"],
        "objects": ["风筝", "草地"],
        "relations": ["小兔子追风筝"],
        "scene": "草地",
        "keywords": ["小兔子", "风筝"],
    }
    turns = [
        {"id": 1, "question": "小兔子在草地上做什么呀？", "answer_text": "追风筝"},
        {"id": 2, "question": "风筝飞到哪里去了呀？", "answer_text": "天上"},
    ]
    child = {"name": "试跑样例", "age_months": 54, "guardian_consent": 1}

    cases: list[dict] = []

    def add_case(name: str, reason: str, input_text: str, report) -> None:
        data = report.to_dict()
        data["summary"] = report.summary_line()
        cases.append(
            {
                "name": name,
                "input": input_text,
                "reason": reason,
                "report": data,
                "expected": "block" if report.failures else "pass",
            }
        )

    off_page = {"question": "大象躲到哪里去了呀？", "type": "observe"}
    add_case(
        "画外实体",
        "问题里提到了画面没有的东西，孩子看不到就无法回答",
        off_page["question"],
        checker.check_question(off_page, facts, age_months=54, language="zh", history=[]),
    )

    abstract = {"question": "小兔子现在的心情是怎样的呀？", "type": "empathy"}
    add_case(
        "抽象词",
        "“心情”这类词 4 岁孩子答不出来，要换成看得见的表情和动作",
        abstract["question"],
        checker.check_question(abstract, facts, age_months=54, language="zh", history=[]),
    )

    too_long = {"question": "小兔子在草地上追着那只红色的风筝跑来跑去的时候，旁边的小草有没有被风吹得晃来晃去呀？", "type": "observe"}
    add_case(
        "一句话太长",
        f"超过 {selfcheck.MAX_LEN['4_6']} 字的适龄上限，孩子听到后面忘了前面",
        too_long["question"],
        checker.check_question(too_long, facts, age_months=54, language="zh", history=[]),
    )

    repeated = {"question": "小兔子在草地上做什么呀？", "type": "observe"}
    add_case(
        "重复上一题",
        "与上一页问题几乎一样，孩子会觉得没意思",
        repeated["question"],
        checker.check_question(repeated, facts, age_months=54, language="zh", history=turns),
    )

    two_questions = {"question": "小兔子在做什么呀？风筝飞得高不高呀？", "type": "observe"}
    add_case(
        "一次问两件事",
        "两个问号就是两道题，孩子只会答其中一个",
        two_questions["question"],
        checker.check_question(two_questions, facts, age_months=54, language="zh", history=[]),
    )

    bad_advice = [
        {"text": "小雨的语言能力比同班孩子强，可以多让她领读。", "evidence": [1]},
        {"text": "建议家长在家多和孩子聊画面里的细节。", "evidence": [2]},
        {"text": "这条没有依据。", "evidence": []},
    ]
    add_case(
        "建议排名 / 无依据",
        "给孩子贴“比别人强”的标签，或建议不挂到任何一条问答上，都不允许",
        "；".join(item["text"] for item in bad_advice),
        checker.check_advice(bad_advice, turns, child),
    )

    good = {"question": "小兔子在草地上追什么呀？", "type": "observe"}
    add_case(
        "合格样例（对照）",
        "只用画面事实、一句话、长度适龄",
        good["question"],
        checker.check_question(good, facts, age_months=54, language="zh", history=[]),
    )

    return {
        "cases": cases,
        "human_hint": "这些都是闸门当场会拦下的写法；拦下后先让模型改一次，还不合格就换成安全模板。",
    }


@api.get("/teacher/metrics")
async def metrics(role: str = Depends(auth.require_teacher)) -> dict:
    return today_summary()


@api.get("/teacher/audit")
async def outbound_audit(role: str = Depends(auth.require_teacher)) -> dict:
    findings = guard.audit_env_for_public_urls()
    return {
        "items": [
            {"url": item.url, "host": item.host, "allowed": item.allowed, "reason": item.reason}
            for item in findings
        ],
        "offline_only": get_settings().offline_only,
        "human_hint": (
            "没有发现园外地址，符合完全离线要求。"
            if all(item.allowed for item in findings)
            else "发现园外地址，请改成园内服务地址。"
        ),
    }


# ---------------- 设备端（机器人） ----------------


def _device_session(session_id: str, token: str) -> dict:
    session = db.get_session(session_id)
    if not session or session["token"] != token:
        raise HTTPException(status_code=403, detail="设备凭证无效，请老师重新打开机器人端。")
    return session


# ---------------- 网络摄像头（ESP32 / 手机 IP 摄像头 / USB 桥接） ----------------

CAMERA_MAX_BYTES = 8 * 1024 * 1024


def _camera_url_allowed(url: str) -> tuple[bool, str, str]:
    """只允许局域网里的 http 图片地址，并返回校验通过的那个 IP。

    设备端会把用户填的地址交给服务端去拉，这等于一次 SSRF 机会：
    必须锁死为「http + 解析后全部落在私有/环回地址」，否则这台完全离线的机器
    可能被当成跳板去访问外网。域名解析到公网同样拒绝，重定向一律不跟随。

    返回的 IP 会被钉进实际请求（Host 头保留原名）：校验一次解析、连接又一次解析的话，
    控制 DNS 的人可以在两次之间把域名重绑到公网，钉死就堵住了这个时间差。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme in {"rtsp", "rtsps"}:
        return False, (
            "RTSP 流要先转换：在电脑上运行 _tools/camera_bridge.py --url "
            + url.strip()
            + "，把它显示的 http 地址填到这里"
        ), ""
    if parsed.scheme != "http":
        return False, "只支持 http 地址（局域网摄像头一般不配 https 证书）", ""
    if not parsed.hostname:
        return False, "地址里没有主机名", ""
    try:
        port = parsed.port or 80
    except ValueError:
        return False, "端口写错了", ""
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "解析不了这个地址，检查摄像头是否在线、IP 是否写对", ""
    private: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False, "地址格式不对", ""
        if not (ip.is_private or ip.is_loopback):
            return False, "只允许局域网地址（192.168/10/172.16-31），不能指向外网", ""
        private.append(info[4][0])
    # IPv4 优先：有些摄像头只监听 v4，localhost 会同时解析出 ::1 和 127.0.0.1
    private.sort(key=lambda value: ":" in value)
    return True, "", private[0]


def _pin_camera_target(url: str, ip: str) -> tuple[str, dict[str, str]]:
    """把 host 换成校验过的 IP，Host 头保留原名（摄像头/虚拟主机按名字区分）。"""
    parsed = urlparse(url)
    host = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host).geturl(), {"Host": parsed.netloc.split("@")[-1]}


def _slice_jpeg(buffer: bytearray) -> bytes | None:
    """从字节流里切出第一张完整的 JPEG（FFD8 … FFD9）。"""
    start = buffer.find(b"\xff\xd8")
    if start < 0:
        return None
    end = buffer.find(b"\xff\xd9", start + 2)
    if end < 0:
        return None
    return bytes(buffer[start : end + 2])


async def _read_capped(response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="这一帧太大了，检查摄像头分辨率")
    return bytes(body)


class _CameraAuthError(Exception):
    """摄像头返回 401：带出服务器的认证方式，决定用地址里的账号密码重试还是直接提示。"""

    def __init__(self, challenge: str) -> None:
        self.challenge = challenge
        super().__init__(challenge)


async def _grab_frame(
    client: httpx.AsyncClient,
    url: str,
    auth: httpx.Auth | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """拉一帧：单帧图片原样返回，MJPEG 流（multipart/x-mixed-replace）抠出第一张完整 JPEG 就断开。"""
    async with client.stream("GET", url, auth=auth, headers=headers) as response:
        if response.status_code == 401:
            raise _CameraAuthError(response.headers.get("www-authenticate") or "")
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"摄像头返回了 {response.status_code}")
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if "multipart" in content_type or "mjpeg" in content_type:
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                frame = _slice_jpeg(buffer)
                if frame:
                    return frame
                if len(buffer) > CAMERA_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="流里一帧太大了，检查摄像头分辨率")
            raise HTTPException(status_code=502, detail="这个流里没读到画面，检查摄像头是否在推流")
        body = await _read_capped(response, CAMERA_MAX_BYTES)
        if body[:2] == b"\xff\xd8":
            return body
        raise HTTPException(
            status_code=415,
            detail="这个地址返回的不是图片，换成摄像头抓帧地址（如 /capture、/shot.jpg）",
        )


def _url_credentials(url: str) -> tuple[str, str, str]:
    """拆出 http://账号:密码@IP/… 里的账号密码，返回 (账号, 密码, 去掉账号密码的地址)。"""
    parsed = urlparse(url)
    if not parsed.username:
        return "", "", url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return parsed.username, parsed.password or "", parsed._replace(netloc=host).geturl()


@api.get("/camera/snapshot")
async def camera_snapshot(request: Request, session_id: str = "", token: str = "", url: str = "") -> Response:
    """代设备拉一帧。浏览器直接读网络摄像头会被跨域和安全上下文拦住，由同源的服务端去拿。

    支持三种来源：单帧图片（/capture、/shot.jpg）、MJPEG 流（/stream、/video，抠第一帧就断），
    以及需要 Basic / Digest 登录的摄像头（把账号密码写进地址）。
    有些固件不写 Content-Type，所以兜底看 JPEG 的 FFD8 头。

    凭证：机器人端带设备 session token；教师端上传页没有 token，登录 cookie 同样放行。
    两者都只是“谁可以用”，真正防 SSRF 的是下面的局域网白名单。
    """
    try:
        _device_session(session_id, token)
    except HTTPException:
        if auth.role_of(request) not in {"teacher", "admin"}:
            raise
    # camera:0 这种本机摄像头：服务端直接拍一帧，不需要 camera_bridge 转发
    local_index = local_camera.camera_index(url)
    if local_index is not None:
        try:
            frame = await asyncio.to_thread(local_camera.capture, local_index)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:120]) from exc
        return Response(
            content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )
    user, password, clean_url = _url_credentials(url)
    ok, reason, ip = await asyncio.to_thread(_camera_url_allowed, clean_url)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    pinned_url, pin_headers = _pin_camera_target(clean_url, ip)
    try:
        timeout = httpx.Timeout(4.0, read=4.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            try:
                # 地址里带账号密码时先试 Basic，海康/大华这类要 Digest 的再换 Digest 重算
                body = await _grab_frame(
                    client,
                    pinned_url,
                    auth=httpx.BasicAuth(user, password) if user else None,
                    headers=pin_headers,
                )
            except _CameraAuthError as challenge:
                if not user or "digest" not in challenge.challenge.lower():
                    detail = (
                        "这台摄像头要登录：把地址写成 http://账号:密码@摄像头IP/路径 再试"
                        if not user
                        else "摄像头的账号或密码不对，检查地址里的账号密码"
                    )
                    raise HTTPException(status_code=401, detail=detail) from challenge
                body = await _grab_frame(client, pinned_url, auth=httpx.DigestAuth(user, password), headers=pin_headers)
    except _CameraAuthError as challenge:
        raise HTTPException(status_code=401, detail="摄像头的账号或密码不对，检查地址里的账号密码") from challenge
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"连不上摄像头：{str(exc)[:80]}") from exc
    return Response(content=body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@api.get("/device/{session_id}")
async def device_snapshot(session_id: str, token: str) -> dict:
    _device_session(session_id, token)
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="这次共读已经结束了。")
    return {
        "snapshot": runtime.snapshot(),
        "history": bus.recent(session_id),
        "demo_mode": get_settings().demo_mode,
        "modes": get_settings().model_modes,
    }


@api.post("/device/{session_id}/page")
async def device_page(
    session_id: str,
    page_no: int = Form(...),
    token: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    _device_session(session_id, token)
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="这次共读已经结束了。")
    data = await file.read()
    return await runtime.capture(page_no, data)


@api.post("/device/{session_id}/answer")
async def device_answer(session_id: str, payload: dict = Body(...), token: str = "") -> dict:
    session = _device_session(session_id, token or str(payload.get("token") or ""))
    runtime = SessionRuntime.load(session["id"])
    if not runtime:
        raise HTTPException(status_code=404, detail="这次共读已经结束了。")
    return await runtime.answer(text=str(payload.get("text") or ""))


@api.post("/device/{session_id}/listen")
async def device_listen(session_id: str, token: str = Form(...), file: UploadFile = File(...)) -> dict:
    _device_session(session_id, token)
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="这次共读已经结束了。")
    data = await file.read()
    return await runtime.answer(audio=data)


@api.post("/device/{session_id}/control")
async def device_control(session_id: str, payload: dict = Body(...)) -> dict:
    _device_session(session_id, str(payload.get("token") or ""))
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="这次共读已经结束了。")
    return await runtime.control(str(payload.get("action") or ""))


@ws_api.websocket("/ws/device/{session_id}")
async def device_ws(websocket: WebSocket, session_id: str, token: str = "") -> None:
    session = db.get_session(session_id)
    if not session or session["token"] != token:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    runtime = SessionRuntime.load(session_id)
    if not runtime:
        await websocket.send_json({"type": "error", "human_hint": "这次共读已经结束了。", "fatal": True})
        await websocket.close(code=4404)
        return
    queue = bus.subscribe(session_id)
    await websocket.send_json({"type": "snapshot", "data": runtime.snapshot(), "history": bus.recent(session_id)})

    send_lock = asyncio.Lock()
    jobs: set[asyncio.Task] = set()

    async def send(payload: dict) -> None:
        async with send_lock:
            try:
                await websocket.send_json(payload)
            except Exception:
                pass  # 连接已断开：事件丢就丢，不能让后台任务炸掉

    async def pump() -> None:
        while True:
            payload = await queue.get()
            await send(payload)

    async def interact(label: str, coro) -> None:
        """长任务放后台：识别期间心跳必须还能回 pong，否则机器人会误判掉线。"""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单次失败不拆连接，只告诉机器人“重来一次”
            await send(
                {
                    "type": "error",
                    "stage": label,
                    "human_hint": "这一步没成功，我们再来一次好吗？",
                    "detail": str(exc)[:120],
                }
            )

    def spawn(label: str, coro) -> None:
        task = asyncio.create_task(interact(label, coro))
        jobs.add(task)
        task.add_done_callback(jobs.discard)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            kind = message.get("type")
            try:
                if kind == "ping":
                    await send({"type": "pong"})
                elif kind == "capture":
                    raw = base64.b64decode(message.get("image") or "")
                    page_no = int(message.get("page_no") or runtime.page_no + 1)
                    spawn("capture", runtime.capture(page_no, raw))
                elif kind == "answer":
                    spawn("answer", runtime.answer(text=str(message.get("text") or "")))
                elif kind == "audio":
                    spawn("answer", runtime.answer(audio=base64.b64decode(message.get("audio") or "")))
                elif kind == "control":
                    spawn("control", runtime.control(str(message.get("action") or "")))
                else:
                    await send({"type": "error", "human_hint": "这个指令机器人不认识，已忽略。", "detail": str(kind)[:40]})
            except (binascii.Error, ValueError) as exc:
                await send({"type": "error", "human_hint": "这张图片没传完整，再拍一次好吗？", "detail": str(exc)[:80]})
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        bus.unsubscribe(queue)
        pump_task.cancel()
        # 不取消进行中的识别：平板网络插一下不该白白丢掉这一页，
        # 结果会进事件总线，重连后的 snapshot/事件能接上。


@ws_api.websocket("/ws/teacher")
async def teacher_ws(websocket: WebSocket) -> None:
    if not auth.role_of(websocket):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = bus.subscribe(None)
    await websocket.send_json({"type": "snapshot", "sessions": list_active_sessions(), "history": bus.recent(None, 30)})

    async def pump() -> None:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if message.get("type") == "control":
                runtime = SessionRuntime.load(str(message.get("session_id") or ""))
                if runtime:
                    await runtime.control(str(message.get("action") or ""))
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        bus.unsubscribe(queue)
        pump_task.cancel()


# ---------------- 家长端 ----------------


@api.get("/parent/{token}")
async def parent_json(token: str) -> dict:
    child = db.get_child_by_parent_token(token)
    if not child:
        raise HTTPException(status_code=404, detail="这个链接已失效，请找老师重新获取。")
    if not child["guardian_consent"]:
        return {"consent": False, "human_hint": "还没有登记监护人授权，暂时看不到周报。"}
    report = db.latest_report(child["id"])
    published = report and report["status"] == "published"
    return {
        "consent": True,
        "child_name": child["name"],
        "published": bool(published),
        "payload": report["payload"] if published else None,
        "expires_at": report["expires_at"] if published else None,
        "human_hint": "" if published else "这周的周报老师还在准备，稍后再来看吧。",
    }


@api.post("/parent/{token}/revoke")
async def parent_revoke(token: str) -> dict:
    child = db.get_child_by_parent_token(token)
    if not child:
        raise HTTPException(status_code=404, detail="这个链接已失效。")
    db.set_consent(child["id"], False)
    db.x("DELETE FROM weekly_reports WHERE child_id=?", (child["id"],))
    db.audit("guardian", "revoke_consent", {"child_id": child["id"]})
    return {"ok": True, "human_hint": "已撤回授权，相关周报已删除。"}