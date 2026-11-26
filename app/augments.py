"""海克斯大乱斗强化符文：符文池、效果描述、国服统计，按这些给三选一排序。

数据（都按符文 key / ID 合到符文池的每一项上）：
- 符文池：同步来的（CommunityDragon）优先，没有就用 data/augments.json 快照
- desc / tags：游戏本体里的效果描述和它吃哪类属性，只在快照里（update_augments.py 生成）
- stats：腾讯 101 的国服胜率 / 选取率 / 最适合的英雄，同步来的（每天）优先，没有就用快照

排序：国服胜率为底，最适合的英雄里有你、知识库推荐、属性和你的定位对路时加分。
都没数据的（名字认错 / 新符文）才需要问 AI（/api/augments/ai）。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# 知识库 hextech_augments 的分组 → (显示名, 加分)。手写数据可能跟不上版本，加分比国服胜率的影响小
KB_GROUPS = {"prismatic_s_tier": ("棱彩 S 级", 15), "gold_key": ("金色核心", 12), "situational": ("看情况", 5)}
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
WIN_RATE_WEIGHT = 250     # 胜率每高出平均 1 个百分点 +2.5 分
WIN_RATE_CAP = 25
BEST_CHAMPION_BONUS = 20  # 国服数据里最适合的英雄有你
# 定位 → 吃哪类属性（tags 见 augment_text.TAG_WORDS）；只在符文明显只吃一类属性时用来加减分
ROLE_TAGS = {"法师": "AP", "射手": "攻击"}
RARITY_NAMES = {"silver": "银", "gold": "金", "prismatic": "彩"}


def load_pool(synced: Optional[List[Dict[str, Any]]], static_path: Path,
              synced_stats: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """符文池：同步来的优先，没有就用快照；再把快照里的描述、最新的国服统计合进去。"""
    try:
        static = json.loads(Path(static_path).read_text(encoding="utf-8"))
    except Exception:
        static = {}
    snapshot = static.get("augments") or []
    by_key = {a.get("key"): a for a in snapshot if a.get("key")}
    stats = (synced_stats or {}).get("augments") or {}
    pool = []
    for a in synced or snapshot:
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        s = by_key.get(a.get("key")) or {}
        row = {**a, "name": name}
        for k in ("desc", "tags"):
            if not row.get(k) and s.get(k):
                row[k] = s[k]
        st = stats.get(str(a.get("id"))) or s.get("stats")
        if st:
            row["stats"] = st
        pool.append(row)
    return pool


def kb_index(kb: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for group, (label, base) in KB_GROUPS.items():
        for a in (kb.get("hextech_augments") or {}).get(group) or []:
            if isinstance(a, dict) and a.get("name"):
                out[a["name"]] = {"group": label, "base": base, "effect": a.get("effect", ""),
                                  "best_for": a.get("best_for", "")}
    return out


def _avg_win_rate(pool: List[Dict[str, Any]]) -> float:
    rates = [a["stats"]["win_rate"] for a in pool if (a.get("stats") or {}).get("win_rate")]
    return sum(rates) / len(rates) if rates else 0.5


def _stat_count(pool: List[Dict[str, Any]]) -> int:
    return sum(1 for a in pool if a.get("stats"))


def best_champion_names(a: Dict[str, Any], champion_names: Dict[str, str], limit: int = 6) -> List[str]:
    ids = ((a.get("stats") or {}).get("champions") or [])[:limit]
    return [champion_names[str(i)] for i in ids if str(i) in champion_names]


def rank(options: List[str], coach: Optional[Dict[str, Any]], kb_specific: Optional[Dict[str, Any]],
         kb: Dict[str, Any], pool: List[Dict[str, Any]], champion_id: Optional[int] = None,
         champion_names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """给玩家看到的三个选项排序。

    返回 {ranked: [{name, rarity, score, reasons, desc, known, win_rate, best_for}], pick, confident}；
    desc 是效果（知识库写了就用知识库的短句，否则用游戏描述），best_for 是国服数据里最适合的英雄名。
    """
    coach = coach or {}
    champion_names = champion_names or {}
    idx = kb_index(kb)
    by_name = {a["name"]: a for a in pool}
    avg, n_stats = _avg_win_rate(pool), _stat_count(pool)
    best_runes = list((kb_specific or {}).get("best_runes") or [])
    role = coach.get("role", "")
    champ = coach.get("name", "")
    names = [n for n in (champ, coach.get("alias", "")) if n]  # 称号 + 本名，知识库两种写法都有

    ranked = []
    for name in options:
        a = by_name.get(name) or {}
        score, reasons, known = 0.0, [], bool(a.get("desc") or a.get("stats"))
        if name in best_runes:
            score += 60 - 5 * best_runes.index(name)
            reasons.append(f"{champ}专属推荐第 {best_runes.index(name) + 1}")
            known = True

        st = a.get("stats") or {}
        if champion_id and int(champion_id) in (st.get("champions") or []):
            score += BEST_CHAMPION_BONUS
            reasons.append(f"国服数据：最适合{champ or '你'}的强化之一")
        if st.get("win_rate"):
            score += max(-WIN_RATE_CAP, min(WIN_RATE_CAP, (st["win_rate"] - avg) * WIN_RATE_WEIGHT))
            reasons.append(f"国服胜率 {st['win_rate'] * 100:.1f}%（第 {st['win_rank']}/{n_stats}）")

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
        else:
            # 知识库没写适合谁：看描述里吃的属性和你的定位对不对路（只判断明显只吃一类属性的）
            tags, want = a.get("tags") or [], ROLE_TAGS.get(role)
            if want and len(tags) == 1:
                if tags[0] == want:
                    score += 8
                    reasons.append(f"吃{want}，适合{role}")
                else:
                    score -= 15
                    reasons.append(f"偏{tags[0]}属性，{role}用不上")

        ranked.append({"name": name, "rarity": a.get("rarity", ""), "score": round(score, 1), "reasons": reasons,
                       "desc": (info or {}).get("effect") or a.get("desc", ""), "known": known,
                       "win_rate": st.get("win_rate"), "best_for": best_champion_names(a, champion_names)})
    ranked.sort(key=lambda r: r["score"], reverse=True)

    pick, confident = None, False
    if ranked and ranked[0]["known"]:
        pick = ranked[0]["name"]
        second = ranked[1]["score"] if len(ranked) > 1 else 0
        confident = ranked[0]["score"] - second >= CONFIDENT_MARGIN
    return {"ranked": ranked, "pick": pick, "confident": confident}


def describe(a: Dict[str, Any], champion_names: Optional[Dict[str, str]] = None) -> str:
    """给 AI 看的一行：名字（稀有度）：效果；国服胜率；最适合的英雄。"""
    desc = (a.get("desc") or "没有效果描述").rstrip("。 ")
    bits = [f"{a['name']}（{RARITY_NAMES.get(a.get('rarity', ''), '?')}）：{desc}"]
    st = a.get("stats") or {}
    if st.get("win_rate"):
        bits.append(f"国服胜率 {st['win_rate'] * 100:.1f}%、选取率 {st['pick_rate'] * 100:.0f}%")
    best = best_champion_names(a, champion_names or {})
    if best:
        bits.append(f"最适合 {'、'.join(best)}")
    return "；".join(bits)


def for_champion(pool: List[Dict[str, Any]], champion_id: Optional[int], limit: int = 8) -> List[Dict[str, Any]]:
    """国服数据里"最适合的英雄"包含这个英雄的强化符文，按胜率从高到低。"""
    if not champion_id:
        return []
    cid = int(champion_id)
    hits = [a for a in pool if cid in ((a.get("stats") or {}).get("champions") or [])]
    return sorted(hits, key=lambda a: a["stats"].get("win_rate") or 0, reverse=True)[:limit]


def mentioned(pool: List[Dict[str, Any]], text: str, limit: int = 6) -> List[Dict[str, Any]]:
    """问题里提到的强化符文（长名字优先，免得"属性！"把"属性叠属性！"也算进去）。"""
    found, rest = [], text or ""
    for a in sorted(pool, key=lambda x: len(x["name"]), reverse=True):
        if len(a["name"]) >= 2 and a["name"] in rest:
            found.append(a)
            rest = rest.replace(a["name"], " ")
            if len(found) >= limit:
                break
    return found
