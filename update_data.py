"""手动同步大乱斗数据（OP.GG 梯度/出装/符文 + Meraki 平衡修正），写入 data/aram_meta.json。

应用启动时也会在数据过期后自动同步，这个脚本用于手动刷新或排查网络问题。
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

from app import data_sync

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

data_dir = Path(__file__).resolve().parent / "data"
item_map = json.loads((data_dir / "item_map.json").read_text(encoding="utf-8"))

report = asyncio.run(data_sync.sync(data_dir, item_map))
for source, result in report.items():
    print(f"{source:12s} {result}")
