import asyncio
import json
import logging
import httpx
from typing import AsyncGenerator, Optional, Dict, Any, List

from app.advice import moment_text
from app.lcd_client import resolve_my_enemy_teams

log = logging.getLogger("aram_coach.llm")


SYSTEM_PROMPT = """你是英雄联盟大乱斗（ARAM）专家教练，名叫"乱斗导师"。
你精通：
- 170+ 英雄的乱斗适配性、符文、出装、连招
- 大乱斗特殊机制：雪球、治疗宝珠、草丛、窄团战
- 大乱斗不能回城：只有开局在泉水、或阵亡后才能买装备，别建议"回城"
- 装备克制、对线期策略、团战站位
- 强化符文（Augments）三选一抉择

回答风格：
- 简洁实用，分点列出，每点不超 20 字
- 先给结论，再解释原因
- 数据说话（装备对比、属性差距、装备总价差）
- 不用 markdown 标题，直接用 emoji 分点
- 特殊提示用 ⚠️ 标记

当被要求分析时，按以下结构输出：
1. 🎯 当前局势（优/均/劣，理由）
2. 📊 装备对比（我 vs 对面关键装备、装备总价差）
3. 💪 属性面板解读（当前面板是否合理，缺什么）
4. 🛒 下一件装备推荐（优先级 + 替代）
5. ⚡ 操作建议（雪球怎么用、团战站位、技能连招）
6. 🎲 强化符文建议（如果是升级节点）"""


