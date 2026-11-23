"""决策时刻的建议：选人（换 / 骰 / 留）、阵亡或开局的购物清单、针对敌方阵容的出装调整。

大乱斗里能停下来看建议、而且有东西可决定的只有这几个时刻；其余时间不输出泛泛的提示。
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.items import ItemDB

# 回复能力强、值得出重伤的英雄（按英雄 ID，名称见 champion_map.json）
HEALER_CHAMPION_IDS = {
    16, 267, 37, 350, 902, 147, 44, 40, 235,   # 治疗型辅助：索拉卡/娜美/娑娜/悠米/米利欧/萨勒芬妮/塔里克/迦娜/赛娜
    8, 50, 19, 36, 266, 517, 233, 23, 9, 20,   # 高额自我回复：吸血鬼/乌鸦/狼人/蒙多/剑魔/塞拉斯/贝蕾亚/蛮王/稻草人/努努
}
AP_ROLES = ("法师", "辅助")

SWAP_MARGIN = 5     # 备选席英雄综合分至少高这么多才建议换
REROLL_MARGIN = 3   # 当前英雄比"随机一个"的期望低这么多才建议骰
START_WINDOW = 90   # 开局多少秒内、还没买装备时算"出门装"时刻


def counter_item(kind: str, role: str, dmg: str) -> Optional[str]:
    """针对性装备：kind ∈ heal（重伤）/ tank（穿透）/ mr / armor（防御），按定位和输出类型选。"""
    if kind == "heal":
        if role == "坦克":
            return "荆棘之甲"
        if dmg == "ap":
            return "莫雷洛秘典"
        return "凡性的提醒" if role == "射手" else "炼金朋克链锯剑"
    if kind == "tank":
        if role in ("坦克", "辅助"):
            return None
        if dmg == "ap":
            return "虚空之杖"
        return {"射手": "多米尼克领主的致意", "刺客": "赛瑞尔达的怨恨"}.get(role, "黑色切割者")
    if kind in ("mr", "armor") and role in ("坦克", "战士"):  # 脆皮出防装不划算，只给前排
        return "自然之力" if kind == "mr" else "兰顿之兆"
    return None


def enemy_threats(enemy_team: List[Dict[str, Any]]) -> Dict[str, Any]:
    """敌方阵容里需要出装应对的东西：回复型英雄、坦克数、AP/AD 构成。"""
    roles = [(m.get("coach") or {}).get("role", "") for m in enemy_team]
    return {
        "healers": [m.get("champion_name") or "?" for m in enemy_team if m.get("champion_id") in HEALER_CHAMPION_IDS],
        "tanks": [m.get("champion_name") or "?" for m, r in zip(enemy_team, roles) if r == "坦克"],
        "ap": sum(r in AP_ROLES for r in roles),
        "ad": sum(bool(r) and r not in AP_ROLES for r in roles),
    }


def counter_adjustments(coach: Dict[str, Any], enemy_team: List[Dict[str, Any]],
                        owned_names: List[str], dmg: str) -> List[Dict[str, str]]:
    """针对敌方阵容要提前的装备，按优先级排列；已经有了的不再提。

    [{kind, item, tip}]：tip 优先用英雄自己 situational 里的写法。
    """
    situational = coach.get("situational") or {}
    role = coach.get("role", "")
    owned = set(owned_names)
    th = enemy_threats(enemy_team)
    total = th["ap"] + th["ad"]
    wants: List[Tuple[str, str, Tuple[str, ...]]] = []
    if th["healers"]:
        wants.append(("heal", f"敌方回复强（{'、'.join(th['healers'])}）", ("against_heal", "enemy_heal")))
    if len(th["tanks"]) >= 2:
        wants.append(("tank", f"敌方坦克多（{'、'.join(th['tanks'])}）", ("against_tank", "all_tank")))
    if total >= 3 and th["ap"] / total >= 0.6:
        wants.append(("mr", f"敌方 {th['ap']}/{total} 是 AP", ("need_ap", "against_ap", "heavy_ap")))
    elif total >= 3 and th["ad"] / total >= 0.7:
        wants.append(("armor", f"敌方 {th['ad']}/{total} 是 AD", ("need_ad",)))

    out = []
    for kind, why, keys in wants:
        item = counter_item(kind, role, dmg)
        if not item or item in owned:
            continue
        own_tip = next((situational[k] for k in keys if situational.get(k)), "")
        out.append({"kind": kind, "item": item, "why": why,
                    "tip": f"{why}：{own_tip}" if own_tip else f"{why}：提前出 {item}"})
    return out


def build_targets(coach: Dict[str, Any], owned_ids: List[str], adjustments: List[Dict[str, str]],
                  db: ItemDB) -> List[str]:
    """接下来要合成的成装（ID，按优先级）：推荐出装里还没有的 + 针对性装备提前。"""
    owned = set(owned_ids)
    owned_names = {db.name(i) for i in owned}
    has_boots = any("Boots" in ((db.items.get(i) or {}).get("cats") or []) for i in owned)
    build_ids = [i for i in (coach.get("build_ids") or []) if i]
    done = sum(1 for i in build_ids if i in owned or db.name(i) in owned_names)

    targets = []
    for iid in build_ids:
        if iid in owned or db.name(iid) in owned_names or iid in targets:
            continue
        if has_boots and "Boots" in ((db.items.get(iid) or {}).get("cats") or []):
            continue  # 已经有一双鞋了（可能和推荐的不同），不再买第二双
        targets.append(iid)

    # 第一件核心装做完之前，针对性装备排在它后面；之后排到最前
    at = 0 if done else min(1, len(targets))
    for adj in adjustments:
        iid = db.id_of(adj["item"])
        if not iid or iid in owned:
            continue
        if iid in targets:
            targets.remove(iid)
        targets.insert(min(at, len(targets)), iid)
        at += 1
    return targets


def in_game_moment(coach: Optional[Dict[str, Any]], me: Optional[Dict[str, Any]], active: Dict[str, Any],
                   game_time: float, enemy_team: List[Dict[str, Any]], db: ItemDB) -> Optional[Dict[str, Any]]:
    """对局中的决策卡：阵亡 → 复活前买什么；开局 → 出门装；活着 → 下次阵亡买什么（预告）。"""
    if not coach or not me:
        return None
    owned_ids = [str(it["item_id"]) for it in me.get("items") or []
                 if it.get("item_id") and it.get("slot") != 6]
    owned_names = [db.name(i) for i in owned_ids]
    dmg = db.damage_type(coach.get("build_ids") or [])
    adjustments = counter_adjustments(coach, enemy_team, owned_names, dmg)
    targets = build_targets(coach, owned_ids, adjustments, db)
    gold = int(active.get("gold") or 0)

    if me.get("is_dead"):
        kind = "shop"
    elif game_time < START_WINDOW and not owned_ids:
        kind = "start"
    else:
        kind = "next"

    plan = None
    starter = [db.id_of(n) for n in coach.get("starter_items") or []]
    if kind == "start" and starter and all(starter):
        # 有 OP.GG 出门装统计就照它买
        cost = sum(db.total(i) for i in starter)
        if cost <= gold:
            plan = {"gold": gold, "spend": cost, "left": gold - cost, "focus": None, "inventory_full": False,
                    "buys": [{"id": i, "name": db.name(i), "cost": db.total(i), "full": True, "for": "出门装"}
                             for i in starter]}
    if plan is None:
        plan = db.plan_purchase(targets, owned_ids, gold)

    owned_set = set(owned_names)
    return {
        "kind": kind,
        "respawn": round(me.get("respawn_timer") or 0),
        "plan": plan,
        "targets": [db.name(i) for i in targets[:6]],
        "adjustments": adjustments,
        "dmg": dmg,
        "build_status": [{"name": n, "owned": n in owned_set} for n in coach.get("build") or []],
    }


def plan_text(plan: Dict[str, Any]) -> str:
    """购物清单 → 一句话（给 Markdown 快照和 LLM 提示词用）。"""
    parts = []
    if plan.get("buys"):
        parts.append("买 " + " + ".join(
            f"{b['name']}({b['cost']}G)" + ("" if b["full"] or not b.get("for") else f"→{b['for']}")
            for b in plan["buys"]))
    focus = plan.get("focus")
    if focus:
        parts.append(f"攒 {focus['name']} 还差 {focus['missing']}G")
    if plan.get("inventory_full"):
        parts.append("装备栏满了")
    return "；".join(parts) or "暂时不用买"


def moment_text(moment: Optional[Dict[str, Any]]) -> str:
    if not moment:
        return ""
    head = {"shop": f"阵亡中（{moment['respawn']}s 复活），现在就买",
            "start": "开局出门装",
            "next": "下次阵亡时买"}[moment["kind"]]
    lines = [f"{head}：{plan_text(moment['plan'])}"]
    lines += [a["tip"] for a in moment.get("adjustments") or []]
    return "\n".join(lines)


def champ_select_verdict(my_team: List[Dict[str, Any]], current_id: Any, current_coach: Optional[Dict[str, Any]],
                         bench: List[Dict[str, Any]], dice: int,
                         fit_fn: Callable[..., Tuple[int, list]], mean_strength: float) -> Dict[str, Any]:
    """选人阶段只给一个结论：换备选席的谁 / 用骰子重随 / 保留。

    综合分 = 强度 × 0.6 + 阵容适配 × 0.4（与骰子建议同一口径）；
    重随的期望按"随机一个英雄、阵容适配一般（50）"估算。
    """
    def option(cid: Any, coach: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        strength = (coach or {}).get("aram_strength", 50)
        fitness, notes = fit_fn(my_team, coach, current_id)
        return {
            "champion_id": cid,
            "name": (coach or {}).get("name", "?"),
            "role": (coach or {}).get("role", ""),
            "strength": strength,
            "fitness": fitness,
            "score": round(strength * 0.6 + fitness * 0.4),
            "notes": [t for k, t in notes if k in ("fill", "dup")],
            "opgg": (coach or {}).get("opgg"),
        }

    reroll_ev = round(mean_strength * 0.6 + 50 * 0.4)
    if not current_id or not current_coach:
        return {"action": "wait", "headline": "等待分配英雄", "reason": "", "options": [], "dice": dice,
                "reroll_ev": reroll_ev}

    cur = option(current_id, current_coach)
    opts = sorted((option(b["champion_id"], b.get("coach")) for b in bench if b.get("coach")),
                  key=lambda o: o["score"], reverse=True)
    best = opts[0] if opts else None

    if best and best["score"] >= cur["score"] + SWAP_MARGIN:
        action, target = "swap", best
        headline = f"换成 {best['name']}"
        reason = f"{_describe(best)}；综合 {best['score']}，比当前 {cur['name']} 高 {best['score'] - cur['score']}"
    elif dice > 0 and cur["score"] < reroll_ev - REROLL_MARGIN:
        action, target = "reroll", None
        headline = "用骰子重随"
        reason = f"{cur['name']} 综合 {cur['score']}，低于随机一个英雄的期望 {reroll_ev}（{_describe(cur)}）"
    else:
        action, target = "keep", cur
        headline = f"保留 {cur['name']}"
        why = _describe(cur)
        if best:
            why += f"；备选席最好的 {best['name']} 综合 {best['score']}，差距不够换"
        reason = why
    return {
        "action": action,
        "headline": headline,
        "reason": reason,
        "current": cur,
        "target": target,
        "options": opts,
        "dice": dice,
        "reroll_ev": reroll_ev,
    }


def _describe(o: Dict[str, Any]) -> str:
    op = o.get("opgg") or {}
    if op.get("tier") is not None and op.get("win_rate"):
        base = f"OP.GG T{op['tier']} · 胜率 {op['win_rate'] * 100:.1f}%"
    else:
        base = f"强度 {o['strength']}"
    if o["notes"]:
        base += f"，{o['notes'][0]}"
    return base
