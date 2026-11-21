"""接口级联调：登录 → 开始共读 → 设备拍照 → 提问 → 回答 → 结束 → 建议/周报 → 家长。"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import threading

import pytest
from conftest import page_jpeg

_DIGEST_FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


class _CameraHandler(http.server.BaseHTTPRequestHandler):
    """模拟网络摄像头：/capture 单帧、/stream MJPEG、/auth 要 Basic、/digest 要 Digest、/html 是网页。"""

    REALM = "esp32cam"
    NONCE = "abc123"

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/capture"):
            body, content_type = page_jpeg(), "image/jpeg"
            self._send(200, body, content_type)
        elif self.path.startswith("/stream"):
            # multipart/x-mixed-replace：两块，每块前都有边界行，跟廉价网络摄像头一致
            frame = page_jpeg()
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            for _ in range(2):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        elif self.path.startswith("/auth"):
            expected = "Basic " + base64.b64encode(b"cam:secret").decode()
            if self.headers.get("Authorization") == expected:
                self._send(200, page_jpeg(), "image/jpeg")
            else:
                self._send(401, b"login", "text/plain", {"WWW-Authenticate": 'Basic realm="cam"'})
        elif self.path.startswith("/digest"):
            if self._digest_ok():
                self._send(200, page_jpeg(), "image/jpeg")
            else:
                self._send(
                    401,
                    b"login",
                    "text/plain",
                    {"WWW-Authenticate": f'Digest realm="{self.REALM}", nonce="{self.NONCE}", qop="auth"'},
                )
        else:
            body, content_type = b"<html>not a camera</html>", "text/html"
            self._send(200, body, content_type)

    def _digest_ok(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Digest "):
            return False
        fields = {key: quoted or bare for key, quoted, bare in _DIGEST_FIELD.findall(header[7:])}
        ha1 = _md5(f"cam:{self.REALM}:secret")
        ha2 = _md5(f"GET:{fields.get('uri', '')}")
        expected = _md5(
            ":".join([ha1, self.NONCE, fields.get("nc", ""), fields.get("cnonce", ""), fields.get("qop", ""), ha2])
        )
        return fields.get("username") == "cam" and fields.get("response") == expected

    def _send(
        self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D401
        return


@pytest.fixture()
def fake_camera():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CameraHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _first_book(client) -> dict:
    books = client.get("/api/teacher/books").json()["items"]
    assert books, "种子数据里应该有绘本"
    return books[0]


def _session(client) -> dict:
    child = client.get("/api/teacher/children").json()["items"][0]
    book = _first_book(client)
    response = client.post(
        "/api/teacher/sessions", json={"child_id": child["id"], "book_id": book["id"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_login_required(client):
    anonymous = client.get("/api/teacher/overview")
    assert anonymous.status_code == 401
    assert "口令" in anonymous.json()["detail"]


def test_wrong_pin_is_rejected(client):
    response = client.post("/api/login", json={"pin": "0000"})
    assert response.status_code == 401


def test_home_and_healthz(client):
    assert client.get("/healthz").json()["ok"] is True
    home = client.get("/")
    assert home.status_code == 200
    assert "绘本" in home.text


def test_device_check_page_is_public(client):
    """换机验收页不读孩子数据，所以不需要登录。"""
    page = client.get("/device-check")
    assert page.status_code == 200
    assert "设备自检" in page.text
    assert "/static/device.js" in page.text


def test_favicon_is_served(client):
    """浏览器自己会来要图标；没有路由就会在控制台多一条 404。"""
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert "svg" in icon.headers["content-type"]


def test_device_snapshot_keeps_page_thumbnail(teacher):
    """提问/回答/刷新重连后，设备端都要能拿回这一页的小图。"""
    client = teacher
    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    captured = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    )
    assert captured.status_code == 200, captured.text
    snapshot = client.get(f"/api/device/{session_id}", params={"token": token}).json()
    thumb = snapshot["snapshot"]["last_thumb"]
    assert thumb.get("b64"), snapshot["snapshot"]
    assert thumb.get("mime") == "image/jpeg"


def test_camera_snapshot_rejects_bad_credentials(teacher):
    """代抓帧等于服务端替设备发请求，没登录也没设备凭证一律不给用。"""
    client = teacher
    client.cookies.clear()  # 先退出登录，再试“游客 + 假设备凭证”
    response = client.get(
        "/api/camera/snapshot",
        params={"session_id": "nope", "token": "bad", "url": "http://127.0.0.1/capture"},
    )
    assert response.status_code == 403
    assert client.post("/api/login", json={"pin": "2026"}).status_code == 200  # 恢复登录态


def test_camera_snapshot_allows_logged_in_teacher(teacher, fake_camera):
    """教师端上传页没有设备 token：登录 cookie 也能代抓（同样受局域网白名单约束）。"""
    response = teacher.get("/api/camera/snapshot", params={"url": f"{fake_camera}/capture"})
    assert response.status_code == 200, response.text
    assert response.content[:2] == b"\xff\xd8"


def test_camera_snapshot_only_allows_lan(teacher):
    """完全离线的机器不能被当成跳板去访问外网。"""
    client = teacher
    started = _session(client)
    base = {"session_id": started["session_id"], "token": started["token"]}
    public = client.get("/api/camera/snapshot", params={**base, "url": "http://8.8.8.8/capture"})
    assert public.status_code == 400
    assert "局域网" in public.json()["detail"]
    https = client.get("/api/camera/snapshot", params={**base, "url": "https://127.0.0.1/capture"})
    assert https.status_code == 400
    file_url = client.get("/api/camera/snapshot", params={**base, "url": "file:///c:/windows/win.ini"})
    assert file_url.status_code == 400


def test_camera_snapshot_proxies_jpeg(teacher, fake_camera):
    """ESP32 / 手机 IP 摄像头都走这条路：服务端拉一帧，原样交给设备端。"""
    client = teacher
    started = _session(client)
    params = {"session_id": started["session_id"], "token": started["token"]}
    ok = client.get("/api/camera/snapshot", params={**params, "url": f"{fake_camera}/capture"})
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"].startswith("image/jpeg")
    assert ok.content[:2] == b"\xff\xd8", "返回的应是原始 JPEG"
    not_image = client.get("/api/camera/snapshot", params={**params, "url": f"{fake_camera}/html"})
    assert not_image.status_code == 415


def test_camera_snapshot_extracts_frame_from_mjpeg(teacher, fake_camera):
    """只给 /stream（MJPEG）的廉价摄像头也能用：从流里抠出第一张完整 JPEG。"""
    client = teacher
    started = _session(client)
    params = {"session_id": started["session_id"], "token": started["token"]}
    ok = client.get("/api/camera/snapshot", params={**params, "url": f"{fake_camera}/stream"})
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"].startswith("image/jpeg")
    assert ok.content[:2] == b"\xff\xd8" and ok.content[-2:] == b"\xff\xd9"
    assert b"Content-Type: image/jpeg" not in ok.content, "应去掉 multipart 的头，只留一帧"


def test_camera_snapshot_explains_rtsp(teacher):
    """只给 RTSP 的机器人摄像头：直接告诉老师怎么转，不让人对着“只支持 http”发懵。"""
    response = teacher.get("/api/camera/snapshot", params={"url": "rtsp://192.168.1.64:554/stream1"})
    assert response.status_code == 400
    assert "camera_bridge" in response.json()["detail"]


def test_camera_snapshot_supports_basic_login(teacher, fake_camera):
    """摄像头网页要登录时，把账号密码写进地址即可（服务端用 Basic 认证拉图）。"""
    host = fake_camera.replace("http://", "")
    started = _session(teacher)
    params = {"session_id": started["session_id"], "token": started["token"]}
    ok = teacher.get("/api/camera/snapshot", params={**params, "url": f"http://cam:secret@{host}/auth"})
    assert ok.status_code == 200, ok.text
    assert ok.content[:2] == b"\xff\xd8"
    denied = teacher.get("/api/camera/snapshot", params={**params, "url": f"http://cam:wrong@{host}/auth"})
    assert denied.status_code == 401
    assert "账号密码" in denied.json()["detail"]


def test_camera_snapshot_supports_digest_login(teacher, fake_camera):
    """海康/大华式 Digest 认证：服务端拿 401 挑战后用同一份账号密码重算，老师不用装别的工具。"""
    host = fake_camera.replace("http://", "")
    started = _session(teacher)
    params = {"session_id": started["session_id"], "token": started["token"]}
    ok = teacher.get("/api/camera/snapshot", params={**params, "url": f"http://cam:secret@{host}/digest"})
    assert ok.status_code == 200, ok.text
    assert ok.content[:2] == b"\xff\xd8"


def test_full_reading_flow(teacher):
    client = teacher
    overview = client.get("/api/teacher/overview").json()
    assert overview["children"] >= 1
    assert overview["modes"]["llm"] in {"stub", "http"}
    assert isinstance(overview["answered_today"], int)
    assert overview["answered_today"] <= overview["pages_today"]

    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    assert started["robot_url"].startswith("/robot?session=")

    # 机器人端：拍一页
    response = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["ok"] is True, page
    question = page["question"]["question"]
    assert question.endswith(("？", "?"))
    assert page["summary"]["raw_text"], "原文层必须来自 OCR 或库内缓存"
    assert page["checker"]["summary"]["verdict"] in {"pass", "rewrite", "block"}
    assert 0 <= page["latency_ms"]

    # 机器人端：回答
    answered = client.post(
        f"/api/device/{session_id}/answer", json={"text": "风筝", "token": token}
    ).json()
    assert answered["ok"] is True
    assert answered["reply"]

    # 教师端：结束并生成建议
    ended = client.post(f"/api/teacher/sessions/{session_id}/end", json={}).json()
    assert ended["ok"] is True
    assert ended["pages"] >= 1
    assert ended["session_check"]["verdict"] in {"pass", "warn", "block"}
    for item in ended["advice"]:
        assert item["evidence"], "每条建议都必须挂到具体问答上"

    advice = client.get("/api/teacher/advice").json()["items"]
    assert advice
    first = advice[0]
    decided = client.post(
        f"/api/teacher/advice/{first['id']}/decide", json={"action": "accept"}
    )
    assert decided.status_code == 200


def test_device_websocket_streams_events(teacher):
    """机器人端走的实时链路：连上 → 拍照 → 收到 page 事件。"""

    import base64

    client = teacher
    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    image = base64.b64encode(page_jpeg()).decode("ascii")
    with client.websocket_connect(f"/ws/device/{session_id}?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "snapshot"
        ws.send_json({"type": "capture", "page_no": 1, "image": image})
        events = []
        for _ in range(8):
            message = ws.receive_json()
            if message.get("event"):
                events.append(message["event"])
            if "page" in events:
                break
        assert "page" in events, events
    client.post(f"/api/teacher/sessions/{session_id}/end", json={})


def test_device_audio_answer_via_http(teacher):
    """兼容模式下的「按住说话」：WAV 走 /listen，和文字回答进同一条理解链路。"""
    client = teacher
    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    page = client.post(
        f"/api/device/{session_id}/page",
        data={"page_no": 1, "token": token},
        files={"file": ("page.jpg", page_jpeg(), "image/jpeg")},
    ).json()
    assert page["ok"] is True

    # 最小合法 WAV（16k 单声道 16bit 静音）；stub ASR 按字节哈希选样例
    samples = b"\x00" * 3200
    wav = (
        b"RIFF" + (36 + len(samples)).to_bytes(4, "little") + b"WAVEfmt "
        + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (1).to_bytes(2, "little")
        + (16000).to_bytes(4, "little") + (32000).to_bytes(4, "little")
        + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
        + b"data" + len(samples).to_bytes(4, "little") + samples
    )
    response = client.post(
        f"/api/device/{session_id}/listen",
        data={"token": token},
        files={"file": ("answer.wav", wav, "audio/wav")},
    )
    assert response.status_code == 200, response.text
    answered = response.json()
    assert answered["ok"] is True, answered
    assert answered["reply"]
    client.post(f"/api/teacher/sessions/{session_id}/end", json={})


def test_device_websocket_rejects_bad_token(teacher):
    from starlette.websockets import WebSocketDisconnect

    client = teacher
    started = _session(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/device/{started['session_id']}?token=bad") as ws:
            ws.receive_json()
    client.post(f"/api/teacher/sessions/{started['session_id']}/end", json={})


def test_device_websocket_errors_do_not_drop_connection(teacher):
    """机器人端容错：坏图 / 不认识的指令只回人话提示，不能把实时通道拆掉。"""
    client = teacher
    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    with client.websocket_connect(f"/ws/device/{session_id}?token={token}") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "capture", "page_no": 1, "image": "abc!"})
        error = None
        for _ in range(6):
            message = ws.receive_json()
            if message.get("type") == "error":
                error = message
                break
        assert error and "没传完整" in error["human_hint"], error

        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        ws.send_json({"type": "nonsense"})
        reply = None
        for _ in range(4):
            message = ws.receive_json()
            if message.get("type") == "error":
                reply = message
                break
        assert reply and "不认识" in reply["human_hint"], reply
    client.post(f"/api/teacher/sessions/{session_id}/end", json={})


def test_device_websocket_tells_finished_session(teacher):
    """共读结束后机器人端不能再无限重连，要能拿到 fatal 提示。"""
    from starlette.websockets import WebSocketDisconnect

    client = teacher
    started = _session(client)
    session_id, token = started["session_id"], started["token"]
    client.post(f"/api/teacher/sessions/{session_id}/end", json={})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/device/{session_id}?token={token}") as ws:
            message = ws.receive_json()
            assert message["type"] == "error"
            assert message.get("fatal") is True
            ws.receive_json()


def test_quality_gate_rejects_blank_photo(teacher):
    client = teacher
    started = _session(client)
    from io import BytesIO

    from PIL import Image

    blank = BytesIO()
    Image.new("RGB", (900, 1200), (255, 255, 255)).save(blank, format="JPEG")
    response = client.post(
        f"/api/device/{started['session_id']}/page",
        data={"page_no": 1, "token": started["token"]},
        files={"file": ("blank.jpg", blank.getvalue(), "image/jpeg")},
    )
    body = response.json()
    assert body["ok"] is False
    assert body["human_hint"], "必须给人话提示，而不是错误码"
    assert "retake" in body["actions"]
    client.post(f"/api/teacher/sessions/{started['session_id']}/end", json={})


def test_device_token_is_checked(teacher):
    client = teacher
    started = _session(client)
    response = client.post(
        f"/api/device/{started['session_id']}/answer", json={"text": "风筝", "token": "bad"}
    )
    assert response.status_code == 403
    client.post(f"/api/teacher/sessions/{started['session_id']}/end", json={})


def test_selfcheck_demo_blocks_bad_examples(teacher):
    data = teacher.get("/api/teacher/selfcheck/demo").json()
    cases = {item["name"]: item for item in data["cases"]}
    assert cases["画外实体"]["report"]["verdict"] == "block"
    assert cases["抽象词"]["report"]["verdict"] == "block"
    assert cases["一次问两件事"]["report"]["verdict"] == "block"
    assert cases["建议排名 / 无依据"]["report"]["verdict"] == "block"
    assert cases["合格样例（对照）"]["report"]["verdict"] == "pass"


def test_page_detail_returns_full_text(teacher):
    client = teacher
    book = _first_book(client)
    page_id = book["pages"][0]["id"]
    detail = client.get(f"/api/teacher/pages/{page_id}").json()["page"]
    assert detail["faithful_description"]
    assert isinstance(detail["keywords"], list)


def test_teacher_can_edit_page(teacher):
    client = teacher
    book = _first_book(client)
    page = book["pages"][0]
    saved = client.patch(
        f"/api/teacher/pages/{page['id']}",
        json={"faithful_description": "画面里有小兔子。小兔子拿着风筝。"},
    )
    assert saved.status_code == 200
    detail = client.get(f"/api/teacher/pages/{page['id']}").json()["page"]
    assert detail["status"] == "ready"


def test_bank_crud(teacher):
    client = teacher
    book = _first_book(client)
    page_id = book["pages"][0]["id"]
    created = client.post(
        "/api/teacher/bank",
        json={"book_page_id": page_id, "text": "草地上有什么呀？", "qtype": "observe"},
    ).json()
    assert created["ok"] is True
    items = client.get(f"/api/teacher/bank?book_id={book['id']}").json()["items"]
    assert any(item["id"] == created["id"] for item in items)
    assert client.delete(f"/api/teacher/bank/{created['id']}").status_code == 200


def test_weekly_report_and_parent_view(teacher):
    client = teacher
    child = [item for item in client.get("/api/teacher/children").json()["items"] if item["guardian_consent"]][0]
    generated = client.post(f"/api/teacher/weekly/{child['id']}", json={})
    assert generated.status_code == 200, generated.text
    report = generated.json()
    assert report["selfcheck"]["verdict"] == "pass"
    assert report["payload"]["headline"]
    assert "排名" not in report["payload"]["summary"]

    published = client.post(f"/api/teacher/reports/{report['report_id']}/publish", json={})
    assert published.status_code == 200

    parent = client.get(f"/api/parent/{child['parent_token']}").json()
    assert parent["consent"] is True
    assert parent["published"] is True
    assert parent["payload"]["week_start"]
    assert "audio" not in parent["payload"]

    page = client.get(f"/parent/{child['parent_token']}")
    assert page.status_code == 200


def test_weekly_report_requires_consent(teacher):
    client = teacher
    children = client.get("/api/teacher/children").json()["items"]
    target = next(item for item in children if not item["guardian_consent"])
    response = client.post(f"/api/teacher/weekly/{target['id']}", json={})
    assert response.status_code == 403
    assert "授权" in response.json()["detail"]


def test_parent_revoke_deletes_reports(teacher):
    client = teacher
    child = [item for item in client.get("/api/teacher/children").json()["items"] if item["guardian_consent"]][0]
    client.post(f"/api/teacher/weekly/{child['id']}", json={})
    revoked = client.post(f"/api/parent/{child['parent_token']}/revoke").json()
    assert revoked["ok"] is True
    after = client.get(f"/api/parent/{child['parent_token']}").json()
    assert after["consent"] is False
    # 恢复授权，避免影响其他用例
    client.post(f"/api/teacher/children/{child['id']}/consent", json={"consented": True})


def test_system_selfcheck_and_audit(teacher):
    client = teacher
    system = client.get("/api/teacher/selfcheck/system").json()
    ids = {item["id"] for item in system["checks"]}
    assert {"db_writable", "disk", "outbound", "library", "consent"} <= ids
    audit = client.get("/api/teacher/audit").json()
    assert audit["offline_only"] is True
    assert all(item["allowed"] for item in audit["items"]), audit["items"]


def test_ops_metrics_and_history(teacher):
    client = teacher
    metrics = client.get("/api/teacher/metrics").json()
    assert "avg_latency" in metrics
    history = client.get("/api/teacher/selfcheck/history").json()["items"]
    assert history, "自检结论必须留痕"
    stats = client.get("/api/teacher/selfcheck/stats").json()
    assert stats["today"]["total"] >= 1


def test_tts_cache_is_cleaned(client):
    """播放缓存不能永久留在磁盘：会话结束时删、并行会话共用的保留、超期清扫。"""
    import os
    import time

    from app.services import speech

    first = speech.synthesize("我们一起看看这一页")
    path = speech_path(first["url"])
    assert path.is_file()
    assert speech.forget([first["url"]]) == 1
    assert not path.is_file()

    second = speech.synthesize("别的小朋友还在听这一句")
    keep_path = speech_path(second["url"])
    assert speech.forget([second["url"]], keep={second["url"]}) == 0
    assert keep_path.is_file()

    old = time.time() - (speech.KEEP_DAYS + 1) * 86400
    os.utime(keep_path, (old, old))
    assert speech.prune_old() >= 1
    assert not keep_path.is_file()


def speech_path(url: str):
    from app.config import get_settings

    from app.services import speech

    return get_settings().tts_dir / f"{speech.key_from_url(url)}.wav"