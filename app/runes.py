"""把推荐符文写进客户端：建一个临时符文页（不占页位），之后原地改写它，从不删除任何符文页。

LCU 行为参考 rank-analysis 的真机验证（docs/superpowers/specs/2026-09-15-rune-recommend-design.md）：
- POST /lol-perks/v1/pages 带 isTemporary: true 会被接受，且不计入页位上限
- POST / PUT 之后新页自动成为当前页
- 临时页不会互相顶掉，所以要找到自己建的那页原地改写，否则越堆越多
"""
from typing import Any, Dict, List, Optional, Tuple

PAGE_SUFFIX = " (AC)"  # ARAM Coach 建的页，靠这个后缀认出来


def page_from_opgg(runes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ids = runes.get("perk_ids") or []
    if len(ids) != 9 or not runes.get("primary_style_id") or not runes.get("sub_style_id"):
        return None
    return {"primaryStyleId": runes["primary_style_id"], "subStyleId": runes["sub_style_id"],
            "selectedPerkIds": list(ids)}


def page_from_names(coach: Dict[str, Any], tree: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """手写数据（符文名）→ 符文页；拼不出合法的 4/2/3 结构就返回 (None, 原因)。"""
    styles = tree.get("styles") or {}
    primary = styles.get(coach.get("primary_branch") or "")
    secondary = styles.get(coach.get("secondary_branch") or "")
    if not primary or not secondary or primary["id"] == secondary["id"]:
        return None, f"符文系不认识：{coach.get('primary_branch')} / {coach.get('secondary_branch')}"

    def find(style: Dict[str, Any], rows: range, name: str) -> Optional[Tuple[int, int]]:
        for r in rows:
            for p in style["slots"][r]:
                if p["name"] == name:
                    return r, p["id"]
        return None

    picks: Dict[int, int] = {}
    for name in coach.get("primary_details") or []:
        hit = find(primary, range(4), name)
        if not hit or hit[0] in picks:
            return None, f"主系符文对不上：{name}"
        picks[hit[0]] = hit[1]
    if sorted(picks) != [0, 1, 2, 3]:
        return None, "主系符文不全"

    sub_rows: Dict[int, int] = {}
    for name in [n.strip() for n in (coach.get("secondary_rune") or "").split("+") if n.strip()]:
        hit = find(secondary, range(1, 4), name)
        if not hit or hit[0] in sub_rows:
            return None, f"副系符文对不上：{name}"
        sub_rows[hit[0]] = hit[1]
    if len(sub_rows) != 2:
        return None, "副系符文要两个"

    shards = list(tree.get("default_shards") or [])
    if len(shards) != 3:
        return None, "缺少属性碎片默认值"
    return {
        "primaryStyleId": primary["id"],
        "subStyleId": secondary["id"],
        "selectedPerkIds": [picks[r] for r in range(4)] + [sub_rows[r] for r in sorted(sub_rows)] + shards,
    }, ""


def build_page(coach: Dict[str, Any], opgg_runes: Optional[Dict[str, Any]],
               tree: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """优先用 OP.GG 的原始 ID（含属性碎片）；没有就用手写数据里的符文名还原。返回 (page, 来源或失败原因)。"""
    if opgg_runes:
        page = page_from_opgg(opgg_runes)
        if page:
            return page, "opgg"
    page, why = page_from_names(coach, tree)
    return (page, "local") if page else (None, why)


async def apply_page(lcu: Any, champion_name: str, page: Dict[str, Any]) -> Dict[str, Any]:
    """写入客户端。lcu 需要提供 request(method, path, json=None) -> (status, body)。"""
    status, pages = await lcu.request("GET", "/lol-perks/v1/pages")
    if status != 200 or not isinstance(pages, list):
        return {"ok": False, "reason": f"读取符文页失败（{status}）"}
    own = next((p for p in pages if p.get("isTemporary") and str(p.get("name", "")).endswith(PAGE_SUFFIX)), None)
    body = {"name": f"{champion_name}{PAGE_SUFFIX}", "isTemporary": True, "current": True, **page}
    if own:
        status, resp = await lcu.request("PUT", f"/lol-perks/v1/pages/{own['id']}", json={**body, "id": own["id"]})
        action = "updated"
    else:
        status, resp = await lcu.request("POST", "/lol-perks/v1/pages", json=body)
        action = "created"
    if 200 <= status < 300:
        page_id = (resp or {}).get("id") if isinstance(resp, dict) else None
        return {"ok": True, "action": action, "page_id": page_id or (own or {}).get("id")}
    msg = resp.get("message") if isinstance(resp, dict) else str(resp or "")
    return {"ok": False, "reason": f"客户端拒绝（{status}）{(msg or '')[:120]}"}


def describe(page: Dict[str, Any], tree: Dict[str, Any]) -> List[str]:
    """符文页 ID → 中文名（用于界面确认写入了什么）。"""
    names = {p["id"]: p["name"] for s in (tree.get("styles") or {}).values() for row in s["slots"] for p in row}
    return [names.get(i, str(i)) for i in page.get("selectedPerkIds") or []]
