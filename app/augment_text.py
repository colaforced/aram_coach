"""海斗强化符文的效果描述：从游戏本体数据生成（CommunityDragon 导出的 kiwi.bin.json + 中文文本表）。

cherry-augments.json 里海斗（KIWI）的符文只有名字、没有描述，只能读游戏文件：
- kiwi.bin.json 的 AugmentData：符文 key → 描述文本 key（DescriptionTra）、根技能（RootSpell）
- 技能的 DataValues / mSpellCalculations：填描述里的 @数值@ 占位符
- lol.stringtable.json（zh_cn）：文本 key → 中文

两个文件合计约 46MB，不放进启动同步，由 update_augments.py 生成 data/augments.json 的快照。
"""
import re
from typing import Any, Dict, List, Optional, Tuple, Union

KIWI_BIN_URL = "https://raw.communitydragon.org/latest/game/maps/modespecificdata/kiwi.bin.json"
STRINGTABLE_URL = "https://raw.communitydragon.org/latest/game/zh_cn/data/menu/en_us/lol.stringtable.json"

PLACEHOLDER = re.compile(r"@([A-Za-z0-9_.]+?)(?:\*(-?[0-9.]+))?@")
KEYWORD = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
# 技能数值里的属性编号 → 中文。按描述原文核对过（蛋白粉奶昔"基于护甲和魔抗"= 1、6，急急小子"相当于技能急速"= 11，
# 巨像的勇气按最大生命值 = 12 …）；不认识的编号整条算式放弃，不瞎写
STAT_NAMES = {0: "法强", 1: "护甲", 2: "攻击力", 4: "攻速", 6: "魔抗", 7: "移速", 8: "暴击几率", 9: "暴击伤害",
              11: "技能急速", 12: "最大生命值"}
BONUS_FORMULA = 2   # mStatFormula：2 = 额外属性

# 描述里出现这些标记 / 词 → 这个符文吃哪类属性（用来判断和英雄是否对路）
TAG_WORDS = {
    "AP": ("scaleAP", "法术强度", "法强"),
    "攻击": ("scaleAD", "攻击力", "<attackSpeed", "攻击速度", "Item_Keyword_OnHit", "攻击特效", "<crit"),
}

Num = Union[float, Tuple[str, Any]]


def _num(x: float) -> str:
    r = round(x, 2)
    return str(int(r)) if r == int(r) else f"{r:g}"


def _value(values: List[float]) -> float:
    # 数组按技能等级排，强化符文只有一级：取 1 号位（0 号位常是占位）
    return values[1] if len(values) > 1 else (values[0] if values else 0.0)


def _part(p: Any, dv: Dict[str, float]) -> Optional[Num]:
    """算式的一个组成部分 → 常数、('range', 起, 止)、('stat', 文本)；算不了返回 None。"""
    if not isinstance(p, dict):
        return None
    t = p.get("__type")
    if t == "NumberCalculationPart":
        return float(p.get("mNumber", 0.0))
    if t == "NamedDataValueCalculationPart":
        return dv.get(str(p.get("mDataValue", "")).lower())
    if t in ("StatByNamedDataValueCalculationPart", "StatByCoefficientCalculationPart"):
        ratio = (dv.get(str(p.get("mDataValue", "")).lower()) if t.startswith("StatByNamed")
                 else p.get("mCoefficient", 0.0))
        stat = STAT_NAMES.get(p.get("mStat", 0))
        if ratio is None or stat is None:
            return None
        if p.get("mStatFormula") == BONUS_FORMULA:
            stat = "额外" + stat.replace("最大", "")
        return ("stat", f"{_num(ratio * 100)}%{stat}")
    if t == "ByCharLevelInterpolationCalculationPart":
        return ("range", float(p.get("mStartValue", 0.0)), float(p.get("mEndValue", 0.0)))
    return None


def _calc(c: Any, dv: Dict[str, float]) -> Optional[str]:
    """mSpellCalculations 里的一条 → 可读文本：常数求和，按等级成长写成 10~50，属性加成写成 (+30%法强)。"""
    if not isinstance(c, dict) or c.get("__type") != "GameCalculation":
        return None
    const, rng, stats = 0.0, None, []
    for p in c.get("mFormulaParts") or []:
        r = _part(p, dv)
        if r is None:
            return None
        if isinstance(r, float):
            const += r
        elif r[0] == "range" and rng is None:
            rng = (r[1], r[2])
        elif r[0] == "stat":
            stats.append(r[1])
        else:
            return None
    mult = 1.0
    if c.get("mMultiplier") is not None:
        m = _part(c["mMultiplier"], dv)
        if not isinstance(m, float):
            return None
        mult = m
    if stats and mult != 1.0:
        return None
    scale = mult * (100 if c.get("mDisplayAsPercent") else 1)
    pct = "%" if c.get("mDisplayAsPercent") else ""
    if rng:
        base = f"{_num((rng[0] + const) * scale)}{pct}~{_num((rng[1] + const) * scale)}{pct}（随等级）"
    elif const or not stats:
        base = f"{_num(const * scale)}{pct}"
    else:
        return " + ".join(stats)
    return base + (f"（+{' +'.join(stats)}）" if stats else "")


