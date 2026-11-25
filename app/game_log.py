"""对局记录与复盘：每局一个目录，打完自动拉战绩，生成 review.md。

game_logs/<开局时间>/
  meta.json        这局是谁、哪个英雄、gameId
  ai.jsonl         每次问 AI / 深度分析 / 强化符文：发给模型的完整消息、回答、当时的对局快照和 LCD 原始数据
  snapshots.jsonl  对局中每分钟一份快照：每人 KDA、补刀、装备总价、估算经济
  match.json       赛后战绩（客户端 /lol-match-history），原样保存
  timeline.json    赛后逐分钟数据（每人真实经济），原样保存
  eog.json         结算页数据，原样保存
  review.md        复盘：估算经济 vs 真实经济、每次 AI 回答时的真实局面

战绩接口的格式没在国服实机确认过，所以原始数据都原样存下来；解析不对时改完代码重新生成：
    python -m app.game_log game_logs/<目录>
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("aram_coach.game_log")

GAME_PHASES = ("GameStart", "InProgress", "Reconnect")
SNAPSHOT_EVERY = 60      # 游戏内秒
FETCH_INTERVAL = 15      # 秒
FETCH_TRIES = 40         # 约 10 分钟：战绩一般在结算后一两分钟才查得到
TEAM_ID = {"ORDER": 100, "CHAOS": 200}
_SNAP_KEYS = ("name", "champion", "champion_id", "team", "is_me", "is_dead", "level",
              "kills", "deaths", "assists", "cs", "item_gold", "est_gold")


def snapshot(lcd: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not lcd or not lcd.get("team_map"):
        return None
    return {
        "game_time": lcd.get("game_time", 0),
        "my_gold": (lcd.get("active") or {}).get("gold"),
        "my_lost": lcd.get("my_lost", 0),   # 卖装备等看不到的钱（economy.SellLoss），校准时补回
        "towers": lcd.get("towers"),
        "players": [{k: p.get(k) for k in _SNAP_KEYS} for ps in lcd["team_map"].values() for p in ps],
    }


class Record:
    def __init__(self, root: Path, in_game: bool):
        self.dir = root / time.strftime("%Y%m%d-%H%M%S")
        self.meta: Dict[str, Any] = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.in_game = in_game
        self.snap_t: Optional[float] = None       # 上一份写盘快照的游戏时间
        self.pending: Optional[Dict[str, Any]] = None  # 最新一份还没写盘的快照，对局结束时补上

    def write_json(self, name: str, data: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def append(self, name: str, data: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


class GameRecorder:
    def __init__(self, root: Path):
        self.root = root
        self.rec: Optional[Record] = None

    def observe(self, state: Dict[str, Any]) -> Optional[Record]:
        """轮询循环每次调用。对局刚结束时返回这局的记录（调用方去拉战绩），否则返回 None。"""
        phase = state.get("phase")
        rec = self.rec
        if rec is None:
            if phase == "ChampSelect" or phase in GAME_PHASES:
                rec = self.rec = Record(self.root, in_game=phase in GAME_PHASES)
            else:
                return None
        elif phase in GAME_PHASES:
            rec.in_game = True
        elif phase != "ChampSelect":
            self.rec = None
            if not rec.in_game:   # 选人阶段有人秒退，没开局
                return None
            if rec.pending:
                rec.append("snapshots.jsonl", rec.pending)
            return rec

        if rec.in_game:
            meta = dict(rec.meta, game_id=state.get("game_id") or rec.meta.get("game_id"),
                        summoner=state.get("summoner_name"), champion=state.get("my_champion_name"),
                        champion_id=state.get("my_champion_id"))
            if meta != rec.meta:
                rec.meta = meta
                rec.write_json("meta.json", meta)
            snap = snapshot(state.get("lcd"))
            if snap:
                rec.pending = snap
                if rec.snap_t is None or snap["game_time"] - rec.snap_t >= SNAPSHOT_EVERY:
                    rec.append("snapshots.jsonl", snap)
                    rec.snap_t, rec.pending = snap["game_time"], None
        return None

    def log_ai(self, state: Dict[str, Any], lcd_raw: Optional[Dict[str, Any]], **entry: Any) -> None:
        """记下一次 AI 调用；不在选人 / 对局里（没有记录）时不记。"""
        rec = self.rec
        if rec is None:
            return
        in_game = state.get("phase") in GAME_PHASES
        rec.append("ai.jsonl", {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": state.get("phase"),
            "game_time": (state.get("lcd") or {}).get("game_time"),
            **entry,
            "snapshot": snapshot(state.get("lcd")) if in_game else None,
            "lcd_raw": lcd_raw if in_game else None,
        })

    async def finalize(self, rec: Record, get: Callable[[str], Awaitable[Any]]) -> None:
        """对局结束后从客户端拉结算数据和战绩，然后生成 review.md。"""
        gid = rec.meta.get("game_id")
        got: Dict[str, Any] = {}
        for _ in range(FETCH_TRIES):
            if "eog" not in got:
                eog = await get("/lol-end-of-game/v1/eog-stats-block")
                if eog and (not gid or str(eog.get("gameId")) == str(gid)):
                    got["eog"] = eog
                    rec.write_json("eog.json", eog)
            if gid:
                for key, path, field in (("match", f"/lol-match-history/v1/games/{gid}", "participants"),
                                         ("timeline", f"/lol-match-history/v1/game-timelines/{gid}", "frames")):
                    if key not in got:
                        data = await get(path)
                        if isinstance(data, dict) and data.get(field):
                            got[key] = data
                            rec.write_json(f"{key}.json", data)
            if ("match" in got and "timeline" in got) or (not gid and "eog" in got):
                break
            await asyncio.sleep(FETCH_INTERVAL)
        write_review(rec.dir)
        missing = [k for k in ("eog", "match", "timeline") if k not in got]
        log.info(f"复盘已生成: {rec.dir / 'review.md'}" + (f"（没拿到: {', '.join(missing)}）" if missing else ""))


# ---------------------------------------------------------------- 复盘

def _load(d: Path, name: str) -> Any:
    p = d / name
    if not p.exists():
        return None
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(p.read_text(encoding="utf-8"))


def _mmss(t: Optional[float]) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}" if t is not None else "—"


class RealGold:
    """赛后战绩里的真实经济：timeline 逐分钟 totalGold / currentGold，按时间线性插值。"""

    def __init__(self, match: Optional[Dict[str, Any]], timeline: Dict[str, Any]):
        self.frames = sorted(timeline.get("frames") or [], key=lambda f: f.get("timestamp", 0))
        self.team: Dict[int, int] = {}
        self.champ: Dict[int, int] = {}
        self.name: Dict[int, str] = {}
        for p in (match or {}).get("participants") or []:
            self.team[p["participantId"]] = p.get("teamId")
            self.champ[p["participantId"]] = p.get("championId")
        for pi in (match or {}).get("participantIdentities") or []:
            pl = pi.get("player") or {}
            self.name[pi["participantId"]] = pl.get("gameName") or pl.get("summonerName") or ""
        for f in self.frames:   # 没有战绩详情时按惯例：1～5 号是 100 队
            for pid in (int(k) for k in (f.get("participantFrames") or {})):
                self.team.setdefault(pid, 100 if pid <= 5 else 200)

    def at(self, t: float) -> Dict[int, Dict[str, float]]:
        ms = t * 1000
        before = [f for f in self.frames if f.get("timestamp", 0) <= ms]
        after = [f for f in self.frames if f.get("timestamp", 0) > ms]
        a = before[-1] if before else after[0]
        b = after[0] if after and before else a
        span = (b.get("timestamp", 0) - a.get("timestamp", 0)) or 1
        w = 0.0 if a is b else (ms - a.get("timestamp", 0)) / span
        out = {}
        for k, fa in (a.get("participantFrames") or {}).items():
            fb = (b.get("participantFrames") or {}).get(k) or fa
            out[int(k)] = {key: (1 - w) * (fa.get(key) or 0) + w * (fb.get(key) or 0) for key in ("totalGold", "currentGold")}
        return out

    def pid_of(self, player: Dict[str, Any]) -> Optional[int]:
        for pid, cid in self.champ.items():
            if cid and cid == player.get("champion_id"):
                return pid
        for pid, name in self.name.items():
            if name and name == player.get("name"):
                return pid
        return None

    def team_diff(self, t: float, my_team: int) -> Dict[str, int]:
        g = self.at(t)
        mine = [v for pid, v in g.items() if self.team.get(pid) == my_team]
        theirs = [v for pid, v in g.items() if self.team.get(pid) != my_team]
        s = lambda vs, k: int(sum(v[k] for v in vs))  # noqa: E731
        return {"diff": s(mine, "totalGold") - s(theirs, "totalGold"),
                "my_cash": s(mine, "currentGold"), "enemy_cash": s(theirs, "currentGold")}


def _my_team(snap: Dict[str, Any]) -> Optional[str]:
    return next((p.get("team") for p in snap.get("players") or [] if p.get("is_me")), None)


def _est_diff(snap: Dict[str, Any], key: str = "est_gold") -> int:
    side = _my_team(snap) or "ORDER"
    return sum((p.get(key) or 0) * (1 if p.get("team") == side else -1) for p in snap.get("players") or [])


def build_review(d: Path) -> str:
    meta = _load(d, "meta.json") or {}
    snaps = _load(d, "snapshots.jsonl") or []
    calls = _load(d, "ai.jsonl") or []
    match, timeline, eog = _load(d, "match.json"), _load(d, "timeline.json"), _load(d, "eog.json")
    real = RealGold(match, timeline) if timeline and timeline.get("frames") else None

    out = [f"# 对局复盘 · {meta.get('champion') or '?'} · {meta.get('started_at', d.name)}", ""]
    sides = {_my_team(s) for s in snaps}
    side = next((s for s in sides if s), None)
    me_ok = "是" if side else "**否**（敌我按蓝方处理，可能整个颠倒）"
    if len(sides - {None}) > 1:
        me_ok = f"**中途变过**：{sides}"
    out.append(f"- gameId：{meta.get('game_id') or '—'} · 召唤师：{meta.get('summoner') or '—'}")
    if match:
        dur = match.get("gameDuration")
        mine = next((p for p in match.get("participants") or [] if p.get("championId") == meta.get("champion_id")), None)
        win = (mine or {}).get("stats", {}).get("win")
        out.append(f"- 模式：{match.get('gameMode', '?')} · 时长：{_mmss(dur) if dur else '—'}"
                   f" · 结果：{'胜' if win else '负' if win is False else '—'}")
    out.append(f"- 我方：{side or '—'} · 认出自己：{me_ok}")
    have = lambda x: "✅" if x else "❌"  # noqa: E731
    out.append(f"- 数据：快照 {len(snaps)} 份 · AI 记录 {len(calls)} 条 · 战绩 {have(match)} · "
               f"逐分钟经济 {have(real)} · 结算 {have(eog)}")
    out.append("")

    out.append("## 经济：估算 vs 真实")
    out.append("")
    if not snaps:
        out.append("没有对局快照。")
    else:
        if not real:
            out.append("没拿到逐分钟经济（timeline），只能列出估算值。")
            out.append("")
        out.append("差值都是 我方 − 敌方。真实经济按战绩里的逐分钟数据插值；“未花”是身上还没花的钱。")
        out.append("")
        out.append("| 时间 | 估算经济差 | 真实经济差 | 估算偏差 | 装备总价差 | 真实未花 我方 / 敌方 |")
        out.append("|---|---|---|---|---|---|")
        for s in snaps:
            est, items = _est_diff(s), _est_diff(s, "item_gold")
            if real:
                r = real.team_diff(s["game_time"], TEAM_ID.get(_my_team(s) or "ORDER", 100))
                out.append(f"| {_mmss(s['game_time'])} | {est:+d} | {r['diff']:+d} | {est - r['diff']:+d} | {items:+d} "
                           f"| {r['my_cash']} / {r['enemy_cash']} |")
            else:
                out.append(f"| {_mmss(s['game_time'])} | {est:+d} | — | — | {items:+d} | — |")
        out.append("")

        last = snaps[-1]
        out.append(f"### 每人（{_mmss(last['game_time'])}）")
        out.append("")
        out.append("| 英雄 | 队伍 | K/D/A | 补刀 | 装备总价 | 估算经济 | 真实经济 | 偏差 |")
        out.append("|---|---|---|---|---|---|---|---|")
        g = real.at(last["game_time"]) if real else {}
        for p in last.get("players") or []:
            pid = real.pid_of(p) if real else None
            tg = int(g[pid]["totalGold"]) if pid in g else None
            real_s, err_s = ("—", "—") if tg is None else (str(tg), f"{(p.get('est_gold') or 0) - tg:+d}")
            me = "（我）" if p.get("is_me") else ""
            out.append(f"| {p.get('champion')}{me} | {p.get('team')} "
                       f"| {p.get('kills')}/{p.get('deaths')}/{p.get('assists')} | {p.get('cs')} | {p.get('item_gold')} "
                       f"| {p.get('est_gold')} | {real_s} | {err_s} |")
        out.append("")

    out.append("## AI 记录")
    out.append("")
    if not calls:
        out.append("这局没有问过 AI。")
    labels = {"ask": "问 AI", "deep": "深度分析", "augment": "强化符文"}
    for i, c in enumerate(calls, 1):
        who = c.get("model") if c.get("mode") == "llm" else "本地规则"
        when = _mmss(c["game_time"]) if c.get("game_time") is not None else "选人" if c.get("phase") == "ChampSelect" else c.get("phase")
        out.append(f"### {i} · {when} · "
                   f"{labels.get(c.get('task'), c.get('task'))} · {who}")
        out.append("")
        if c.get("question"):
            out.append(f"**问**：{c['question']}")
            out.append("")
        elif c.get("task") == "deep":   # 深度分析的提问里带着死活和上次的分析
            asked = next((m.get("content", "") for m in reversed(c.get("messages") or []) if m.get("role") == "user"), "")
            if "\n" in asked:
                out.append("**问**：" + asked.replace("\n", "  \n"))
                out.append("")
        out.append("**答**：" + (f"（出错：{c['error']}）" if c.get("error") else ""))
        out.append("")
        out.append(c.get("answer") or "（无）")
        out.append("")
        snap = c.get("snapshot")
        if snap:
            line = f"**当时**：估算经济差 {_est_diff(snap):+d} · 装备总价差 {_est_diff(snap, 'item_gold'):+d}"
            if real:
                line += f" · 真实经济差 {real.team_diff(snap['game_time'], TEAM_ID.get(_my_team(snap) or 'ORDER', 100))['diff']:+d}"
            out.append(line)
            out.append("")
        system = next((m.get("content", "") for m in c.get("messages") or [] if m.get("role") == "system"), "")
        if "\n【对局数据】\n" in system:   # 规则说明里也提到【对局数据】，按段落标题切
            facts = system.rpartition("\n【对局数据】\n")[2].split("\n\n【知识库", 1)[0].strip()
            out.append("<details><summary>当时发给 AI 的对局数据</summary>")
            out.append("")
            out.append("```text")
            out.append(facts)
            out.append("```")
            out.append("</details>")
            out.append("")
    return "\n".join(out)


def write_review(d: Path) -> Path:
    path = d / "review.md"
    path.write_text(build_review(d), encoding="utf-8")
    return path


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(write_review(Path(arg)))
