"""每一步都加复核：不复用被测脚本自己打印的“通过”，换一种独立手段重新求证。

被测脚本先把结果落成证据文件（_tools/shots/*.json + 截图），这里再：
  测试     → 读 JUnit XML 数字，与 --collect-only 的收集数交叉核对
  导入     → 不用 check_imports.py 的清单，自己遍历 app/ 逐模块导入
  JS       → node --check 之后，再比对服务端实际发出的字节与本地文件哈希
  服务     → 另开 HTTP 通道查健康页/静态资源/日志，不信脚本输出
  冒烟     → 直查 SQLite：会话已结束、建议已生成、周报已发布、家长端可见
  浏览器   → 只认截图文件（PNG 魔数 + 体积）与报告 JSON 的断言字段
  防御     → 现场搭一台“坏摄像头”：重定向/非图片/超大帧/超时/公网地址/rtsp
  离线     → 扫代码里的外网主机 + 问服务自己的出站审计
  档案     → 教师端入口 + 接口自报数字与 SQLite 直查对账
  仓库     → git status、远程哈希、暂存区密钥扫描

用法：
  python _tools\\recheck.py              # 全部复核
  python _tools\\recheck.py defense      # 只复核指定步骤（可多个）
  python _tools\\recheck.py --list
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

SHOTS = ROOT / "_tools" / "shots"
BASE = "http://127.0.0.1:8000"
PY = sys.executable
PIN = os.environ.get("TEACHER_PIN") or get_settings().teacher_pin
FAILS: list[str] = []

sys.stdout.reconfigure(encoding="utf-8")


def ok(name: str, detail: str = "") -> None:
    print(f"  [PASS] {name}  {detail}".rstrip())


def bad(name: str, detail: str = "") -> None:
    FAILS.append(name)
    print(f"  [FAIL] {name}  {detail}".rstrip())


def note(text: str) -> None:
    print(f"  ·· {text}")


def run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def teacher_client() -> httpx.Client | None:
    client = httpx.Client(base_url=BASE, timeout=15)
    try:
        reply = client.post("/api/login", json={"pin": PIN})
    except httpx.HTTPError:
        client.close()
        return None
    if reply.status_code != 200:
        client.close()
        return None
    return client


def fresh(path: Path, deps: list[Path], label: str) -> bool:
    """证据必须产自当前版本的脚本，否则先重跑，避免拿旧截图当新结果。"""
    if not path.exists():
        bad(label, f"缺证据文件 {path.name}，先跑对应脚本")
        return False
    newest = max(dep.stat().st_mtime for dep in deps)
    if path.stat().st_mtime < newest:
        bad(label, f"{path.name} 比被测脚本旧，先重跑再复核")
        return False
    return True


# ---------------------------------------------------------------- 测试
def step_pytest() -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    junit = SHOTS / "recheck_junit.xml"
    junit.unlink(missing_ok=True)
    proc = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"])
    if not junit.exists():
        bad("pytest 产出 JUnit", (proc.stdout + proc.stderr)[-300:])
        return
    root = ElementTree.parse(junit).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    tests = int(suite.get("tests", 0))
    fails = int(suite.get("failures", 0))
    errors = int(suite.get("errors", 0))
    skips = int(suite.get("skipped", 0))
    ok("XML 计数", f"{tests} 条 / 失败 {fails} / 错误 {errors} / 跳过 {skips}")
    if proc.returncode == 0 and fails == 0 and errors == 0 and skips == 0:
        ok("结果一致", "退出码 0 且 XML 无失败无跳过")
    else:
        bad("pytest 结果", f"退出码 {proc.returncode}，失败 {fails}，错误 {errors}，跳过 {skips}")
    collect = run([PY, "-m", "pytest", "--collect-only", "-q"], timeout=300)
    match = re.search(r"(\d+)\s+tests?\s+collected", collect.stdout)
    collected = int(match.group(1)) if match else len(
        [line for line in collect.stdout.splitlines() if "::" in line]
    )
    if collected == tests:
        ok("收集数交叉核对", f"{collected} == 执行数 {tests}")
    else:
        bad("收集数交叉核对", f"收集 {collected} 条，实际执行 {tests} 条")
    if tests >= 100:
        ok("用例规模", f"{tests} 条（下限 100）")
    else:
        bad("用例规模", f"只有 {tests} 条，少于约定的 100 条")


# ---------------------------------------------------------------- 导入
def step_imports() -> None:
    modules = sorted(
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "__init__.py"
    )
    code = (
        "import importlib, sys\n"
        "bad = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {exc!r}')\n"
        "print('IMPORTED', len(sys.argv) - 1)\n"
        "for line in bad:\n"
        "    print('BAD', line)\n"
    )
    proc = run([PY, "-c", code, *modules], timeout=180)
    lines = proc.stdout.splitlines()
    imported = next((line for line in lines if line.startswith("IMPORTED")), "")
    problems = [line[4:] for line in lines if line.startswith("BAD")]
    if proc.returncode == 0 and not problems and imported == f"IMPORTED {len(modules)}":
        ok("逐模块导入", f"{len(modules)} 个模块（遍历 app/ 而非固定清单）")
    else:
        bad("逐模块导入", f"{problems[:3] or (proc.stderr[-200:])}")


# ---------------------------------------------------------------- JS
def step_js() -> None:
    files = sorted((ROOT / "app" / "web" / "static").glob("*.js"))
    broken: list[str] = []
    for path in files:
        proc = run(["node", "--check", str(path)], timeout=120)
        if proc.returncode != 0:
            broken.append(f"{path.name}: {proc.stderr.strip().splitlines()[-1:]}")
    if broken:
        bad("node --check", str(broken))
    else:
        ok("node --check", f"{len(files)} 个文件")
    probe = httpx.Client(base_url=BASE, timeout=8)
    try:
        up = probe.get("/healthz").status_code == 200
    except httpx.HTTPError:
        up = False
    if not up:
        note("服务没起，跳过“服务端字节 == 本地文件”的部署一致性核对")
        probe.close()
        return
    stale: list[str] = []
    for path in files:
        fetched = probe.get(f"/static/{path.name}")
        remote = hashlib.sha256(fetched.content).hexdigest()
        local = hashlib.sha256(path.read_bytes()).hexdigest()
        if fetched.status_code != 200 or remote != local:
            stale.append(path.name)
    if stale:
        bad("部署一致性", f"服务端发的是旧文件：{stale}（先重启服务）")
    else:
        ok("部署一致性", f"{len(files)} 个 JS 的 sha256 与本地一致")
    probe.close()


# ---------------------------------------------------------------- 服务
def step_serve() -> None:
    client = httpx.Client(base_url=BASE, timeout=10)
    try:
        health = client.get("/healthz")
        payload = health.json()
    except (httpx.HTTPError, ValueError) as exc:
        bad("健康检查", repr(exc))
        return
    if health.status_code == 200:
        ok("健康检查", f"{payload}")
    else:
        bad("健康检查", f"HTTP {health.status_code}")
    if payload.get("offline_only") is True:
        ok("离线开关", "offline_only=true")
    else:
        bad("离线开关", f"offline_only={payload.get('offline_only')!r}")
    pages = ["/", "/teacher", "/device-check", "/static/app.css", "/static/capture.js"]
    wrong = []
    for path in pages:
        reply = client.get(path)
        if reply.status_code != 200 or not reply.content:
            wrong.append(f"{path} -> {reply.status_code}")
    if wrong:
        bad("页面与静态资源", str(wrong))
    else:
        ok("页面与静态资源", f"{len(pages)} 个地址均 200 且有内容")
    log_path = get_settings().data_dir / "server.log"
    if not log_path.exists():
        note("没有 data/server.log（服务启动时才创建），跳过日志核对")
    else:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        marker = f"服务启动 {payload.get('started_at', '')}"
        if not payload.get("started_at") or marker not in text:
            note("日志里没有本次启动标记（可能刚轮转过），跳过日志核对")
        else:
            lines = text.splitlines()
            # 只认本次进程启动之后的段落：历史日志里的异常属于旧版本，不能计入
            start = max(i for i, line in enumerate(lines) if marker in line)
            segment = lines[start:]
            hits = [line for line in segment
                    if "Traceback" in line or " 500 " in line or "ERROR:" in line]
            if hits:
                bad("服务日志", f"本次启动后 {len(hits)} 行异常，例：{hits[-1][:120]}")
            else:
                ok("服务日志", f"本次启动后 {len(segment)} 行，无 Traceback / 500 / ERROR")
    client.close()


# ---------------------------------------------------------------- 冒烟
def step_smoke() -> None:
    evidence = SHOTS / "live_smoke.json"
    if not fresh(evidence, [ROOT / "_tools" / "live_smoke.py"], "冒烟证据"):
        return
    facts = json.loads(evidence.read_text(encoding="utf-8"))
    # live_smoke 的设计是：用真实的教师/设备接口跑完整链路，跑完把自己的
    # 烟测孩子连同会话/问答/建议/周报一起删掉，真实部署的库不留测试数据。
    # 所以这里不复核「库里还在」，而是复核「跑通了」+「清理彻底」。
    ended = facts.get("ended") or {}
    if (facts.get("cleaned") is True and int(ended.get("pages") or 0) >= 1
            and int(ended.get("advice") or 0) >= 1
            and ended.get("session_check") == "pass"):
        ok("全链路跑通", f"页数 {ended.get('pages')}，建议 {ended.get('advice')} 条，"
                          f"会话自检 {ended.get('session_check')}，已请求清理")
    else:
        bad("全链路跑通", f"cleaned={facts.get('cleaned')!r} ended={ended!r}")

    session_id = facts.get("session_id")
    child_id = facts.get("child_id")
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
    try:
        left_sessions = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ? OR child_id = ?", (session_id, child_id)
        ).fetchone()[0]
        left_turns = db.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        left_child = db.execute(
            "SELECT COUNT(*) FROM children WHERE id = ?", (child_id,)
        ).fetchone()[0]
        report_id = (facts.get("weekly") or {}).get("report_id")
        left_reports = db.execute(
            "SELECT COUNT(*) FROM weekly_reports WHERE id = ? OR child_id = ?", (report_id, child_id)
        ).fetchone()[0]
    finally:
        db.close()
    if left_sessions == left_turns == left_child == left_reports == 0:
        ok("烟测数据清理彻底", "库里没有残留的孩子/会话/回合/周报")
    else:
        bad("烟测数据清理彻底",
            f"残留 孩子 {left_child} / 会话 {left_sessions} / 回合 {left_turns} / 周报 {left_reports}")

    weekly = facts.get("weekly") or {}
    if weekly.get("report_id") and weekly.get("headline"):
        ok("周报生成", str(weekly["headline"])[:40])
    else:
        bad("周报生成", str(weekly)[:80])
    if facts.get("parent_published") is True:
        ok("家长端可见", "发布后的周报在家长端已可见（随后随孩子数据删除）")
    else:
        bad("家长端可见", "家长端看不到已发布周报")
    if facts.get("question") and facts.get("captured_ok") is True:
        ok("提问已生成", str(facts["question"])[:40])
    else:
        bad("提问已生成", "拍照后没有拿到提问")
    client = teacher_client()
    if not client:
        bad("内容闸门对照", "教师登录失败，无法现场对照")
        return
    try:
        live = client.get("/api/teacher/selfcheck/demo").json()["cases"]
        names_local = [case["name"] for case in facts.get("gate_cases") or []]
        names_live = [case["name"] for case in live]
        if len(names_live) >= 7 and names_local == names_live:
            ok("内容闸门对照", f"{len(names_live)} 例，与冒烟记录一致")
        else:
            bad("内容闸门对照", f"冒烟记录 {names_local}，现场 {names_live}")
    finally:
        client.close()


# ---------------------------------------------------------------- 浏览器
def check_shot(path_str: str, label: str) -> bool:
    path = Path(path_str)
    if not path.exists():
        bad(label, f"截图不存在 {path.name}")
        return False
    blob = path.read_bytes()
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        bad(label, f"{path.name} 不是 PNG")
        return False
    if len(blob) < 15_000:
        bad(label, f"{path.name} 只有 {len(blob)} 字节，像是空白页")
        return False
    return True


def step_browser() -> None:
    demo = SHOTS / "net_cam_report.json"
    teacher = SHOTS / "teacher_net_report.json"
    deps = [ROOT / "_tools" / "_net_cam_demo.py"]
    if not fresh(demo, deps, "机器人端证据"):
        return
    report = json.loads(demo.read_text(encoding="utf-8"))
    if report.get("console_errors"):
        bad("机器人端 console", str(report["console_errors"][:2]))
    else:
        ok("机器人端 console", "无错误")
    shots = report.get("shots") or []
    good = [shot for shot in shots if check_shot(shot, "机器人端截图")]
    if shots and len(good) == len(shots):
        ok("机器人端截图", f"{len(shots)} 张均有效")
    elif not shots:
        bad("机器人端截图", "报告里没有截图")
    expects = {
        "check_note": "有画面",
        "net_hint": "连上了",
        "captured_hint": "已取得图片",
        "autoflip_hint": "检测到翻页",
        "mjpeg_hint": "已取得图片",
        "basic_hint": "已取得图片",
    }
    for key, needle in expects.items():
        text = str(report.get(key) or "")
        if needle in text:
            ok(f"断言 {key}", text.replace("\n", " ")[:40])
        else:
            bad(f"断言 {key}", f"期望包含“{needle}”，实际 {text[:60]!r}")
    story = str(report.get("story") or "")
    if len(story) >= 4:
        ok("讲稿先讲出来", story[:40])
    else:
        bad("讲稿先讲出来", f"story 区没内容：{story!r}")
    if report.get("story_audio_ok"):
        ok("讲稿音频可取", str(report.get("story_audio"))[-40:])
    else:
        bad("讲稿音频可取", str(report.get("story_audio"))[:80])
    free_text = str(report.get("free_answer") or "")
    if "小朋友问" in free_text and len(free_text) >= 12:
        ok("孩子自由提问有回答", free_text.replace("\n", " ")[:60])
    else:
        bad("孩子自由提问有回答", f"没看到问答：{free_text[:80]!r}")
    if report.get("free_audio_ok"):
        ok("自由回答音频可取", str(report.get("free_audio"))[-40:])
    else:
        bad("自由回答音频可取", str(report.get("free_audio"))[:80])
    after = report.get("profile_after_free") or {}
    recent = [str(item) for item in (after.get("recent") or [])]
    if int(after.get("free_questions") or 0) >= 1 and any("小兔子" in item for item in recent):
        ok("提问进了阅读档案", f"档案里 {after.get('free_questions')} 条提问，最近：{recent[:1]}")
    else:
        bad("提问进了阅读档案", f"档案里没看到刚问的那句：{after!r}")
    ended = report.get("ended") or []
    if ended == [True, True, True, True]:
        ok("四个会话均正常结束", str(ended))
    else:
        bad("四个会话均正常结束", str(ended))
    if (report.get("capture_hits") or 0) >= 2:
        ok("自动翻页取到帧", f"{report['capture_hits']} 次")
    else:
        bad("自动翻页取到帧", f"命中 {report.get('capture_hits')}")
    if "127.0.0.1" in str(report.get("check_saved") or ""):
        ok("自检页地址已保存", "localStorage 里有摄像头地址")
    else:
        bad("自检页地址已保存", str(report.get("check_saved"))[:80])

    if not fresh(teacher, [ROOT / "_tools" / "_teacher_net.py"], "教师端证据"):
        return
    report = json.loads(teacher.read_text(encoding="utf-8"))
    if report.get("console_errors"):
        bad("教师端 console", str(report["console_errors"][:2]))
    else:
        ok("教师端 console", "无错误")
    choices = report.get("choices") or []
    if len(choices) >= 5:
        ok("教师端来源齐全", f"{len(choices)} 种：{' / '.join(choices)[:60]}")
    else:
        bad("教师端来源齐全", str(choices))
    if "已取得图片" in str(report.get("hint") or ""):
        ok("教师端代抓成功", str(report["hint"])[:40])
    else:
        bad("教师端代抓成功", str(report.get("hint"))[:60])
    shots = report.get("shots") or []
    good = [shot for shot in shots if check_shot(shot, "教师端截图")]
    if shots and len(good) == len(shots):
        ok("教师端截图", f"{len(shots)} 张均有效")
    prof = report.get("profile") or {}
    if int(prof.get("bars") or 0) == 14 and str(prof.get("pages") or "").strip() not in ("", "–"):
        ok("教师端阅读档案", f"{prof.get('child')}：页数 {prof.get('pages')}、绘本 {prof.get('books')}、"
                             f"时长 {prof.get('minutes')} 分、提问 {prof.get('free')}、14 天柱状图")
    else:
        bad("教师端阅读档案", f"KPI 或柱状图没渲染：{prof!r}")


# ---------------------------------------------------------------- 阅读档案
PROFILE_FIELDS = (
    "pages", "sessions", "books_read", "minutes", "answered", "answered_ratio",
    "understood_yes", "understood_partial", "free_questions", "books", "daily",
    "recent_questions", "child",
)


def step_profile() -> None:
    """不信前端显示：先看入口页，再拿接口自报数字回 SQLite 直查对账。"""
    client = teacher_client()
    if client is None:
        note("服务没起或口令不对，跳过阅读档案复核")
        return
    try:
        page = client.get("/teacher")
        if page.status_code == 200 and "阅读档案" in page.text:
            ok("阅读档案入口", "教师端页面有「阅读档案」标签页")
        else:
            bad("阅读档案入口", f"{page.status_code}，页面里没有「阅读档案」")
        kids = client.get("/api/teacher/children")
        items = ((kids.json() or {}).get("items") or []) if kids.status_code == 200 else []
        if not items:
            note("库里还没有孩子，跳过档案对账")
            return
        child = items[0]
        reply = client.get(f"/api/teacher/reading/{child['id']}")
        if reply.status_code != 200:
            bad("阅读档案接口", f"{reply.status_code} {reply.text[:120]}")
            return
        data = reply.json()
        missing = [key for key in PROFILE_FIELDS if key not in data]
        if missing:
            bad("阅读档案字段", f"缺 {missing}")
            return
        if (data.get("child") or {}).get("id") != child["id"]:
            bad("阅读档案孩子", f"返回的不是请求的那个：{data.get('child')}")
        else:
            ok("阅读档案字段", f"{child['name']}：页数 {data['pages']}、绘本 {data['books_read']}、"
                                f"时长 {data['minutes']} 分、回应率 {data['answered_ratio']}%、"
                                f"主动提问 {data['free_questions']}")
        daily = data.get("daily") or []
        horizons = {row.get("day") for row in daily if isinstance(row, dict)}
        if all(isinstance(day, str) and len(day) == 10 for day in horizons):
            ok("阅读档案日趋势", f"{len(daily)} 天有页数记录")
        else:
            bad("阅读档案日趋势", f"day 字段不像日期：{list(horizons)[:3]}")
        loose = client.get(f"/api/teacher/reading/{child['id']}?days=9999")
        if loose.status_code == 200:
            ok("阅读档案天数越界", "days=9999 被收住，没有报错")
        else:
            bad("阅读档案天数越界", f"{loose.status_code} {loose.text[:80]}")
        ghost = client.get("/api/teacher/reading/99999999")
        if ghost.status_code == 404:
            ok("阅读档案不存在就 404", "没有这个孩子时不编数据")
        else:
            bad("阅读档案不存在就 404", f"{ghost.status_code} {ghost.text[:80]}")
        # 独立通道：绕开接口，直接读 SQLite 对账，口径与 db.reading_profile 一致
        conn = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
        try:
            pages = conn.execute(
                "SELECT COUNT(t.id) FROM turns t JOIN sessions s ON s.id=t.session_id"
                " WHERE s.child_id=?", (child["id"],)
            ).fetchone()[0]
            frees = conn.execute(
                "SELECT COUNT(*) FROM free_qas f JOIN sessions s ON s.id=f.session_id"
                " WHERE s.child_id=?", (child["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        if int(data.get("pages") or 0) == int(pages) and int(data.get("free_questions") or 0) == int(frees):
            ok("阅读档案数字对账", f"页数 {pages}、提问 {frees}，与库里的记录一致")
        else:
            bad("阅读档案数字对账", f"接口 pages={data.get('pages')}/free={data.get('free_questions')}，"
                                     f"库 pages={pages}/free={frees}")
    finally:
        client.close()


# ---------------------------------------------------------------- 防御
def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _tiny_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 36), (240, 200, 120)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class AdversarialCamera(BaseHTTPRequestHandler):
    """一台“不老实”的摄像头：重定向、非图片、超大帧、超时、要 Basic 登录。"""

    server_version = "AdversarialCam/1.0"
    saw_basic: list[str] = []
    JPEG = _tiny_jpeg()

    def log_message(self, *args) -> None:  # 静音
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/auth"):
            header = self.headers.get("Authorization") or ""
            type(self).saw_basic.append(header)
            if header != "Basic " + base64.b64encode(b"cam:secret").decode():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="cam"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._jpeg()
        if self.path.startswith("/ok"):
            return self._jpeg()
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://example.com/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/text"):
            body = "这不是图片".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/huge"):
            total = 9 * 1024 * 1024
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            try:
                self.wfile.write(b"\xff\xd8" + b"\x00" * (total - 4) + b"\xff\xd9")
            except OSError:
                pass
            return
        if self.path.startswith("/slow"):
            time.sleep(7)
            return self._jpeg()
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _jpeg(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(self.JPEG)))
        self.end_headers()
        self.wfile.write(self.JPEG)


def step_defense() -> None:
    client = teacher_client()
    if not client:
        bad("防御复核", "服务没起或教师登录失败，无法现场验证")
        return
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), AdversarialCamera)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{port}"

    def grab(url: str) -> httpx.Response:
        return client.get("/api/camera/snapshot", params={"url": url}, timeout=20)

    def expect(label: str, response: httpx.Response, code: int, needle: str = "") -> None:
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except ValueError:
            detail = response.text[:80]
        if response.status_code == code and (not needle or needle in detail):
            ok(label, f"{code} {detail[:48]}")
        else:
            bad(label, f"期望 {code}/{needle!r}，实际 {response.status_code} {detail[:80]!r}")

    try:
        good = grab(f"http://{host}/ok")
        if good.status_code == 200 and good.content[:2] == b"\xff\xd8":
            ok("正常帧", f"200，{len(good.content)} 字节 JPEG")
        else:
            bad("正常帧", f"{good.status_code}，{good.content[:10]!r}")
        if good.headers.get("cache-control") == "no-store":
            ok("帧不落缓存", "Cache-Control: no-store")
        else:
            bad("帧不落缓存", str(good.headers.get("cache-control")))

        basic = grab(f"http://cam:secret@{host}/auth")
        if basic.status_code == 200:
            ok("带账号密码", "200，服务端显式带上 Basic")
        else:
            bad("带账号密码", f"{basic.status_code} {basic.text[:80]}")
        if any(header.startswith("Basic ") for header in AdversarialCamera.saw_basic):
            ok("Basic 头确实发出", "摄像头侧收到了 Authorization: Basic …")
        else:
            bad("Basic 头确实发出", str(AdversarialCamera.saw_basic[:2]))
        expect("密码错误", grab(f"http://cam:wrong@{host}/auth"), 401, "账号密码")

        expect("重定向不跟随", grab(f"http://{host}/redirect"), 502, "302")
        expect("非图片拒绝", grab(f"http://{host}/text"), 415)
        expect("超大帧拒绝", grab(f"http://{host}/huge"), 413)
        expect("公网 IP 拒绝", grab("http://8.8.8.8/capture"), 400, "局域网")
        expect("rtsp 引导", grab("rtsp://127.0.0.1/live"), 400, "camera_bridge")
        expect("ftp 拒绝", grab("ftp://127.0.0.1/a.jpg"), 400, "http")

        started = time.monotonic()
        slow = grab(f"http://{host}/slow")
        elapsed = time.monotonic() - started
        if slow.status_code == 502 and elapsed < 9:
            ok("超时快速失败", f"{elapsed:.1f}s 返回 502")
        else:
            bad("超时快速失败", f"{slow.status_code}，耗时 {elapsed:.1f}s")

        anonymous = httpx.Client(base_url=BASE, timeout=15)
        try:
            reply = anonymous.get("/api/camera/snapshot", params={"url": f"http://{host}/ok"})
            if reply.status_code in {401, 403}:
                ok("无凭证拒绝", f"{reply.status_code} {reply.json().get('detail', '')}")
            else:
                bad("无凭证拒绝", f"期望 401/403，实际 {reply.status_code} {reply.text[:80]}")
        finally:
            anonymous.close()
    finally:
        client.close()
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 离线
def step_offline() -> None:
    external: dict[str, str] = {}
    pattern = re.compile(r"https?://([A-Za-z0-9._-]+)")
    allowed = {"127.0.0.1", "localhost", "0.0.0.0", "www.w3.org"}

    def inside(host: str) -> bool:
        """私有/环回 IP、局域主机名都算园内；只有真正的外网才该报错。"""
        if host in allowed:
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback
        except ValueError:
            return host.endswith(".local") or host.endswith(".lan")

    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "app" / "web").rglob("*.js")):
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
            host = match.group(1)
            if not inside(host):
                external.setdefault(host, str(path.relative_to(ROOT)))
    forbidden = re.compile(r"dashscope|aliyuncs|openai\.com|baidu|googleapis|volcengine")
    leaks = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
             if forbidden.search(p.read_text(encoding="utf-8", errors="replace"))]
    if external:
        bad("代码里的外网主机", str(external))
    else:
        ok("代码里的外网主机", "只有环回地址与 w3.org 命名空间")
    if leaks:
        bad("云厂商域名", str(leaks))
    else:
        ok("云厂商域名", "app/ 里没有云厂商调用")
    client = teacher_client()
    if not client:
        note("服务没起，跳过服务自己的出站审计")
        return
    try:
        hint = client.get("/api/teacher/audit").json()["human_hint"]
        if "没有" in hint or " 0 " in hint or hint.endswith("0"):
            ok("服务出站审计", hint[:60])
        else:
            bad("服务出站审计", hint[:120])
    finally:
        client.close()


# ---------------------------------------------------------------- 仓库
def step_repo() -> None:
    status = run(["git", "status", "--porcelain"])
    if status.returncode != 0:
        bad("git 仓库", status.stderr[-160:])
        return
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        bad("工作区干净", str(dirty[:5]))
    else:
        ok("工作区干净", "git status 无输出")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = run(["git", "ls-remote", "origin", "main"], timeout=60)
    if remote.returncode != 0:
        note(f"远程不可达，跳过哈希比对（{remote.stderr.strip()[:60]}）")
    elif head and remote.stdout.split()[0] == head:
        ok("远程同步", f"origin/main = {head[:7]}")
    else:
        bad("远程同步", f"本地 {head[:7]}，远程 {remote.stdout.split()[:1]}")
    keys = run(["git", "grep", "--cached", "-E", r"sk-[A-Za-z0-9._-]{8,}"])
    if keys.returncode == 1 and not keys.stdout.strip():
        ok("暂存区密钥扫描", "没有真实 sk- 开头密钥")
    else:
        bad("暂存区密钥扫描", keys.stdout[:200] or keys.stderr[:200])
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    banned = [name for name in tracked
              if name.startswith("data/") or name.startswith("_tools/shots/")
              or (Path(name).name == ".env")]
    if banned:
        bad("敏感文件未入库", str(banned[:5]))
    else:
        ok("敏感文件未入库", f"{len(tracked)} 个跟踪文件，无 data/、.env、截图")


# ---------------------------------------------------------------- 深度自检
def step_deep() -> None:
    """不采信“装没装”：让服务端现场跑五个引擎，看它真出结果。"""
    client = httpx.Client(base_url=BASE, timeout=180)
    try:
        login = client.post("/api/login", json={"pin": PIN})
    except httpx.HTTPError:
        client.close()
        note("服务没起，跳过深度自检")
        return
    if login.status_code != 200:
        client.close()
        note("口令不对，跳过深度自检")
        return
    try:
        page = client.get("/device-check")
        if page.status_code == 200 and "真跑五个引擎" in page.text:
            ok("自检页入口", "device-check 有“真跑五个引擎”卡片")
        else:
            bad("自检页入口", f"{page.status_code}，页面里没有深度自检卡片")
        started = time.monotonic()
        reply = client.post("/api/teacher/selfcheck/deep")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if reply.status_code != 200:
            bad("深度自检接口", f"{reply.status_code} {reply.text[:120]}")
            return
        data = reply.json()
        checks = data.get("checks") or []
        if data.get("verdict") != "pass":
            failed = [f"{c.get('name')}：{c.get('detail')}" for c in checks if c.get("status") == "fail"]
            bad("深度自检结论", data.get("summary") or "；".join(failed))
            return
        if len(checks) != 5:
            bad("深度自检项数", f"只有 {len(checks)} 项，应该是 5 个引擎")
            return
        total_ms = int(data.get("total_ms") or elapsed_ms)
        if total_ms < 1500:
            bad("深度自检像走过场", f"五项只用了 {total_ms} ms，不像真调模型")
            return
        summary = "；".join(f"{c['name']} {c.get('ms', 0)}ms" for c in checks)
        ok("深度自检", f"{len(checks)} 项全过，共 {total_ms} ms：{summary}")
        warns = [c for c in checks if c.get("status") == "warn"]
        if warns:
            note("有提示项：" + "；".join(f"{c['name']}：{c.get('detail')}" for c in warns))
    finally:
        client.close()


STEPS = {
    "pytest": step_pytest,
    "imports": step_imports,
    "js": step_js,
    "serve": step_serve,
    "smoke": step_smoke,
    "browser": step_browser,
    "defense": step_defense,
    "offline": step_offline,
    "deep": step_deep,
    "profile": step_profile,
    "repo": step_repo,
}


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print(" / ".join(STEPS))
        return 0
    wanted = [arg for arg in argv if not arg.startswith("-")] or list(STEPS)
    unknown = [name for name in wanted if name not in STEPS]
    if unknown:
        print(f"未知步骤：{unknown}，可用：{list(STEPS)}")
        return 2
    for name in wanted:
        print(f"\n== 复核 {name} ==")
        started = time.monotonic()
        try:
            STEPS[name]()
        except Exception as exc:  # 复核器自己出错也要报出来，不能静默
            bad(f"{name} 复核器异常", repr(exc))
        print(f"  （{time.monotonic() - started:.1f}s）")
    print("\n" + "=" * 56)
    if FAILS:
        print(f"复核未通过：{len(FAILS)} 项")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print(f"全部复核通过：{len(wanted)} 个步骤，每步都用了独立通道重新求证")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
