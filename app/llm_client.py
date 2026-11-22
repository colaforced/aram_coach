import asyncio
import json
import logging
import httpx
from typing import AsyncGenerator, Optional, Dict, Any, List

log = logging.getLogger("aram_coach.llm")


SYSTEM_PROMPT = """你是英雄联盟大乱斗（ARAM）专家教练，名叫"乱斗导师"。
你精通：
- 170+ 英雄的乱斗适配性、符文、出装、连招
- 大乱斗特殊机制：雪球、治疗宝珠、草丛、窄团战
- 装备克制、对线期策略、团战站位
- 强化符文（Augments）三选一抉择

回答风格：
- 简洁实用，分点列出，每点不超 20 字
- 先给结论，再解释原因
- 数据说话（装备对比、属性差距、金币差）
- 不用 markdown 标题，直接用 emoji 分点
- 特殊提示用 ⚠️ 标记

当被要求分析时，按以下结构输出：
1. 🎯 当前局势（优/均/劣，理由）
2. 📊 装备对比（我 vs 对面关键装备、金币差）
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

    def _resolve_teams(tm: Dict) -> tuple:
        all_players = []
        for key in ("ORDER", "CHAOS"):
            all_players.extend(tm.get(key, []))
        my_team = []
        enemy_team = []
        for key in ("ORDER", "CHAOS"):
            roster = tm.get(key, [])
            if any(p.get("is_me") for p in roster):
                my_team = roster
            else:
                enemy_team = roster
        if not my_team:
            my_team = all_players[:5]
            enemy_team = all_players[5:]
        return my_team, enemy_team

    my_team, enemy_team = _resolve_teams(team_map)

    my_total_gold = sum(int(p.get("gold", 0)) for p in my_team)
    enemy_total_gold = sum(int(p.get("gold", 0)) for p in enemy_team)
    parts.append(f"【团队金币差】我方={my_total_gold}G vs 敌方={enemy_total_gold}G (差={my_total_gold - enemy_total_gold:+d})")

    def _fmt_player(p: Dict[str, Any]) -> str:
        name = p.get("name", "?")
        champ = p.get("champion", "?")
        level = p.get("level", "?")
        k = p.get("kills", 0)
        d = p.get("deaths", 0)
        a = p.get("assists", 0)
        gold = int(p.get("gold", 0))
        dead = p.get("is_dead", False)
        respawn = p.get("respawn_timer", 0)
        dead_tag = f" 💀复活{respawn}s" if dead else ""
        items = [it.get("name", "") for it in p.get("items", []) if it.get("name")]
        item_str = f" 装备=[{','.join(items)}]" if items else ""
        is_me = p.get("is_me", False)
        me_tag = " (我)" if is_me else ""
        dmg = int(p.get("total_damage_champs", 0))
        cs = p.get("minions_killed", 0)
        extra = ""
        if dmg:
            extra += f" 伤害={dmg}"
        if cs:
            extra += f" 补刀={cs}"
        return f"  - {name}{me_tag} [{champ}] Lv.{level} KDA={k}/{d}/{a} 金币={gold}{dead_tag}{item_str}{extra}"

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
        if coach.get("runes"):
            parts.append(f"【推荐符文】{coach.get('runes')}")

    composite = state.get("composite_advice") or {}
    if composite:
        score = composite.get("score")
        tips = composite.get("tips") or []
        if score:
            parts.append(f"【教练评分】综合={score} 分")
        if tips:
            parts.append(f"【教练提示】{'; '.join(tips[:5])}")

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

    async def analyze(
        self,
        state: Dict[str, Any],
        question: str = "",
        kb: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """流式生成对局分析"""
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
            "max_tokens": 16000,
            "reasoning_effort": "low",
        }

        client = await self._get_client()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429:
                    log.warning("LLM 429 rate limited, retry %d/%d in 2s", attempt + 1, max_retries)
                    await asyncio.sleep(2)
                    continue
                if resp.status_code != 200:
                    log.error("LLM API error %s: %s", resp.status_code, resp.text[:300])
                    yield f"❌ API 错误 {resp.status_code}: {resp.text[:200]}"
                    return

                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    yield content
                return
            except httpx.TimeoutException:
                log.error("LLM request timeout")
                yield "⏱️ 请求超时了，网络可能有问题"
                return
            except Exception as e:
                log.error("LLM error: %s", e)
                yield f"❌ 出错了: {str(e)[:200]}"
                return

        yield "⏳ LLM 接口繁忙，请稍后再试（月之暗面账户并发限制）"

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None
