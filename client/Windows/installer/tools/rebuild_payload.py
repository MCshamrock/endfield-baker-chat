"""rebuild_payload.py —— 只刷新载荷里的 dist 并重压 + 重编外壳 + 拼装

等价于 build.py 的 write_app_resources → write_payload_root → compress_payload
→ write_facts → build_shell → assemble，但**不重解 electron**（省 1 分钟）。

用法: python setup\\tools\\rebuild_payload.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build  # noqa: E402

build.write_app_resources()
build.write_payload_root()
build.compress_payload()
build.write_facts()
build.build_shell()
build.assemble()
print("done:", build.SETUP_EXE, build.human(os.path.getsize(build.SETUP_EXE)))
