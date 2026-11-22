import sys
import types

_overlapped_fake = types.ModuleType("_overlapped")
_overlapped_fake.OVERLAPPED_PIPE = 0
_overlapped_fake.OVERLAPPED_SOCKET = 1
sys.modules["_overlapped"] = _overlapped_fake

import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
from app.main import app

config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=8765,
    log_level="info",
)
server = uvicorn.Server(config)
asyncio.run(server.serve())
