import os
import json
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.lcu_client import LCUClient
from app import advice, ai, augments, data_sync, items, lcd_client, runes
from app.lcd_client import resolve_my_enemy_teams
from app.llm_client import LLMClient, LLMError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aram_coach")

BASE_DIR = Path(__file__).resolve().parent.parent
GAME_STATE_FILE = BASE_DIR / "game_state.md"

LLM_KEY_ENV = "ARAM_COACH_LLM_API_KEY"


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(base_dir: Path) -> tuple:
    """config.json 之上叠加 config.local.json（gitignore，放 Key 等私人配置），
    最后环境变量 ARAM_COACH_LLM_API_KEY 覆盖 Key。返回 (配置, Key 来源)。"""
    with open(base_dir / "config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    key_from = "config.json" if (cfg.get("llm") or {}).get("api_key") else ""
    local = base_dir / "config.local.json"
    if local.exists():
        with open(local, "r", encoding="utf-8") as f:
            override = json.load(f)
        cfg = _merge(cfg, override)
        if (override.get("llm") or {}).get("api_key"):
            key_from = "config.local.json"
    if os.environ.get(LLM_KEY_ENV):
        cfg.setdefault("llm", {})["api_key"] = os.environ[LLM_KEY_ENV]
        key_from = f"环境变量 {LLM_KEY_ENV}"
    return cfg, key_from


CONFIG, LLM_KEY_FROM = load_config(BASE_DIR)

with open(BASE_DIR / "data" / "champions.json", "r", encoding="utf-8") as f:
    CHAMPIONS_DB = json.load(f)

with open(BASE_DIR / "data" / "champion_map.json", "r", encoding="utf-8") as f:
    CHAMPION_MAP = json.load(f)

# championId → 英雄本名（亚索 / 金克丝…）：CHAMPION_MAP 存的是称号（疾风剑豪），知识库里常写本名
with open(BASE_DIR / "data" / "champion_alias.json", "r", encoding="utf-8") as f:
    CHAMPION_ALIAS: Dict[str, str] = json.load(f)

KNOWLEDGE_BASE: Dict[str, Any] = {}
_kb_path = BASE_DIR / "data" / "knowledge_base.json"
if _kb_path.exists():
    with open(_kb_path, "r", encoding="utf-8") as f:
        KNOWLEDGE_BASE = json.load(f)
    log.info(f"  知识库已加载: version={KNOWLEDGE_BASE.get('version','?')}")
else:
    log.warning("  knowledge_base.json 不存在，卡片将不包含版本专属数据")

ITEM_MAP: Dict[str, Dict[str, Any]] = {}
_item_map_path = BASE_DIR / "data" / "item_map.json"
if _item_map_path.exists():
    with open(_item_map_path, "r", encoding="utf-8") as f:
        ITEM_MAP = json.load(f)

# OP.GG / Meraki 同步来的大乱斗数据（运行时缓存，可能不存在）
DATA_DIR = BASE_DIR / "data"
ARAM_META: Dict[str, Any] = data_sync.load_meta(DATA_DIR)
DATA_SYNC_CFG: Dict[str, Any] = {"auto": True, "max_age_hours": 24, **CONFIG.get("data_sync", {})}
if ARAM_META:
    _opgg_src = (ARAM_META.get("sources") or {}).get("opgg") or {}
    log.info(f"  大乱斗数据已加载: {len(ARAM_META.get('champions', {}))} 个英雄, "
             f"OP.GG 版本={_opgg_src.get('version', '无')}")
else:
    log.info("  尚无大乱斗同步数据（data/aram_meta.json），暂用 champions.json")

# 装备：名称索引（跳过斗魂竞技场等变体）、合成树与当前版本价格（同步数据优先）
ITEM_DB = items.ItemDB(ITEM_MAP)
ITEM_DB.apply_economics((ARAM_META.get("cdragon") or {}).get("items"))
# 符文树：把手写数据里的符文名还原成客户端符文页 ID
RUNE_TREE: Dict[str, Any] = json.loads((DATA_DIR / "runes.json").read_text(encoding="utf-8"))
# 海克斯大乱斗强化符文池
AUGMENTS_STATIC = DATA_DIR / "augments.json"
AUGMENT_POOL: list = augments.load_pool((ARAM_META.get("cdragon") or {}).get("augments"), AUGMENTS_STATIC)
RUNES_CFG: Dict[str, Any] = {"auto_apply": False, **CONFIG.get("runes", {})}
# 哪些等级会弹强化符文三选一（未在实机确认，可在 config.json 的 augments.levels 改）
AUGMENT_CFG: Dict[str, Any] = {"levels": [3, 7, 11, 15], **CONFIG.get("augments", {})}

ARAM_BUILD_REPLACEMENTS: Dict[str, str] = {
    "守护天使": "死亡之舞",
    "石像鬼石板甲": "日炎圣盾",
    "岚切": "星蚀",
    "狂风之力": "不朽盾弓",
    "德拉克萨的暮刃": "幽梦之灵",
    "渴血战斧": "死亡之舞",
    "神圣分离者": "三相之力",
    "破碎王后之冕": "女妖面纱",
    "海克斯科技枪刃": "暗夜收割者",
    "梅贾的窃魂卷": "莫雷洛秘典",
    "凛冬之临": "末日寒冬",
    "钢铁烈阳之匣": "救赎",
}

ROLE_MAP = {
    "坦克": "tank", "战士": "fighter", "刺客": "assassin",
    "法师": "mage", "射手": "marksman", "辅助": "support",
}
ROLE_ALIASES = {"ADC": "射手"}


def normalize_role(role: str) -> str:
    """把数据里的定位归一成 ROLE_MAP 中的六种之一：ADC→射手，复合定位（刺客/辅助）取第一个。"""
    role = (role or "").split("/")[0].strip()
    return ROLE_ALIASES.get(role, role)


def replace_banned_items(build: list) -> tuple:
    """大乱斗禁用装备换成替代品；替换后与已有装备重复（如 守护天使→死亡之舞）的直接去掉。

    返回 (新出装, 替换数, 去重数)。
    """
    out, replaced, deduped = [], 0, 0
    for item in build:
        repl = ARAM_BUILD_REPLACEMENTS.get(item, item)
        replaced += repl != item
        if repl in out:
            deduped += 1
            continue
        out.append(repl)
    return out, replaced, deduped


_replaced = _deduped = 0
for _cid, _cdata in CHAMPIONS_DB.items():
    _cdata["role"] = normalize_role(_cdata.get("role", ""))
    _cdata["build"], _r, _d = replace_banned_items(_cdata.get("build", []))
    _replaced += _r
    _deduped += _d
log.info(f"  出装中禁用装备替换 {_replaced} 处，替换后重复去掉 {_deduped} 件")

from contextlib import asynccontextmanager

_data_sync_lock = asyncio.Lock()


async def refresh_aram_meta(force: bool = False) -> Dict[str, Any]:
    """数据过期（或 force）时同步 OP.GG / Meraki / CommunityDragon，成功后原地替换 ARAM_META。

    非 force 时只拉过期的数据源（失败过的下次启动会重试，成功的 24 小时内不重复下载）。
    """
    if _data_sync_lock.locked():
        return {"status": "running"}
    async with _data_sync_lock:
        only = None if force else data_sync.stale_sources(ARAM_META, DATA_SYNC_CFG["max_age_hours"])
        if only == []:
            return {"status": "fresh"}
        log.info(f"同步大乱斗数据: {only or '全部'}")
        try:
            report = await data_sync.sync(DATA_DIR, ITEM_MAP, only=only)
        except Exception as e:
            log.exception("大乱斗数据同步失败")
            return {"status": "error", "error": str(e)[:200]}
        fresh = await asyncio.to_thread(data_sync.load_meta, DATA_DIR)
        if fresh:
            ARAM_META.clear()
            ARAM_META.update(fresh)
            cd = ARAM_META.get("cdragon") or {}
            ITEM_DB.apply_economics(cd.get("items"))
            AUGMENT_POOL[:] = augments.load_pool(cd.get("augments"), AUGMENTS_STATIC)
            _mean_strength_cache.clear()
        log.info(f"大乱斗数据同步结束: {report}")
        return {"status": "done", "report": report}


_background_tasks: set = set()


def spawn(coro) -> asyncio.Task:
    """后台任务要留引用，否则可能被 GC 回收；退出时统一取消。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Starting ARAM Coach...")
    spawn(poll_lcu())
    if DATA_SYNC_CFG.get("auto"):
        spawn(refresh_aram_meta())
    yield
    for task in list(_background_tasks):
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    await lcu.close()
    await llm_client.close()

app = FastAPI(title="ARAM Coach", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend")), name="static")

lcu = LCUClient(search_paths=CONFIG["lol_install_paths"])
llm_client = LLMClient(CONFIG.get("llm", {}))
log.info(f"  LLM: {llm_client.model if llm_client.enabled else '未配置 Key（深度分析走本地规则）'}"
         + (f"，Key 来自 {LLM_KEY_FROM}" if llm_client.enabled else ""))
AI_DEPS = ai.Deps(items=ITEM_DB, kb=KNOWLEDGE_BASE, kb_specific_for=lambda name: kb_specific_for(name),
                  banned=ARAM_BUILD_REPLACEMENTS, alias=CHAMPION_ALIAS)

ws_clients: list[WebSocket] = []
LAST_STATE: Dict[str, Any] = {}

GAMEFLOW_PHASES = [
    "None",
    "Lobby",
    "Matchmaking",
    "ReadyCheck",
    "ChampSelect",
    "GameStart",
    "FailedToLaunch",
    "InProgress",
    "Reconnect",
    "WaitingForStats",
    "PreEndOfGame",
    "EndOfGame",
]


def resolve_champion_name(character_id: Any) -> Optional[str]:
    if character_id is None:
        return None
    key = str(int(character_id)) if isinstance(character_id, (int, float)) else str(character_id)
    return CHAMPION_MAP.get(key) or CHAMPIONS_DB.get(key, {}).get("name")


def kb_specific_for(name: str) -> Optional[Dict[str, Any]]:
    """知识库里这个英雄的专属数据（版本强度、核心出装、专属强化符文……），没有返回 None。"""
    if not KNOWLEDGE_BASE or not name:
        return None
    aliases = {
        "chogath_specific": ["虚空恐惧", "科加斯", "大虫子"],
        "darius_specific": ["诺克萨斯之手", "诺手", "德莱厄斯"],
    }
    for kb_key, names in aliases.items():
        if name in names:
            return KNOWLEDGE_BASE.get(kb_key)
    for v in KNOWLEDGE_BASE.values():
        if isinstance(v, dict) and v.get("chinese") == name:
            return v
    return None


def opgg_runes_for(character_id: Any) -> Optional[Dict[str, Any]]:
    meta = (ARAM_META.get("champions") or {}).get(str(character_id)) or {}
    return (meta.get("opgg_build") or {}).get("runes")


def get_champion_coach(character_id: Any) -> Optional[Dict[str, Any]]:
    if character_id is None:
        return None
    key = str(int(character_id)) if isinstance(character_id, (int, float)) else str(character_id)
    data = CHAMPIONS_DB.get(key)
    if not data:
        return None
    name = data.get("name") or resolve_champion_name(character_id) or "未知英雄"

    tips = list(data.get("tips", []))
    build = list(data.get("build", []))
    snowball_tip = data.get("snowball_tip", "")
    aram_strength = data.get("aram_strength", 65)
    core_cost = data.get("core_cost", 0)
    rune_fields = ("primary_rune", "primary_branch", "primary_details", "secondary_rune", "secondary_branch")
    runes = {k: data.get(k, [] if k == "primary_details" else "") for k in rune_fields}

    # 优先级：知识库（手写的版本专属数据）> OP.GG 统计（aram_meta.json）> champions.json 模板
    meta = (ARAM_META.get("champions") or {}).get(key) or {}
    opgg = meta.get("opgg") or {}
    opgg_build = meta.get("opgg_build") or {}
    balance = meta.get("aram_balance") or {}
    if opgg.get("strength") is not None:
        aram_strength = opgg["strength"]
    starter_items = list(opgg_build.get("starter_items", []))
    if opgg_build.get("build"):
        # OP.GG 是全体大乱斗的统计，里面可能有海克斯大乱斗禁用的装备
        build, _, _ = replace_banned_items(opgg_build["build"])
        starter_items, _, _ = replace_banned_items(starter_items)
    if opgg_build.get("runes"):
        runes = {k: opgg_build["runes"][k] for k in rune_fields}

    kb_specific = kb_specific_for(name)

    if kb_specific:
        if kb_specific.get("tips"):
            for t in kb_specific["tips"]:
                if t not in tips:
                    tips.insert(0, t)
        if kb_specific.get("best_core_build"):
            build = list(kb_specific["best_core_build"]) + [b for b in build if b not in kb_specific["best_core_build"]]
        if kb_specific.get("skill_order"):
            tips.insert(0, f"加点: {kb_specific['skill_order']}")
        if kb_specific.get("best_runes"):
            tips.insert(0, f"强化符文: {' · '.join(kb_specific['best_runes'][:3])}")
    if opgg_build.get("skill_priority") and not (kb_specific or {}).get("skill_order"):
        tips.insert(0, f"加点: {opgg_build['skill_priority']}")

    build_ids = [ITEM_DB.id_of(n) for n in build]
    # 前端拿它和当前金币比"下一件大件攒了多少"，所以取第一件核心装的价格，不是几件之和
    if build_ids and build_ids[0]:
        core_cost = ITEM_DB.total(build_ids[0]) or core_cost

    return {
        "character_id": character_id,
        "name": name,
        "alias": CHAMPION_ALIAS.get(key, ""),
        "role": data.get("role", "未知"),
        "aram_strength": aram_strength,
        **runes,
        "rune_shards": (opgg_build.get("runes") or {}).get("shards", []),
        "build": build,
        "build_ids": build_ids,
        "starter_items": starter_items,
        "spells": opgg_build.get("spells", []),
        "skill_priority": opgg_build.get("skill_priority", ""),
        "core_cost": core_cost,
        "situational": data.get("situational", {}),
        "tips": tips,
        "snowball_tip": snowball_tip,
        "flash_tip": data.get("flash_tip", ""),
        "aram_balance": balance,
        "aram_balance_text": data_sync.balance_text(balance),
        "opgg": {
            "tier": opgg.get("tier"),
            "rank": opgg.get("rank"),
            "win_rate": opgg.get("win_rate"),
            "pick_rate": opgg.get("pick_rate"),
            "version": ((ARAM_META.get("sources") or {}).get("opgg") or {}).get("version", ""),
        } if opgg else None,
        "data_source": "opgg" if (opgg_build.get("build") or opgg_build.get("runes")) else "local",
        "kb_enhanced": bool(kb_specific),
    }


_TEAM_GAPS = [
    # (缺口, 判断“已有”的条件, 缺口扣分, 缺口提示, 补缺提示)
    ("tank", lambda c: c["tank"] > 0 or c["fighter"] >= 2, 15,
     "阵容缺前排（坦克/重装战士），注意抱团别分散", "你补上了前排缺口，完美契合"),
    ("mage", lambda c: c["mage"] > 0 or c["assassin"] > 0, 10,
     "阵容缺AP输出，后期可能打不动肉", "你补上了AP缺口，完美契合"),
    ("adc", lambda c: c["marksman"] > 0 or c["fighter"] > 0, 5,
     "阵容缺持续输出射手", "你补上了持续输出缺口"),
]
_FILL_BONUS = 10


def evaluate_team_fit(my_team: list, my_coach: Optional[Dict[str, Any]],
                      my_champion_id: Any) -> tuple:
    """阵容契合度（0-100）和说明。

    my_team 里通常包含玩家自己：先按 champion_id 排除自己统计队友，
    再把自己的定位加回来，这样才能分辨"缺口是你补上的"还是"队友本来就有"。
    返回 (fitness, notes)，notes 为 [(kind, text)]，kind ∈ gap / fill / dup。
    """
    mates = {k: 0 for k in ROLE_MAP.values()}
    for m in my_team:
        if my_champion_id and m.get("champion_id") == my_champion_id:
            continue
        key = ROLE_MAP.get((m.get("coach") or {}).get("role", ""))
        if key:
            mates[key] += 1

    my_key = ROLE_MAP.get(my_coach.get("role", "")) if my_coach else None
    full = dict(mates)
    if my_key:
        full[my_key] += 1

    fitness = 50
    notes = []
    filled = False
    for _gap, has, penalty, gap_text, fill_text in _TEAM_GAPS:
        if not has(full):
            fitness -= penalty
            notes.append(("gap", gap_text))
        elif not has(mates):
            fitness += _FILL_BONUS
            notes.append(("fill", fill_text))
            filled = True

    if my_key and not filled and mates[my_key] >= 2:
        fitness -= 15
        notes.append(("dup", f"队友里已有{mates[my_key]}个{my_coach.get('role')}，再加你略显重复"))

    return max(0, min(100, fitness)), notes


_mean_strength_cache: Dict[str, float] = {}


def mean_strength() -> float:
    """全英雄平均强度（同步数据更新后重算），用来估计"用骰子随一个"的期望。"""
    if "v" not in _mean_strength_cache:
        vals = [c["aram_strength"] for cid in CHAMPIONS_DB if (c := get_champion_coach(cid))]
        _mean_strength_cache["v"] = sum(vals) / len(vals) if vals else 70.0
    return _mean_strength_cache["v"]


def rune_page_for(character_id: Any, coach: Optional[Dict[str, Any]]) -> tuple:
    """(符文页, 来源 opgg/local 或失败原因)"""
    if not coach:
        return None, "没有英雄数据"
    return runes.build_page(coach, opgg_runes_for(character_id), RUNE_TREE)


async def broadcast(message: Dict[str, Any]):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


async def collect_game_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {"lcu_connected": lcu.connected, "phase": "None"}
    if not lcu.connected:
        return state

    phase_data = await lcu.get("/lol-gameflow/v1/gameflow-phase")
    state["phase"] = phase_data or "None"

    if phase_data == "ChampSelect":
        session = await lcu.get("/lol-champ-select/v1/session")
        summoner = await lcu.get("/lol-summoner/v1/current-summoner")
        if summoner:
            state["summoner_name"] = summoner.get("gameName") or summoner.get("displayName")
            reroll = summoner.get("rerollPoints", {})
            max_rolls = reroll.get("maxRolls", 2)
            used_rolls = reroll.get("numberOfRolls", 0)
            state["reroll_remaining"] = max(0, max_rolls - used_rolls)

        if session:
            my_id = session.get("localPlayerCellId")
            state["my_cell_id"] = my_id
            state["my_team"] = []
            state["enemy_team"] = []

            pick_map: Dict[int, Any] = {}
            for action in session.get("actions", []):
                for act in action:
                    if act.get("type") == "pick" and act.get("completed"):
                        cid = act.get("championId")
                        cell_id = act.get("cellId")
                        if cid and cell_id is not None:
                            pick_map[cell_id] = cid

            def _build_team(team_list, target):
                for member in team_list:
                    cell_id = member.get("cellId")
                    cid = member.get("championId") or pick_map.get(cell_id)
                    if cid:
                        target.append({
                            "cell_id": cell_id,
                            "champion_id": cid,
                            "champion_name": resolve_champion_name(cid),
                            "coach": get_champion_coach(cid),
                        })

            _build_team(session.get("myTeam", []), state["my_team"])
            _build_team(session.get("theirTeam", []), state["enemy_team"])

            if my_id is not None:
                for member in session.get("myTeam", []):
                    if member.get("cellId") == my_id:
                        cid = member.get("championId") or pick_map.get(my_id)
                        state["my_champion_id"] = cid
                        state["my_champion_name"] = resolve_champion_name(cid)
                        state["my_coach"] = get_champion_coach(cid)
                        break

            # 大乱斗备选席：新版字段 benchChampions=[{championId}]，旧版 benchChampionIds=[id]
            bench_ids = [b.get("championId") for b in session.get("benchChampions") or [] if isinstance(b, dict)]
            bench_ids += [c for c in session.get("benchChampionIds") or [] if isinstance(c, int)]
            bench = [{"champion_id": cid, "champion_name": resolve_champion_name(cid), "coach": get_champion_coach(cid)}
                     for cid in dict.fromkeys(bench_ids) if cid]
            state["bench"] = [{k: b[k] for k in ("champion_id", "champion_name")} for b in bench]
            state["verdict"] = advice.champ_select_verdict(
                state["my_team"], state.get("my_champion_id"), state.get("my_coach"), bench,
                state.get("reroll_remaining", 0), evaluate_team_fit, mean_strength(),
            )
            page, source = rune_page_for(state.get("my_champion_id"), state.get("my_coach"))
            state["rune_page"] = {"available": bool(page), "source": source if page else "",
                                  "reason": "" if page else source,
                                  "auto_apply": bool(RUNES_CFG.get("auto_apply"))}

    elif phase_data in ("GameStart", "InProgress", "Reconnect"):
        summoner = await lcu.get("/lol-summoner/v1/current-summoner")
        if summoner:
            state["summoner_name"] = summoner.get("gameName") or summoner.get("displayName")
            state["summoner_level"] = summoner.get("summonerLevel")
            my_puuid = summoner.get("puuid")
            state["my_champion_id"] = summoner.get("selectedChampionId")
            state["my_champion_name"] = resolve_champion_name(summoner.get("selectedChampionId"))
            state["my_coach"] = get_champion_coach(summoner.get("selectedChampionId"))

            gf_session = await lcu.get("/lol-gameflow/v1/session")
            if gf_session:
                if my_puuid:
                    selections = gf_session.get("gameData", {}).get("playerChampionSelections", [])
                    for sel in selections:
                        if sel.get("puuid") == my_puuid:
                            cid = sel.get("championId")
                            if cid and not state["my_champion_id"]:
                                state["my_champion_id"] = cid
                                state["my_champion_name"] = resolve_champion_name(cid)
                                state["my_coach"] = get_champion_coach(cid)
                            break

                game_data = gf_session.get("gameData", {})
                team_one = game_data.get("teamOne", [])
                team_two = game_data.get("teamTwo", [])

                # playerChampionSelections 只有 championId/puuid，没有队伍信息；
                # 看自己的 puuid 在哪一队
                my_team_idx = 0
                if my_puuid and any(p.get("puuid") == my_puuid for p in team_two):
                    my_team_idx = 1

                my_team_raw = team_one if my_team_idx == 0 else team_two
                enemy_team_raw = team_two if my_team_idx == 0 else team_one

                def _build_game_team(raw_list):
                    result = []
                    for p in raw_list:
                        cid = p.get("championId")
                        if cid:
                            result.append({
                                "champion_id": cid,
                                "champion_name": resolve_champion_name(cid),
                                "coach": get_champion_coach(cid),
                            })
                    return result

                state["my_team"] = _build_game_team(my_team_raw)
                state["enemy_team"] = _build_game_team(enemy_team_raw)

                state["matchup_analysis"] = compute_matchup_analysis(
                    state["my_team"], state["enemy_team"], state.get("my_coach")
                )

        # urllib 是同步阻塞的（超时 5 秒），放到线程里跑，避免卡住事件循环
        lcd_raw = await asyncio.to_thread(lcd_client.fetch_allgamedata)
        if lcd_raw:
            state["lcd"] = extract_lcd_data(lcd_raw, state)
            lcd = state["lcd"]
            me = next((p for team in (lcd.get("team_map") or {}).values() for p in team if p.get("is_me")), None)
            state["moment"] = advice.in_game_moment(
                state.get("my_coach"), me, lcd.get("active") or {}, lcd.get("game_time", 0),
                state.get("enemy_team") or [], ITEM_DB,
            )

    if phase_data == "ChampSelect" and (state.get("my_team") or state.get("enemy_team")):
        state["composite_advice"] = compute_composite_advice(state)

    return state


def extract_lcd_data(lcd_raw: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"available": True}

    gd = lcd_raw.get("gameData", {})
    result["game_time"] = round(gd.get("gameTime", 0), 1)

    ap = lcd_raw.get("activePlayer", {})
    if ap:
        s = ap.get("championStats", {})
        result["active"] = {
            "gold": round(ap.get("currentGold", 0), 0),
            "level": ap.get("level"),
            "champion_stats": {
                "hp": round(s.get("currentHealth", 0)),
                "max_hp": round(s.get("maxHealth", 0)),
                "hp_pct": round(s.get("currentHealth", 0) / max(s.get("maxHealth", 1), 1) * 100, 1),
                "ad": round(s.get("attackDamage", 0)),
                "ap": round(s.get("abilityPower", 0)),
                "armor": round(s.get("armor", 0)),
                "mr": round(s.get("magicResist", 0)),
                "attack_speed": round(s.get("attackSpeed", 0), 3),
                "attack_range": round(s.get("attackRange", 0)),
                "move_speed": round(s.get("moveSpeed", 0)),
                "ability_haste": round(s.get("abilityHaste", 0)),
                "omnivamp": round(s.get("omnivamp", 0), 3),
                "crit_chance": round(s.get("critChance", 0), 3),
                "crit_damage": round(s.get("critDamage", 0), 3),
                "life_steal": round(s.get("lifeSteal", 0), 3),
                "spell_vamp": round(s.get("spellVamp", 0), 3),
                "physical_vamp": round(s.get("physicalVamp", 0), 3),
                "tenacity": round(s.get("tenacity", 0), 3),
                "heal_shield_power": round(s.get("healShieldPower", 0), 3),
                "armor_pen_flat": round(s.get("armorPenetrationFlat", 0)),
                "armor_pen_pct": round(s.get("armorPenetrationPercent", 0), 3),
                "bonus_armor_pen_pct": round(s.get("bonusArmorPenetrationPercent", 0), 3),
                "magic_pen_flat": round(s.get("magicPenetrationFlat", 0)),
                "magic_pen_pct": round(s.get("magicPenetrationPercent", 0), 3),
                "bonus_magic_pen_pct": round(s.get("bonusMagicPenetrationPercent", 0), 3),
                "physical_lethality": round(s.get("physicalLethality", 0)),
                "magic_lethality": round(s.get("magicLethality", 0)),
                "health_regen": round(s.get("healthRegenRate", 0), 1),
                "resource_type": s.get("resourceType", ""),
                "resource_value": round(s.get("resourceValue", 0)),
                "resource_max": round(s.get("resourceMax", 0)),
                "resource_regen": round(s.get("resourceRegenRate", 0), 1),
            },
            "abilities": {},
        }
        for key, ab in ap.get("abilities", {}).items():
            result["active"]["abilities"][key] = {
                "level": ab.get("abilityLevel"),
                "displayName": ab.get("displayName", ""),
                "rawDescription": ab.get("rawDescription", ""),
            }

    # LCD 的 allPlayers 只给 KDA / 补刀 / 视野分和装备，拿不到别人的金币和伤害，
    # 所以团队经济用"装备总价"衡量；当前金币只有自己的（activePlayer）。
    all_p = lcd_raw.get("allPlayers", [])
    team_map: Dict[str, Dict[str, Any]] = {}
    me_idx = lcd_client.find_me_index(all_p, ap, state.get("summoner_name") or "")
    for idx, p in enumerate(all_p):
        team = p.get("team", "")
        scores = p.get("scores", {})
        items = []
        item_gold = 0
        for it in p.get("items", []):
            count = it.get("count") or 1
            unit = (ITEM_MAP.get(str(it.get("itemID"))) or {}).get("gold_total") or it.get("price", 0)
            item_gold += unit * count
            items.append({
                "name": it.get("displayName", ""),
                "item_id": it.get("itemID"),
                "price": it.get("price", 0),
                "count": count,
                "slot": it.get("slot"),
            })
        entry = {
            "name": p.get("riotIdGameName") or p.get("summonerName", ""),
            "team": team,
            "champion": p.get("championName", ""),
            "is_dead": p.get("isDead", False),
            "respawn_timer": round(p.get("respawnTimer", 0), 1),
            "level": p.get("level"),
            "kills": scores.get("kills", 0),
            "deaths": scores.get("deaths", 0),
            "assists": scores.get("assists", 0),
            "kda": round(
                (scores.get("kills", 0) + scores.get("assists", 0)) /
                max(scores.get("deaths", 1), 1), 2
            ),
            "cs": scores.get("creepScore", 0),
            "ward_score": round(scores.get("wardScore", 0), 1),
            "items": items,
            "item_count": len([i for i in items if i.get("name")]),
            "item_gold": int(item_gold),
            "is_me": idx == me_idx,
        }
        if entry["is_me"]:
            entry["gold"] = (result.get("active") or {}).get("gold", 0)
        if team not in team_map:
            team_map[team] = []
        team_map[team].append(entry)

    result["team_map"] = team_map

    events_raw = lcd_raw.get("events", {}).get("Events", [])
    result["events"] = events_raw[-20:] if events_raw else []

    return result


def state_to_md(state: Dict[str, Any]) -> str:
    lines: list[str] = []

    phase = state.get("phase", "Unknown")
    summoner = state.get("summoner_name", "?")
    my_champ = state.get("my_champion_name") or "?"
    my_role = ""
    if state.get("my_coach"):
        my_role = state["my_coach"].get("role", "")

    lines.append(f"# 🎮 大乱斗实时对局 · {summoner}")
    lines.append("")
    lines.append(f"> 英雄: **{my_champ}** ({my_role}) · 阶段: `{phase}`")

    lcd = state.get("lcd") or {}
    game_time = lcd.get("game_time", 0)
    if game_time:
        lines.append(f"> 游戏时间: {int(game_time // 60)}分{int(game_time % 60)}秒")

    lines.append("")

    active = (lcd.get("active") or {})
    if active:
        stats = active.get("champion_stats") or {}
        gold = int(active.get("gold", 0))
        level = active.get("level", "?")
        hp = stats.get("hp", 0)
        max_hp = stats.get("max_hp", 1)
        hp_pct = stats.get("hp_pct", 0)

        lines.append("## 📊 我的属性面板")
        lines.append("")
        lines.append(f"| 项目 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 等级 | Lv.{level} |")
        lines.append(f"| 当前金币 | {gold} G |")
        lines.append(f"| 生命值 | {int(hp)} / {int(max_hp)} ({hp_pct}%) |")
        lines.append(f"| 攻击力 AD | {stats.get('ad')} |")
        lines.append(f"| 法强 AP | {stats.get('ap')} |")
        lines.append(f"| 护甲 | {stats.get('armor')} |")
        lines.append(f"| 魔抗 | {stats.get('mr')} |")
        lines.append(f"| 攻击速度 | {stats.get('attack_speed')} |")
        lines.append(f"| 移速 | {stats.get('move_speed')} |")
        lines.append(f"| 技能急速 | {stats.get('ability_haste')} |")
        lines.append(f"| 暴击率 | {_pct(stats.get('crit_chance'))} |")
        lines.append(f"| 暴击伤害 | {_pct(stats.get('crit_damage'))} |")
        lines.append(f"| 护甲穿透(固定/%) | {stats.get('armor_pen_flat')} / {_pct(stats.get('armor_pen_pct'))} |")
        lines.append(f"| 法术穿透(固定/%) | {stats.get('magic_pen_flat')} / {_pct(stats.get('magic_pen_pct'))} |")
        lines.append(f"| 物理致命/法术致命 | {stats.get('physical_lethality')} / {stats.get('magic_lethality')} |")
        lines.append(f"| 全能吸血 | {_pct(stats.get('omnivamp'))} |")
        lines.append(f"| 物理/法术吸血 | {_pct(stats.get('life_steal'))} / {_pct(stats.get('spell_vamp'))} |")
        lines.append(f"| 韧性 | {_pct(stats.get('tenacity'))} |")
        lines.append(f"| 治疗/护盾强度 | {_pct(stats.get('heal_shield_power'))} |")
        lines.append("")

        abilities = active.get("abilities") or {}
        if abilities:
            ab_parts = []
            for k, v in abilities.items():
                ab_parts.append(f"{k.upper()}=Lv.{v.get('level', '?')}")
            lines.append(f"**技能等级**: {' | '.join(ab_parts)}")
            lines.append("")

    my_team, enemy_team = resolve_my_enemy_teams(lcd.get("team_map") or {})

    my_total = sum(p.get("item_gold", 0) for p in my_team)
    enemy_total = sum(p.get("item_gold", 0) for p in enemy_team)
    diff = my_total - enemy_total
    lines.append(f"## 💰 团队装备总价")
    lines.append("")
    lines.append(f"| 我方 | 差值 | 敌方 |")
    lines.append(f"|------|------|------|")
    sign = "+" if diff >= 0 else ""
    lines.append(f"| **{my_total}G** | **{sign}{diff}G** | **{enemy_total}G** |")
    lines.append("")

    def _fmt_team(team, label, emoji):
        rows = []
        for p in team:
            name = p.get("name", "?")
            champ = p.get("champion", "?")
            lv = p.get("level", "?")
            k = p.get("kills", 0)
            d = p.get("deaths", 0)
            a = p.get("assists", 0)
            dead = p.get("is_dead", False)
            respawn = p.get("respawn_timer", 0)
            items = [it.get("name", "") for it in p.get("items", []) if it.get("name")]
            items_str = ", ".join(items) if items else "—"
            me = " 👉 **(你)**" if p.get("is_me") else ""
            dead_tag = f" 💀复活{respawn}s" if dead else ""
            rows.append(f"| {emoji} {name}{me} | {champ} | Lv.{lv} | {k}/{d}/{a} | {p.get('cs', 0)} "
                        f"| {p.get('item_gold', 0)}G | {items_str}{dead_tag} |")

        lines.append(f"## {label}")
        lines.append("")
        lines.append("| 玩家 | 英雄 | 等级 | KDA | 补刀 | 装备总价 | 装备 |")
        lines.append("|------|------|------|-----|------|----------|------|")
        lines.extend(rows)
        lines.append("")

    if my_team:
        _fmt_team(my_team, "🟢 我方阵容", "🟢")
    if enemy_team:
        _fmt_team(enemy_team, "🔴 敌方阵容", "🔴")

    if state.get("my_coach"):
        coach = state["my_coach"]
        lines.append("## 🧠 英雄教练数据")
        lines.append("")
        if coach.get("primary_branch"):
            lines.append(f"**符文**: {coach.get('primary_branch')} - {coach.get('primary_rune')} / {coach.get('secondary_branch')}")
        if coach.get("build"):
            lines.append(f"**推荐出装**: {' → '.join(coach['build'])}")
        if coach.get("spells"):
            lines.append(f"**召唤师技能**: {' + '.join(coach['spells'])}")
        if coach.get("opgg"):
            o = coach["opgg"]
            lines.append(f"**OP.GG 大乱斗**: T{o.get('tier')} · 胜率 {(o.get('win_rate') or 0) * 100:.1f}%"
                         f" · 选取率 {(o.get('pick_rate') or 0) * 100:.1f}% · 版本 {o.get('version')}")
        if coach.get("aram_balance_text"):
            lines.append(f"**大乱斗平衡**: {coach['aram_balance_text']}")
        if coach.get("tips"):
            lines.append("")
            lines.append("**大乱斗 Tips**:")
            for t in coach["tips"][:5]:
                lines.append(f"- {t}")
        lines.append("")

    verdict = state.get("verdict") or {}
    if verdict.get("headline"):
        lines.append(f"## 🎯 选人建议: **{verdict['headline']}**")
        lines.append("")
        if verdict.get("reason"):
            lines.append(f"> {verdict['reason']}")
            lines.append("")

    moment_md = advice.moment_text(state.get("moment"))
    if moment_md:
        lines.append("## 🛒 出装决策")
        lines.append("")
        for t in moment_md.split("\n"):
            lines.append(f"- {t}")
        lines.append("")

    lcd_events = (lcd.get("events") or [])
    if lcd_events:
        lines.append("## 📜 最近事件")
        lines.append("")
        for ev in lcd_events[-10:]:
            name = ev.get("EventName", "")
            t = ev.get("EventTime", 0)
            mm = int(t // 60)
            ss = int(t % 60)
            if name == "ChampionKill":
                lines.append(f"- [{mm}:{ss}] 💀 {ev.get('KillerName')} 击杀 {ev.get('VictimName')}")
            elif name == "Multikill":
                lines.append(f"- [{mm}:{ss}] 🔥 {ev.get('KillerName')} 连杀 {ev.get('KillStreak')}")
            elif name == "TurretKilled":
                lines.append(f"- [{mm}:{ss}] 🏰 防御塔被摧毁")
            elif name == "InhibKilled":
                lines.append(f"- [{mm}:{ss}] 💎 水晶被摧毁")
            elif name == "DragonKill":
                lines.append(f"- [{mm}:{ss}] 🐉 龙被击杀")
            else:
                lines.append(f"- [{mm}:{ss}] {name}")
        lines.append("")

    from datetime import datetime
    lines.append(f"---")
    lines.append(f"*最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(lines)


def _pct(val) -> str:
    if val is None or val == 0:
        return "0%"
    if isinstance(val, float) and val < 1:
        return f"{val * 100:.1f}%"
    return f"{val:.0f}%"


def compute_matchup_analysis(my_team: list, enemy_team: list,
                              my_coach: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """敌方阵容构成（只给事实：AP/AD 人数、回复型英雄、坦克）。怎么出装应对见 advice.counter_adjustments。"""
    th = advice.enemy_threats(enemy_team)
    return {"enemy_ap_count": th["ap"], "enemy_ad_count": th["ad"],
            "healers": th["healers"], "tanks": th["tanks"]}


ROLE_COUNTERS = {
    "坦克": ["射手", "ADC", "法师"],
    "战士": ["射手", "ADC", "法师"],
    "法师": ["刺客", "战士"],
    "射手": ["刺客", "法师"],
    "刺客": ["射手", "辅助"],
    "辅助": ["刺客", "法师"],
}

STRONG_VS = {
    "VN": ["坦克"], "薇恩": ["坦克"],
    "卡莎": ["坦克"],
    "克烈": ["坦克"],
    "蛇女": ["战士", "坦克"],
    "蚂蚱": ["刺客"], "玛尔扎哈": ["刺客"],
    "铁男": ["射手", "ADC"],
    "剑姬": ["坦克"],
    "青钢影": ["射手", "ADC"],
    "小鱼人": ["射手", "ADC", "法师"],
    "卡萨丁": ["法师"],
    "莫甘娜": ["刺客"],
    "兰博": ["坦克", "战士"],
    "EZ": ["坦克"],
}


def compute_counter_score(my_role: str, my_name: str,
                          enemy_team: list) -> float:
    score = 50.0
    my_counters = ROLE_COUNTERS.get(my_role, [])
    for enemy in enemy_team:
        ecoach = enemy.get("coach")
        if not ecoach:
            continue
        erole = ecoach.get("role", "")
        ename = ecoach.get("name", "") or enemy.get("champion_name", "")
        if erole in my_counters:
            score += 6
        if my_name in STRONG_VS and erole in STRONG_VS.get(my_name, []):
            score += 10
        if ename in STRONG_VS.get(my_name, []):
            score += 15
    return max(0, min(100, score))


def compute_composite_advice(state: Dict[str, Any]) -> Dict[str, Any]:
    """选人阶段的综合分（强度 / 阵容适配 / 克制），给骰子弹窗显示；要怎么做看 state["verdict"]。"""
    my_coach = state.get("my_coach")
    my_team = state.get("my_team") or []
    enemy_team = state.get("enemy_team") or []

    base_strength = my_coach.get("aram_strength", 65) if my_coach else 50
    team_fit, _notes = evaluate_team_fit(my_team, my_coach, state.get("my_champion_id"))
    counter_score = 50
    if my_coach and enemy_team:
        counter_score = compute_counter_score(my_coach.get("role", ""), my_coach.get("name", ""), enemy_team)
    breakdown = {"英雄强度": base_strength, "阵容适配": team_fit, "敌方克制": counter_score}
    return {
        "phase": state.get("phase", "None"),
        "score": int(base_strength * 0.4 + team_fit * 0.3 + counter_score * 0.3),
        "score_breakdown": breakdown,
        "immediate_action": (state.get("verdict") or {}).get("headline", ""),
    }


async def apply_rune_page(character_id: Any) -> Dict[str, Any]:
    coach = get_champion_coach(character_id)
    page, source = rune_page_for(character_id, coach)
    if not page:
        return {"ok": False, "reason": source}
    result = await runes.apply_page(lcu, coach["name"], page)
    result.update(source=source, champion=coach["name"], perks=runes.describe(page, RUNE_TREE))
    log.info(f"写入符文页 {coach['name']}（{source}）: {result.get('action') or result.get('reason')}")
    return result


async def poll_lcu():
    last_phase = None
    last_champ_id = None
    last_rolls = None
    last_level = None
    runes_applied_for = None
    _debug_count = 0
    while True:
        try:
            await lcu.wait_for_connection(poll_interval=3.0)
            state = await collect_game_state()

            if state["phase"] != last_phase:
                log.info(f"Phase changed: {last_phase} -> {state['phase']}")
                last_phase = state["phase"]
                last_champ_id = None
                last_rolls = None
                last_level = None
                runes_applied_for = None
                _debug_count = 0
                await broadcast({"type": "phase_change", "data": state})

            if state["phase"] == "ChampSelect":
                cur_champ = state.get("my_champion_id")
                cur_rolls = state.get("reroll_remaining")

                _debug_count += 1
                if _debug_count <= 5 or cur_champ != last_champ_id:
                    log.info(f"[DEBUG ChampSelect #{_debug_count}] my_champion_id={cur_champ}, reroll_remaining={cur_rolls}, last_champ={last_champ_id}, my_team_count={len(state.get('my_team') or [])}")

                if last_champ_id is not None and cur_champ is not None and cur_champ != last_champ_id:
                    # 骰子数变少才是重随，否则是和备选席/队友交换
                    via = "reroll" if last_rolls is not None and cur_rolls is not None and cur_rolls < last_rolls else "swap"
                    log.info(f"🎲 英雄变更({via}) {last_champ_id} -> {cur_champ} (剩余骰子={cur_rolls})")
                    await broadcast({
                        "type": "dice_rolled",
                        "data": {
                            "old_champion_id": last_champ_id,
                            "new_champion_id": cur_champ,
                            "new_champion_name": state.get("my_champion_name"),
                            "remaining_dice": cur_rolls,
                            "via": via,
                            "verdict": state.get("verdict"),
                            "composite": state.get("composite_advice"),
                        }
                    })
                last_rolls = cur_rolls
                if cur_champ and cur_champ != 0:
                    last_champ_id = cur_champ
                    if RUNES_CFG.get("auto_apply") and cur_champ != runes_applied_for:
                        runes_applied_for = cur_champ
                        spawn(apply_rune_page(cur_champ))

            if state["phase"] in ("InProgress", "GameStart"):
                active = (state.get("lcd") or {}).get("active") or {}
                cur_level = active.get("level")
                if last_level is not None and cur_level is not None and cur_level > last_level:
                    augment = any(last_level < lv <= cur_level for lv in AUGMENT_CFG["levels"])
                    log.info(f"⭐ 升级! Lv.{last_level} -> Lv.{cur_level}" + (" (强化符文三选一)" if augment else ""))
                    await broadcast({
                        "type": "level_up",
                        "data": {"old_level": last_level, "new_level": cur_level, "augment": augment},
                    })
                last_level = cur_level

            LAST_STATE.clear()
            LAST_STATE.update(state)
            await broadcast({"type": "state_update", "data": state})

            try:
                md_text = state_to_md(state)
                GAME_STATE_FILE.write_text(md_text, encoding="utf-8")
            except Exception as md_err:
                log.warning(f"写 game_state.md 失败: {md_err}")
        except Exception as e:
            log.error(f"Poll error: {e}")
        await asyncio.sleep(2.0)



@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "frontend" / "index.html"))


@app.get("/coach")
@app.get("/deep")
async def old_pages():
    """以前的副屏卡片 / 深度分析页已合并进主页面，旧书签跳回首页。"""
    return RedirectResponse("/")


@app.get("/api/state")
async def api_state():
    return await collect_game_state()


@app.get("/api/data/status")
async def api_data_status():
    champs = ARAM_META.get("champions") or {}
    return {
        "loaded": bool(ARAM_META),
        "syncing": _data_sync_lock.locked(),
        "generated_at": ARAM_META.get("generated_at"),
        "sources": ARAM_META.get("sources", {}),
        "last_report": ARAM_META.get("last_report", {}),
        "champions": len(champs),
        "with_opgg_build": sum(1 for c in champs.values() if c.get("opgg_build")),
        "with_balance": sum(1 for c in champs.values() if c.get("aram_balance")),
        "auto_sync": DATA_SYNC_CFG,
    }


@app.post("/api/data/refresh")
async def api_data_refresh():
    return await refresh_aram_meta(force=True)


@app.get("/api/coach/status")
async def api_coach_status():
    return {
        "enabled": llm_client.enabled,
        "model": llm_client.model,
        "base_url": llm_client.base_url,
    }


@app.get("/api/kb/status")
async def api_kb_status():
    kb = KNOWLEDGE_BASE
    if not kb:
        return {"loaded": False, "error": "knowledge_base.json 未找到或为空"}

    tier = kb.get("tier_list", {})
    runes = kb.get("hextech_augments", {})
    formulas = kb.get("core_build_recipes", {})

    t0_list = []
    for t in tier.get("t0", []):
        t0_list.append(t.get("chinese") or t.get("name"))

    t1_list = []
    for t in tier.get("t1", []):
        t1_list.append(t.get("chinese") or t.get("name"))

    return {
        "loaded": True,
        "version": kb.get("version"),
        "mode": kb.get("mode"),
        "meta_summary": kb.get("meta_summary"),
        "t0_count": len(t0_list),
        "t0_heroes": t0_list,
        "t1_count": len(t1_list),
        "t1_heroes": t1_list,
        "snowball_tips_count": len(kb.get("snowball_tips", [])),
        "grass_tips_count": len(kb.get("grass_tips", [])),
        "build_formulas_count": len(formulas),
        "build_formulas": list(formulas.keys()),
        "rune_prismatic_count": len(runes.get("prismatic_s_tier", [])),
        "rune_gold_count": len(runes.get("gold_key", [])),
        "rune_situational_count": len(runes.get("situational", [])),
        "has_chogath": "chogath_specific" in kb,
        "has_darius": "darius_specific" in kb,
    }


@app.get("/api/coach/md")
async def api_coach_md():
    if GAME_STATE_FILE.exists():
        md_content = GAME_STATE_FILE.read_text(encoding="utf-8")
        return {"exists": True, "content": md_content}
    try:
        state = await collect_game_state()
        md_text = state_to_md(state)
        GAME_STATE_FILE.write_text(md_text, encoding="utf-8")
        return {"exists": True, "content": md_text}
    except Exception as e:
        return {"exists": False, "content": "", "error": str(e)}


async def current_state() -> Dict[str, Any]:
    """轮询循环每 2 秒采集一次状态；端点直接用它，不再额外打 LCU。"""
    return LAST_STATE or await collect_game_state()


async def _lcu_action(method: str, path: str, what: str) -> Dict[str, Any]:
    if not lcu.connected:
        return {"ok": False, "reason": "LoL 客户端未连接"}
    status, body = await lcu.request(method, path)
    if 200 <= status < 300:
        return {"ok": True}
    msg = body.get("message") if isinstance(body, dict) else str(body or "")
    return {"ok": False, "reason": f"{what}失败（{status}）{(msg or '')[:120]}"}


@app.post("/api/runes/apply")
async def api_runes_apply(champion_id: Optional[int] = None):
    """把推荐符文写成客户端里的临时符文页（不占页位，之后原地改写，不删任何页）。"""
    cid = champion_id or (await current_state()).get("my_champion_id")
    if not cid:
        return {"ok": False, "reason": "还没有分配到英雄"}
    if not lcu.connected:
        return {"ok": False, "reason": "LoL 客户端未连接"}
    return await apply_rune_page(cid)


@app.post("/api/champ-select/swap/{champion_id}")
async def api_bench_swap(champion_id: int):
    """和大乱斗备选席上的英雄交换。"""
    return await _lcu_action("POST", f"/lol-champ-select/v1/session/bench/swap/{champion_id}", "交换英雄")


@app.post("/api/champ-select/reroll")
async def api_reroll():
    return await _lcu_action("POST", "/lol-champ-select/v1/session/my-selection/reroll", "重随")


class AugmentOptions(BaseModel):
    options: List[str]


def _augment_ranking(state: Dict[str, Any], options: List[str]) -> Dict[str, Any]:
    coach = state.get("my_coach") or {}
    return augments.rank(options, coach, kb_specific_for(coach.get("name", "")), KNOWLEDGE_BASE, AUGMENT_POOL)


@app.get("/api/augments")
async def api_augments():
    return {
        "augments": [{"name": a["name"], "rarity": a.get("rarity", "")} for a in AUGMENT_POOL],
        "levels": AUGMENT_CFG["levels"],
        "llm": llm_client.enabled,
    }


@app.post("/api/augments/rank")
async def api_augments_rank(body: AugmentOptions):
    opts = [o.strip() for o in body.options if o.strip()][:3]
    return _augment_ranking(await current_state(), opts)


@app.post("/api/augments/ai")
async def api_augments_ai(body: AugmentOptions):
    opts = [o.strip() for o in body.options if o.strip()][:3]
    state = await current_state()
    ranking = _augment_ranking(state, opts)
    try:
        text = await llm_client.chat(ai.augment_messages(state, AI_DEPS, opts, ranking["ranked"]), "augment")
    except LLMError as e:
        return {"text": str(e), "ok": False}
    return {"text": text.replace("**", ""), "ok": True}


# ---------------------------------------------------------------- AI 教练（SSE 流式）
# 事件：start {mode, model} → thinking（推理模型在思考）→ delta {text}… → done；出错 error {text}

class AskBody(BaseModel):
    question: str
    history: List[Dict[str, str]] = []


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _relay(messages: List[Dict[str, str]], task: str):
    yield _sse({"type": "start", "mode": "llm", "model": llm_client.model})
    thinking = False
    try:
        async for kind, text in llm_client.stream(messages, task):
            if kind == "reasoning":
                if not thinking:
                    thinking = True
                    yield _sse({"type": "thinking"})
            else:
                yield _sse({"type": "delta", "text": text})
        yield _sse({"type": "done"})
    except LLMError as e:
        yield _sse({"type": "error", "text": str(e)})
    except Exception as e:  # 别让一个异常把连接挂住
        log.exception("AI 流式输出出错")
        yield _sse({"type": "error", "text": f"❌ 内部错误：{str(e)[:160]}"})


def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ai/ask")
async def api_ai_ask(body: AskBody):
    """向 AI 教练提问；history 是本局之前的问答 [{q, a}]，用于追问。"""
    question = body.question.strip()

    async def fail(text):
        yield _sse({"type": "error", "text": text})

    if not question:
        return _sse_response(fail("问题是空的"))
    if not llm_client.enabled:
        return _sse_response(fail("⚠️ 未配置 LLM API Key：在 config.local.json 或环境变量 "
                                  f"{LLM_KEY_ENV} 里配置后重启"))
    state = await current_state()
    return _sse_response(_relay(ai.ask_messages(state, AI_DEPS, question, body.history), "ask"))


@app.post("/api/ai/deep")
async def api_ai_deep():
    """整局分析：配置了 Key 走 LLM（流式），否则走本地规则引擎（一次给完）。"""
    state = await current_state()
    if llm_client.enabled:
        return _sse_response(_relay(ai.deep_messages(state, AI_DEPS), "deep"))

    async def rules():
        yield _sse({"type": "start", "mode": "rules"})
        try:
            yield _sse({"type": "delta", "text": _deep_analysis(state)})
            yield _sse({"type": "done"})
        except Exception as e:
            log.exception("规则引擎深度分析失败")
            yield _sse({"type": "error", "text": f"❌ 分析失败：{str(e)[:160]}"})
    return _sse_response(rules())


def _deep_analysis(state: Dict[str, Any]) -> str:
    lines = []
    lcd = state.get("lcd") or {}
    active = lcd.get("active") or {}
    stats = active.get("champion_stats") or {}
    team_map = lcd.get("team_map") or {}
    my_team, enemy_team = resolve_my_enemy_teams(team_map)

    my_info = None
    for p in my_team + enemy_team:
        if p.get("is_me"):
            my_info = p
            break

    my_champ = state.get("my_champion_name") or "未知"
    my_role = (state.get("my_coach") or {}).get("role", "")
    game_time = lcd.get("game_time", 0)
    m, s = int(game_time // 60), int(game_time % 60)
    gold = int(active.get("gold", 0))
    level = active.get("level") or (my_info.get("level", "?") if my_info else "?")
    phase = state.get("phase", "Unknown")
    hp_pct = stats.get("hp_pct", 0)

    my_team_gold = sum(p.get("item_gold", 0) for p in my_team)
    enemy_team_gold = sum(p.get("item_gold", 0) for p in enemy_team)
    gold_diff = my_team_gold - enemy_team_gold
    my_alive = sum(1 for p in my_team if not p.get("is_dead", False))
    enemy_alive = sum(1 for p in enemy_team if not p.get("is_dead", False))

    kb = KNOWLEDGE_BASE
    tier = kb.get("tier_list", {})
    t0_raw = tier.get("t0", [])
    t1_raw = tier.get("t1", [])
    t0_names = set(x.get("name", x) if isinstance(x, dict) else x for x in t0_raw)
    t1_names = set(x.get("name", x) if isinstance(x, dict) else x for x in t1_raw)
    role_formulas = kb.get("core_build_recipes", {})
    tips_kb = kb.get("champion_tips", {})

    my_tier = ""
    if my_champ in t0_names:
        my_tier = "T0"
    elif my_champ in t1_names:
        my_tier = "T1"

    lines.append("=" * 50)
    lines.append(f"🎮 乱斗深度分析 · {my_champ} Lv.{level}")
    lines.append(f"⏱️ {m}:{s} · 💰 {gold}G · ❤️ HP {hp_pct:.0f}%")
    if my_tier:
        lines.append(f"⭐ 英雄定位: {my_tier} {my_role}")
    else:
        lines.append(f"⭐ 英雄定位: {my_role}")
    lines.append(f"📊 团队装备总价: 我方 {my_team_gold}G vs 敌方 {enemy_team_gold}G (差 {gold_diff:+d})")
    lines.append("=" * 50)
    lines.append("")

    # 1. 局势判断
    lines.append("🎯 一、局势深度判断")
    lines.append("-" * 30)

    adv_score = 0
    reasons = []
    if gold_diff > 1000:
        adv_score += 2
        reasons.append(f"团队经济领先 {gold_diff}G，装备压制")
    elif gold_diff > 0:
        adv_score += 1
        reasons.append(f"团队经济小优 +{gold_diff}G")
    elif gold_diff < -1000:
        adv_score -= 2
        reasons.append(f"团队经济落后 {-gold_diff}G，需要避战")
    elif gold_diff < 0:
        adv_score -= 1
        reasons.append(f"团队经济小劣 {gold_diff}G")

    if my_alive > enemy_alive + 1:
        adv_score += 2
        reasons.append(f"我方 {my_alive} 人存活 vs 敌方 {enemy_alive} 人，人数碾压")
    elif my_alive > enemy_alive:
        adv_score += 1
        reasons.append(f"人数小优 ({my_alive} vs {enemy_alive})")
    elif my_alive < enemy_alive - 1:
        adv_score -= 2
        reasons.append(f"我方 {my_alive} vs 敌方 {enemy_alive}，人数劣势")
    elif my_alive < enemy_alive:
        adv_score -= 1
        reasons.append(f"人数小劣 ({my_alive} vs {enemy_alive})")

    if my_tier == "T0":
        adv_score += 1
        reasons.append(f"T0 英雄自带强度加成")

    if hp_pct < 30:
        adv_score -= 2
        reasons.append(f"血量极低 ({hp_pct:.0f}%)，容易被秒")
    elif hp_pct < 50:
        adv_score -= 1
        reasons.append(f"血量偏低 ({hp_pct:.0f}%)，谨慎操作")

    if adv_score >= 4:
        verdict = "🟢 大优势 · 主动开团"
    elif adv_score >= 1:
        verdict = "🟡 小优势 · 稳扎稳打"
    elif adv_score >= 0:
        verdict = "⚪ 均势 · 找机会"
    elif adv_score >= -2:
        verdict = "🟠 小劣势 · 避战发育"
    else:
        verdict = "🔴 大劣势 · 缩塔等队友"

    lines.append(f"结论: {verdict}")
    for r in reasons:
        lines.append(f"  • {r}")
    if not reasons:
        lines.append("  • 无明显优劣势，看操作")
    lines.append("")

    # 2. 属性面板解读
    lines.append("💪 二、属性面板解读")
    lines.append("-" * 30)
    lines.append(f"  AD  {stats.get('ad', 0):.0f}    AP  {stats.get('ap', 0):.0f}")
    lines.append(f"  护甲 {stats.get('armor', 0):.0f}    魔抗 {stats.get('mr', 0):.0f}")
    lines.append(f"  攻速 {stats.get('attack_speed', 0):.2f}    移速 {stats.get('move_speed', 0):.0f}")
    lines.append(f"  技能急速 {stats.get('ability_haste', 0):.0f}    暴击 {(stats.get('crit_chance', 0) * 100):.0f}%")
    lines.append(f"  护穿 {stats.get('armor_pen_flat', 0):.0f}({(stats.get('armor_pen_pct', 0) * 100):.0f}%)    法穿 {stats.get('magic_pen_flat', 0):.0f}({(stats.get('magic_pen_pct', 0) * 100):.0f}%)")

    panel_tips = []
    if my_role in ("坦克", "战士"):
        if stats.get('armor', 0) < 60 and any(champ in [p.get("champion","") for p in enemy_team] for champ in ["诺克萨斯之手","皮城执法官","祖安怒兽"]):
            panel_tips.append("护甲偏低，对面有 AD 爆发，优先补护甲装")
        if stats.get('mr', 0) < 50 and any(champ in [p.get("champion","") for p in enemy_team] for champ in ["黑暗之女","虚空之眼","邪恶小法师"]):
            panel_tips.append("魔抗偏低，对面有 AP 爆发，考虑加魔抗")
        if stats.get('ability_haste', 0) < 80:
            panel_tips.append("技能急速偏低，大乱斗节奏快，CD 很重要")
    elif my_role in ("法师",):
        if stats.get('ap', 0) < 200:
            panel_tips.append("法强偏低，优先堆法强（卢登/永霜/暗夜收割者）")
        if stats.get('magic_pen_flat', 0) + stats.get('magic_pen_pct', 0) < 0.1:
            panel_tips.append("法穿不足，对面有魔抗时需要法穿棒或虚空之杖")
        if stats.get('hp_pct', 1) < 0.5:
            panel_tips.append("法师脆皮，注意站位，别被雪球拉脸")
    elif my_role in ("射手",):
        if stats.get('attack_speed', 0) < 1.2:
            panel_tips.append("攻速偏低，考虑补狂战士胫甲或鬼索")
        if stats.get('crit_chance', 0) < 0.5:
            panel_tips.append("暴击率不足，射手核心输出靠暴击")
        if stats.get('armor_pen_pct', 0) < 0.15:
            panel_tips.append("护穿不足，对面坦克多时需要塞瑞尔达或黑切")
    elif my_role in ("刺客",):
        if stats.get('armor', 0) < 40:
            panel_tips.append("刺客太脆，进场容易被秒，考虑死亡之舞/守护天使")
        if stats.get('ability_haste', 0) < 60:
            panel_tips.append("刺客靠技能连招，CD 很重要")
    if panel_tips:
        lines.append("")
        lines.append("面板建议:")
        for t in panel_tips:
            lines.append(f"  ⚠️ {t}")
    lines.append("")

    # 3. 装备路线
    lines.append("🛒 三、装备路线规划")
    lines.append("-" * 30)

    my_items = []
    if my_info:
        my_items = [it.get("name", "") for it in my_info.get("items", []) if it.get("name")]
    coach_build = (state.get("my_coach") or {}).get("build", [])

    role_key = ""
    for k in role_formulas:
        if k in my_role or my_role in k:
            role_key = k
            break
    if not role_key:
        for k in ["坦克", "战士", "法师", "射手", "刺客"]:
            if k in my_role:
                role_key = k
                break

    formula = role_formulas.get(role_key, {}) if role_key else {}
    formula_name = formula.get("name", "")
    formula_items = formula.get("items", [])
    formula_note = formula.get("note", "")

    lines.append(f"你的角色: {my_role}")
    if my_items:
        lines.append(f"已购买: {' → '.join(my_items)}")
    lines.append(f"知识库版本公式: {formula_name}")
    if formula_items:
        lines.append(f"推荐路线: {' → '.join(formula_items)}")
    if coach_build:
        lines.append(f"champions.json 路线: {' → '.join(coach_build)}")
    if formula_note:
        lines.append(f"版本备注: {formula_note}")

    moment = state.get("moment")
    if moment:
        lines.append("")
        lines.append(advice.moment_text(moment).split("\n")[0])

    # 海克斯大乱斗专属
    lines.append("")
    lines.append("📐 海克斯大乱斗专属提示:")
    if my_role in ("坦克",):
        lines.append("  • 心之钢叠层是核心，别断！雪球拉脸接控再开团")
        lines.append("  • 日炎圣盾对线期清兵快，可先出")
    elif my_role in ("法师",):
        lines.append("  • 珠光护手+炼狱导管是版本 T0 组合")
        lines.append("  • 暗夜收割者比卢登更适合乱斗持续输出")
    elif my_role in ("射手",):
        lines.append("  • 不朽盾弓防暴毙，比狂风之力好")
        lines.append("  • 海妖杀手打肉快，对面坦多就换")
    elif my_role in ("刺客",):
        lines.append("  • 巨蛇之牙破石像鬼，必备！")
        lines.append("  • 幽梦之灵 + 暮刃双穿秒后排")
    elif my_role in ("战士",):
        lines.append("  • 焚天 + 黑切是版本答案")
        lines.append("  • 死舞比渴血更适合乱斗节奏")
    lines.append("")

    # 4. 敌方威胁评估
    lines.append("🔥 四、敌方威胁评估")
    lines.append("-" * 30)

    enemy_threats = []
    for p in enemy_team:
        champ = p.get("champion", "?")
        k = p.get("kills", 0)
        d = p.get("deaths", 0)
        a = p.get("assists", 0)
        dead = p.get("is_dead", False)
        tier_tag = ""
        if champ in t0_names:
            tier_tag = " 🔥T0"
        elif champ in t1_names:
            tier_tag = " ⚡T1"

        kda = k / max(1, d) + a / 2
        threat = kda
        if champ in t0_names:
            threat += 3
        elif champ in t1_names:
            threat += 1.5
        if dead:
            threat -= 5

        enemy_threats.append((threat, champ, k, d, a, dead, tier_tag))

    enemy_threats.sort(key=lambda x: -x[0])

    for threat, champ, k, d, a, dead, tier_tag in enemy_threats:
        dead_tag = " 💀复活中" if dead else ""
        lines.append(f"  • {champ} KDA={k}/{d}/{a}{dead_tag}{tier_tag}")

    top_threat = enemy_threats[0] if enemy_threats else None
    if top_threat and not top_threat[5]:
        lines.append("")
        lines.append(f"⚠️ 最需要盯防的: {top_threat[1]}{top_threat[6]}")
        if top_threat[1] in t0_names:
            lines.append("  这英雄版本 T0，即使 KDA 不高也别让他发育起来")
    lines.append("")

    # 5. 操作技巧
    lines.append("⚡ 五、操作与团战技巧")
    lines.append("-" * 30)

    champ_tips = tips_kb.get(my_champ, [])
    if champ_tips:
        lines.append(f"【{my_champ} 专属技巧】")
        for t in champ_tips[:5]:
            lines.append(f"  🎯 {t}")
        lines.append("")

    lines.append("【雪球技巧】")
    lines.append("  • 先 Q 再雪球：很多英雄技能命中后雪球更准")
    lines.append("  • 草丛躲雪球：雪球没视野会断")

    lines.append("")
    lines.append("【团战站位】")
    if my_role in ("坦克",):
        lines.append("  • 你在前排扛伤害，雪球拉敌方后排")
        lines.append("  • 别冲太深，保护我方 C 位")
    elif my_role in ("法师",):
        lines.append("  • 站第二排 poke，别被雪球拉脸")
        lines.append("  • 利用草丛卡视野放技能")
    elif my_role in ("射手",):
        lines.append("  • 最远输出距离，保持安全")
        lines.append("  • 石像鬼或不朽盾弓防暴毙")
    elif my_role in ("刺客",):
        lines.append("  • 等对面 C 位露头或关键技能交了再进")
        lines.append("  • 雪球进场 + 秒后排 + 撤")
    elif my_role in ("战士",):
        lines.append("  • 半前排，可进可退")
        lines.append("  • 黑切叠层后追着砍")
    lines.append("")

    lines.append("【经济】")
    lines.append(f"  • 你有 {gold}G。大乱斗不能回城，阵亡时按上面的购物清单买")
    lines.append("")

    # 6. 海克斯大乱斗强化符文
    augments = kb.get("hextech_augments", {})
    if augments:
        lines.append("🎲 六、海克斯强化符文指南")
        lines.append("-" * 30)

        top_t0_augments = augments.get("t0_recommended", [])[:3]
        if top_t0_augments:
            lines.append("版本 T0 强化:")
            for a in top_t0_augments:
                lines.append(f"  ⭐ {a.get('name', a)} - {a.get('effect', '')}")

        role_augments = augments.get(my_role, [])
        if role_augments:
            lines.append(f"")
            lines.append(f"适合 {my_role} 的强化:")
            for a in role_augments[:3]:
                if isinstance(a, dict):
                    lines.append(f"  • {a.get('name', a)} - {a.get('effect', '')}")
                else:
                    lines.append(f"  • {a}")
        lines.append("")

    lines.append("=" * 50)
    lines.append(f"💡 以上基于本地知识库 v{kb.get('version', '?')} 生成 · 零延迟 · 零网络依赖")
    lines.append("=" * 50)

    return "\n".join(lines)


@app.get("/api/lcu/status")
async def api_lcu_status():
    return {
        "connected": lcu.connected,
        "port": lcu.info["port"] if lcu.info else None,
    }


@app.get("/api/champion/{character_id}")
async def api_champion(character_id: int):
    coach = get_champion_coach(character_id)
    if not coach:
        return {"error": "No data for this champion yet"}
    return coach


@app.get("/api/champions/search")
async def api_champions_search(q: str = ""):
    results = []
    kw = q.strip().lower()
    for cid, data in CHAMPIONS_DB.items():
        name = data.get("name", "")
        title = data.get("title", "")
        if not kw or kw in name.lower() or kw in title.lower():
            results.append({
                "character_id": int(cid),
                "name": name,
                "title": title,
                "role": data.get("role"),
            })
    return results


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        await ws.send_json({"type": "initial", "data": await current_state()})
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"WS error: {e}")
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)
