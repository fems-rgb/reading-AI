"""重启本地开发服务：杀掉监听 8000 的进程，再后台拉起 run.py。

改了 app/ 下的 Python 代码（路由、业务逻辑）必须重启；只改 static/ 与 templates/ 不用。
用法：python _tools\\_restart.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"


def kill_listener() -> list[str]:
    output = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, errors="ignore"
    ).stdout
    pids: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(":8000") and parts[3] == "LISTENING":
            pids.add(parts[4])
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        print("已停止旧服务 PID", pid)
    return sorted(pids)


def log_handle():
    """上一个服务用 cmd 重定向占着 server.log 时换个名字，不要因为一个日志文件重启失败。"""
    try:
        return (ROOT / "_tools" / "server.log").open("wb"), ROOT / "_tools" / "server.log"
    except PermissionError:
        path = ROOT / "_tools" / f"server-{time.strftime('%H%M%S')}.log"
        return path.open("wb"), path


def start() -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    handle, path = log_handle()
    subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=str(ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    print("日志:", path)


def wait_ready(seconds: float = 30) -> dict:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=2) as response:
                return json.loads(response.read().decode())
        except Exception as exc:  # noqa: BLE001 - 启动中
            last = str(exc)
            time.sleep(0.5)
    raise SystemExit(f"服务没起来：{last}，看 _tools/server.log")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    kill_listener()
    start()
    print("healthz:", wait_ready())
