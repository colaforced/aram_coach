"""ARAM Coach 唯一启动入口（start.bat / start.ps1 也是调用它）。"""
import sys
import types

# 必须在 import asyncio 之前注入：部分 Windows Python 环境加载 _overlapped 会报错，
# 用空壳顶替后改用 SelectorEventLoop，不再依赖它
_overlapped_fake = types.ModuleType("_overlapped")
_overlapped_fake.OVERLAPPED_PIPE = 0
_overlapped_fake.OVERLAPPED_SOCKET = 1
sys.modules["_overlapped"] = _overlapped_fake

import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from app.main import app, CONFIG, log

port = CONFIG.get("web_port", 8765)
log.info(f"Web UI: http://127.0.0.1:{port}/")

config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=port,
    log_level="info",
)
server = uvicorn.Server(config)
asyncio.run(server.serve())
