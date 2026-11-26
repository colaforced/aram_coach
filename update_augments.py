"""重新生成 data/augments.json：海斗强化符文池 + 效果描述 + 国服胜率快照。

- 符文池（名字、稀有度）：CommunityDragon 的 augment-lists.json / cherry-augments.json（启动同步也会拉）
- 效果描述：游戏本体的 kiwi.bin.json + 中文文本表（约 46MB，只在这里拉，见 app/augment_text.py）
- 胜率 / 选取率 / 最适合的英雄：腾讯 101 海斗强化符文榜（启动同步每天也会拉，这里存一份快照兜底）

新版本加了强化符文、或者描述对不上时跑一次：python update_augments.py
"""
import win_compat  # noqa: F401  必须在 import asyncio 之前（同 run.py）

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from app import augment_text, data_sync

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(__file__).resolve().parent / "data" / "augments.json"


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True,
                                 headers={"User-Agent": data_sync.USER_AGENT}) as client:
        pool = await data_sync._fetch_augments(client)
        print(f"符文池 {len(pool)} 个")
        print("下载游戏数据（kiwi.bin.json + 中文文本表，约 46MB）…")
        kiwi, strings = await asyncio.gather(
            data_sync._get_json(client, augment_text.KIWI_BIN_URL),
            data_sync._get_json(client, augment_text.STRINGTABLE_URL),
        )
        texts = augment_text.parse_kiwi_augments(kiwi, strings.get("entries") or strings)
        try:
            stats = await data_sync.fetch_augment_stats(client)
        except Exception as e:  # 国外网络可能访问不了 101：描述照样更新，统计沿用旧快照
            print(f"国服统计拉取失败（{data_sync._err(e)}），沿用旧快照")
            old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
            stats = {"date": old.get("stats_date", ""),
                     "augments": {str(a["id"]): a["stats"] for a in old.get("augments") or [] if a.get("stats")}}

    rows = []
    for a in pool:
        if not a["name"].strip():
            continue
        row = dict(a)
        row.update(texts.get(a["key"]) or {})
        if str(a["id"]) in stats["augments"]:
            row["stats"] = stats["augments"][str(a["id"])]
        rows.append(row)
    # 一个强化符文一行，版本更新时 diff 好看
    head = json.dumps({"mode": data_sync.AUGMENT_MODE, "generated_at": int(time.time()),
                       "stats_date": stats["date"]}, ensure_ascii=False)[:-1]
    body = ",\n".join(" " + json.dumps(r, ensure_ascii=False) for r in rows)
    OUT.write_text(f'{head}, "augments": [\n{body}\n]}}\n', encoding="utf-8")
    no_desc = [r["name"] for r in rows if not r.get("desc")]
    no_stats = [r["name"] for r in rows if not r.get("stats")]
    print(f"写入 {OUT.name}：{len(rows)} 个，有描述 {len(rows) - len(no_desc)}，有国服统计 {len(rows) - len(no_stats)}"
          f"（统计日期 {stats['date'] or '无'}）")
    if no_desc:
        print(f"  没有描述：{'、'.join(no_desc)}")
    if no_stats:
        print(f"  没有统计：{'、'.join(no_stats)}")


asyncio.run(main())
