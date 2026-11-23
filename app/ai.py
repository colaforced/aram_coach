"""AI 教练：把对局状态整理成"事实"，按任务拼提示词。

分工原则：确定的事（价格、合成、敌我、该买什么、换不换英雄）由本地规则算好，作为
【本地规则结论】交给模型；模型负责解释、补充判断，数值只能用这里给出的，不凭记忆。

三个任务各有提示词：
- ask：玩家随口问，直接回答，几行以内；带最近几轮问答，追问不丢上下文
- deep：整局分析，固定四段；选人阶段换一套四段
- augment：强化符文三选一，只要三行
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.advice import enemy_threats, moment_text
from app.items import ItemDB
from app.lcd_client import resolve_my_enemy_teams

HISTORY_TURNS = 4          # 追问时带上最近几轮
HISTORY_ANSWER_CHARS = 1200
MAX_PRICE_ITEMS = 16

SYSTEM_BASE = """你是英雄联盟「海克斯大乱斗」（ARAM: Mayhem）教练。
规则：
- 大乱斗不能回城：只有开局在泉水、或阵亡后才能买装备，不要建议"回城"。
- 数值只用【对局数据】里给出的：价格、属性、胜率、梯度、装备总价都以它为准；数据里没有的数字不要报，也不要凭记忆补。
- 【本地规则结论】（选人建议、购物计划、针对性出装）是按当前版本数据和装备合成树算出来的，默认照它说；你有不同意见时要讲清理由。
- 不要建议【本模式禁用装备】里的装备。
- 用中文，口语化，结论先行；不用 Markdown 标题和表格，可以用 emoji 开头分点，重点可以用 **加粗**。"""

TASK_ASK = """玩家在对局中向你提问。直接回答这个问题：3～6 行，先给结论；不要套固定的分析结构，和问题无关的内容不要说。"""

TASK_DEEP_GAME = """给出这一局的分析，按下面四段输出，每段 2～4 行：
🎯 局势：优势 / 均势 / 劣势，依据（装备总价差、存活、双方关键英雄）
🛒 出装：照【本地规则结论】说接下来买什么、为什么；对面有回复、坦克多或伤害偏 AP/AD 时怎么应对
⚔️ 打法：团战站位、先打谁、雪球和关键技能怎么用（结合双方具体英雄）
🎲 强化符文：适合这个英雄和本局的方向（参考知识库）"""

TASK_DEEP_SELECT = """现在是选人阶段（看不到敌方），按下面四段输出，每段 2～4 行：
🎯 定位：这个英雄在本队阵容里负责什么
🔁 换不换：照【本地规则结论】的选人建议说，补充理由
🛒 出装与符文：核心思路，和推荐的不同时说明原因
🎲 强化符文：适合的方向（参考知识库）"""

TASK_AUGMENT = """玩家正在强化符文三选一。只输出三行，每行不超过 30 字：
1. 选哪个
2. 为什么
3. 拿了之后出装或打法要怎么调整"""

# 定位 → 知识库里对应的出装公式
ROLE_RECIPES = {
    "战士": ["战士通用公式"],
    "坦克": ["坦克通用公式"],
    "法师": ["法师爆发公式", "法师持续伤害公式"],
    "射手": ["射手暴击公式", "射手穿甲公式"],
    "刺客": ["刺客通用公式"],
}


@dataclass
class Deps:
    """构建上下文要用的本地数据（由 main 提供，方便测试时替换）。"""
    items: ItemDB
    kb: Dict[str, Any]
    kb_specific_for: Callable[[str], Optional[Dict[str, Any]]]
    banned: Dict[str, str] = field(default_factory=dict)        # 禁用装备 → 替代
    alias: Dict[str, str] = field(default_factory=dict)         # 英雄 ID → 本名


# ---------------------------------------------------------------- 事实

def _pct(v: Any) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) and v < 1 else f"{v}"


def _champ(p: Dict[str, Any]) -> str:
    return p.get("champion") or p.get("champion_name") or "?"


def build_facts(state: Dict[str, Any], deps: Deps) -> str:
    """【对局数据】：只放英雄名，不放召唤师名（不需要，也不该发给第三方）。"""
    lines: List[str] = []
    coach = state.get("my_coach") or {}
    phase = state.get("phase", "")
    me_name = coach.get("name") or state.get("my_champion_name") or "?"
    lines.append(f"【阶段】{'选人' if phase == 'ChampSelect' else '对局中'}；我玩 {me_name}（{coach.get('role', '?')}）")

    op = coach.get("opgg") or {}
    if op.get("tier") is not None:
        lines.append(f"【我的英雄数据】OP.GG 大乱斗 T{op['tier']}，胜率 {_pct(op.get('win_rate') or 0)}"
                     f"，选取率 {_pct(op.get('pick_rate') or 0)}")
    if coach.get("aram_balance_text"):
        lines.append(f"【大乱斗平衡修正】{coach['aram_balance_text']}")
    if coach.get("build"):
        lines.append(f"【推荐出装】{' → '.join(coach['build'])}")
    if coach.get("primary_rune"):
        lines.append(f"【推荐符文】{coach.get('primary_branch')}·{' / '.join(coach.get('primary_details') or [])}；"
                     f"副系 {coach.get('secondary_branch')}·{coach.get('secondary_rune')}")

    lcd = state.get("lcd") or {}
    if phase == "ChampSelect" or not lcd.get("available"):
        mates = [f"{m.get('champion_name')}（{(m.get('coach') or {}).get('role', '?')}）"
                 for m in state.get("my_team") or [] if m.get("champion_id") != state.get("my_champion_id")]
        if mates:
            lines.append(f"【队友】{'、'.join(mates)}")
        bench = [b.get("champion_name") for b in state.get("bench") or []]
        if bench:
            lines.append(f"【备选席】{'、'.join(bench)}")
    else:
        lines.extend(_game_facts(state, lcd))

    lines.extend(_conclusions(state))
    lines.extend(_prices(state, deps))
    if deps.banned:
        lines.append(f"【本模式禁用装备】{'、'.join(deps.banned)}")
    return "\n".join(lines)


def _game_facts(state: Dict[str, Any], lcd: Dict[str, Any]) -> List[str]:
    lines = []
    t = lcd.get("game_time", 0)
    a = lcd.get("active") or {}
    st = a.get("champion_stats") or {}
    stats = [("AD", st.get("ad")), ("AP", st.get("ap")), ("护甲", st.get("armor")), ("魔抗", st.get("mr")),
             ("攻速", st.get("attack_speed")), ("技能急速", st.get("ability_haste")),
             ("暴击", _pct(st.get("crit_chance") or 0)), ("护甲穿透", st.get("armor_pen_flat")),
             ("法穿", st.get("magic_pen_flat")), ("移速", st.get("move_speed"))]
    lines.append(f"【时间】{int(t // 60)}分{int(t % 60)}秒")
    lines.append(f"【我】Lv{a.get('level', '?')}，金币 {int(a.get('gold') or 0)}，"
                 f"HP {st.get('hp', '?')}/{st.get('max_hp', '?')}；"
                 + "，".join(f"{k} {v}" for k, v in stats if v not in (None, 0, "0%")))

    my_team, enemy_team = resolve_my_enemy_teams(lcd.get("team_map") or {})
    mine = sum(p.get("item_gold", 0) for p in my_team)
    theirs = sum(p.get("item_gold", 0) for p in enemy_team)
    alive = lambda team: sum(not p.get("is_dead") for p in team)  # noqa: E731
    lines.append(f"【装备总价】我方 {mine}G vs 敌方 {theirs}G（差 {mine - theirs:+d}）；"
                 f"存活 {alive(my_team)} vs {alive(enemy_team)}")

    roles = {m.get("champion_id"): (m.get("coach") or {}).get("role", "")
             for m in (state.get("my_team") or []) + (state.get("enemy_team") or [])}
    names = {m.get("champion_name"): roles.get(m.get("champion_id"), "")
             for m in (state.get("my_team") or []) + (state.get("enemy_team") or [])}

    def row(p: Dict[str, Any]) -> str:
        items = "、".join(it.get("name", "") for it in p.get("items") or [] if it.get("name") and it.get("slot") != 6)
        role = names.get(_champ(p), "")
        dead = f"，阵亡 {round(p.get('respawn_timer') or 0)}s" if p.get("is_dead") else ""
        return (f"  - {_champ(p)}{'（我）' if p.get('is_me') else ''}{f'[{role}]' if role else ''} Lv{p.get('level', '?')} "
                f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)} 装备 {p.get('item_gold', 0)}G"
                f"{f'：{items}' if items else ''}{dead}")

    lines.append("【我方】")
    lines.extend(row(p) for p in my_team)
    lines.append("【敌方】")
    lines.extend(row(p) for p in enemy_team)
    th = enemy_threats(state.get("enemy_team") or [])
    notes = []
    if th["healers"]:
        notes.append(f"回复型：{'、'.join(th['healers'])}")
    if th["tanks"]:
        notes.append(f"坦克：{'、'.join(th['tanks'])}")
    if th["ap"] + th["ad"]:
        notes.append(f"伤害构成 AP {th['ap']} / AD {th['ad']}")
    if notes:
        lines.append(f"【敌方特点】{'；'.join(notes)}")
    return lines


def _conclusions(state: Dict[str, Any]) -> List[str]:
    lines = []
    v = state.get("verdict") or {}
    if v.get("headline"):
        lines.append(f"【本地规则结论·选人】{v['headline']}：{v.get('reason', '')}")
    mo = state.get("moment")
    if mo:
        lines.append("【本地规则结论·出装】" + moment_text(mo).replace("\n", "；"))
        if mo.get("targets"):
            lines.append(f"【接下来的成装顺序】{' → '.join(mo['targets'])}")
    return lines


def _prices(state: Dict[str, Any], deps: Deps) -> List[str]:
    """出现在推荐、计划、针对性调整里的装备价格，免得模型用记忆里的旧价格。"""
    names: List[str] = []
    coach = state.get("my_coach") or {}
    names += coach.get("build") or []
    mo = state.get("moment") or {}
    plan = mo.get("plan") or {}
    names += [b["name"] for b in plan.get("buys") or []]
    if plan.get("focus"):
        names.append(plan["focus"]["name"])
    names += [a["item"] for a in mo.get("adjustments") or []]
    names += mo.get("targets") or []
    seen, out = set(), []
    for n in names:
        if n and n not in seen and deps.items.id_of(n):
            seen.add(n)
            out.append(f"{n} {deps.items.total_by_name(n)}G")
    return [f"【装备价格（当前版本）】{'、'.join(out[:MAX_PRICE_ITEMS])}"] if out else []


# ---------------------------------------------------------------- 知识库切片

def kb_slices(state: Dict[str, Any], deps: Deps, task: str) -> str:
    """只给和本局相关的知识库内容：本局英雄的梯度、我这个英雄的专属数据、我这个定位的出装公式；
    整局分析再加装备误区、雪球技巧和强化符文梯度。"""
    kb = deps.kb or {}
    if not kb:
        return ""
    coach = state.get("my_coach") or {}
    lines = [f"版本 {kb.get('version', '?')}：{kb.get('meta_summary', '')}"]

    in_game = set()
    for m in (state.get("my_team") or []) + (state.get("enemy_team") or []):
        for n in (m.get("champion_name"), (m.get("coach") or {}).get("name"), deps.alias.get(str(m.get("champion_id")))):
            if n:
                in_game.add(n)
    for tier in ("t0", "t1"):
        for e in (kb.get("tier_list") or {}).get(tier) or []:
            if isinstance(e, dict) and ({e.get("name"), e.get("chinese")} & in_game):
                lines.append(f"梯度 {tier.upper()}：{e.get('chinese') or e.get('name')}（{e.get('role', '')}）{e.get('reason', '')}")

    spec = deps.kb_specific_for(coach.get("name", ""))
    if spec:
        bits = [f"{k}={v if not isinstance(v, list) else '、'.join(map(str, v))}" for k, v in spec.items()
                if k in ("tier", "win_rate", "aram_buff", "best_core_build", "skill_order", "best_runes")]
        lines.append(f"{coach.get('name')}专属：{'；'.join(bits)}")
        lines.extend(f"{coach.get('name')}技巧：{t}" for t in (spec.get("tips") or [])[:4])

    for rname in ROLE_RECIPES.get(coach.get("role", ""), []):
        r = (kb.get("core_build_recipes") or {}).get(rname)
        if r:
            seq = " → ".join("/".join(r.get(k) or []) for k in ("first", "second", "third", "late") if r.get(k))
            lines.append(f"{rname}：{seq}。{r.get('note', '')}")

    if task in ("deep", "augment"):
        aug = kb.get("hextech_augments") or {}
        for group, label in (("prismatic_s_tier", "棱彩S级"), ("gold_key", "金色核心"), ("situational", "看情况")):
            for a in aug.get(group) or []:
                if isinstance(a, dict):
                    lines.append(f"强化符文[{label}] {a.get('name')}：{a.get('effect', '')}（适合 {a.get('best_for', '')}）")
    if task == "deep":
        for m in kb.get("item_myths") or []:
            if isinstance(m, dict):
                lines.append(f"装备误区：{m.get('myth')} → {m.get('truth')}")
        lines.extend(f"雪球技巧：{t}" for t in (kb.get("snowball_tips") or [])[:3])
    return "\n".join(lines)


# ---------------------------------------------------------------- 组装消息

def _system(task_prompt: str, state: Dict[str, Any], deps: Deps, task: str) -> Dict[str, str]:
    kb = kb_slices(state, deps, task)
    content = f"{SYSTEM_BASE}\n\n任务：{task_prompt}\n\n【对局数据】\n{build_facts(state, deps)}"
    if kb:
        content += f"\n\n【知识库（本局相关）】\n{kb}"
    return {"role": "system", "content": content}


def ask_messages(state: Dict[str, Any], deps: Deps, question: str,
                 history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    msgs = [_system(TASK_ASK, state, deps, "ask")]
    for turn in (history or [])[-HISTORY_TURNS:]:
        q, a = (turn.get("q") or "").strip(), (turn.get("a") or "").strip()
        if q and a:
            msgs.append({"role": "user", "content": q})
            msgs.append({"role": "assistant", "content": a[:HISTORY_ANSWER_CHARS]})
    msgs.append({"role": "user", "content": question.strip()})
    return msgs


def deep_messages(state: Dict[str, Any], deps: Deps) -> List[Dict[str, str]]:
    task = TASK_DEEP_SELECT if state.get("phase") == "ChampSelect" else TASK_DEEP_GAME
    return [_system(task, state, deps, "deep"), {"role": "user", "content": "分析一下这一局。"}]


def augment_messages(state: Dict[str, Any], deps: Deps, options: List[str],
                     ranking: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    known = "\n".join(f"- {r['name']}：{'；'.join(r.get('reasons') or []) or '知识库无数据'}" for r in ranking)
    return [
        _system(TASK_AUGMENT, state, deps, "augment"),
        {"role": "user", "content": f"三个选项：{' / '.join(options)}\n本地知识库的参考：\n{known}"},
    ]
