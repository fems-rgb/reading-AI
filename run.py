"""本地启动器：python run.py

默认只监听 127.0.0.1:8000（园内网用 HOST=0.0.0.0 打开，
但出站访问仍然只允许园内地址，见 app/guard.py）。
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    print("=" * 56)
    print("绘本阅读陪伴 AI 智能体")
    print(f"  地址      : http://{settings.host}:{settings.port}")
    print(f"  教师端    : http://{settings.host}:{settings.port}/teacher")
    print(f"  数据目录  : {settings.data_dir}")
    print(f"  模型模式  : {settings.model_modes}")
    print(f"  演示模式  : {'是（未接本地模型，用确定性规则）' if settings.demo_mode else '否'}")
    print(f"  教师口令  : {settings.teacher_pin}")
    print("=" * 56)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()