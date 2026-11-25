"""用假的 LCU / LCD 数据启动 ARAM Coach（没有 LoL 客户端也能看到选人、对局界面）。

运行: python .claude/skills/run-aram-coach/mock_run.py
环境变量: MOCK_PORT（默认 8799，避开正式端口 8765）、MOCK_PHASE（初始阶段，默认 ChampSelect）
切换场景: POST /mock/phase/{None|ChampSelect|InProgress|EndOfGame}
（InProgress → EndOfGame 会触发对局记录拉战绩、生成 game_logs/<时间>/review.md；战绩接口返回的也是假数据）
模拟识别到强化符文三选一: POST /mock/augments?names=炼狱导管,坦克引擎,冰寒

只替换 main.lcu 和 lcd_client.fetch_allgamedata，其余（规则引擎、数据同步、LLM）都是真的。
"""
import os
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[3])
sys.path.insert(0, REPO)
os.chdir(REPO)

import win_compat  # noqa: F401,E402

import asyncio  # noqa: E402

import uvicorn  # noqa: E402

from app import main, lcd_client  # noqa: E402

PORT = int(os.environ.get("MOCK_PORT", "8799"))
SCENE = {"phase": os.environ.get("MOCK_PHASE", "ChampSelect")}

ME = "大乱斗教练"
SUMMONER = {
    "gameName": ME, "displayName": ME, "puuid": "p-me", "summonerLevel": 238,
    "rerollPoints": {"maxRolls": 2, "numberOfRolls": 1},
}

# 选人：我拿到盖伦，备选席有火男和科加斯
CHAMP_SELECT = {
    "localPlayerCellId": 2,
    "myTeam": [
        {"cellId": 0, "championId": 412},
        {"cellId": 1, "championId": 201},
        {"cellId": 2, "championId": 86},
        {"cellId": 3, "championId": 99},
        {"cellId": 4, "championId": 122},
    ],
    "theirTeam": [],
    "actions": [],
    "benchEnabled": True,
    "benchChampions": [{"championId": 63}, {"championId": 31}, {"championId": 22}],
}

MY_TEAM = [("p-me", 63, ME), ("p2", 412, "锤石不空大"), ("p3", 201, "门神布隆"), ("p4", 99, "光辉之子"), ("p5", 122, "诺手五杀")]
ENEMY = [("e1", 16, "奶妈在此"), ("e2", 516, "山隐之焰"), ("e3", 14, "赛恩冲冲冲"), ("e4", 110, "韦鲁斯"), ("e5", 55, "卡特琳娜")]

GAME_ID = 8800001234
GAMEFLOW = {
    "gameData": {
        "gameId": GAME_ID,
        "teamOne": [{"puuid": p, "championId": c} for p, c, _ in MY_TEAM],
        "teamTwo": [{"puuid": p, "championId": c} for p, c, _ in ENEMY],
        "playerChampionSelections": [{"puuid": p, "championId": c} for p, c, _ in MY_TEAM + ENEMY],
    }
}

ITEM_NAMES = {
    6653: "兰德里的折磨", 3020: "法师之靴", 1026: "爆裂魔杖", 1052: "增幅典籍", 3165: "莫雷洛秘典",
    3068: "日炎圣盾", 3075: "荆棘之甲", 3047: "铁板靴", 3111: "水银之靴", 6655: "卢登的配枪",
    3157: "中娅沙漏", 3078: "三相之力", 3053: "斯特拉克的挑战护手", 3143: "兰顿之兆", 3071: "黑色切割者",
    3042: "魔切", 3115: "纳什之牙", 3006: "狂战士胫甲", 3916: "湮灭宝珠", 3084: "心之钢",
}


def items(*ids):
    return [{"itemID": i, "displayName": ITEM_NAMES[i], "price": 0, "count": 1, "slot": n}
            for n, i in enumerate(ids)]


