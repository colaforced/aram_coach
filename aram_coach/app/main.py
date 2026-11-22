import os
import json
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
import uvicorn

from app.lcu_client import LCUClient
from app import lcd_client
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

ITEM_MAP: Dict[str, Dict[str, Any]] = {}
_item_map_path = BASE_DIR / "data" / "item_map.json"
if _item_map_path.exists():
    with open(_item_map_path, "r", encoding="utf-8") as f:
        ITEM_MAP = json.load(f)

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

_champion_roles = {}
for _cid, _cdata in CHAMPIONS_DB.items():
    _role = _cdata.get("role", "")
    _champion_roles[_cid] = _role
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

@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Starting ARAM Coach...")
    asyncio.create_task(poll_lcu())
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
    return {
        "character_id": character_id,
        "name": name,
        "role": data.get("role", "未知"),
        "aram_strength": data.get("aram_strength", 65),
        "primary_rune": data.get("primary_rune", ""),
        "primary_branch": data.get("primary_branch", ""),
        "primary_details": data.get("primary_details", []),
        "secondary_rune": data.get("secondary_rune", ""),
        "secondary_branch": data.get("secondary_branch", ""),
        "build": data.get("build", []),
        "core_cost": data.get("core_cost", 0),
        "situational": data.get("situational", {}),
        "tips": data.get("tips", []),
        "snowball_tip": data.get("snowball_tip", ""),
        "flash_tip": data.get("flash_tip", ""),
    }


ROLE_MAP = {
    "坦克": "tank", "战士": "fighter", "刺客": "assassin",
    "法师": "mage", "射手": "marksman", "辅助": "support",
}


