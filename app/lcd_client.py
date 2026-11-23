import json
import ssl
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

log = logging.getLogger("aram_coach.lcd")

LCD_URL = "https://127.0.0.1:2999"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def fetch(endpoint: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    url = LCD_URL + endpoint
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, context=_ssl_ctx, timeout=timeout)
        body = resp.read().decode("utf-8")
        return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError, ConnectionRefusedError, OSError, TimeoutError) as e:
        log.debug("LCD fetch %s failed: %s", endpoint, type(e).__name__)
        return None
    except Exception as e:
        log.debug("LCD fetch %s error: %s", endpoint, str(e)[:100])
        return None


def fetch_allgamedata() -> Optional[Dict[str, Any]]:
    return fetch("/liveclientdata/allgamedata", timeout=5.0)


def is_available() -> bool:
    data = fetch("/liveclientdata/activeplayer", timeout=2.0)
    return data is not None


def find_me_index(players: List[Dict[str, Any]], active_player: Dict[str, Any],
                  lcu_game_name: str = "") -> Optional[int]:
    """在 allPlayers 里找到自己。

    Riot ID 改版后，LCD 的 summonerName 可能是 "名字#TAG"，和 LCU 的 gameName
    对不上。所以先用同一个接口里 activePlayer 的身份字段精确匹配，再退到
    gameName 匹配 riotIdGameName / summonerName 去掉 #TAG 的部分。
    """
    for key in ("riotId", "summonerName"):
        target = active_player.get(key)
        if target:
            for i, p in enumerate(players):
                if p.get(key) == target:
                    return i
    if lcu_game_name:
        # 只凭名字（不带 #TAG）可能撞名：不止一个匹配时宁可认不出，也不要认错成敌人
        hits = [i for i, p in enumerate(players)
                if lcu_game_name in (p.get("riotIdGameName"), p.get("summonerName") or "",
                                     (p.get("summonerName") or "").split("#")[0])]
        if len(hits) == 1:
            return hits[0]
    return None


def resolve_my_enemy_teams(team_map: Dict[str, Any]) -> tuple:
    """从 LCD 的 team_map（ORDER/CHAOS）里按 is_me 找出我方和敌方。

    找不到自己时把 ORDER 当我方（与前端 renderLiveTeamPanel 一致）。
    """
    order, chaos = team_map.get("ORDER", []), team_map.get("CHAOS", [])
    if any(p.get("is_me") for p in chaos):
        return chaos, order
    return order, chaos