def player(name, champ, team, lvl, k, d, a, cs, its, dead=False, timer=0.0):
    return {
        "riotId": f"{name}#ARAM", "riotIdGameName": name, "summonerName": f"{name}#ARAM",
        "championName": main.resolve_champion_name(champ) or "", "team": team, "level": lvl,
        "isDead": dead, "respawnTimer": timer,
        "scores": {"kills": k, "deaths": d, "assists": a, "creepScore": cs, "wardScore": 0},
        "items": its,
    }


# 赛后战绩：每人最终真实经济（自己的 = 装备 5350 + 现金 1730；敌方攒着不少没花的钱）
GAME_LEN = 742.5
REAL_GOLD = {63: 7080, 412: 4700, 201: 6900, 99: 7400, 122: 8200,
             16: 5200, 516: 7600, 14: 5600, 110: 8300, 55: 7900}
PIDS = [c for _, c, _ in MY_TEAM + ENEMY]   # participantId = 下标 + 1，前 5 个是 100 队


def match_data():
    return {
        "gameId": GAME_ID, "gameMode": "KIWI", "gameDuration": int(GAME_LEN),
        "participants": [{"participantId": i + 1, "teamId": 100 if i < 5 else 200, "championId": c,
                          "stats": {"goldEarned": REAL_GOLD[c], "win": i < 5}} for i, c in enumerate(PIDS)],
        "participantIdentities": [{"participantId": i + 1, "player": {"gameName": n, "puuid": p}}
                                  for i, (p, _, n) in enumerate(MY_TEAM + ENEMY)],
    }


def timeline_data():
    # 从 1400 线性涨到最终经济；身上的钱按 30% 算
    frames = []
    for t in list(range(0, int(GAME_LEN), 60)) + [GAME_LEN]:
        f = {}
        for i, c in enumerate(PIDS):
            total = 1400 + (REAL_GOLD[c] - 1400) * t / GAME_LEN
            f[str(i + 1)] = {"participantId": i + 1, "totalGold": int(total), "currentGold": int(total * 0.3)}
        frames.append({"timestamp": int(t * 1000), "participantFrames": f})
    return {"frames": frames}


