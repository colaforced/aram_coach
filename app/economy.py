"""团队经济估算。

LCD 只给自己的金币，别人只看得到装备。大乱斗只能阵亡（或开局）时买东西，所以
"装备总价"只是已经花掉的钱：不怎么死的一方钱攒在身上看不到，装备总价会低估它，
而越是优势方越少阵亡——直接拿装备总价比，落后时反而常显示"领先"。

所以按每人累计挣到的金币来比：
- 所有人共有的部分（初始金币 + 被动收入）用自己的真实数字校准：
  我挣到的 = 装备总价 + 现金，减去我的人头 / 助攻 / 补刀收入，剩下的就是共有部分
- 人头 / 助攻 / 补刀按下面的常数估算；赏金、终结、一血和推塔金币不算，所以是估算值
- 一个人至少挣到了他身上装备的钱：估算值低于装备总价时取装备总价
"""
from typing import Any, Dict, List, Optional

KILL_GOLD = 300          # 每个人头
ASSIST_POOL = 150        # 每次击杀，助攻者合计分到的钱（按各人助攻数分摊）
CS_GOLD = 20             # 每个补刀，大乱斗各类小兵的大致均值
START_GOLD = 1400        # 认不出自己、没法校准时的兜底
FALLBACK_PASSIVE = 2.0   # 兜底用的被动收入（每秒，1 分钟后开始）


def _personal(p: Dict[str, Any], assist_value: float) -> float:
    return KILL_GOLD * (p.get("kills") or 0) + assist_value * (p.get("assists") or 0) + CS_GOLD * (p.get("cs") or 0)


def annotate(team_map: Dict[str, List[Dict[str, Any]]], my_gold: Optional[float], game_time: float) -> None:
    """给每个玩家加上 est_gold（估算的累计金币），自己的是真实值。"""
    assist_value = {}
    for team, players in team_map.items():
        kills = sum(p.get("kills") or 0 for p in players)
        assists = sum(p.get("assists") or 0 for p in players)
        assist_value[team] = ASSIST_POOL * kills / assists if assists else 0.0

    me_team = next((t for t, ps in team_map.items() for p in ps if p.get("is_me")), None)
    me = next((p for p in team_map.get(me_team, []) if p.get("is_me")), None)
    if me is not None and my_gold is not None:
        base = max(0.0, me.get("item_gold", 0) + my_gold - _personal(me, assist_value[me_team]))
    else:
        base = START_GOLD + FALLBACK_PASSIVE * max(0.0, game_time - 60)

    for team, players in team_map.items():
        for p in players:
            if p is me and my_gold is not None:
                est = p.get("item_gold", 0) + my_gold
            else:
                est = max(base + _personal(p, assist_value[team]), p.get("item_gold", 0))
            p["est_gold"] = int(round(est, -1))
