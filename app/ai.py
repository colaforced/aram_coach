"""AI 教练：把对局状态整理成"事实"，按任务拼提示词。

分工原则：确定的事（价格、合成、敌我、该买什么、换不换英雄）由本地规则算好，作为
【本地规则结论】交给模型；模型负责解释、补充判断，数值只能用这里给出的，不凭记忆。

三个任务各有提示词：
- ask：玩家随口问，直接回答，几行以内；带最近几轮问答，追问不丢上下文
- deep：整局分析，一句结论 + 最多三条行动；对局中带上上次的分析，只讲变化；选人阶段换一套
- augment：强化符文三选一，只要三行
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app import augments
from app.advice import enemy_threats, moment_text
from app.items import ItemDB
from app.lcd_client import resolve_my_enemy_teams

HISTORY_TURNS = 4          # 追问时带上最近几轮
HISTORY_ANSWER_CHARS = 1200
PREVIOUS_ANSWER_CHARS = 400   # 深度分析带上上次的结论
MAX_PRICE_ITEMS = 16

SYSTEM_BASE = """你是英雄联盟大乱斗教练，玩家正在玩的模式见【模式】。
地图是嚎哭深渊：只有一条路连着两边基地，路边有回血包；没有河道、野区、打野、小龙、大龙、峡谷先锋，也不能插眼。
不要套用召唤师峡谷的打法（过河道、控龙、打野、换线、带线、做视野、回城补给），这些在大乱斗里都不存在。
规则：
- 大乱斗不能回城：只有开局在泉水、或阵亡后才能买装备，不要建议"回城"。
- 数值只用【对局数据】里给出的：价格、属性、胜率、梯度、经济都以它为准；数据里没有的数字不要报，也不要凭记忆补。
- 看不到别人身上的现金，团队经济是按人头 / 助攻 / 补刀估算的，说经济差时说"约"，不要说成精确值。
- 【本地规则结论】（选人建议、购物计划、针对性出装）是按当前版本数据和装备合成树算出来的，默认照它说；你有不同意见时要讲清理由。
- 不要建议【本模式禁用装备】里的装备。
- 用中文，口语化，结论先行；不用 Markdown 标题和表格，可以用 emoji 开头分点，重点可以用 **加粗**。"""

TASK_ASK = """玩家在对局中向你提问。直接回答这个问题：3～6 行，先给结论；不要套固定的分析结构，和问题无关的内容不要说。"""

TASK_DEEP_GAME = """玩家在对局中点了"深度分析"。他只有阵亡读秒或团战间隙的几秒能看，所以要短、要有取舍。
输出：
- 第一行：🎯 局势一句话（优势 / 均势 / 劣势，约多少经济）+ 最关键的一个原因；有【上次分析】时先说跟上次比变了什么
- 然后 1～3 条行动，每条一行、不超过 30 字、emoji 开头，按对这局胜负的影响排序
- 全部加起来不超过 150 字；宁可少写一条，也不要凑数
每条行动都要落到具体的英雄、装备或时机上。不要写：
- 放到哪一局都成立的话（"别乱送""抱团""别第一个进场""雪球留着收残血"）
- 屏幕上已经有的人头、存活数
- 【本地规则结论·出装】里已经写了的购买计划，以及现金够不够买什么（购物卡会讲）；只有你不同意它、
  或敌方阵容要求调整时才提出装，并说理由，装备名只用【对局数据】里出现过的
- 强化符文（三选一时有单独的分析）
玩家阵亡中：购物卡已经告诉他现在买什么，重点说复活后第一波怎么打。
上次分析说过、现在仍然成立的行动不再重复，除非它仍是最重要的一条。