def lcd_data():
    players = [
        player(ME, 63, "ORDER", 11, 7, 3, 12, 58, items(6653, 3020, 1026, 1052), dead=True, timer=14.6),
        player("锤石不空大", 412, "ORDER", 10, 1, 4, 17, 12, items(3068, 3047)),
        player("门神布隆", 201, "ORDER", 10, 0, 2, 15, 9, items(3075, 3111, 3084)),
        player("光辉之子", 99, "ORDER", 11, 5, 3, 14, 41, items(6655, 3020, 3157)),
        player("诺手五杀", 122, "ORDER", 11, 9, 5, 6, 47, items(3078, 3047, 3053)),
        player("奶妈在此", 16, "CHAOS", 10, 0, 4, 19, 10, items(6655, 3020)),
        player("山隐之焰", 516, "CHAOS", 11, 2, 3, 13, 22, items(3068, 3047, 3143)),
        player("赛恩冲冲冲", 14, "CHAOS", 10, 3, 6, 8, 19, items(3068, 3111)),
        player("韦鲁斯", 110, "CHAOS", 11, 6, 3, 9, 63, items(3042, 3020, 3115)),
        player("卡特琳娜", 55, "CHAOS", 11, 8, 6, 5, 38, items(6655, 3020, 3916)),
    ]
    events = [
        {"EventName": "GameStart", "EventTime": 0.03},
        {"EventName": "MinionsSpawning", "EventTime": 65.0},
        {"EventName": "FirstBlood", "EventTime": 92.4, "Recipient": ME},
        {"EventName": "ChampionKill", "EventTime": 92.4, "KillerName": ME, "VictimName": "卡特琳娜"},
        {"EventName": "ChampionKill", "EventTime": 248.1, "KillerName": "韦鲁斯", "VictimName": "锤石不空大"},
        {"EventName": "ChampionKill", "EventTime": 251.7, "KillerName": "诺手五杀", "VictimName": "赛恩冲冲冲"},
        {"EventName": "Multikill", "EventTime": 253.0, "KillerName": "诺手五杀", "KillStreak": 2},
        {"EventName": "FirstBrick", "EventTime": 402.6, "KillerName": "诺手五杀"},
        {"EventName": "TurretKilled", "EventTime": 402.6, "KillerName": "诺手五杀"},
        {"EventName": "ChampionKill", "EventTime": 571.3, "KillerName": ME, "VictimName": "奶妈在此"},
        {"EventName": "ChampionKill", "EventTime": 705.9, "KillerName": "光辉之子", "VictimName": "韦鲁斯"},
        {"EventName": "ChampionKill", "EventTime": 731.8, "KillerName": "卡特琳娜", "VictimName": ME},
    ]
    return {
        "gameData": {"gameTime": 742.5, "gameMode": "KIWI"},
        "activePlayer": {
            "riotId": f"{ME}#ARAM", "summonerName": f"{ME}#ARAM",
            "currentGold": 1730.0, "level": 11,
            "championStats": {
                "currentHealth": 0, "maxHealth": 1720, "attackDamage": 94, "abilityPower": 268,
                "armor": 71, "magicResist": 48, "attackSpeed": 0.721, "attackRange": 550, "moveSpeed": 380,
                "abilityHaste": 25, "magicPenetrationFlat": 12, "healthRegenRate": 4.2,
                "resourceType": "MANA", "resourceValue": 1320, "resourceMax": 1320, "resourceRegenRate": 9.8,
                "critDamage": 1.75,
            },
            "abilities": {
                "Passive": {"displayName": "炽热之焰"},
                "Q": {"abilityLevel": 3, "displayName": "火焰烙印"},
                "W": {"abilityLevel": 5, "displayName": "烈焰之柱"},
                "E": {"abilityLevel": 2, "displayName": "烈火燃烧"},
                "R": {"abilityLevel": 2, "displayName": "烈焰风暴"},
            },
        },
        "allPlayers": players,
        "events": {"Events": events},
    }


class FakeLCU:
    info = {"port": 0}
    connected = True

    async def get(self, path):
        phase = SCENE["phase"]
        if path == "/lol-gameflow/v1/gameflow-phase":
            return phase
        if path == "/lol-summoner/v1/current-summoner":
            return dict(SUMMONER, selectedChampionId=63 if phase == "InProgress" else 0)
        if path == "/lol-champ-select/v1/session":
            return CHAMP_SELECT
        if path == "/lol-gameflow/v1/session":
            return GAMEFLOW
        if path == "/lol-end-of-game/v1/eog-stats-block" and phase == "EndOfGame":
            return {"gameId": GAME_ID, "gameLength": int(GAME_LEN)}
        if path == f"/lol-match-history/v1/games/{GAME_ID}":
            return match_data()
        if path == f"/lol-match-history/v1/game-timelines/{GAME_ID}":
            return timeline_data()
        return None

    async def request(self, method, path, json=None):
        return 200, {}

    async def wait_for_connection(self, poll_interval=3.0):
        return

    async def close(self):
        pass


main.lcu = FakeLCU()
lcd_client.fetch_allgamedata = lambda: lcd_data() if SCENE["phase"] == "InProgress" else None


@main.app.post("/mock/phase/{name}")
async def mock_phase(name: str):
    SCENE["phase"] = name
    return {"phase": name}


@main.app.post("/mock/augments")
async def mock_augments(names: str = "炼狱导管,坦克引擎,冰寒"):
    """模拟截屏识别到三选一（真实识别只能在 Windows 上截屏）：推送和自动识别一模一样的消息。
    names 用逗号分隔，留空的位置表示那张没认出来。"""
    opts = [n.strip() or None for n in names.split(",")][:3]
    await main.broadcast({"type": "augment_options", "data": {"options": opts, "level": 3}})
    return {"options": opts}


main.log.info(f"MOCK Web UI: http://127.0.0.1:{PORT}/")
asyncio.run(uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="info")).serve())