def _format_state_for_prompt(state: Dict[str, Any]) -> str:
    """将对局状态格式化为精简文本喂给 LLM"""
    parts: List[str] = []

    phase = state.get("phase", "Unknown")
    summoner = state.get("summoner_name", "?")
    parts.append(f"【基本信息】召唤师={summoner}, 阶段={phase}")

    my_champ = state.get("my_champion_name") or "?"
    my_role = ""
    if state.get("my_coach"):
        my_role = state["my_coach"].get("role", "")
    parts.append(f"【我的英雄】{my_champ} ({my_role})")

    lcd = state.get("lcd") or {}
    game_time = lcd.get("game_time", 0)
    m = int(game_time // 60)
    s = int(game_time % 60)
    parts.append(f"【游戏时间】{m}分{s}秒")

    active = (lcd.get("active") or {})
    if active:
        stats = active.get("champion_stats") or {}
        gold = active.get("gold", 0)
        level = active.get("level", "?")
        hp_pct = stats.get("hp_pct", 0)
        parts.append(
            f"【我的属性】等级={level}, 金币={int(gold)}, HP={stats.get('hp')}/{stats.get('max_hp')}({hp_pct}%), "
            f"AD={stats.get('ad')}, AP={stats.get('ap')}, "
            f"护甲={stats.get('armor')}, 魔抗={stats.get('mr')}, "
            f"攻速={stats.get('attack_speed')}, 移速={stats.get('move_speed')}, "
            f"技能急速={stats.get('ability_haste')}, 暴击={_pct(stats.get('crit_chance'))}, "
            f"护甲穿透={stats.get('armor_pen_flat')}({_pct(stats.get('armor_pen_pct'))}), "
            f"法穿={stats.get('magic_pen_flat')}({_pct(stats.get('magic_pen_pct'))}), "
            f"全能吸血={_pct(stats.get('omnivamp'))}, 韧性={_pct(stats.get('tenacity'))}"
        )
        abilities = active.get("abilities") or {}
        ab_info = " ".join(
            f"{k.upper()}={v.get('level', '?')}"
            for k, v in abilities.items()
        )
        if ab_info:
            parts.append(f"【技能】{ab_info}")

    team_map = lcd.get("team_map") or {}

    my_team, enemy_team = resolve_my_enemy_teams(team_map)

    my_total_gold = sum(p.get("item_gold", 0) for p in my_team)
    enemy_total_gold = sum(p.get("item_gold", 0) for p in enemy_team)
    parts.append(f"【团队装备总价】我方={my_total_gold}G vs 敌方={enemy_total_gold}G (差={my_total_gold - enemy_total_gold:+d})")

    def _fmt_player(p: Dict[str, Any]) -> str:
        name = p.get("name", "?")
        champ = p.get("champion", "?")
        level = p.get("level", "?")
        k = p.get("kills", 0)
        d = p.get("deaths", 0)
        a = p.get("assists", 0)
        dead = p.get("is_dead", False)
        respawn = p.get("respawn_timer", 0)
        dead_tag = f" 💀复活{respawn}s" if dead else ""
        items = [it.get("name", "") for it in p.get("items", []) if it.get("name")]
        item_str = f" 装备=[{','.join(items)}]" if items else ""
        me_tag = " (我)" if p.get("is_me") else ""
        cs = p.get("cs", 0)
        extra = f" 补刀={cs}" if cs else ""
        return (f"  - {name}{me_tag} [{champ}] Lv.{level} KDA={k}/{d}/{a} "
                f"装备总价={p.get('item_gold', 0)}G{dead_tag}{item_str}{extra}")

    if my_team:
        parts.append("【我方】")
        for p in my_team:
            parts.append(_fmt_player(p))
    if enemy_team:
        parts.append("【敌方】")
        for p in enemy_team:
            parts.append(_fmt_player(p))

    if state.get("my_coach") and active:
        coach = state["my_coach"]
        build = coach.get("build", [])
        if build:
            my_items = []
            for p in my_team + enemy_team:
                if p.get("is_me"):
                    my_items = [it.get("name", "") for it in p.get("items", []) if it.get("name")]
                    break
            parts.append(f"【推荐出装】{' → '.join(build)}")
            if my_items:
                parts.append(f"【已购买】{','.join(my_items)}")
        if coach.get("primary_rune"):
            rune_line = f"{coach.get('primary_branch')}·{' / '.join(coach.get('primary_details') or [coach['primary_rune']])}"
            if coach.get("secondary_branch"):
                rune_line += f"；副系 {coach['secondary_branch']}·{coach.get('secondary_rune', '')}"
            parts.append(f"【推荐符文】{rune_line}")
        if coach.get("spells"):
            parts.append(f"【召唤师技能】{' + '.join(coach['spells'])}")
        if coach.get("opgg"):
            o = coach["opgg"]
            parts.append(f"【OP.GG 大乱斗数据】T{o.get('tier')}，胜率 {(o.get('win_rate') or 0) * 100:.1f}%，"
                         f"选取率 {(o.get('pick_rate') or 0) * 100:.1f}%（版本 {o.get('version')}）")
        if coach.get("aram_balance_text"):
            parts.append(f"【大乱斗平衡修正】{coach['aram_balance_text']}")

    moment = moment_text(state.get("moment"))
    if moment:
        parts.append(f"【本地规则算出的购物计划】{moment}")
    verdict = state.get("verdict") or {}
    if verdict.get("headline"):
        parts.append(f"【选人建议】{verdict['headline']}：{verdict.get('reason', '')}")

    question = state.get("coach_question", "")
    if question:
        parts.append(f"【玩家问题】{question}")

    return "\n".join(parts)


def _pct(val) -> str:
    if val is None or val == 0:
        return "0%"
    if val < 1:
        return f"{val * 100:.1f}%"
    return f"{val:.0f}%"


class LLMClient:
    def __init__(self, config: Dict[str, Any]):
        self.base_url = config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "qwen-plus")
        self.timeout = config.get("timeout", 60.0)
        self.temperature = config.get("temperature", 0.7)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def _chat(self, messages: List[Dict[str, str]], max_tokens: int) -> tuple:
        """调一次 chat/completions，返回 (ok, 内容或错误提示)。429 时最多重试 3 次。"""
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": "low",
        }
        client = await self._get_client()
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429:
                    log.warning("LLM 429 rate limited, retry %d/3 in 2s", attempt + 1)
                    await asyncio.sleep(2)
                    continue
                if resp.status_code != 200:
                    log.error("LLM API error %s: %s", resp.status_code, resp.text[:300])
                    return False, f"❌ API 错误 {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                if not content and choice.get("finish_reason") == "length":
                    # 推理模型（如 kimi-k3）的思考过程也算在 max_tokens 里，可能还没写结论就到上限了
                    return False, "⚠️ 模型思考用完了长度上限，没来得及给结论，请重试"
                return True, content
            except httpx.TimeoutException:
                log.error("LLM request timeout")
                return False, "⏱️ 请求超时了，网络可能有问题"
            except Exception as e:
                log.error("LLM error: %s", e)
                return False, f"❌ 出错了: {str(e)[:200]}"
        return False, "⏳ LLM 接口繁忙，请稍后再试（月之暗面账户并发限制）"

    async def analyze(
        self,
        state: Dict[str, Any],
        question: str = "",
        kb: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """生成对局分析（一次性返回，保留生成器接口给流式端点用）"""
        if not self.enabled:
            yield "⚠️ 未配置 LLM API Key。请在 config.json 的 llm.api_key 填入你的 API Key。"
            return

        prompt = _format_state_for_prompt(state)
        if question:
            prompt += f"\n\n【玩家追问】{question}"

        if kb:
            kb_text = json.dumps(kb, ensure_ascii=False, indent=2)
            prompt += f"\n\n【乱斗知识库（版本参考）】\n{kb_text}"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        _ok, content = await self._chat(messages, max_tokens=16000)
        if content:
            yield content

    async def pick_augment(self, state: Dict[str, Any], options: List[str],
                           notes: List[Dict[str, Any]]) -> str:
        """强化符文三选一：只要结论和一两句理由。"""
        if not self.enabled:
            return "⚠️ 未配置 LLM API Key"
        coach = state.get("my_coach") or {}
        enemies = "、".join(f"{m.get('champion_name')}({(m.get('coach') or {}).get('role', '?')})"
                           for m in state.get("enemy_team") or [])
        known = "\n".join(f"- {n['name']}：{'；'.join(n.get('reasons') or []) or '知识库无数据'}" for n in notes)
        prompt = (
            f"海克斯大乱斗，我玩 {coach.get('name', '?')}（{coach.get('role', '?')}），"
            f"出装 {' → '.join(coach.get('build') or [])}。敌方：{enemies or '未知'}。\n"
            f"现在三选一的强化符文：{' / '.join(options)}\n"
            f"本地知识库的参考：\n{known}\n\n"
            "只输出三行，每行不超过 30 字：\n1. 选哪个\n2. 为什么\n3. 拿了之后出装或打法要怎么调整"
        )
        messages = [
            {"role": "system", "content": "你是英雄联盟海克斯大乱斗（ARAM: Mayhem）强化符文专家，回答简短直接。"},
            {"role": "user", "content": prompt},
        ]
        # 只要三行结论，但推理模型的思考过程也占 max_tokens，要留足余量
        _ok, content = await self._chat(messages, max_tokens=2000)
        return content or "（没有返回内容）"

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