def compute_reroll_advice(my_team: list, my_coach: Optional[Dict[str, Any]],
                          remaining_dice: int) -> Dict[str, Any]:
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

    role_counts = {"tank": 0, "mage": 0, "marksman": 0, "assassin": 0, "fighter": 0, "support": 0}
    for m in my_team:
        coach = m.get("coach")
        if not coach:
            continue
        role = ROLE_MAP.get(coach.get("role", ""), "")
        if role in role_counts:
            role_counts[role] += 1

    has_tank = role_counts["tank"] > 0 or role_counts["fighter"] >= 2
    has_mage = role_counts["mage"] > 0 or role_counts["assassin"] > 0
    has_adc = role_counts["marksman"] > 0 or role_counts["fighter"] > 0

    fitness = 50
    reasons = []
    if not has_tank:
        fitness -= 15
        reasons.append("阵容缺前排（坦克/重装战士）")
    if not has_mage:
        fitness -= 10
        reasons.append("阵容缺AP输出")
    if not has_adc:
        fitness -= 5
        reasons.append("阵容缺持续输出射手")

    if my_coach:
        my_role = ROLE_MAP.get(my_coach.get("role", ""), "")
        if my_role == "tank" and not has_tank:
            fitness += 25
            reasons.append("你来补前排缺口，完美契合")
        elif my_role in ("mage", "assassin") and not has_mage:
            fitness += 20
            reasons.append("你来补AP缺口，完美契合")
        elif my_role == "marksman" and not has_adc:
            fitness += 15
            reasons.append("你来补持续输出，挺好")
        elif role_counts[my_role] >= 2:
            fitness -= 15
            reasons.append("这个位置阵容里已经有2个了，过于重复")

    fitness = max(0, min(100, fitness))
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
                state["my_team"], state.get("my_coach"), remaining_dice
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

        lcd_raw = lcd_client.fetch_allgamedata()
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

    all_p = lcd_raw.get("allPlayers", [])
    team_map: Dict[str, Dict[str, Any]] = {}
    my_summoner = state.get("summoner_name", "")
    for p in all_p:
        name = p.get("summonerName", "")
        team = p.get("team", "")
        scores = p.get("scores", {})
        items = []
        for it in p.get("items", []):
            items.append({
                "name": it.get("displayName", ""),
                "item_id": it.get("itemID"),
                "price": it.get("price", 0),
                "slot": it.get("slot"),
            })
        entry = {
            "name": name,
            "team": team,
            "champion": p.get("championName", ""),
            "is_dead": p.get("isDead", False),
            "respawn_timer": round(p.get("respawnTimer", 0), 1),
            "level": p.get("level"),
            "gold": round(p.get("currentGold", 0), 0),
            "kills": scores.get("kills", 0),
            "deaths": scores.get("deaths", 0),
            "assists": scores.get("assists", 0),
            "kda": round(
                (scores.get("kills", 0) + scores.get("assists", 0)) /
                max(scores.get("deaths", 1), 1), 2
            ),
            "gold_earned": int(scores.get("goldEarned", 0)),
            "gold_spent": int(scores.get("goldSpent", 0)),
            "items_purchased": scores.get("itemsPurchased", 0),
            "minions_killed": scores.get("minionsKilled", 0),
            "neutral_minions": scores.get("neutralMinionsKilled", 0),
            "turrets_killed": scores.get("turretsKilled", 0),
            "ward_placed": scores.get("wardPlaced", 0),
            "ward_killed": scores.get("wardKilled", 0),
            "total_damage_champs": int(scores.get("totalDamageDealtToChampions", 0)),
            "total_damage_taken": int(scores.get("totalDamageTaken", 0)),
            "largest_multi_kill": scores.get("largestMultiKill", 0),
            "largest_killing_spree": scores.get("largestKillingSpree", 0),
            "items": items,
            "item_count": len([i for i in items if i.get("name")]),
            "is_me": name == my_summoner,
        }
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

    team_map = lcd.get("team_map") or {}
    order = team_map.get("ORDER", [])
    chaos = team_map.get("CHAOS", [])

    my_total = sum(int(p.get("gold", 0)) for p in order)
    enemy_total = sum(int(p.get("gold", 0)) for p in chaos)
    diff = my_total - enemy_total
    lines.append(f"## 💰 团队金币差")
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
            gold = int(p.get("gold", 0))
            dead = p.get("is_dead", False)
            respawn = p.get("respawn_timer", 0)
            dmg = int(p.get("total_damage_champs", 0))
            items = [it.get("name", "") for it in p.get("items", []) if it.get("name")]
            items_str = ", ".join(items) if items else "—"
            me = " 👉 **(你)**" if p.get("is_me") else ""
            dead_tag = f" 💀复活{respawn}s" if dead else ""
            rows.append(f"| {emoji} {name}{me} | {champ} | Lv.{lv} | {k}/{d}/{a} | {gold}G | {dmg} | {items_str}{dead_tag} |")

        lines.append(f"## {label}")
        lines.append("")
        lines.append("| 玩家 | 英雄 | 等级 | KDA | 金币 | 对英雄伤害 | 装备 |")
        lines.append("|------|------|------|-----|------|-----------|------|")
        lines.extend(rows)
        lines.append("")

    if order:
        _fmt_team(order, "🟢 我方阵容", "🟢")
    if chaos:
        _fmt_team(chaos, "🔴 敌方阵容", "🔴")

    if state.get("my_coach"):
        coach = state["my_coach"]
        lines.append("## 🧠 英雄教练数据")
        lines.append("")
        if coach.get("primary_branch"):
            lines.append(f"**符文**: {coach.get('primary_branch')} - {coach.get('primary_rune')} / {coach.get('secondary_branch')}")
        if coach.get("build"):
            lines.append(f"**推荐出装**: {' → '.join(coach['build'])}")
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

    team_map = lcd.get("team_map") or {}
    order = team_map.get("ORDER", [])
    chaos = team_map.get("CHAOS", [])

    my_alive = sum(1 for p in order if not p.get("is_dead"))
    enemy_alive = sum(1 for p in chaos if not p.get("is_dead"))
    alive_diff = my_alive - enemy_alive

    my_total_gold = sum(int(p.get("gold", 0)) for p in order)
    enemy_total_gold = sum(int(p.get("gold", 0)) for p in chaos)
    gold_diff = my_total_gold - enemy_total_gold

    me = None
    for p in order:
        if p.get("is_me"):
            me = p
            break
    if not me and order:
        me = order[0]

    my_kda = f"{me.get('kills',0)}/{me.get('deaths',0)}/{me.get('assists',0)}" if me else "?"

    coach = state.get("my_coach") or {}
    build = coach.get("build") or []

    owned_items = [it.get("name","") for it in (active.get("items") or []) if it.get("name")]
    next_item = None
    for b in build:
        if b not in owned_items:
            next_item = b
            break

    tips = []
    composite = state.get("composite_advice") or {}
    if composite.get("tips"):
        tips.extend(composite["tips"][:3])
    matchup = state.get("matchup_analysis") or {}
    if matchup.get("tips"):
        tips.extend(matchup["tips"][:2])

    threats = []
    for p in chaos:
        if not p.get("is_dead"):
            kd = p.get("kills",0) - p.get("deaths",0)
            if kd >= 2 or p.get("is_carry"):
                threats.append(p)
    threats = sorted(threats, key=lambda x: x.get("kills",0), reverse=True)[:3]

    if alive_diff >= 2:
        situation_color = "#43cea2"
        situation_label = "🟢 大优势"
        action = "主动开团推塔！"
    elif alive_diff == 1:
        situation_color = "#43cea2"
        situation_label = "🟢 小优"
        action = "稳扎稳打继续压"
    elif alive_diff == 0:
        situation_color = "#f5b041"
        situation_label = "🟡 均势"
        action = "等技能CD再打"
    elif alive_diff == -1:
        situation_color = "#ef5350"
        situation_label = "🔴 小劣"
        action = "避战等队友复活！"
    else:
        situation_color = "#ef5350"
        situation_label = "🔴 大劣"
        action = "别团！苟住！"

    if gold_diff > 500:
        situation_label += " 💰"
    elif gold_diff < -500:
        situation_label += " 💸"

    next_item_line = ""
    if next_item:
        next_item_line = f"""
        <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:10px;padding:12px 16px;margin-top:10px;color:white;">
            <div style="font-size:11px;opacity:0.8;">🛒 下一件建议</div>
            <div style="font-size:22px;font-weight:700;margin-top:2px;">{next_item}</div>
            <div style="font-size:12px;opacity:0.9;margin-top:4px;">你有 <b>{gold}G</b> · 还差 <b style="color:#ffd54f">?</b></div>
        </div>"""

    threat_html = ""
    if threats:
        threat_cards = []
        for t in threats:
            dead_tag = f' <span style="color:#888">💀复活{t.get("respawn_timer",0):.0f}s</span>' if t.get("is_dead") else ""
            threat_cards.append(
                f'<span style="background:rgba(239,83,80,0.15);border:1px solid rgba(239,83,80,0.5);border-radius:6px;padding:4px 10px;font-size:12px;color:#ff8a80;">'
                f'{t.get("champion","?")} K{t.get("kills",0)}</span>{dead_tag}'
            )
        threat_html = f'<div style="margin-top:10px;"><div style="font-size:11px;color:#888;margin-bottom:4px;">⚠️ 敌方威胁</div>{" ".join(threat_cards)}</div>'

    tips_html = ""
    if tips:
        tip_items = "".join(f'<div style="font-size:12px;color:#c8d6e5;padding:2px 0;">💡 {t}</div>' for t in tips[:4])
        tips_html = f'<div style="background:rgba(67,206,162,0.08);border-radius:8px;padding:10px 12px;margin-top:10px;">{tip_items}</div>'

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

    {next_item_line}
    {threat_html}
    {tips_html}

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
        info = ITEM_MAP.get(str(iid), {})
        owned_names.append(info.get("name", ""))

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
        for iid, info in ITEM_MAP.items():
            if info["name"] == next_item:
                next_item_cost = info["gold_total"]
                break

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

    role_counts = {"tank": 0, "mage": 0, "marksman": 0, "assassin": 0, "fighter": 0, "support": 0}
    for m in my_team:
        coach = m.get("coach")
        if coach:
            role = ROLE_MAP.get(coach.get("role", ""), "")
            if role in role_counts:
                role_counts[role] += 1

    my_role = ROLE_MAP.get(my_coach.get("role", ""), "") if my_coach else ""
    has_tank = role_counts["tank"] > 0 or role_counts["fighter"] >= 2
    has_mage = role_counts["mage"] > 0 or role_counts["assassin"] > 0
    has_adc = role_counts["marksman"] > 0 or role_counts["fighter"] > 0

    team_fit = 50
    if not has_tank:
        team_fit -= 15
        suggestions.append("阵容缺前排（坦克/重装战士），注意抱团别分散")
    if not has_mage:
        team_fit -= 10
        suggestions.append("阵容缺AP输出，后期可能打不动肉")
    if not has_adc:
        team_fit -= 5
        suggestions.append("阵容缺持续输出射手")

    if my_coach:
        if my_role == "tank" and not has_tank:
            team_fit += 25
            suggestions.append("✅ 你补了前排缺口，完美契合阵容")
        elif my_role in ("mage", "assassin") and not has_mage:
            team_fit += 20
            suggestions.append("✅ 你补了AP缺口，完美契合阵容")
        elif my_role == "marksman" and not has_adc:
            team_fit += 15
            suggestions.append("✅ 你补了持续输出缺口")
        elif role_counts[my_role] >= 2:
            team_fit -= 15
            suggestions.append("⚠️ 同位置已有2人，略显重复")
    team_fit = max(0, min(100, team_fit))
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
            my_list = team_map.get("ORDER", [])
            enemy_list = team_map.get("CHAOS", [])
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
            for p in lcd.get("all_players", []):
                if p.get("summonerName") == state.get("summoner_name", ""):
                    owned_ids = [it.get("itemID") for it in p.get("items", []) if it.get("itemID")]
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


@app.get("/api/coach/status")
async def api_coach_status():
    return {
        "enabled": llm_client.enabled,
        "model": llm_client.model,
        "base_url": llm_client.base_url,
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
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(content=f"<div style='color:#ef5350;padding:20px;'>❌ 生成失败: {e}</div>")


@app.get("/api/lcu/status")
async def api_lcu_status():
    return {
        "connected": lcu.connected,
        "lockfile": lcu.lockfile_path,
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
    for cid, data in CHAMPIONS_DB.items():
        if not q or q.lower() in data.get("name", "").lower():
            results.append({
                "character_id": int(cid),
                "name": data.get("name"),
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


if __name__ == "__main__":
    port = CONFIG.get("web_port", 8765)
    log.info(f"Web UI: http://127.0.0.1:{port}/")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
