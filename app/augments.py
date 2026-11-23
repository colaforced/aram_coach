"""海克斯大乱斗强化符文：符文池（CommunityDragon）、按知识库给三选一排序。

LCD 接口拿不到当前给出的三个选项，需要玩家在界面里选出来；排序只用本地知识库，
知识库没覆盖到的再交给 LLM（见 llm_client.pick_augment）。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# 知识库 hextech_augments 的分组 → (显示名, 基础分)
KB_GROUPS = {"prismatic_s_tier": ("棱彩 S 级", 30), "gold_key": ("金色核心", 25), "situational": ("看情况", 10)}
ROLE_WORDS = {
    "坦克": ("坦克", "前排"),
    "战士": ("战士", "近战"),
    "法师": ("法师", "AP", "法强"),
    "射手": ("射手", "ADC", "攻击特效", "普攻"),
    "刺客": ("刺客", "爆发"),
    "辅助": ("辅助",),
}
UNIVERSAL_WORDS = ("所有英雄", "任何")
CONFIDENT_MARGIN = 10


def load_pool(synced: Optional[List[Dict[str, Any]]], static_path: Path) -> List[Dict[str, Any]]:
    """优先用同步来的符文池，没有就用仓库里的快照。"""
    if synced:
        return synced
    try:
        return json.loads(Path(static_path).read_text(encoding="utf-8")).get("augments") or []
    except Exception:
        return []


def kb_index(kb: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for group, (label, base) in KB_GROUPS.items():
        for a in (kb.get("hextech_augments") or {}).get(group) or []:
            if isinstance(a, dict) and a.get("name"):
                out[a["name"]] = {"group": label, "base": base, "effect": a.get("effect", ""),
                                  "best_for": a.get("best_for", "")}
    return out


def rank(options: List[str], coach: Optional[Dict[str, Any]], kb_specific: Optional[Dict[str, Any]],
         kb: Dict[str, Any], pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    """给玩家看到的三个选项排序。返回 {ranked: [{name, rarity, score, reasons, known}], pick, confident}"""
    coach = coach or {}
    idx = kb_index(kb)
    rarity = {a["name"]: a.get("rarity", "") for a in pool}
    best_runes = list((kb_specific or {}).get("best_runes") or [])
    role = coach.get("role", "")
    champ = coach.get("name", "")
    names = [n for n in (champ, coach.get("alias", "")) if n]  # 称号 + 本名，知识库两种写法都有

    ranked = []
    for name in options:
        score, reasons, known = 0, [], False
        if name in best_runes:
            score += 60 - 5 * best_runes.index(name)
            reasons.append(f"{champ}专属推荐第 {best_runes.index(name) + 1}")
            known = True
        info = idx.get(name)
        if info:
            known = True
            score += info["base"]
            reasons.append(f"知识库{info['group']}")
            best_for = info["best_for"]
            named_roles = [r for r, words in ROLE_WORDS.items() if any(w in best_for for w in words)]
            hit = next((n for n in names if n in best_for), None)
            if hit:
                score += 20
                reasons.append(f"适合{hit}")
            elif role in named_roles:
                score += 15
                reasons.append(f"适合{role}")
            elif named_roles:
                # 写明了适合哪些定位、但不包括你（比如"任何AP英雄"给了射手）
                score -= 15
                reasons.append(f"偏{'/'.join(named_roles)}，不太适合{role or '你'}")
            elif any(w in best_for for w in UNIVERSAL_WORDS):
                score += 5
            if info["effect"]:
                reasons.append(info["effect"])
        ranked.append({"name": name, "rarity": rarity.get(name, ""), "score": score,
                       "reasons": reasons, "known": known})
    ranked.sort(key=lambda r: r["score"], reverse=True)

    pick, confident = None, False
    if ranked and ranked[0]["known"]:
        pick = ranked[0]["name"]
        second = ranked[1]["score"] if len(ranked) > 1 else 0
        confident = ranked[0]["score"] - second >= CONFIDENT_MARGIN
    return {"ranked": ranked, "pick": pick, "confident": confident}
