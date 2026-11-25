"""强化符文三选一：截屏识别三张卡的名字，不用手动输入。

LCU / LCD 都不提供三选一的选项，只能读屏幕。识别算法和模型移植自 RankPeek
（https://github.com/wxl11071123/rankpeek ，AGPL-3.0，src/main/hextech/），权重原样使用，
见 data/hextech-model/README.md：

1. 只截三张卡标题所在的一条（屏幕比例的固定位置），切成三个卡位
2. 闸门：真卡的标题区是"深色卡底 + 少量白字 + 固定高度的一行字"，三个卡位都像卡才往下算
3. 切字：按字块起点拟合等距网格 / 按墨迹宽均分，几种切法都试
4. 每个字过一个极小的全连接网络（24×24 → 192 → 599 个字），得到每个字的对数概率
5. 约束解码：不让模型自由拼字，而是在官方强化名字表里挑总分最高的名字；
   切出的格数和名字字数对不上时，用带 gap 的动态规划对齐（模糊解码）
6. 分数不够、也不明显领先第二名就不认——宁可不填，也不填错

截屏只在 Windows 上可用（mss，GDI）。独占全屏模式下截到的是黑屏，闸门会判成"没卡"。
抓取区按游戏窗口（类名 RiotWindowClass）的画面区域算，游戏在副屏、窗口模式都能对上；
找不到游戏窗口时退回主显示器。
"""
import json
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger("aram_coach.augment_ocr")

MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "hextech-model"

THRESH = 160          # 白字门槛
INK_THRESH = 60       # 墨迹门槛（含卡框等较暗的亮像素）
NAME_SCORE = -3.0     # 平均对数概率达到这个直接收
LOOSE_SCORE = -7.0    # 绝对下限：再低就不收
MARGIN = 1.0          # 分数不漂亮时，至少领先第二名这么多才收
BG_UNIFORM = 25       # 三个卡位的背景亮度必须接近
PITCH_RATIO = 1.05    # 每字步进 ≈ 字高 × 这个比例
FUZZY_MAX_GAPS = 2
FUZZY_GAP = -5.0      # 每个 gap 的惩罚：比认对的字（-0.1~-0.3）贵得多，比认错的字（-8~-11）便宜
CELL = 24
CELL_INNER = 20
FRAME_COVER = 0.9     # 某一列 90% 的行都有墨 = 卡框的竖边，不是字
# 名字里的标点和空格：切字时占位，打分时跳过（模型对窄笔画字符判别力弱）
SKIP_CHARS = set("，。！？、；：…—·「」『』（）《》 -_")

# 三张卡标题的抓取区（占游戏画面宽高的比例，16:9）
STRIP_X0, STRIP_X1, STRIP_Y0, STRIP_Y1 = 0.246, 0.755, 0.370, 0.430
SLOT_X = ((0.246, 0.376), (0.438, 0.568), (0.625, 0.755))


def _jround(x: float) -> int:
    """和 JS 的 Math.round 一致（.5 向上取整），Python 的 round 是银行家舍入。"""
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- 模型

class Model:
    def __init__(self, model_dir: Path = MODEL_DIR, extra_names: Iterable[str] = ()):
        self.W1 = np.fromfile(model_dir / "W1.f32", dtype="<f4")
        self.b1 = np.fromfile(model_dir / "b1.f32", dtype="<f4")
        self.W2 = np.fromfile(model_dir / "W2.f32", dtype="<f4")
        self.b2 = np.fromfile(model_dir / "b2.f32", dtype="<f4")
        self.chars: List[str] = json.loads((model_dir / "chars.json").read_text(encoding="utf-8"))
        n2, nc = len(self.b1), len(self.b2)
        if self.W1.size != CELL * CELL * n2 or self.W2.size != n2 * nc or len(self.chars) != nc:
            raise ValueError(f"模型权重形状不一致：W1={self.W1.size} W2={self.W2.size} chars={len(self.chars)}")
        self.W1 = self.W1.reshape(CELL * CELL, n2)
        self.W2 = self.W2.reshape(n2, nc)
        self.char_index = {c: i for i, c in enumerate(self.chars)}
        packaged = [x.get("name", "").strip() for x in json.loads((model_dir / "names.json").read_text(encoding="utf-8"))]
        # 随包名单是基线，同步来的官方名单只做增量（RankPeek 踩过"整表覆盖把新强化抹掉"的坑）
        self.names = list(dict.fromkeys(n for n in [*packaged, *(s.strip() for s in extra_names)] if n))
        self.by_length: Dict[int, List[str]] = {}
        for n in self.names:
            self.by_length.setdefault(len(n), []).append(n)

    def log_prob(self, x: np.ndarray) -> np.ndarray:
        h = np.maximum(x @ self.W1 + self.b1, 0)
        logits = h @ self.W2 + self.b2
        mx = logits.max()
        return logits - mx - math.log(float(np.exp(logits - mx).sum()))


