"""团队经济估算。

LCD 只给自己的金币，别人只看得到装备。大乱斗只能阵亡（或开局）时买东西，所以
"装备总价"只是已经花掉的钱：不怎么死的一方钱攒在身上看不到，装备总价会低估它，
而越是优势方越少阵亡——直接拿装备总价比，落后时反而常显示"领先"。

所以按每人累计挣到的金币来比：
- 所有人共有的部分（初始金币 + 被动收入）用自己的真实数字校准：
  我挣到的 = 装备总价 + 现金 + 卖装备亏掉的，减去我的人头 / 助攻 / 补刀 / 推塔收入，剩下的就是共有部分
- 人头 / 助攻 / 补刀 / 推塔按下面的常数估算；赏金、终结和强化符文给的钱不算，所以是估算值
- 一个人至少挣到了他身上装备的钱：估算值低于装备总价时取装备总价

常数是用两局国服海克斯大乱斗的赛后逐分钟真实金币回归、再按团队经济差误差调出来的
（2026-11，团队差 rmse 从约 850 降到约 400），样本少，攒了更多复盘可以再调。
"""
import re
from typing import Any, Dict, Iterable, List, Optional

KILL_GOLD = 240          # 每个人头
ASSIST_POOL = 50         # 每次击杀，助攻者合计分到的钱（按各人助攻数分摊）
CS_GOLD = 20             # 每个补刀，大乱斗各类小兵的大致均值
TOWER_GOLD = 120         # 每推掉一座塔 / 水晶，全队每人
START_GOLD = 1400        # 认不出自己、没法校准时的兜底：初始金币
# 兜底用的共有收入（每秒）：1 分钟后开始，13 分钟后变多（两局实测）
FALLBACK_RATES = ((60, 9.2), (780, 14.5))

# LCD 事件里塔 / 水晶的名字带归属方：Turret_TChaos_L1_P3_…、Inhib_TOrder_L1_P1_…（旧格式 Turret_T1_…、Barracks_T2_…）
_OWNER = re.compile(r"_T(Order|Chaos|1|2)_")
_OWNER_TEAM = {"Order": "ORDER", "1": "ORDER", "Chaos": "CHAOS", "2": "CHAOS"}
_OTHER = {"ORDER": "CHAOS", "CHAOS": "ORDER"}


def structures_destroyed(events: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, int]:
    """双方各推掉了几座塔 / 水晶（{"ORDER": n, "CHAOS": m}，按推掉的一方算）。"""
    out = {"ORDER": 0, "CHAOS": 0}
    for e in events or []:
        name = e.get("TurretKilled") or e.get("InhibKilled")
        m = _OWNER.search(name or "") if e.get("EventName") in ("TurretKilled", "InhibKilled") else None
        if m:
            out[_OTHER[_OWNER_TEAM[m.group(1)]]] += 1
    return out


class SellLoss:
    """自己的"装备总价 + 现金"里看不到的钱：卖装备只退一部分、药水用掉、装备变形后价格对不上。

    挣到的钱只增不减，所以每轮比上一轮少了的部分都是这种情况，累计起来补回去。
    换了一局（gameId 变了或游戏时间倒退）就清零。
    """

    def __init__(self) -> None:
        self.game: Any = None
        self.time = 0.0
        self.last: Optional[float] = None
        self.lost = 0.0

    def update(self, game: Any, game_time: float, total: float) -> float:
        if self.last is None or game != self.game or game_time < self.time - 5:
            self.game, self.lost = game, 0.0
        elif total < self.last:
            self.lost += self.last - total
        self.time, self.last = game_time, total
        return self.lost


def _personal(p: Dict[str, Any], assist_value: float) -> float:
    return KILL_GOLD * (p.get("kills") or 0) + assist_value * (p.get("assists") or 0) + CS_GOLD * (p.get("cs") or 0)


def _fallback_base(game_time: float) -> float:
    base = START_GOLD
    for i, (start, rate) in enumerate(FALLBACK_RATES):
        end = FALLBACK_RATES[i + 1][0] if i + 1 < len(FALLBACK_RATES) else float("inf")
        base += rate * max(0.0, min(game_time, end) - start)
    return base


def annotate(team_map: Dict[str, List[Dict[str, Any]]], my_gold: Optional[float], game_time: float,
             towers: Optional[Dict[str, int]] = None, my_lost: float = 0.0) -> None:
    """给每个玩家加上 est_gold（估算的累计金币），自己的是真实值。

    towers：各队推掉的塔 / 水晶数（structures_destroyed）；my_lost：SellLoss 累计的看不到的钱。
    """
    assist_value = {}
    for team, players in team_map.items():
        kills = sum(p.get("kills") or 0 for p in players)
        assists = sum(p.get("assists") or 0 for p in players)
        assist_value[team] = ASSIST_POOL * kills / assists if assists else 0.0
    shared = {team: TOWER_GOLD * (towers or {}).get(team, 0) for team in team_map}

    me_team = next((t for t, ps in team_map.items() for p in ps if p.get("is_me")), None)
    me = next((p for p in team_map.get(me_team, []) if p.get("is_me")), None)
    if me is not None and my_gold is not None:
        my_total = me.get("item_gold", 0) + my_gold + my_lost
        base = max(0.0, my_total - _personal(me, assist_value[me_team]) - shared[me_team])
    else:
        base = _fallback_base(game_time)

    for team, players in team_map.items():
        for p in players:
            if p is me and my_gold is not None:
                est = my_total
            else:
                est = max(base + shared[team] + _personal(p, assist_value[team]), p.get("item_gold", 0))
            p["est_gold"] = int(round(est, -1))
