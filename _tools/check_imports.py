"""导入自检：把每个模块真正 import 一次，暴露语法/循环导入问题。

用法：python _tools\\check_imports.py
"""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = [
    "app",
    "app.config",
    "app.guard",
    "app.lexicon",
    "app.utils",
    "app.db",
    "app.auth",
    "app.services",
    "app.services.metrics",
    "app.services.vision",
    "app.services.nlg",
    "app.services.llm",
    "app.services.brain",
    "app.services.speech",
    "app.services.selfcheck",
    "app.core.pipeline",
    "app.core.orchestrator",
    "app.api",
    "app.seed",
    "app.main",
]

failed = 0
for name in MODULES:
    try:
        __import__(name)
        print("OK   ", name)
    except Exception:
        failed += 1
        print("FAIL ", name)
        traceback.print_exc()

print("-" * 50)
print("失败模块数:", failed)
sys.exit(1 if failed else 0)