# ---------------------------------------------------------------- 图像特征

def _frame_columns(g: np.ndarray) -> Optional[np.ndarray]:
    """卡框竖直边（棱彩 / 金色卡的发光边框贯穿整条高度），会同时污染闸门和切字。"""
    need = math.ceil(g.shape[0] * FRAME_COVER)
    mask = (g > INK_THRESH).sum(axis=0) >= need
    return mask if mask.any() else None


def _title_band(g: np.ndarray) -> Optional[Tuple[int, int, int]]:
    """墨迹最多的那一段连续有白字的行 → (y0, y1, ink)。"""
    rows = (g > THRESH).sum(axis=1)
    best, start = None, -1
    for y in range(len(rows) + 1):
        v = rows[y] if y < len(rows) else 0
        if v > 0 and start < 0:
            start = y
        elif v == 0 and start >= 0:
            ink = int(rows[start:y].sum())
            if best is None or ink > best[2]:
                best = (start, y - 1, ink)
            start = -1
    return best


def _ink_bounds(g: np.ndarray, y0: int, y1: int, x0: int, x1: int, thr: int = INK_THRESH):
    x0, x1 = max(0, x0), min(g.shape[1] - 1, x1)
    if x1 < x0:
        return None
    ys, xs = np.nonzero(g[y0:y1 + 1, x0:x1 + 1] > thr)
    if not len(xs):
        return None
    return (x0 + int(xs.min()), x0 + int(xs.max()), y0 + int(ys.min()), y0 + int(ys.max()))


