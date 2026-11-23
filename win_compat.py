"""Windows 上 asyncio 的兼容处理。入口脚本（run.py / update_data.py）必须在 import asyncio 之前先 import 本模块。

部分 Windows Python 环境加载 _overlapped 会报错：用空壳顶替，再改用 SelectorEventLoop，不再依赖它。
"""
import sys
import types

_overlapped_fake = types.ModuleType("_overlapped")
_overlapped_fake.OVERLAPPED_PIPE = 0
_overlapped_fake.OVERLAPPED_SOCKET = 1
sys.modules["_overlapped"] = _overlapped_fake

import asyncio  # noqa: E402

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
