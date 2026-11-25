"""装备数据：常规装备索引、合成树计价、阵亡时的购物清单。

ITEM_MAP 的条目形如 {name, gold_total, gold_base, from: [子件 ID], cats: [分类]}：
gold_base 是合成费（不含子件），from / cats 来自 CommunityDragon（见 data_sync.parse_cdragon_items）。
"""
from collections import Counter
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional

INVENTORY_SLOTS = 6  # 第 7 格（slot 6）是饰品，不占装备栏

# 归为 AD / AP 输出装的分类（CommunityDragon categories）
_AD_CATS = {"Damage", "CriticalStrike", "AttackSpeed", "ArmorPenetration", "OnHit", "LifeSteal"}
_AP_CATS = {"SpellDamage", "MagicPenetration"}


class ItemDB:
    def __init__(self, item_map: Dict[str, Dict[str, Any]]):
        self.items = item_map
        self.by_name: Dict[str, str] = {}
        self.reindex()

    def reindex(self) -> None:
        """同名装备有斗魂竞技场等模式的变体（22xxxx 等，价格不同），按名字查时取 ID 最短的常规版本。"""
        self.by_name.clear()
        for iid, info in self.items.items():
            name = info.get("name")
            if not name:
                continue
            cur = self.by_name.get(name)
            if cur is None or (len(iid), int(iid)) < (len(cur), int(cur)):
                self.by_name[name] = iid

    def apply_economics(self, economics: Optional[Dict[str, Dict[str, Any]]]) -> int:
        """用同步来的当前版本价格/合成树覆盖本地数据（名字不动，与出装数据口径保持一致）。"""
        n = 0
        for iid, eco in (economics or {}).items():
            info = self.items.get(iid)
            if info is None:
                continue
            info.update(gold_total=eco["total"], gold_base=eco["base"], **{"from": eco["from"], "cats": eco["cats"]})
            n += 1
        return n

    def id_of(self, name: str) -> Optional[str]:
        return self.by_name.get(name)

    def name(self, iid: Any) -> str:
        return (self.items.get(str(iid)) or {}).get("name", "")

    def total(self, iid: Any) -> int:
        return int((self.items.get(str(iid)) or {}).get("gold_total") or 0)

    def total_by_name(self, name: str) -> int:
        iid = self.id_of(name)
        return self.total(iid) if iid else 0

    # ------------------------------------------------------------ 计价

    def remaining_cost(self, iid: str, pool: Counter) -> int:
        """合成 iid 还要花多少钱：pool 里已有的子件（递归）会被算进去，并从 pool 里扣掉。"""
        if pool[iid] > 0:
            pool[iid] -= 1
            return 0
        info = self.items.get(iid) or {}
        parts = info.get("from") or []
        if not parts:
            return int(info.get("gold_total") or 0)
        return int(info.get("gold_base") or 0) + sum(self.remaining_cost(str(c), pool) for c in parts)

    def damage_type(self, ids: Iterable[str], default: Optional[str] = "ad") -> Optional[str]:
        """一套出装偏 AD 还是 AP（'ad' / 'ap'）：按装备分类计数；一件输出装都没有时返回 default。"""
        ad = ap = 0
        for iid in ids:
            cats = set((self.items.get(str(iid)) or {}).get("cats") or [])
            ap += bool(cats & _AP_CATS)
            ad += bool(cats & _AD_CATS)
        if not ap and not ad:
            return default
        return "ap" if ap > ad else "ad"

    # ------------------------------------------------------------ 购物清单

    def plan_purchase(self, targets: List[str], owned: List[str], gold: int) -> Dict[str, Any]:
        """按出装优先级 targets（未拥有的成装 ID）给出这次能买什么。

        能直接合成的成装直接买（商店会自动用掉已有子件）；买不起的那件，用剩下的钱
        挑一组总价最高的子件先买上；它的子件一个都买不起时，再看后面几件的子件。
        owned 是装备栏里的物品（不含饰品格）。每条购买记录的 for 是它要合成的成装。
        """
        pool = Counter(str(i) for i in owned)
        buys: List[Dict[str, Any]] = []
        left = int(gold)
        focus = None
        for t in targets:
            trial = pool.copy()
            need = self.remaining_cost(t, trial)
            if need <= left:
                pool = trial
                pool[t] += 1
                left -= need
                buys.append({"id": t, "name": self.name(t), "cost": need, "full": True, "for": self.name(t)})
                continue
            spent_before = left
            pool, left = self._buy_parts(t, pool, left, buys, self.name(t))
            focus = {"id": t, "name": self.name(t), "total": self.total(t),
                     "missing": need - (spent_before - left)}
            if left == spent_before:
                for t2 in targets[targets.index(t) + 1:]:
                    pool, left = self._buy_parts(t2, pool, left, buys, self.name(t2))
                    if left < spent_before:
                        break
            break

        slots_over = sum(n for n in pool.values() if n > 0) - INVENTORY_SLOTS
        trimmed = False
        if slots_over > 0:
            # 装备栏放不下：从后往前撤掉零散子件（成装会吃掉子件，不会让格子变多）
            for b in reversed([b for b in buys if not b["full"]]):
                if slots_over <= 0:
                    break
                buys.remove(b)
                left += b["cost"]
                slots_over -= 1
                trimmed = True
        return {
            "gold": int(gold),
            "buys": buys,
            "spend": int(gold) - left,
            "left": left,
            "focus": focus,  # 这次合不出来、正在攒的那件
            "inventory_full": trimmed or slots_over > 0,
        }

    def _buy_parts(self, iid: str, pool: Counter, gold: int, buys: List[Dict[str, Any]], root: str):
        parts = [str(c) for c in (self.items.get(iid) or {}).get("from") or []]
        need = []
        for c in parts:
            trial = pool.copy()
            if self.remaining_cost(c, trial) == 0:
                pool = trial  # 已有这个子件
            else:
                need.append(c)
        # 子件最多 4 个：枚举组合，挑能买得起的总价最高的一组（同价取件数少的）
        best, best_cost = (), 0
        for r in range(1, len(need) + 1):
            for combo in combinations(range(len(need)), r):
                trial = pool.copy()
                cost = sum(self.remaining_cost(need[i], trial) for i in combo)
                if best_cost < cost <= gold or (cost == best_cost and 0 < cost <= gold and len(combo) < len(best)):
                    best, best_cost = combo, cost
        for i in best:
            c = need[i]
            cost = self.remaining_cost(c, pool)
            pool[c] += 1
            gold -= cost
            buys.append({"id": c, "name": self.name(c), "cost": cost, "full": False, "for": root})
        rest = [need[i] for i in range(len(need)) if i not in best]
        # 剩下的钱再往没买的子件里拆：从贵的开始，拆出东西就停
        for c in sorted(rest, key=lambda c: self.remaining_cost(c, pool.copy()), reverse=True):
            if gold <= 0:
                break
            if (self.items.get(c) or {}).get("from"):
                before = len(buys)
                pool, gold = self._buy_parts(c, pool, gold, buys, root)
                if len(buys) > before:
                    break
        return pool, gold