示例（只参考格式和长度，内容按本局数据来；这个示例只有两条行动，说明不必凑满三条）：
🎯 小劣约 800G，对面金克丝 9 杀没死过，是他们的核心
⚔️ 下波团等锤石钩到人再进，先切金克丝，别追坦克
⏳ 对面 3 人 20 秒后才活，趁现在推掉二塔"""

TASK_DEEP_SELECT = """现在是选人阶段（看不到敌方），玩家要在几秒内做决定，所以要短、要有取舍。
输出：
- 第一行：🎯 换 / 骰 / 留 + 一句理由（照【本地规则结论·选人】说，不同意时讲清理由）
- 然后最多 3 条，每条一行、不超过 30 字、emoji 开头，只写这局用这个英雄最该知道的：在本队阵容里负责什么、
  出装或符文和推荐不同的地方、适合的强化符文方向
- 全部加起来不超过 150 字；没有值得说的就少写，不要写对任何英雄都成立的话"""

TASK_AUGMENT = """玩家正在强化符文三选一。只输出三行，每行不超过 30 字：
1. 选哪个
2. 为什么
3. 拿了之后出装或打法要怎么调整
强化符文的效果以玩家消息里给的描述为准，不要凭记忆补效果；国服胜率是所有英雄的平均，要结合我的英雄和阵容判断。"""

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
    augments: List[Dict[str, Any]] = field(default_factory=list)  # 强化符文池（含效果描述、国服统计）
    augment_levels: List[int] = field(default_factory=lambda: [3, 7, 11, 15])


# ---------------------------------------------------------------- 事实

def _pct(v: Any) -> str:
    return f"{v * 100:.0f}%" if isinstance(v, (int, float)) and v < 1 else f"{v}"


def _champ(p: Dict[str, Any]) -> str:
    return p.get("champion") or p.get("champion_name") or "?"


def is_mayhem(state: Dict[str, Any]) -> bool:
    # 拿不到模式时按海斗算：这个工具主要给海斗用
    return state.get("game_mode") in (None, "", "KIWI")


def mode_line(state: Dict[str, Any], deps: Deps) -> str:
    mode = state.get("game_mode")
    if is_mayhem(state):
        levels = "/".join(map(str, deps.augment_levels))
        return f"【模式】海克斯大乱斗（海斗，ARAM: Mayhem），地图嚎哭深渊；升到 {levels} 级时强化符文三选一"
    if mode == "ARAM":
        return "【模式】极地大乱斗（普通大乱斗，没有强化符文），地图嚎哭深渊"
    return f"【模式】{mode}（不是大乱斗，大乱斗的经验未必适用）"


def build_facts(state: Dict[str, Any], deps: Deps) -> str:
    """【对局数据】：只放英雄名，不放召唤师名（不需要，也不该发给第三方）。"""
    lines: List[str] = [mode_line(state, deps)]
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
        lines.extend(_game_facts(state, lcd, deps.alias))

    lines.extend(_conclusions(state))
    lines.extend(_prices(state, deps))
    if deps.banned:
        lines.append(f"【本模式禁用装备】{'、'.join(deps.banned)}")
    return "\n".join(lines)


def _game_facts(state: Dict[str, Any], lcd: Dict[str, Any], alias: Dict[str, str]) -> List[str]:
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
    total = lambda team, key: sum(p.get(key, 0) for p in team)  # noqa: E731
    alive = lambda team: sum(not p.get("is_dead") for p in team)  # noqa: E731
    est_diff = total(my_team, "est_gold") - total(enemy_team, "est_gold")
    towers = lcd.get("towers") or {}
    side = lambda team: (team[0].get("team") if team else None)  # noqa: E731
    tower_text = (f"；推塔 {towers.get(side(my_team), 0)} vs {towers.get(side(enemy_team), 0)}"
                  if any(towers.values()) else "")
    lines.append(f"【团队经济·估算】我方 {total(my_team, 'est_gold')}G vs 敌方 {total(enemy_team, 'est_gold')}G"
                 f"（差 {est_diff:+d}）；人头 {total(my_team, 'kills')} vs {total(enemy_team, 'kills')}{tower_text}")

    def label(p: Dict[str, Any]) -> str:
        """LCD 给的多是称号（残月之肃），附上本名（厄斐琉斯），免得模型自己对错英雄。"""
        name, real = _champ(p), alias.get(str(p.get("champion_id")))
        return f"{name}（{real}）" if real and real != name else name

    def headcount(team: List[Dict[str, Any]], side_name: str) -> str:
        dead = [f"{label(p)} {round(p.get('respawn_timer') or 0)}s" for p in team if p.get("is_dead")]
        return f"{side_name}活着 {alive(team)} 个" + (f"（阵亡：{'、'.join(dead)} 后复活）" if dead else "")

    # 人数单独一行写清谁死了：只在经济那行末尾写"存活 3 vs 2"时，模型会说成"5 打 2"
    lines.append(f"【人数】{headcount(my_team, '我方')}；{headcount(enemy_team, '敌方')}")
    lines.append(f"【装备总价】我方 {total(my_team, 'item_gold')}G vs 敌方 {total(enemy_team, 'item_gold')}G"
                 f"——只算已经买成装备的钱。大乱斗只能阵亡时买，阵亡少的一方钱攒在身上，"
                 f"这个数会低估它，判断局势看上面的估算经济")

    # 本地数据里没有的新英雄没有定位，标出来，免得 AI 把它当成不存在
    roles = {m.get("champion_id"): (m.get("coach") or {}).get("role")
             or (f"新英雄，装备偏{m['dmg'].upper()}" if m.get("dmg") else "新英雄")
             for m in (state.get("my_team") or []) + (state.get("enemy_team") or [])}
    names = {m.get("champion_name"): roles.get(m.get("champion_id"), "")
             for m in (state.get("my_team") or []) + (state.get("enemy_team") or [])}

    def row(p: Dict[str, Any]) -> str:
        items = "、".join(it.get("name", "") for it in p.get("items") or [] if it.get("name") and it.get("slot") != 6)
        role = names.get(_champ(p), "")
        dead = f"，阵亡 {round(p.get('respawn_timer') or 0)}s" if p.get("is_dead") else ""
        return (f"  - {label(p)}{'（我）' if p.get('is_me') else ''}{f'[{role}]' if role else ''} Lv{p.get('level', '?')} "
                f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)} "
                f"估算经济 {p.get('est_gold', 0)}G 装备 {p.get('item_gold', 0)}G"
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
        dmg = f"伤害构成 AP {th['ap']} / AD {th['ad']}"
        if th["by_items"]:
            dmg += f"（{'、'.join(th['by_items'])} 是新英雄，按身上装备判断）"
        if th["unknown"]:
            dmg += f"，另有 {'、'.join(th['unknown'])} 是新英雄、还看不出输出类型"
        notes.append(dmg)
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
    三选一和选人阶段的整局分析再加强化符文梯度。

    对局中的整局分析不给强化符文、装备误区和雪球技巧：给了模型就会每次都拿来凑一段。
    """
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

    if task in ("ask", "deep_select", "augment") and is_mayhem(state):
        best = augments.for_champion(deps.augments, state.get("my_champion_id"))
        if best:
            lines.append(f"国服数据里最适合{coach.get('name') or '我的英雄'}的强化符文（胜率从高到低）："
                         + "、".join(f"{a['name']}（{a['stats']['win_rate'] * 100:.0f}%）" for a in best))

    if task in ("deep_select", "augment"):
        aug = kb.get("hextech_augments") or {}
        for group, label in (("prismatic_s_tier", "棱彩S级"), ("gold_key", "金色核心"), ("situational", "看情况")):
            for a in aug.get(group) or []:
                if isinstance(a, dict):
                    lines.append(f"强化符文[{label}] {a.get('name')}：{a.get('effect', '')}（适合 {a.get('best_for', '')}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 组装消息

def _system(task_prompt: str, state: Dict[str, Any], deps: Deps, task: str, extra: str = "") -> Dict[str, str]:
    kb = kb_slices(state, deps, task)
    content = f"{SYSTEM_BASE}\n\n任务：{task_prompt}\n\n【对局数据】\n{build_facts(state, deps)}"
    if kb:
        content += f"\n\n【知识库（本局相关）】\n{kb}"
    if extra:
        content += f"\n\n{extra}"
    return {"role": "system", "content": content}


def ask_messages(state: Dict[str, Any], deps: Deps, question: str,
                 history: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    # 问题（含追问前的几轮）里提到了哪个强化符文，就把它的效果和国服数据带上，免得模型凭记忆编
    asked = " ".join([question] + [t.get("q") or "" for t in (history or [])[-HISTORY_TURNS:]])
    hits = augments.mentioned(deps.augments, asked)
    extra = ("【问题里提到的强化符文】\n" + "\n".join(f"- {augments.describe(a, deps.alias)}" for a in hits)
             if hits else "")
    msgs = [_system(TASK_ASK, state, deps, "ask", extra)]
    for turn in (history or [])[-HISTORY_TURNS:]:
        q, a = (turn.get("q") or "").strip(), (turn.get("a") or "").strip()
        if q and a:
            msgs.append({"role": "user", "content": q})
            msgs.append({"role": "assistant", "content": a[:HISTORY_ANSWER_CHARS]})
    msgs.append({"role": "user", "content": question.strip()})
    return msgs


def _clock(t: float) -> str:
    return f"{int(t // 60)}分{int(t % 60)}秒"


def deep_digest(state: Dict[str, Any]) -> Dict[str, Any]:
    """这一刻的局势摘要，和这次的分析一起存下来，下次分析时对比。"""
    lcd = state.get("lcd") or {}
    my_team, enemy_team = resolve_my_enemy_teams(lcd.get("team_map") or {})
    total = lambda team, key: sum(p.get(key) or 0 for p in team)  # noqa: E731
    return {"game_id": state.get("game_id"), "game_time": lcd.get("game_time", 0),
            "est_diff": total(my_team, "est_gold") - total(enemy_team, "est_gold"),
            "kills": [total(my_team, "kills"), total(enemy_team, "kills")]}


def deep_request(state: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> str:
    """对局中深度分析的提问：我此刻是死是活（决定重点讲什么），以及上次分析说了什么（只讲变化）。"""
    lcd = state.get("lcd") or {}
    me = next((p for ps in (lcd.get("team_map") or {}).values() for p in ps if p.get("is_me")), None)
    lines = []
    if me and me.get("is_dead"):
        lines.append(f"我现在阵亡，{round(me.get('respawn_timer') or 0)} 秒后复活。")
    elif me:
        lines.append("我现在活着。")
    if previous and previous.get("answer"):
        kills = previous.get("kills") or [0, 0]
        lines.append(f"【上次分析】{_clock(previous.get('game_time') or 0)}（当时估算经济差 {previous.get('est_diff', 0):+d}，"
                     f"人头 {kills[0]} vs {kills[1]}）：\n{previous['answer'][:PREVIOUS_ANSWER_CHARS]}")
    lines.append("分析一下现在的局势。")
    return "\n".join(lines)


def deep_messages(state: Dict[str, Any], deps: Deps,
                  previous: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """previous：本局上一次对局中深度分析的 deep_digest + answer，没有就不带。"""
    if state.get("phase") == "ChampSelect":
        return [_system(TASK_DEEP_SELECT, state, deps, "deep_select"), {"role": "user", "content": "分析一下这一局。"}]
    return [_system(TASK_DEEP_GAME, state, deps, "deep"), {"role": "user", "content": deep_request(state, previous)}]


def augment_messages(state: Dict[str, Any], deps: Deps, options: List[str],
                     ranking: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    by_name = {a["name"]: a for a in deps.augments}
    lines = []
    for r in ranking:
        a = by_name.get(r["name"])
        lines.append(f"- {augments.describe(a, deps.alias)}" if a
                     else f"- {r['name']}：不在海斗强化符文池里（可能名字没认对），没有效果描述")
        if r.get("reasons"):
            lines.append(f"  本地评分 {r['score']}：{'；'.join(r['reasons'])}")
    return [
        _system(TASK_AUGMENT, state, deps, "augment"),
        {"role": "user", "content": f"三个选项：{' / '.join(options)}\n效果和数据：\n" + "\n".join(lines)},
    ]
