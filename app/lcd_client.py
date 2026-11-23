import json
import ssl
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

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


def resolve_my_enemy_teams(team_map: Dict[str, Any]) -> tuple:
    """从 LCD 的 team_map（ORDER/CHAOS）里按 is_me 找出我方和敌方。

    找不到自己时退化为前 5 人算我方。
    """
    all_players = []
    for key in ("ORDER", "CHAOS"):
        all_players.extend(team_map.get(key, []))
    my_team = []
    enemy_team = []
    for key in ("ORDER", "CHAOS"):
        roster = team_map.get(key, [])
        if any(p.get("is_me") for p in roster):
            my_team = roster
        else:
            enemy_team = roster
    if not my_team:
        my_team = all_players[:5]
        enemy_team = all_players[5:]
    return my_team, enemy_team