def _norm_cell(g: np.ndarray, box) -> np.ndarray:
    """字的墨迹框按面积平均缩到 20×20，放进 24×24 的中间，归一化到 0~1。"""
    bx0, bx1, by0, by1 = box
    sw, sh = bx1 - bx0 + 1, by1 - by0 + 1
    out = np.zeros((CELL, CELL), dtype=np.float32)
    for dy in range(CELL_INNER):
        sy0 = by0 + (dy * sh) // CELL_INNER
        sy1 = max(sy0 + 1, by0 + ((dy + 1) * sh) // CELL_INNER)
        for dx in range(CELL_INNER):
            sx0 = bx0 + (dx * sw) // CELL_INNER
            sx1 = max(sx0 + 1, bx0 + ((dx + 1) * sw) // CELL_INNER)
            out[dy + 2, dx + 2] = g[sy0:sy1, sx0:sx1].mean() / 255
    return out.reshape(-1)


def slot_features(g: np.ndarray) -> Dict[str, Any]:
    h = g.shape[0]
    ink = g > THRESH
    ink_ratio = float(ink.mean())
    bg_mean = float(g[~ink].mean()) if (~ink).any() else 0.0
    band = _title_band(g)
    rel_band = ((band[1] - band[0] + 1) / h) if band and h else 0.0
    # 真卡：深色卡底 + 少量白字 + 标题行占抓取高度的固定比例（RankPeek 用 45 个实机采样定的范围）
    gate = bg_mean < 70 and 0.005 <= ink_ratio <= 0.25 and 0.20 <= rel_band <= 0.40
    return {"bg": bg_mean, "ink": ink_ratio, "rel": rel_band, "band": band, "gate": gate}


def _column_groups(g: np.ndarray, y0: int, y1: int) -> List[Tuple[int, int]]:
    cols = (g[y0:y1 + 1] > THRESH).any(axis=0)
    out, start = [], -1
    for x, on in enumerate(cols):
        if on and start < 0:
            start = x
        elif not on and start >= 0:
            out.append((start, x - 1))
            start = -1
    if start >= 0:
        out.append((start, len(cols) - 1))
    return out


def _estimate_grid(groups: List[Tuple[int, int]]) -> Optional[Tuple[float, int, int]]:
    """从字块起点反推单字步进和字数：左右结构的字会被列空隙切碎，但起点仍落在等距网格上。"""
    if len(groups) < 2:
        return None
    starts = [s for s, _ in groups]
    diffs = [b - a for a, b in zip(starts, starts[1:])]
    big = sorted(d for d in diffs if d >= max(8, max(diffs) * 0.55))
    if not big:
        return None
    pitch0 = big[len(big) >> 1]
    if pitch0 < 6:
        return None
    best = None
    lo, hi, step = pitch0 * 0.8, pitch0 * 1.25, max(0.2, pitch0 * 0.01)
    for start in range(starts[0] - 3, starts[0] + 4):
        p = lo
        while p <= hi:
            err = sum(abs(s - start - _jround((s - start) / p) * p) for s in starts)
            if best is None or err < best[2]:
                best = (p, start, err)
            p += step
    if best is None:
        return None
    pitch, start, _ = best
    n = _jround((starts[-1] - start + pitch) / pitch)
    if n < 1 or n > 12:
        return None
    return pitch, n, start


# ---------------------------------------------------------------- 解码

def _align(lps: List[Optional[np.ndarray]], name: str, char_index: Dict[str, int]) -> Optional[float]:
    """带 gap 的动态规划对齐：多出来的格子、少掉的字各记一次 gap，返回按匹配字数平均的分数。"""
    nc, nk = len(lps), len(name)
    if not nc or not nk or abs(nc - nk) > FUZZY_MAX_GAPS:
        return None
    neg = -1e9
    total = [neg] * (nk + 1)
    gaps = [0] * (nk + 1)
    matched = [0] * (nk + 1)
    total[0] = 0.0
    for lp in lps:
        nt, ng, nm = [neg] * (nk + 1), [0] * (nk + 1), [0] * (nk + 1)
        for j in range(nk + 1):
            if total[j] <= neg:
                continue
            g = gaps[j] + 1          # 这一格当 gap
            if g <= FUZZY_MAX_GAPS and total[j] + FUZZY_GAP > nt[j]:
                nt[j], ng[j], nm[j] = total[j] + FUZZY_GAP, g, matched[j]
            if j < nk:               # 这一格匹配下一个字
                ch = name[j]
                ci = char_index.get(ch)
                if ch in SKIP_CHARS:
                    if total[j] > nt[j + 1]:
                        nt[j + 1], ng[j + 1], nm[j + 1] = total[j], gaps[j], matched[j]
                elif lp is not None and ci is not None:
                    v = total[j] + float(lp[ci])
                    if v > nt[j + 1]:
                        nt[j + 1], ng[j + 1], nm[j + 1] = v, gaps[j], matched[j] + 1
        total, gaps, matched = nt, ng, nm
    best = None
    for j in range(nk + 1):
        if total[j] <= neg:
            continue
        rest = nk - j
        if gaps[j] + rest > FUZZY_MAX_GAPS or matched[j] == 0:
            continue
        avg = (total[j] + rest * FUZZY_GAP) / matched[j]
        if best is None or avg > best:
            best = avg
    return best


class _Slot:
    """一个卡位的识别：同一格子的前向结果缓存起来，几种切法共用。"""

    def __init__(self, model: Model, g: np.ndarray, band: Tuple[int, int, int]):
        self.m, self.g, self.y0, self.y1 = model, g, band[0], band[1]
        self.cache: Dict[Tuple[int, int, int, int], np.ndarray] = {}

    def _lp(self, cell: Tuple[int, int]) -> Optional[np.ndarray]:
        box = _ink_bounds(self.g, self.y0, self.y1, cell[0], cell[1])
        if not box:
            return None
        if box not in self.cache:
            self.cache[box] = self.m.log_prob(_norm_cell(self.g, box))
        return self.cache[box]

    def score(self, cells: List[Tuple[int, int]]) -> Optional[Tuple[str, float, float, int]]:
        """严格解码（字数 = 格数），不够有把握时再做模糊对齐。→ (名字, 平均分, 第二名平均分, 格数)"""
        n = len(cells)
        lps = [self._lp(c) for c in cells]
        best, runner = None, -math.inf
        for nm in self.m.by_length.get(n, []):
            score, cnt, want, ok = 0.0, 0, 0, True
            for ch, lp in zip(nm, lps):
                if ch in SKIP_CHARS:
                    continue
                want += 1
                ci = self.m.char_index.get(ch)
                if ci is None or lp is None:   # 字表里没有 / 这个字在图上没有墨迹：这次切分解释不了它
                    ok = False
                    break
                score += float(lp[ci])
                cnt += 1
            if not ok or not cnt or cnt != want:
                continue
            avg = score / cnt
            if best is None or avg > best[1]:
                if best:
                    runner = max(runner, best[1])
                best = (nm, avg)
            elif avg > runner:
                runner = avg
        if best is None or best[1] < NAME_SCORE:
            for nm in self.m.names:
                avg = _align(lps, nm, self.m.char_index)
                if avg is None:
                    continue
                if best is None or avg > best[1]:
                    if best:
                        runner = max(runner, best[1])
                    best = (nm, avg)
                elif avg > runner:
                    runner = avg
        return (best[0], best[1], runner, n) if best else None


def _read_slot(model: Model, g: np.ndarray, feat: Dict[str, Any]) -> Dict[str, Any]:
    band = feat["band"]
    if not band:
        return {"name": None, "reason": "no-band"}
    head = _ink_bounds(g, band[0], band[1], 0, g.shape[1] - 1)
    if not head:
        return {"name": None, "reason": "no-ink"}
    iw, ih = head[1] - head[0] + 1, band[1] - band[0] + 1
    slot = _Slot(model, g, band)
    best = None

    def keep(r):
        nonlocal best
        if r and (best is None or r[1] > best[1]):
            best = r

    # 切法 A：按字块起点拟合出的网格
    grid = _estimate_grid(_column_groups(g, band[0], band[1]))
    if grid:
        pitch, n, start = grid
        keep(slot.score([(start + _jround(k * pitch), start + _jround((k + 1) * pitch) - 1) for k in range(n)]))
    # 切法 B：按墨迹宽均分、按字高估步进，字数在几何估计 ±2 里都试（字数估错是最贵的错）
    n_est = max(1, _jround(iw / max(4, ih * PITCH_RATIO)))
    for n in (n_est, n_est - 1, n_est + 1, n_est - 2, n_est + 2):
        if n < 1 or n > 12:
            continue
        for pitch in (iw / n, max(4, ih * 1.05)):
            keep(slot.score([(head[0] + _jround(k * pitch), head[0] + _jround((k + 1) * pitch) - 1) for k in range(n)]))

    if best is None:
        return {"name": None, "reason": "no-match"}
    name, avg, second, n = best
    confident = avg >= NAME_SCORE
    leading = math.isfinite(second) and avg >= LOOSE_SCORE and avg - second >= MARGIN
    if not confident and not leading:
        return {"name": None, "reason": "low-score", "score": round(avg, 2), "best_guess": name, "chars": n}
    return {"name": name, "score": round(avg, 2), "second": round(second, 2) if math.isfinite(second) else None,
            "chars": n}


def recognize(model: Model, slots: Sequence[np.ndarray]) -> Dict[str, Any]:
    """三个卡位的灰度图（左 / 中 / 右）→ {"cards": 是否像三选一界面, "names": [名字或 None]×3, "slots": 诊断}。"""
    grays, feats = [], []
    for g in slots:
        g = np.asarray(g, dtype=np.uint8)
        frame = _frame_columns(g)
        if frame is not None:
            g = g.copy()
            g[:, frame] = 0
        grays.append(g)
        feats.append(slot_features(g))
    bgs = [f["bg"] for f in feats]
    uniform = max(bgs) - min(bgs) < BG_UNIFORM if bgs else False
    gate = [{"bg": round(f["bg"], 1), "ink": round(f["ink"] * 100, 2), "rel": round(f["rel"], 2), "pass": f["gate"]}
            for f in feats]
    if len(slots) != 3 or not all(f["gate"] for f in feats) or not uniform:
        return {"cards": False, "names": [None, None, None], "slots": gate}
    out = []
    for g, f, gt in zip(grays, feats, gate):
        r = _read_slot(model, g, f)
        r["gate"] = gt
        out.append(r)
    return {"cards": True, "names": [r["name"] for r in out], "slots": out}


def signature(slots: Sequence[np.ndarray]) -> str:
    """便宜的"画面变了没有"：每个卡位的标题行位置 + 字块起止。一样就不用重新识别。"""
    parts = []
    for g in slots:
        band = _title_band(g)
        parts.append("-" if not band else f"{band[0]}:{band[1]}:" +
                     ",".join(f"{a}-{b}" for a, b in _column_groups(g, band[0], band[1])))
    return "|".join(parts)


# ---------------------------------------------------------------- 截屏（Windows）

def capture_supported() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import mss  # noqa: F401
    except ImportError:
        return False
    return True


def _cursor_in(rect: Tuple[int, int, int, int]) -> bool:
    """鼠标停在卡片上时卡片会放大、描边，切字整体偏移 → 这一帧不认。"""
    try:
        import ctypes
        from ctypes import wintypes
        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return False
    except Exception:
        return False
    x, y, w, h = rect
    return x <= pt.x < x + w and y <= pt.y < y + h


GAME_WINDOW_CLASS = "RiotWindowClass"   # 游戏本体窗口；客户端大厅是 RCLIENT


def _game_rect() -> Optional[Tuple[int, int, int, int]]:
    """游戏画面（窗口客户区）在虚拟桌面上的位置 (x, y, w, h)。没在跑 / 最小化 / 读不到 → None。

    要在 mss.mss() 之后调：mss 把进程设成 DPI 感知，这里拿到的才和截屏一样是物理像素
    （双屏常见笔记本 150% 缩放、外接屏 100%，不感知 DPI 时坐标会被缩放错位）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")   # 自己的实例，不改全局 windll.user32 的函数签名
        user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user32.FindWindowW.restype = wintypes.HWND
        for fn in (user32.IsWindowVisible, user32.IsIconic):
            fn.argtypes = (wintypes.HWND,)
        user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        hwnd = user32.FindWindowW(GAME_WINDOW_CLASS, None)
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        rc, pt = wintypes.RECT(), wintypes.POINT(0, 0)
        if not user32.GetClientRect(hwnd, ctypes.byref(rc)) or not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
    except Exception:
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w < 400 or h < 300:
        return None
    return pt.x, pt.y, w, h


def strip_rect(area: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """游戏画面区域 (x, y, w, h) → 三张卡标题那一条在虚拟桌面上的 (x, y, w, h)。"""
    ax, ay, aw, ah = area
    x, y = ax + _jround(STRIP_X0 * aw), ay + _jround(STRIP_Y0 * ah)
    w = max(8, _jround((STRIP_X1 - STRIP_X0) * aw))
    h = max(8, _jround((STRIP_Y1 - STRIP_Y0) * ah))
    return x, y, w, h


def grab_slots() -> Tuple[Optional[List[np.ndarray]], str, str]:
    """截游戏画面上三张卡标题那一条，切成三个卡位的灰度图。→ (卡位图, 不可用的原因, 截的是哪里)"""
    if not capture_supported():
        return None, "只支持 Windows（需要安装 mss）", ""
    import mss
    with mss.mss() as sct:   # mss 会把进程设成 DPI 感知，拿到的是物理像素
        area = _game_rect()
        if area:
            where = f"游戏窗口 {area[2]}×{area[3]} @({area[0]},{area[1]})"
        else:
            mon = sct.monitors[1]
            area = mon["left"], mon["top"], mon["width"], mon["height"]
            where = f"主显示器 {area[2]}×{area[3]}（没找到游戏窗口）"
        x, y, w, h = strip_rect(area)
        if _cursor_in((x, y, w, h)):
            return None, "鼠标停在卡片上，这一帧跳过", where
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    # BGRA，白字三个通道都满；取 R 通道（和 RankPeek 实时链路的 RGBA 取第一个通道一致）
    gray = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)[:, :, 2]
    to_local = lambda rel: _jround((rel - STRIP_X0) / (STRIP_X1 - STRIP_X0) * w)  # noqa: E731
    return [np.ascontiguousarray(gray[:, to_local(a):to_local(b)]) for a, b in SLOT_X], "", where


class Scanner:
    """截屏 + 识别，带模型懒加载和"画面没变就复用上次结果"的缓存。"""

    def __init__(self, extra_names: Iterable[str] = ()):
        self.extra_names = list(extra_names)
        self.model: Optional[Model] = None
        self.error: Optional[str] = None
        self._sig: Optional[str] = None
        self._result: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    def set_names(self, names: Iterable[str]) -> None:
        with self._lock:
            self.extra_names = list(names)
            self.model = None   # 下次识别时按新名单重建
            self._sig = self._result = None

    def _ensure_model(self) -> Optional[Model]:
        if self.model is None and self.error is None:
            try:
                self.model = Model(extra_names=self.extra_names)
            except Exception as e:
                self.error = f"识别模型加载失败：{e}"
                log.warning(self.error)
        return self.model

    def scan(self) -> Dict[str, Any]:
        """→ {"ok": 能否识别, "cards": 是否在三选一界面, "names": [...], "reason": ..., "ms": 耗时}

        在线程里跑（asyncio.to_thread）；自动识别和手动按钮可能同时调，用锁串起来。
        """
        with self._lock:
            return self._scan()

    def _scan(self) -> Dict[str, Any]:
        t0 = time.perf_counter()
        model = self._ensure_model()
        if model is None:
            return {"ok": False, "reason": self.error}
        try:
            slots, reason, where = grab_slots()
        except Exception as e:
            return {"ok": False, "reason": f"截屏失败：{e}"}
        if slots is None:
            return {"ok": False, "reason": reason, "where": where}
        sig = signature(slots)
        if sig == self._sig and self._result is not None:
            result = self._result
        else:
            result = recognize(model, slots)
            self._sig, self._result = sig, result
        return {"ok": True, **result, "where": where, "ms": round((time.perf_counter() - t0) * 1000)}


class Watcher:
    """决定什么时候截屏：只在有没选的强化时截，选完就停。

    强化在 3/7/11/15 级各给一次，可以攒着晚点选（11 级时连拿 7 级和 11 级两次）。
    等级每跨过一档就多一轮待选；识别到卡片、之后卡片消失，算选完一轮。
    一直没看到卡片（选得太快、或独占全屏截不到）超过 GIVE_UP 秒就放弃这一轮，免得整局空截。
    """

    GIVE_UP = 180

    def __init__(self, levels: Sequence[int] = (3, 7, 11, 15)):
        self.levels = sorted(levels)
        self.reset(None)

    def reset(self, game: Any) -> None:
        self.game = game
        self.unlocked = 0
        self.done = 0
        self.showing = False
        self.since = 0.0
        self.last_names: Optional[List[Optional[str]]] = None

    def update(self, game: Any, level: int, now: float) -> None:
        if game != self.game:
            self.reset(game)
        unlocked = sum(1 for lv in self.levels if level >= lv)
        if unlocked > self.unlocked:
            if self.unlocked <= self.done:
                self.since = now
            self.unlocked = unlocked

    @property
    def pending(self) -> int:
        return max(0, self.unlocked - self.done)

    def want_scan(self, now: float) -> bool:
        if not self.pending:
            return False
        if not self.showing and now - self.since > self.GIVE_UP:
            self.done += 1
            self.since = now
            return self.pending > 0
        return True

    def observe(self, result: Dict[str, Any], now: float) -> Optional[List[Optional[str]]]:
        """喂一次识别结果；认出的名字变了（新的一轮 / 重随）时返回要推给前端的三个名字。"""
        if not result.get("ok"):
            return None   # 鼠标压着、截屏失败：不改变状态
        if result.get("cards"):
            self.showing = True
            names = result.get("names") or [None] * 3
            if any(names) and names != self.last_names:
                self.last_names = list(names)
                return list(names)
            return None
        if self.showing:   # 卡片消失：这一轮选完了
            self.showing = False
            self.done += 1
            self.since = now
            self.last_names = None
        return None
