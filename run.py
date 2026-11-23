"""ARAM Coach 唯一启动入口（start.bat / start.ps1 也是调用它）。"""
import win_compat  # noqa: F401  必须在 import asyncio 之前

import asyncio

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