def _spell_data(kiwi: Dict[str, Any], aug: Dict[str, Any]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """符文用到的数值：根技能、附加技能，再加同目录下的其他技能（描述有时引用它们的数值）。"""
    paths = [aug.get("RootSpell")] + list(aug.get("AdditionalSpells") or [])
    folder = (aug.get("RootSpell") or "").rsplit("/", 1)[0] + "/"
    if folder != "/":
        paths += [k for k, v in kiwi.items() if k.startswith(folder) and isinstance(v, dict)
                  and v.get("__type") == "SpellObject" and k not in paths]
    dv: Dict[str, float] = {}
    calcs: Dict[str, Any] = {}
    for p in paths:
        spell = (kiwi.get(p) or {}).get("mSpell") or {}
        for d in spell.get("DataValues") or []:
            if d.get("name"):
                dv.setdefault(d["name"].lower(), float(_value(d.get("values") or [])))
        for k, c in (spell.get("mSpellCalculations") or {}).items():
            calcs.setdefault(k.lower(), c)
    return dv, calcs


def render(text: str, dv: Dict[str, float], calcs: Dict[str, Any], strings: Dict[str, str]) -> Tuple[str, int]:
    """填占位符、去掉排版标签，返回 (纯文本, 没填上的占位符个数)。"""
    missing = 0

    def fill(m: re.Match) -> str:
        nonlocal missing
        name, mul = m.group(1).lower(), m.group(2)
        if name in dv:
            return _num(dv[name] * (float(mul) if mul else 1.0))
        if name in calcs and not mul:
            r = _calc(calcs[name], dv)
            if r is not None:
                return r
        missing += 1
        return ""

    def strip(s: str) -> str:
        s = re.sub(r"%i:[A-Za-z]+%", "", s)
        s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
        return re.sub(r"<[^>]+>", "", s)

    def keyword(m: re.Match) -> str:
        # {{ Item_Keyword_OnHit }} → "攻击特效"；{{SpellName}} 这类运行时才知道的留空
        s = strip(strings.get(m.group(1).lower(), "")).strip()
        return s if s and "@" not in s and "{{" not in s and len(s) <= 12 else ""

    out = PLACEHOLDER.sub(fill, text)
    out = KEYWORD.sub(keyword, out)
    out = out.replace("【】", "指定")           # 【{{SpellName}}】技能：随机指定的某个技能
    out = strip(out)
    out = re.sub(r"%(（[^）]*）)%", r"%\1", out)          # 算式自带 % 又跟了原文的 %
    out = re.sub(r"(\d)(（[^）]*）)%", r"\1%\2", out)     # 25（+35%额外护甲）% → 25%（+35%额外护甲）
    out = re.sub(r"(?<![\d）])%", "", out)                # 数值没填上留下的孤立 %
    out = re.sub(r"\s+", " ", out).strip()
    return out, missing


def tags_of(raw: str) -> List[str]:
    return [tag for tag, words in TAG_WORDS.items() if any(w in raw for w in words)]


def parse_kiwi_augments(kiwi: Dict[str, Any], strings: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """kiwi.bin.json + 文本表 → {符文 key（ARAM_BigBrain）: {desc, tags}}。

    strings 的 key 不区分大小写（bin 里写 Kiwi_BigBrain_Summary，文本表里是 kiwi_bigbrain_summary）。
    描述优先用卡片上的简介（DescriptionTra），占位符填不上的比完整说明（AugmentTooltipTra）多时换成完整说明。
    """
    strings = {k.lower(): v for k, v in strings.items()}
    out: Dict[str, Dict[str, Any]] = {}
    for aug in kiwi.values():
        if not isinstance(aug, dict) or aug.get("__type") != "AugmentData" or not aug.get("AugmentNameId"):
            continue
        dv, calcs = _spell_data(kiwi, aug)
        best: Optional[Tuple[str, int, str]] = None
        for key in ("DescriptionTra", "AugmentTooltipTra"):
            raw = strings.get(str(aug.get(key) or "").lower())
            if not raw:
                continue
            text, missing = render(raw, dv, calcs, strings)
            if text and (best is None or missing < best[1]):
                best = (text, missing, raw)
        if best:
            out[aug["AugmentNameId"]] = {"desc": best[0], "tags": tags_of(best[2] + best[0])}
    return out
