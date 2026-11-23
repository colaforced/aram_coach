import os
import json
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse

from app.lcu_client import LCUClient
from app import data_sync, lcd_client
from app.lcd_client import resolve_my_enemy_teams
from app.llm_client import LLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aram_coach")

BASE_DIR = Path(__file__).resolve().parent.parent
GAME_STATE_FILE = BASE_DIR / "game_state.md"

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

with open(BASE_DIR / "data" / "champions.json", "r", encoding="utf-8") as f:
    CHAMPIONS_DB = json.load(f)

with open(BASE_DIR / "data" / "champion_map.json", "r", encoding="utf-8") as f:
    CHAMPION_MAP = json.load(f)

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


for _cid, _cdata in CHAMPIONS_DB.items():
    _cdata["role"] = normalize_role(_cdata.get("role", ""))
    _build = _cdata.get("build", [])
    _new_build = []
    for _item in _build:
        if _item in ARAM_BUILD_REPLACEMENTS:
            _repl = ARAM_BUILD_REPLACEMENTS[_item]
            log.info(f"  [{_cdata.get('name',_cid)}] 替换禁用装备: {_item} -> {_repl}")
            _new_build.append(_repl)
        else:
            _new_build.append(_item)
    _cdata["build"] = _new_build

from contextlib import asynccontextmanager

_data_sync_lock = asyncio.Lock()


async def refresh_aram_meta(force: bool = False) -> Dict[str, Any]:
    """数据过期（或 force）时同步 OP.GG / Meraki 数据，成功后原地替换 ARAM_META。"""
    if _data_sync_lock.locked():
        return {"status": "running"}
    async with _data_sync_lock:
        if not force and not data_sync.is_stale(ARAM_META, DATA_SYNC_CFG["max_age_hours"]):
            return {"status": "fresh"}
        log.info("同步大乱斗数据（OP.GG / Meraki / CommunityDragon）...")
        try:
            report = await data_sync.sync(DATA_DIR, ITEM_MAP)
        except Exception as e:
            log.exception("大乱斗数据同步失败")
            return {"status": "error", "error": str(e)[:200]}
        fresh = data_sync.load_meta(DATA_DIR)
        if fresh:
            ARAM_META.clear()
            ARAM_META.update(fresh)
        log.info(f"大乱斗数据同步结束: {report}")
        return {"status": "done", "report": report}


@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Starting ARAM Coach...")
    asyncio.create_task(poll_lcu())
    if DATA_SYNC_CFG.get("auto"):
        asyncio.create_task(refresh_aram_meta())
    yield
    await lcu.close()
    await llm_client.close()

