"""FastAPI 应用入口：页面路由 + 静态资源 + 启动自举。

启动：python run.py  或  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, auth, db
from .api import api, ws_api
from .config import get_settings
from .seed import ensure_seed
from .services import metrics, speech
from .utils import age_label, today_start

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def render(request: Request, name: str, **context) -> HTMLResponse:
    template = TEMPLATES.get_template(name)
    return HTMLResponse(template.render(request=request, **context))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    ensure_seed()
    settings = get_settings()
    db.audit(
        "system",
        "startup",
        {
            "offline_only": settings.offline_only,
            "modes": settings.model_modes,
            "demo_mode": settings.demo_mode,
        },
    )
    pruned = speech.prune_old()
    if pruned:
        db.audit("system", "tts_pruned", {"files": pruned, "keep_days": speech.KEEP_DAYS})
    yield


app = FastAPI(title="绘本阅读陪伴 AI 智能体", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(api)
app.include_router(ws_api)


# ---------------- 页面 ----------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    settings = get_settings()
    children = db.list_children()
    parent_child = next(
        (child for child in children if child["guardian_consent"] and child["parent_token"]), None
    )
    summary = metrics.today_summary()
    return render(
        request,
        "index.html",
        demo_mode=settings.demo_mode,
        modes=settings.model_modes,
        offline_only=settings.offline_only,
        children=len(children),
        books=len(db.list_books()),
        pages_today=db.q("SELECT COUNT(*) AS n FROM turns WHERE created_at>=?", (today_start(),))[0]["n"],
        avg_latency=summary["avg_latency"],
        parent_token=parent_child["parent_token"] if parent_child else "",
        version=__version__,
    )


@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request) -> HTMLResponse:
    role = auth.role_of(request)
    if not role:
        return render(request, "login.html", demo_mode=get_settings().demo_mode)
    return render(
        request,
        "teacher.html",
        role=role,
        demo_mode=get_settings().demo_mode,
        modes=get_settings().model_modes,
    )


@app.get("/device-check", response_class=HTMLResponse)
async def device_check_page(request: Request) -> HTMLResponse:
    """设备自检：换机/上架前确认摄像头、麦克风、喇叭、网络。不读孩子数据，无需登录。"""
    return render(request, "device_check.html", demo_mode=get_settings().demo_mode)


# 浏览器每次都会自己来要图标，没有这个路由控制台就多一条 404
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#2f7a5f'/>"
    "<path d='M17 21h12a6 6 0 0 1 6 6v19a6 6 0 0 0-6-6H17z' fill='#ffffff'/>"
    "<path d='M47 21H35a6 6 0 0 0-6 6v19a6 6 0 0 1 6-6h12z' fill='#dff0e7'/>"
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/robot", response_class=HTMLResponse)
async def robot_page(request: Request, session: str = "", token: str = "") -> HTMLResponse:
    row = db.get_session(session) if session else None
    if not row or row["token"] != token:
        return render(
            request,
            "robot.html",
            session=None,
            token="",
            demo_mode=get_settings().demo_mode,
            human_hint="请老师先在教师端点【开始共读】，再用页面上的二维码打开机器人端。",
        )
    child = db.get_child(row["child_id"])
    book = db.get_book(row["book_id"])
    pages = db.list_pages(book["id"]) if book else []
    return render(
        request,
        "robot.html",
        session=row,
        token=token,
        child=child,
        book=book,
        pages_total=len(pages),
        demo_mode=get_settings().demo_mode,
        human_hint="",
    )


@app.get("/parent/{token}", response_class=HTMLResponse)
async def parent_page(request: Request, token: str) -> HTMLResponse:
    child = db.get_child_by_parent_token(token)
    if not child:
        return render(request, "parent.html", child=None, report=None, token=token, human_hint="这个链接已失效，请找老师重新获取。")
    report = db.latest_report(child["id"])
    published = bool(report and report["status"] == "published")
    return render(
        request,
        "parent.html",
        child=child,
        report=report if published else None,
        token=token,
        human_hint="" if published else "这周的周报老师还在准备，稍后再来看吧。",
        age=age_label(int(child["age_months"])),
    )


@app.get("/healthz")
async def healthz() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "version": __version__,
        "demo_mode": settings.demo_mode,
        "modes": settings.model_modes,
        "offline_only": settings.offline_only,
    }


# ---------------- 媒体 ----------------


@app.get("/media/library/{book_id}/{filename}")
async def library_media(book_id: str, filename: str) -> FileResponse:
    settings = get_settings()
    base = (settings.library_dir / book_id).resolve()
    path = (base / filename).resolve()
    if base not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@app.get("/media/tts/{filename}")
async def tts_media(filename: str) -> FileResponse:
    settings = get_settings()
    path = (settings.tts_dir / filename).resolve()
    if settings.tts_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="音频不存在")
    return FileResponse(path)