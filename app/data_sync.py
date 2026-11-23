"""大乱斗数据同步：把外部统计数据整理成 data/aram_meta.json，供 main.py 覆盖 champions.json 的模板数据。

数据源：
- OP.GG 大乱斗：英雄梯度 / 胜率 / 出装 / 符文 / 召唤师技能 / 加点（非官方接口）
- Meraki lolstaticdata：大乱斗平衡修正（造成/承受伤害、治疗、护盾、韧性、急速…），源自 LoL wiki
- CommunityDragon zh_cn：符文、符文系、召唤师技能、装备的中文名

各数据源互相独立：任何一个失败，只沿用它上一次的结果，不影响其他来源。
aram_meta.json 是运行时缓存（已 gitignore），没有它时 main.py 退回 champions.json。

命令行：python update_data.py
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("aram_coach.data_sync")

SCHEMA_VERSION = 1
META_FILENAME = "aram_meta.json"

OPGG_LIST_URL = "https://lol-api-champion.op.gg/api/global/champions/aram"
OPGG_BUILD_URL = "https://lol-api-champion.op.gg/api/global/champions/aram/{champion_id}/none?tier=all"
MERAKI_CHAMPIONS_URL = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions.json"
CDRAGON_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1"

# OP.GG 对不带浏览器 UA 的请求可能拒绝
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
OPGG_CONCURRENCY = 4

# OP.GG 的 T 级 1~5（1 最强）→ 与 champions.json 里 aram_strength 同一量纲（手写数据 55~92，均值 75）
TIER_STRENGTH = {1: 90, 2: 82, 3: 74, 4: 66, 5: 58}

# Meraki stats 字段 → 输出字段与中文名
ARAM_BALANCE_FIELDS = {
    "aramDamageDealt": ("dmg_dealt", "造成伤害"),
    "aramDamageTaken": ("dmg_taken", "承受伤害"),
    "aramHealing": ("healing", "治疗"),
    "aramShielding": ("shielding", "护盾"),
    "aramTenacity": ("tenacity", "韧性"),
    "aramAbilityHaste": ("ability_haste", "技能急速"),
    "aramAttackSpeed": ("attack_speed", "攻速"),
    "aramEnergyRegen": ("energy_regen", "能量回复"),
}


# ---------------------------------------------------------------- 纯解析函数（可单测）

def parse_cdragon_names(perks: list, perkstyles: dict, spells: list, items: list) -> Dict[str, Dict[str, str]]:
    """CommunityDragon 的几份列表 → {类别: {id 字符串: 中文名}}"""
    return {
        "perks": {str(p["id"]): p["name"] for p in perks if p.get("name")},
        "styles": {str(s["id"]): s["name"] for s in perkstyles.get("styles", []) if s.get("name")},
        "spells": {str(s["id"]): s["name"] for s in spells if s.get("name")},
        "items": {str(i["id"]): i["name"] for i in items if i.get("name")},
    }


def parse_meraki_balance(champions: dict) -> Dict[str, Dict[str, float]]:
    """Meraki champions.json → {英雄数字 ID: {dmg_dealt: 1.05, ...}}，只保留不等于 1 的修正。"""
    result = {}
    for champ in champions.values():
        cid = champ.get("id")
        stats = champ.get("stats") or {}
        if cid is None:
            continue
        mods = {}
        for src, (dst, _label) in ARAM_BALANCE_FIELDS.items():
            flat = (stats.get(src) or {}).get("flat")
            if isinstance(flat, (int, float)) and flat != 1:
                mods[dst] = round(float(flat), 3)
        result[str(cid)] = mods
    return result


def balance_text(mods: Dict[str, float]) -> str:
    """{dmg_dealt: 1.05, dmg_taken: 0.9} → '造成伤害 +5% · 承受伤害 -10%'；无修正返回空串。"""
    parts = []
    for _src, (key, label) in ARAM_BALANCE_FIELDS.items():
        if key in mods:
            pct = round((mods[key] - 1) * 100)
            parts.append(f"{label} {pct:+d}%")
    return " · ".join(parts)


def tier_to_strength(tier: Optional[int], win_rate: Optional[float]) -> Optional[int]:
    """OP.GG T 级为主、胜率微调（±5），换算到 aram_strength 量纲。"""
    if not tier:
        return None
    base = TIER_STRENGTH.get(max(1, min(5, int(tier))), 74)
    adjust = 0
    if win_rate:
        adjust = max(-5, min(5, round((win_rate - 0.5) * 100)))
    return base + adjust


def parse_opgg_list(body: dict) -> Dict[str, Any]:
    """OP.GG 大乱斗英雄列表 → {version, champions: {id: {tier, rank, win_rate, pick_rate, play, kda}}}"""
    champions = {}
    for c in body.get("data", []):
        stats = c.get("average_stats") or {}
        tier_data = stats.get("tier_data") or {}
        tier = tier_data.get("tier") or stats.get("tier")
        champions[str(c["id"])] = {
            "tier": tier,
            "rank": tier_data.get("rank") or stats.get("rank"),
            "rank_prev_patch": tier_data.get("rank_prev_patch"),
            "win_rate": stats.get("win_rate"),
            "pick_rate": stats.get("pick_rate"),
            "play": stats.get("play"),
            "kda": stats.get("kda"),
        }
    return {"version": (body.get("meta") or {}).get("version", ""), "champions": champions}


def _top(entries: list) -> List[dict]:
    """按 pick_rate 降序（OP.GG 的数组本身大致有序，这里不依赖它）"""
    return sorted((e for e in entries or [] if e.get("ids")), key=lambda e: e.get("pick_rate") or 0, reverse=True)


def skill_priority(order: List[str]) -> str:
    """15 级加点顺序 → 'Q > E > W'（按 Q/W/E 各自先满 5 级的先后排序）。"""
    seen: Dict[str, int] = {}
    maxed_at: Dict[str, int] = {}
    for i, key in enumerate(order):
        if key not in ("Q", "W", "E"):
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 5:
            maxed_at[key] = i
    keys = sorted(("Q", "W", "E"), key=lambda k: (maxed_at.get(k, 99), -seen.get(k, 0)))
    return " > ".join(keys) if seen else ""


def parse_opgg_build(body: dict, names: Dict[str, Dict[str, str]],
                     item_names: Dict[str, str]) -> Dict[str, Any]:
    """OP.GG 单英雄大乱斗详情 → 出装/符文/召唤师技能/加点（已转中文名，同时保留 ID）。

    item_names 优先用本地 item_map（与局内"是否已购买"的比对口径一致），缺的再用 CommunityDragon。
    """
    data = body.get("data") or {}

    def item_name(iid: int) -> str:
        return item_names.get(str(iid)) or names["items"].get(str(iid), "")

    result: Dict[str, Any] = {}

    # 出装：核心 3 件 + 鞋 + 后期常见装备补满 6 件
    ids: List[int] = []
    core = _top(data.get("core_items"))
    if core:
        ids.extend(core[0]["ids"])
    boots = _top(data.get("boots"))
    if boots:
        ids.extend(i for i in boots[0]["ids"] if i not in ids)
    for entry in _top(data.get("last_items")):
        for i in entry["ids"]:
            if len(ids) < 6 and i not in ids:
                ids.append(i)
    build = [(i, item_name(i)) for i in ids]
    build = [(i, n) for i, n in build if n]
    if build:
        result["build_ids"] = [i for i, _ in build]
        result["build"] = [n for _, n in build]
        if core:
            result["build_win_rate"] = round(core[0].get("win", 0) / max(core[0].get("play", 1), 1), 4)
    starter = _top(data.get("starter_items"))
    if starter:
        result["starter_items"] = [n for n in (item_name(i) for i in starter[0]["ids"]) if n]

    # 符文：只认 4/2/3 结构完整的页（拼不出合法符文页的直接丢弃），取选取率最高的
    pages = [r for r in data.get("runes") or []
             if len(r.get("primary_rune_ids") or []) == 4
             and len(r.get("secondary_rune_ids") or []) == 2
             and len(r.get("stat_mod_ids") or []) == 3]
    pages.sort(key=lambda r: r.get("pick_rate") or 0, reverse=True)
    if pages:
        p = pages[0]
        perk = lambda i: names["perks"].get(str(i), str(i))  # noqa: E731
        style = lambda i: names["styles"].get(str(i), str(i))  # noqa: E731
        result["runes"] = {
            "primary_branch": style(p["primary_page_id"]),
            "primary_rune": perk(p["primary_rune_ids"][0]),
            "primary_details": [perk(i) for i in p["primary_rune_ids"]],
            "secondary_branch": style(p["secondary_page_id"]),
            "secondary_rune": " + ".join(perk(i) for i in p["secondary_rune_ids"]),
            "shards": [perk(i) for i in p["stat_mod_ids"]],
            # 写客户端符文页要用的原始 ID
            "primary_style_id": p["primary_page_id"],
            "sub_style_id": p["secondary_page_id"],
            "perk_ids": p["primary_rune_ids"] + p["secondary_rune_ids"] + p["stat_mod_ids"],
            "pick_rate": p.get("pick_rate"),
            "win_rate": round(p.get("win", 0) / max(p.get("play", 1), 1), 4),
        }

    spells = _top(data.get("summoner_spells"))
    if spells:
        result["spells"] = [names["spells"].get(str(i), str(i)) for i in spells[0]["ids"]]

    skills = _top([dict(s, ids=s.get("order")) for s in data.get("skills") or []])
    if skills:
        result["skill_order"] = skills[0]["order"]
        result["skill_priority"] = skill_priority(skills[0]["order"])

    summary = (data.get("summary") or {}).get("average_stats") or {}
    if summary.get("play"):
        result["sample_size"] = summary["play"]
    return result


# ---------------------------------------------------------------- 读写缓存

def meta_path(data_dir: Path) -> Path:
    return Path(data_dir) / META_FILENAME


def load_meta(data_dir: Path) -> Dict[str, Any]:
    path = meta_path(data_dir)
    if not path.exists():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # 缓存坏了就当没有
        log.warning("读取 %s 失败，忽略: %s", path.name, e)
        return {}
    if meta.get("schema_version") != SCHEMA_VERSION:
        log.warning("%s 版本不匹配，忽略", path.name)
        return {}
    return meta


def is_stale(meta: Dict[str, Any], max_age_hours: float) -> bool:
    """按上次同步时间判断（不论成败）——数据源长期不可达时，不至于每次启动都重下 13MB 的 Meraki 数据。"""
    synced = meta.get("generated_at")
    return not synced or time.time() - synced > max_age_hours * 3600


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 拉取

async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def _fetch_names(client: httpx.AsyncClient) -> Dict[str, Dict[str, str]]:
    perks, styles, spells, items = await asyncio.gather(
        _get_json(client, f"{CDRAGON_BASE}/perks.json"),
        _get_json(client, f"{CDRAGON_BASE}/perkstyles.json"),
        _get_json(client, f"{CDRAGON_BASE}/summoner-spells.json"),
        _get_json(client, f"{CDRAGON_BASE}/items.json"),
    )
    return parse_cdragon_names(perks, styles, spells, items)


def _err(e: Exception) -> str:
    # httpx 的连接错误 str() 常为空，退到底层原因
    detail = str(e) or repr(e.__cause__ or e.__context__ or "")
    return f"{type(e).__name__}: {detail}"[:200]


async def sync(data_dir: Path, item_map: Dict[str, Dict[str, Any]],
               transport: Optional[httpx.AsyncBaseTransport] = None) -> Dict[str, Any]:
    """拉取全部数据源并写入 aram_meta.json，返回各数据源的结果摘要。

    transport 仅供测试注入假响应。
    """
    data_dir = Path(data_dir)
    old = load_meta(data_dir)
    old_champs: Dict[str, Dict[str, Any]] = old.get("champions") or {}
    sources: Dict[str, Dict[str, Any]] = dict(old.get("sources") or {})
    champions: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in old_champs.items()}
    item_names = {k: v.get("name", "") for k, v in item_map.items()}
    report: Dict[str, Any] = {}
    now = int(time.time())

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), transport=transport,
                                 headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        # 1) 中文名：OP.GG 出装/符文需要它来转名字
        names = None
        try:
            names = await _fetch_names(client)
            report["names"] = "ok"
        except Exception as e:
            report["names"] = _err(e)
            log.warning("CommunityDragon 名称表拉取失败: %s", report["names"])

        # 2) 大乱斗平衡修正
        try:
            balance = parse_meraki_balance(await _get_json(client, MERAKI_CHAMPIONS_URL))
            for cid, mods in balance.items():
                champions.setdefault(cid, {})["aram_balance"] = mods
            sources["balance"] = {"synced_at": now, "count": len(balance)}
            report["balance"] = f"ok ({len(balance)} 个英雄)"
        except Exception as e:
            report["balance"] = _err(e)
            log.warning("Meraki 平衡数据拉取失败: %s", report["balance"])

        # 3) OP.GG 梯度 + 每个英雄的出装（列表拉不到就整段跳过，避免 170 次超时）
        try:
            listing = parse_opgg_list(await _get_json(client, OPGG_LIST_URL))
        except Exception as e:
            listing = None
            report["opgg"] = _err(e)
            log.warning("OP.GG 大乱斗列表拉取失败: %s", report["opgg"])

        if listing:
            for cid, stats in listing["champions"].items():
                stats["strength"] = tier_to_strength(stats.get("tier"), stats.get("win_rate"))
                champions.setdefault(cid, {})["opgg"] = stats

            ok = failed = 0
            if names is None:
                report["opgg_builds"] = "跳过：没有中文名称表"
            else:
                sem = asyncio.Semaphore(OPGG_CONCURRENCY)

                async def one(cid: str):
                    nonlocal ok, failed
                    async with sem:
                        try:
                            body = await _get_json(client, OPGG_BUILD_URL.format(champion_id=cid))
                            build = parse_opgg_build(body, names, item_names)
                            if build:
                                champions[cid]["opgg_build"] = build
                            ok += 1
                        except Exception as e:  # 单个英雄失败：沿用旧数据
                            failed += 1
                            log.debug("OP.GG 英雄 %s 出装拉取失败: %s", cid, _err(e))

                await asyncio.gather(*(one(cid) for cid in listing["champions"]))
                report["opgg_builds"] = f"ok {ok} / 失败 {failed}"
            sources["opgg"] = {"synced_at": now, "version": listing["version"],
                               "count": len(listing["champions"]), "builds_ok": ok, "builds_failed": failed}
            report["opgg"] = f"ok (版本 {listing['version']}, {len(listing['champions'])} 个英雄)"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "sources": sources,
        "last_report": report,
        "champions": champions,
    }
    if champions:
        _write_atomic(meta_path(data_dir), payload)
    return report