app = FastAPI(title="ARAM Coach", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend")), name="static")

lcu = LCUClient(search_paths=CONFIG["lol_install_paths"])
llm_client = LLMClient(CONFIG.get("llm", {}))

ws_clients: list[WebSocket] = []

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
    if opgg.get("strength"):
        aram_strength = opgg["strength"]
    if opgg_build.get("build"):
        build = list(opgg_build["build"])
        # 前端拿它和当前金币比"下一件大件攒了多少"，所以取第一件核心装的价格，不是三件之和
        first_core = (opgg_build.get("build_ids") or [None])[0]
        core_cost = ITEM_MAP.get(str(first_core), {}).get("gold_total") or core_cost
    if opgg_build.get("runes"):
        runes = {k: opgg_build["runes"][k] for k in rune_fields}

    kb_specific = None
    if KNOWLEDGE_BASE:
        cn_name_map = {
            "chogath_specific": ["虚空恐惧", "科加斯", "大虫子"],
            "darius_specific": ["诺克萨斯之手", "诺手", "德莱厄斯"],
        }
        for kb_key, aliases in cn_name_map.items():
            if name in aliases:
                kb_specific = KNOWLEDGE_BASE.get(kb_key)
                break
        if not kb_specific:
            for k, v in KNOWLEDGE_BASE.items():
                if isinstance(v, dict) and v.get("chinese") == name:
                    kb_specific = v
                    break

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

    return {
        "character_id": character_id,
        "name": name,
        "role": data.get("role", "未知"),
        "aram_strength": aram_strength,
        **runes,
        "rune_shards": (opgg_build.get("runes") or {}).get("shards", []),
        "build": build,
        "starter_items": opgg_build.get("starter_items", []),
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


def compute_reroll_advice(my_team: list, my_coach: Optional[Dict[str, Any]],
                          remaining_dice: int, my_champion_id: Any = None) -> Dict[str, Any]:
    advice = {
        "remaining_dice": remaining_dice,
        "decision": "keep",
        "score": 0,
        "strength": 65,
        "fitness": 50,
        "reason": "",
        "suggestions": [],
    }

    if my_coach:
        strength = my_coach.get("aram_strength", 65)
        advice["strength"] = strength
    else:
        strength = 50

    fitness, notes = evaluate_team_fit(my_team, my_coach, my_champion_id)
    reasons = [text for _kind, text in notes]
    advice["fitness"] = fitness
    advice["score"] = int(strength * 0.6 + fitness * 0.4)

    if remaining_dice <= 0:
        advice["decision"] = "keep"
        advice["reason"] = "骰子用完了，只能保留"
    elif advice["score"] < 50:
        advice["decision"] = "reroll"
        advice["reason"] = f"综合分 {advice['score']} 太低，强烈建议骰子重随"
    elif advice["score"] < 65:
        advice["decision"] = "consider"
        advice["reason"] = f"综合分 {advice['score']}，看你喜好"
    else:
        advice["decision"] = "keep"
        advice["reason"] = f"综合分 {advice['score']}，建议保留"

    advice["suggestions"] = reasons
    return advice


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

            remaining_dice = state.get("reroll_remaining", 2)
            state["reroll_advice"] = compute_reroll_advice(
                state["my_team"], state.get("my_coach"), remaining_dice,
                state.get("my_champion_id"),
            )

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

                my_team_idx = 0
                if my_puuid:
                    for sel in selections:
                        if sel.get("puuid") == my_puuid:
                            ptid = sel.get("teamParticipantId", 0)
                            my_team_idx = 0 if ptid <= 5 else 1
                            break

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

    if state.get("my_team") or state.get("enemy_team"):
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

    composite = state.get("composite_advice") or {}
    if composite:
        score = composite.get("score")
        if score:
            lines.append(f"## 🎯 当前教练评分: **{score} 分**")
            lines.append("")
        tips = composite.get("tips") or []
        if tips:
            lines.append("**即时提示**:")
            for t in tips[:5]:
                lines.append(f"- ⚠️ {t}")
            lines.append("")

    matchup = state.get("matchup_analysis") or {}
    if matchup:
        advs = matchup.get("item_advs") or []
        tips2 = matchup.get("tips") or []
        if advs or tips2:
            lines.append("## ⚔️ 对局分析")
            lines.append("")
            for a in advs:
                lines.append(f"- 🛡️ {a}")
            for t in tips2:
                lines.append(f"- 💡 {t}")
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


def generate_quick_html(state: Dict[str, Any]) -> str:
    lcd = state.get("lcd") or {}
    active = (lcd.get("active") or {})
    stats = active.get("champion_stats") or {}
    gold = int(active.get("gold", 0))
    level = active.get("level", "?")
    game_time = lcd.get("game_time", 0)

    my_team, enemy_team = resolve_my_enemy_teams(lcd.get("team_map") or {})

    my_alive = sum(1 for p in my_team if not p.get("is_dead"))
    enemy_alive = sum(1 for p in enemy_team if not p.get("is_dead"))
    alive_diff = my_alive - enemy_alive

    my_total_gold = sum(p.get("item_gold", 0) for p in my_team)
    enemy_total_gold = sum(p.get("item_gold", 0) for p in enemy_team)
    gold_diff = my_total_gold - enemy_total_gold

    me = None
    for p in my_team:
        if p.get("is_me"):
            me = p
            break
    if not me and my_team:
        me = my_team[0]

    my_kda = f"{me.get('kills',0)}/{me.get('deaths',0)}/{me.get('assists',0)}" if me else "?"

    coach = state.get("my_coach") or {}
    build = coach.get("build") or []

    me_items = me.get("items") if me and me.get("is_me") else []
    owned_items = [it.get("name", "") for it in me_items or [] if it.get("name")]
    next_item = None
    for b in build:
        if b not in owned_items:
            next_item = b
            break

    build_tips = []
    situational = coach.get("situational") or {}
    enemy_mages = [p for p in enemy_team if p.get("is_mage")]
    enemy_ads = [p for p in enemy_team if p.get("is_ad")]
    enemy_healers = [p for p in enemy_team if p.get("is_healer")]
    enemy_tanks = [p for p in enemy_team if p.get("is_tank")]

    if enemy_healers and "against_heal" in situational:
        build_tips.append(f"敌有治疗！{situational['against_heal']}")
    if enemy_tanks and "against_tank" in situational:
        build_tips.append(f"敌有肉！{situational['against_tank']}")

    hp_pct = stats.get("hp", 0) / stats.get("max_hp", 1) * 100 if stats.get("max_hp", 0) > 0 else 100
    if hp_pct < 30 and "need_survive" in situational:
        build_tips.append(f"你残血！{situational['need_survive']}")

    owned_brief = [b for b in build if b in owned_items]
    need_brief = [b for b in build if b not in owned_items]

    situation_tips = []
    composite = state.get("composite_advice") or {}
    matchup = state.get("matchup_analysis") or {}
    if matchup.get("tips"):
        situation_tips.extend(matchup["tips"][:2])
    if alive_diff < 0:
        situation_tips.append(f"人少！等队友复活再团")
    if gold_diff > 500:
        situation_tips.append(f"装备大优势(+{gold_diff}G) 主动开！")
    elif gold_diff < -500:
        situation_tips.append(f"装备落后({gold_diff}G) poke为主")
    if hp_pct < 25 and not (me or {}).get("is_dead"):
        situation_tips.append(f"你只有 {hp_pct:.0f}% 血！先回家！")

    my_role = coach.get("role", "")
    my_champion_name = me.get("champion") if me else ""
    kb = KNOWLEDGE_BASE
    my_kb_specific = None
    if kb:
        for key, val in kb.items():
            if isinstance(val, dict) and val.get("chinese") == my_champion_name:
                my_kb_specific = val
                break
        if my_champion_name == "虚空恐惧":
            my_kb_specific = kb.get("chogath_specific") or my_kb_specific
        elif my_champion_name == "诺克萨斯之手":
            my_kb_specific = kb.get("darius_specific") or my_kb_specific

    action_tips = []
    if coach.get("tips"):
        action_tips.extend(coach["tips"][:2])
    if my_kb_specific and my_kb_specific.get("tips"):
        for t in my_kb_specific["tips"][:2]:
            if t not in action_tips and len(action_tips) < 4:
                action_tips.append(t)
    if coach.get("snowball_tip"):
        action_tips.append(f"雪球: {coach['snowball_tip']}")
    if coach.get("flash_tip"):
        action_tips.append(f"闪现: {coach['flash_tip']}")
    if composite.get("tips"):
        for t in composite["tips"]:
            if t not in action_tips and len(action_tips) < 4:
                action_tips.append(t)

    t0_set = set()
    t1_set = set()
    if kb:
        for t in kb.get("tier_list", {}).get("t0", []):
            t0_set.add(t.get("chinese") or t.get("name"))
        for t in kb.get("tier_list", {}).get("t1", []):
            t1_set.add(t.get("chinese") or t.get("name"))

    threats = []
    for p in enemy_team:
        if p.get("is_dead"):
            continue
        cn = p.get("champion", "")
        kd = p.get("kills", 0) - p.get("deaths", 0)
        threat_score = 0
        is_tier_threat = False
        if cn in t0_set:
            threat_score += 100
            is_tier_threat = True
        elif cn in t1_set:
            threat_score += 50
            is_tier_threat = True
        threat_score += max(kd * 10, p.get("kills", 0) * 5)
        if kd >= 2 or p.get("is_carry") or is_tier_threat:
            _enriched = dict(p)
            _enriched["threat_score"] = threat_score
            _enriched["is_tier_threat"] = is_tier_threat
            _enriched["is_t0"] = cn in t0_set
            threats.append(_enriched)
    threats = sorted(threats, key=lambda x: x.get("threat_score", 0), reverse=True)[:3]

    kb_t0_enemy = []
    kb_t1_enemy = []
    enemy_names_kb = set(p.get("champion","") for p in enemy_team)
    if kb:
        for t0 in kb.get("tier_list", {}).get("t0", []):
            if t0.get("chinese","") in enemy_names_kb or t0.get("name","") in enemy_names_kb:
                kb_t0_enemy.append(t0)
        for t1 in kb.get("tier_list", {}).get("t1", []):
            if t1.get("chinese","") in enemy_names_kb or t1.get("name","") in enemy_names_kb:
                kb_t1_enemy.append(t1)

    role_build_formula = ""
    if kb:
        formulas = kb.get("core_build_recipes", {})
        if my_role == "战士":
            role_build_formula = formulas.get("战士通用公式", {})
        elif my_role == "坦克":
            role_build_formula = formulas.get("坦克通用公式", {})
        elif my_role == "法师":
            if enemy_mages and len(enemy_mages) >= 2:
                role_build_formula = formulas.get("法师持续伤害公式", {})
            else:
                role_build_formula = formulas.get("法师爆发公式", {})
        elif my_role in ("射手", "ADC"):
            if enemy_tanks and len(enemy_tanks) >= 2:
                role_build_formula = formulas.get("射手暴击公式", {})
            else:
                role_build_formula = formulas.get("射手暴击公式", {})
        elif my_role == "刺客":
            role_build_formula = formulas.get("刺客通用公式", {})

    kb_build_tips = []
    if role_build_formula and role_build_formula.get("note"):
        kb_build_tips.append(role_build_formula["note"])

    if role_build_formula and not my_kb_specific:
        _kb_first = role_build_formula.get("first", [])
        _kb_second = role_build_formula.get("second", [])
        _kb_third = role_build_formula.get("third", [])
        _kb_all = _kb_first + _kb_second + _kb_third
        if _kb_all:
            _existing_set = set(build)
            _kb_prefixed = [x for x in _kb_all if x not in _existing_set]
            build = _kb_first + _kb_second + _kb_third + [b for b in build if b not in _kb_all]
            build = list(dict.fromkeys(build))
            if _kb_prefixed:
                kb_build_tips.insert(0, f"版本推荐首出: {' / '.join(_kb_first[:2])}")

    next_item = None
    for b in build:
        if b not in owned_items:
            next_item = b
            break

    kb_snowball_tip = ""
    if kb.get("snowball_tips"):
        import random as _r
        kb_snowball_tip = _r.choice(kb["snowball_tips"][:4])

    def _role_action(situation):
        _META = "大乱斗一直打架"
        role_map = {
            "坦克": {
                "big_win": "5v3！雪球拉最前面的开团！心之钢叠起来就是爸爸！",
                "small_win": "人多一个！雪球开塔前的！顶上去放心之钢层！",
                "even_rich": "装备碾压！雪球开脆皮！顶塔推别怕死！",
                "even_poor": "装备差！先poke耗血，雪球等队友到齐，心之钢先叠几层",
                "even": "技能齐了！雪球拉脸控住！一波推！心之钢别断！",
                "small_lose": "少一人！躲草丛等雪球好，心之钢别死，叠层掉光亏大了",
                "big_lose": "崩了！草丛蹲好，雪球逃跑用，撑住不死心之钢还能叠",
            },
            "战士": {
                "big_win": "人多！雪球拉后排carry！直接切！黑切破甲巨快！",
                "small_win": "压上去！雪球开塔前脆皮！焚天续航顶住！",
                "even_rich": "装备好！星蚀滚雪球！雪球拉脸R开！一波推！",
                "even_poor": "装备贫！poke耗血，雪球等残血再开，星蚀先出最便宜",
                "even": "技能齐！雪球拉脸接R！秒一个！",
                "small_lose": "少人！草丛蹲好，雪球留逃跑，别硬接",
                "big_lose": "崩了！雪球用来逃跑，别出塔！死亡之舞保你不暴毙",
            },
            "法师": {
                "big_win": "人多！站后排QW甩！珠光护手暴击秒人！雪球留补伤害！",
                "small_win": "压上去poke！炼狱导管叠灼烧刷CD！雪球拉残血收！",
                "even_rich": "装备好！峡谷制造者/大面具poke穿！雪球拉脸R秒脆皮！",
                "even_poor": "装备贫！远距离poke！雪球用来拉开距离！别被贴脸",
                "even": "poke到血线！珠光护手暴击一发入魂！雪球接R秒！",
                "small_lose": "躲后排poke！雪球拉开别被贴脸！",
                "big_lose": "站最远清兵！雪球一被粘就用！",
            },
            "射手": {
                "big_win": "人多！站桩狂射！不朽盾弓保你活！雪球逃跑用！",
                "small_win": "压上去点塔！雪球拉开躲突脸！",
                "even_rich": "装备神装！无尽+飓风射爆！站前排后面！",
                "even_poor": "装备差！猥琐poke发育等大件！巨蛇之牙破肉！",
                "even": "走位输出！盾弓触发就反打！雪球被贴脸就用！",
                "small_lose": "躲后排射！雪球拉开距离！",
                "big_lose": "塔下清兵！雪球一被碰就跑！",
            },
            "刺客": {
                "big_win": "人多！雪球拉后排！巨蛇之牙破石像鬼！秒了就撤！",
                "small_win": "蹲草丛！雪球拉塔前脆皮！巨蛇之牙必备！",
                "even_rich": "装备好！暮刃+赛瑞尔达！雪球拉谁谁死！",
                "even_poor": "装备差！蹲草丛捡残血！雪球留逃跑！巨蛇之牙优先",
                "even": "蹲好！残血一露雪球拉脸秒！",
                "small_lose": "少人！草丛蹲好别动，等对面落单",
                "big_lose": "崩了！雪球逃跑用，别出塔！",
            },
        }
        return role_map.get(my_role, role_map["战士"]).get(situation, role_map["战士"][situation])

    if alive_diff >= 2:
        situation_color = "#43cea2"
        situation_label = "🟢 大优势"
        action = _role_action("big_win")
    elif alive_diff == 1:
        situation_color = "#43cea2"
        situation_label = "🟢 小优"
        action = _role_action("small_win")
    elif alive_diff == 0:
        situation_color = "#f5b041"
        situation_label = "🟡 均势"
        if gold_diff > 300:
            action = _role_action("even_rich")
        elif gold_diff < -300:
            action = _role_action("even_poor")
        else:
            action = _role_action("even")
    elif alive_diff == -1:
        situation_color = "#ef5350"
        situation_label = "🔴 小劣"
        action = _role_action("small_lose")
    else:
        situation_color = "#ef5350"
        situation_label = "🔴 大劣"
        action = _role_action("big_lose")

    if gold_diff > 500:
        situation_label += " 💰"
    elif gold_diff < -500:
        situation_label += " 💸"

    build_section = ""
    build_rows = []
    if next_item:
        build_rows.append(
            f'<div style="font-size:18px;font-weight:700;color:#ffd54f;">{next_item}</div>'
            f'<div style="font-size:12px;opacity:0.85;margin-top:2px;">你有 {gold}G</div>'
        )
    if owned_brief:
        build_rows.append(f'<div style="font-size:11px;opacity:0.7;">✅ 已买: {" · ".join(owned_brief)}</div>')
    if need_brief and len(need_brief) > 1:
        rest = " → ".join(need_brief[1:4])
        build_rows.append(f'<div style="font-size:11px;opacity:0.65;">🔜 后续: {rest}</div>')
    if build_tips:
        build_rows.append("".join(f'<div style="font-size:11px;color:#ffab91;margin-top:2px;">💡 {t}</div>' for t in build_tips))
    if kb_build_tips:
        build_rows.append("".join(f'<div style="font-size:11px;color:#a78bfa;margin-top:2px;">📐 {t}</div>' for t in kb_build_tips))
    if build_rows:
        build_section = f'''
        <div style="background:linear-gradient(135deg,#667eea33,#764ba233);border:1px solid #667eea;border-radius:10px;padding:12px 14px;margin-top:10px;">
            <div style="font-size:12px;color:#a78bfa;font-weight:600;margin-bottom:6px;">🛒 出装建议</div>
            {''.join(f'<div style="margin:2px 0;">{r}</div>' for r in build_rows)}
        </div>'''

    threat_section = ""
    if threats:
        threat_cards = []
        for t in threats:
            dead_tag = f' <span style="color:#888">💀复活{t.get("respawn_timer",0):.0f}s</span>' if t.get("is_dead") else ""
            tier_tag = ""
            if t.get("is_t0"):
                tier_tag = f' <span style="background:#ffd54f;color:#000;border-radius:4px;padding:1px 5px;font-size:10px;font-weight:700;">🔥T0</span>'
            elif t.get("is_tier_threat"):
                tier_tag = f' <span style="background:#f5b041;color:#000;border-radius:4px;padding:1px 5px;font-size:10px;font-weight:700;">⚡T1</span>'
            threat_cards.append(
                f'<span style="background:rgba(239,83,80,0.15);border:1px solid rgba(239,83,80,0.5);border-radius:6px;padding:4px 10px;font-size:12px;color:#ff8a80;">'
                f'{t.get("champion","?")} K{t.get("kills",0)}{tier_tag}</span>{dead_tag}'
            )
        threat_section = f'''
        <div style="margin-top:8px;border:1px solid #ef5350;border-radius:10px;padding:10px 14px;background:rgba(239,83,80,0.06);">
            <div style="font-size:12px;color:#ef5350;font-weight:600;margin-bottom:6px;">⚠️ 敌方威胁</div>
            <div>{" ".join(threat_cards)}</div>
        </div>'''

    situation_section = ""
    sit_rows = []
    if situation_tips:
        sit_rows.extend(
            f'<div style="font-size:12px;color:#c8d6e5;">⚡ {t}</div>' for t in situation_tips[:3]
        )
    if sit_rows:
        situation_section = f'''
        <div style="background:rgba(245,176,65,0.1);border:1px solid #f5b041;border-radius:10px;padding:10px 14px;margin-top:8px;">
            <div style="font-size:12px;color:#f5b041;font-weight:600;margin-bottom:4px;">⚔️ 局势判断</div>
            {''.join(f'<div style="margin:2px 0;">{r}</div>' for r in sit_rows)}
        </div>'''

    action_section = ""
    act_rows = []
    if action_tips:
        act_rows.extend(
            f'<div style="font-size:12px;color:#c8d6e5;">🎯 {t}</div>' for t in action_tips[:4]
        )
    if act_rows:
        action_section = f'''
        <div style="background:rgba(67,206,162,0.1);border:1px solid #43cea2;border-radius:10px;padding:10px 14px;margin-top:8px;">
            <div style="font-size:12px;color:#43cea2;font-weight:600;margin-bottom:4px;">🎮 操作要点</div>
            {''.join(f'<div style="margin:2px 0;">{r}</div>' for r in act_rows)}
        </div>'''

    kb_section = ""
    if kb:
        kb_parts = []

        if kb_t0_enemy:
            t0_names = [t.get("chinese") or t.get("name") for t in kb_t0_enemy]
            kb_parts.append(
                f'<div style="background:rgba(239,83,80,0.12);border:1px solid #ef5350;border-radius:8px;padding:8px 12px;margin-bottom:6px;">'
                f'<div style="font-size:11px;color:#ef5350;font-weight:600;">🔥 版本T0警告</div>'
                f'<div style="font-size:13px;color:#ff8a80;font-weight:700;margin-top:2px;">敌方有 {" · ".join(t0_names)}</div>'
                f'<div style="font-size:10px;color:#ffab91;margin-top:2px;">别硬接！找单抓！</div>'
                f'</div>'
            )

        if role_build_formula:
            first_items = role_build_formula.get("first", [])
            if first_items and not next_item:
                kb_parts.append(
                    f'<div style="background:rgba(102,126,234,0.12);border:1px solid #667eea;border-radius:8px;padding:8px 12px;margin-bottom:6px;">'
                    f'<div style="font-size:11px;color:#a78bfa;font-weight:600;">📐 版本出装公式</div>'
                    f'<div style="font-size:13px;color:#c8d6e5;margin-top:2px;">首出: {" / ".join(first_items[:2])}</div>'
                    f'</div>'
                )

        if my_kb_specific and my_kb_specific.get("best_runes"):
            runes_short = " · ".join(my_kb_specific["best_runes"][:3])
            kb_parts.append(
                f'<div style="background:rgba(255,213,79,0.12);border:1px solid #ffd54f;border-radius:8px;padding:8px 12px;margin-bottom:6px;">'
                f'<div style="font-size:11px;color:#ffd54f;font-weight:600;">✨ 专属强化符文</div>'
                f'<div style="font-size:12px;color:#c8d6e5;margin-top:2px;">{runes_short}</div>'
                f'</div>'
            )

        if kb_snowball_tip:
            kb_parts.append(
                f'<div style="background:rgba(67,206,162,0.1);border:1px solid #43cea2;border-radius:8px;padding:8px 12px;margin-bottom:6px;">'
                f'<div style="font-size:11px;color:#43cea2;font-weight:600;">❄️ 雪球技巧</div>'
                f'<div style="font-size:12px;color:#c8d6e5;margin-top:2px;">{kb_snowball_tip}</div>'
                f'</div>'
            )

        meta_summary = kb.get("meta_summary", "")
        kb_version = kb.get("version", "")
        if meta_summary:
            kb_parts.append(
                f'<div style="font-size:9px;color:#556;margin-top:4px;text-align:center;">{kb_version} · {meta_summary[:30]}</div>'
            )

        if kb_parts:
            kb_section = f'''
            <div style="margin-top:10px;">
                {''.join(kb_parts)}
            </div>'''

    mm = int(game_time // 60)
    ss = int(game_time % 60)

    return f"""
<div style="font-family:-apple-system,'Segoe UI',sans-serif;color:#c8d6e5;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <span style="font-size:13px;color:#8899aa;">⏱️ {mm}:{ss} · Lv.{level}</span>
        <span style="font-size:13px;color:#8899aa;">💰 {gold}G</span>
    </div>

    <div style="background:{situation_color};border-radius:12px;padding:14px;text-align:center;color:white;">
        <div style="font-size:14px;opacity:0.9;">{situation_label}</div>
        <div style="font-size:28px;font-weight:800;margin:4px 0;">{action}</div>
        <div style="font-size:12px;opacity:0.9;">🟢 {my_alive} vs 🔴 {enemy_alive} · KDA {my_kda}</div>
    </div>

    {build_section}
    {threat_section}
    {situation_section}
    {action_section}
    {kb_section}

    <div style="margin-top:10px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;">
        <div style="background:#1e2d3d;border-radius:8px;padding:8px;text-align:center;">
            <div style="font-size:10px;color:#888;">HP</div>
            <div style="font-size:14px;font-weight:700;">{int(stats.get('hp',0))}</div>
        </div>
        <div style="background:#1e2d3d;border-radius:8px;padding:8px;text-align:center;">
            <div style="font-size:10px;color:#888;">法强AP</div>
            <div style="font-size:14px;font-weight:700;color:#bb86fc;">{stats.get('ap',0)}</div>
        </div>
        <div style="background:#1e2d3d;border-radius:8px;padding:8px;text-align:center;">
            <div style="font-size:10px;color:#888;">技能急速</div>
            <div style="font-size:14px;font-weight:700;color:#43cea2;">{stats.get('ability_haste',0)}</div>
        </div>
    </div>
</div>"""


def compute_matchup_analysis(my_team: list, enemy_team: list,
                              my_coach: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    analysis = {
        "enemy_ap_count": 0,
        "enemy_ad_count": 0,
        "enemy_roles": {},
        "tips": [],
        "item_advs": [],
    }

    for m in enemy_team:
        coach = m.get("coach")
        if not coach:
            analysis["enemy_ad_count"] += 1
            continue
        role = coach.get("role", "")
        analysis["enemy_roles"][role] = analysis["enemy_roles"].get(role, 0) + 1
        if role in ("法师", "辅助"):
            analysis["enemy_ap_count"] += 1
        elif role in ("射手", "ADC", "战士", "刺客"):
            analysis["enemy_ad_count"] += 1
        elif role == "坦克":
            analysis["enemy_ad_count"] += 1

    ap_ratio = analysis["enemy_ap_count"] / max(1, len(enemy_team))
    if ap_ratio >= 0.6:
        analysis["item_advs"].append("对面AP伤害占比≥60%，第二件考虑⇒ 自然之力 / 振奋盔甲")
        analysis["tips"].append("AP多：保持移动，别被持续范围技能命中")
    elif ap_ratio <= 0.3:
        analysis["item_advs"].append("对面AD伤害占比≥70%，第二件考虑⇒ 兰顿之兆 / 荆棘之甲")
        analysis["tips"].append("AD多：注意走位躲关键控制技能")
    else:
        analysis["item_advs"].append("对面AP/AD混合输出，均衡出装即可")

    enemy_mages = []
    enemy_cc = []
    for m in enemy_team:
        coach = m.get("coach")
        if not coach:
            continue
        name = coach.get("name", "")
        role = coach.get("role", "")
        if role == "法师":
            enemy_mages.append(name)
        if name in ("拉克丝", "泽拉斯", "龙龟", "墨菲特", "石头人", "妮蔻", "发条", "阿狸"):
            enemy_cc.append(name)

    if enemy_mages:
        analysis["tips"].append(f"敌方法师: {', '.join(enemy_mages)}，小心他们的远程爆发")
    if enemy_cc:
        analysis["tips"].append(f"敌方强控制: {', '.join(enemy_cc)}，建议带净化或提前预判")

    enemy_tanks = [m["champion_name"] for m in enemy_team if (m.get("coach") or {}).get("role") == "坦克"]
    if enemy_tanks:
        analysis["tips"].append(f"敌方有肉坦克({', '.join(enemy_tanks)})，先集火脆皮")

    return analysis


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


def analyze_item_status(recommended_build: list, owned_item_ids: list) -> Dict[str, Any]:
    if not recommended_build:
        return {"owned": [], "next": None, "missing": [], "owned_items_data": []}

    owned_names = []
    for iid in owned_item_ids:
        name = ITEM_MAP.get(str(iid), {}).get("name")
        if name:  # 空名会让下面的 `owned in rec_item` 恒为真
            owned_names.append(name)

    owned_in_build = []
    for rec_item in recommended_build:
        for owned in owned_names:
            if rec_item == owned or rec_item in owned or owned in rec_item:
                if rec_item not in owned_in_build:
                    owned_in_build.append(rec_item)

    missing = [it for it in recommended_build if it not in owned_in_build]
    next_item = missing[0] if missing else None

    next_item_cost = 0
    if next_item:
        # 同名的还有斗魂竞技场版本（22xxxx，价格不同），取 ID 最短的常规版本
        matches = [(len(iid), iid) for iid, info in ITEM_MAP.items() if info["name"] == next_item]
        if matches:
            next_item_cost = ITEM_MAP[min(matches)[1]]["gold_total"]

    return {
        "owned_in_build": owned_in_build,
        "next_item": next_item,
        "next_item_cost": next_item_cost,
        "remaining": missing,
        "owned_count": len(owned_in_build),
        "total_count": len(recommended_build),
    }


def compute_composite_advice(state: Dict[str, Any]) -> Dict[str, Any]:
    advice: Dict[str, Any] = {
        "phase": state.get("phase", "None"),
        "score": 50,
        "score_breakdown": {},
        "immediate_action": "",
        "next_priority": "",
        "suggestions": [],
        "build_recommendation": {},
        "teamfight_readiness": None,
        "gold_tip": "",
        "survival_tip": "",
    }

    my_coach = state.get("my_coach")
    my_team = state.get("my_team") or []
    enemy_team = state.get("enemy_team") or []
    phase = state.get("phase", "None")
    lcd = state.get("lcd")

    suggestions = []
    score_breakdown: Dict[str, float] = {}

    if my_coach:
        base_strength = my_coach.get("aram_strength", 65)
    else:
        base_strength = 50
    score_breakdown["英雄强度"] = base_strength

    team_fit, fit_notes = evaluate_team_fit(my_team, my_coach, state.get("my_champion_id"))
    fit_prefix = {"gap": "", "fill": "✅ ", "dup": "⚠️ "}
    suggestions.extend(fit_prefix[kind] + text for kind, text in fit_notes)
    score_breakdown["阵容适配"] = team_fit

    counter_score = 50
    if my_coach and enemy_team:
        counter_score = compute_counter_score(
            my_coach.get("role", ""), my_coach.get("name", ""), enemy_team
        )
        if counter_score >= 70:
            suggestions.append("🎯 英雄克制敌方阵容，对线有优势")
        elif counter_score <= 35:
            suggestions.append("⚠️ 英雄被敌方克制，需要靠操作弥补")
    score_breakdown["敌方克制"] = counter_score

    reroll_weight = 0.4
    fit_weight = 0.3
    counter_weight = 0.3
    composite = int(
        base_strength * reroll_weight +
        team_fit * fit_weight +
        counter_score * counter_weight
    )
    advice["score"] = composite
    advice["score_breakdown"] = score_breakdown

    if phase == "ChampSelect":
        remaining = state.get("reroll_advice", {}).get("remaining_dice", 2)
        if remaining <= 0:
            advice["immediate_action"] = "骰子用完了，接受命运吧"
        elif composite < 50:
            advice["immediate_action"] = "🎲 强烈建议骰子重随！综合分太低"
        elif composite < 65:
            advice["immediate_action"] = "🤔 综合分一般，可以看看有没有更好的选择"
        else:
            advice["immediate_action"] = "✅ 这个英雄挺好的，保留"

    elif phase in ("InProgress", "GameStart", "Reconnect"):
        if lcd and lcd.get("available"):
            active = lcd.get("active", {})
            gold = active.get("gold", 0)
            level = active.get("level", 1)
            stats = active.get("champion_stats", {})
            hp = stats.get("hp", 0)
            max_hp = stats.get("max_hp", 1)
            hp_pct = hp / max_hp if max_hp > 0 else 0
            gold_tip = ""
            if gold < 300:
                gold_tip = "金币不足300，别乱花钱，等大装"
            elif gold < 1000:
                gold_tip = "可以买个小件过渡（鞋/短剑/增幅典籍）"
            elif gold >= 2000:
                gold_tip = "💰 金币充足！赶紧回城合大件"
            else:
                gold_tip = "金币够买一些核心组件了"
            advice["gold_tip"] = gold_tip

            survival_tip = ""
            if hp_pct < 0.2:
                survival_tip = "⚠️ 血量危险！别冲，找队友/雪球撤退"
            elif hp_pct < 0.4 and level < 6:
                survival_tip = "💡 血量低且等级不高，以发育为主"
            elif hp_pct > 0.8 and gold > 1500:
                survival_tip = "💪 状态好金币多，找机会打一波然后回城"
            else:
                survival_tip = "👍 状态正常，正常打"
            advice["survival_tip"] = survival_tip

            team_map = lcd.get("team_map", {})
            my_list, enemy_list = resolve_my_enemy_teams(team_map)
            my_alive = sum(1 for p in my_list if not p.get("is_dead"))
            enemy_alive = sum(1 for p in enemy_list if not p.get("is_dead"))
            teamfight_ready = {
                "my_alive": my_alive,
                "enemy_alive": enemy_alive,
                "advantage": my_alive - enemy_alive,
            }
            advice["teamfight_readiness"] = teamfight_ready

            if my_alive > enemy_alive:
                suggestions.append(f"🟢 我方多 {my_alive - enemy_alive} 人存活，可以主动开团")
            elif enemy_alive > my_alive:
                suggestions.append(f"🔴 敌方多 {enemy_alive - my_alive} 人存活，避战等队友复活")

            owned_ids = []
            for p in my_list:
                if p.get("is_me"):
                    owned_ids = [it.get("item_id") for it in p.get("items", []) if it.get("item_id")]
                    break

            if my_coach:
                build_status = analyze_item_status(my_coach.get("build", []), owned_ids)
                advice["build_recommendation"] = build_status

                next_item = build_status.get("next_item")
                next_cost = build_status.get("next_item_cost", 0)
                if next_item:
                    if gold >= next_cost and next_cost > 0:
                        advice["next_priority"] = f"🟢 回城合装：{next_item}（差 {next_cost - gold}G）" if gold < next_cost else f"💰 回城买 {next_item}！金币够了"
                    elif gold >= next_cost * 0.7:
                        advice["next_priority"] = f"🔜 攒钱买 {next_item}（还差 {next_cost - gold:.0f}G）"
                    else:
                        advice["next_priority"] = f"📦 规划：{next_item}（约 {next_cost}G）"
                    suggestions.append(advice["next_priority"])

            matchup = state.get("matchup_analysis", {})
            if matchup:
                item_advs = matchup.get("item_advs", [])
                for adv in item_advs:
                    suggestions.append(f"🛡️ {adv}")

        else:
            matchup = state.get("matchup_analysis", {})
            if matchup:
                for adv in matchup.get("item_advs", []):
                    suggestions.append(f"🛡️ {adv}")

    advice["suggestions"] = suggestions[:8]
    return advice


async def poll_lcu():
    last_phase = None
    last_champ_id = None
    last_rolls_used = None
    last_level = None
    _debug_count = 0
    while True:
        try:
            await lcu.wait_for_connection(poll_interval=3.0)
            state = await collect_game_state()

            if state["phase"] != last_phase:
                log.info(f"Phase changed: {last_phase} -> {state['phase']}")
                last_phase = state["phase"]
                last_champ_id = None
                last_rolls_used = None
                last_level = None
                _debug_count = 0
                await broadcast({"type": "phase_change", "data": state})

            if state["phase"] == "ChampSelect":
                cur_champ = state.get("my_champion_id")
                cur_rolls = state.get("reroll_remaining")

                _debug_count += 1
                if _debug_count <= 5 or cur_champ != last_champ_id:
                    log.info(f"[DEBUG ChampSelect #{_debug_count}] my_champion_id={cur_champ}, reroll_remaining={cur_rolls}, last_champ={last_champ_id}, my_team_count={len(state.get('my_team') or [])}")

                if last_champ_id is not None and cur_champ is not None and cur_champ != last_champ_id:
                    log.info(f"🎲 英雄骰子重随! {last_champ_id} -> {cur_champ} (剩余骰子={cur_rolls})")
                    await broadcast({
                        "type": "dice_rolled",
                        "data": {
                            "old_champion_id": last_champ_id,
                            "new_champion_id": cur_champ,
                            "new_champion_name": state.get("my_champion_name"),
                            "remaining_dice": cur_rolls,
                            "advice": state.get("reroll_advice"),
                            "composite": state.get("composite_advice"),
                        }
                    })
                if cur_champ and cur_champ != 0:
                    last_champ_id = cur_champ

            if state["phase"] in ("InProgress", "GameStart"):
                active = (state.get("lcd") or {}).get("active") or {}
                cur_level = active.get("level")
                if last_level is not None and cur_level is not None and cur_level > last_level:
                    log.info(f"⭐ 升级! Lv.{last_level} -> Lv.{cur_level} (可能触发强化符文选择)")
                    await broadcast({
                        "type": "level_up",
                        "data": {
                            "old_level": last_level,
                            "new_level": cur_level,
                            "composite": state.get("composite_advice"),
                            "gold": active.get("gold"),
                        }
                    })
                last_level = cur_level

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
async def coach_page():
    return FileResponse(str(BASE_DIR / "frontend" / "coach.html"))


@app.get("/deep")
async def deep_page():
    return FileResponse(str(BASE_DIR / "frontend" / "deep.html"))


@app.get("/api/state")
async def api_state():
    return await collect_game_state()


@app.get("/api/coach/ask")
async def api_coach_ask(q: str = ""):
    state = await collect_game_state()

    async def generate():
        try:
            async for chunk in llm_client.analyze(state, question=q):
                yield chunk
        except Exception as e:
            log.exception("Coach stream error")
            yield f"\n\n❌ 内部错误: {str(e)[:200]}"

    return StreamingResponse(generate(), media_type="text/event-stream")


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


@app.get("/api/coach/quick")
async def api_coach_quick():
    try:
        state = await collect_game_state()
        html = generate_quick_html(state)
        return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(content=f"<div style='color:#ef5350;padding:20px;'>❌ 生成失败: {e}</div>")


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

    gold_needed = 0
    next_item = ""
    if coach_build and gold > 0:
        for item_name in coach_build:
            if item_name not in my_items:
                for item_key, item_data in ITEM_MAP.items():
                    if item_data.get("name") == item_name:
                        gold_needed = item_data.get("gold", 0)
                        break
                next_item = item_name
                break

    if next_item and gold_needed:
        lines.append("")
        lines.append(f"下一件: 【{next_item}】 需 {gold_needed}G，你有 {gold}G")
        if gold >= gold_needed:
            lines.append("  ✅ 金币充足，回城立即出！")
        else:
            lines.append(f"  还需 {gold_needed - gold}G，大概 {max(1, (gold_needed - gold) // 25)} 波兵")

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
    lines.append("  • 雪球飞一半按回城：骗对面走位")
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

    lines.append("【兵线与经济】")
    lines.append(f"  • 你有 {gold}G，回城优先级：大件 > 小件 > 药水")
    if gold < 500:
        lines.append("  • 金币太少，清兵/助攻攒一波再回")
    elif gold > 2000:
        lines.append("  • 金币很多了，别贪，回城把装备拉满")
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


@app.get("/api/coach/deep")
async def api_coach_deep():
    try:
        state = await collect_game_state()
        if llm_client.enabled:
            log.info("Deep analysis: using LLM (%s)", llm_client.model)
            chunks = []
            async for chunk in llm_client.analyze(state, kb=KNOWLEDGE_BASE):
                chunks.append(chunk)
            text = "".join(chunks)
            return {"analysis": text, "mode": "llm", "model": llm_client.model}
        else:
            log.info("Deep analysis: LLM not configured, fallback to rules")
            text = _deep_analysis(state)
            return {"analysis": text, "mode": "rules"}
    except Exception as e:
        log.exception("Deep analysis failed")
        return {"analysis": f"❌ 分析失败: {e}"}


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
        initial_state = await collect_game_state()
        await ws.send_json({"type": "initial", "data": initial_state})
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
