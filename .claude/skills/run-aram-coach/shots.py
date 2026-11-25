"""驱动 mock_run.py 起来的 ARAM Coach，按场景截图。

用法: python shots.py [--out DIR] [--base URL] [场景 ...]
场景: idle（未连接）、champ（选人）、game（对局：阵亡购物卡、强化符文、深度分析、问 AI）
默认: champ game

game 场景会调用 /api/ai/deep 和 /api/ai/ask：配置了 LLM Key 时是真实的 LLM 请求（花 token），
没配置时深度分析走本地规则引擎，问 AI 跳过。
装了 Pillow 会把 PNG 量化到 256 色（体积约降到 1/5）。
"""
import argparse
import json
import urllib.request
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

W, H, SCALE = 1680, 1050, 1.5  # 宽度 >= 1600 才是三列布局


def api(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def compress(path):
    try:
        from PIL import Image
    except ImportError:
        return
    im = Image.open(path).convert("RGB")
    im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


class Shooter:
    def __init__(self, p, base, out):
        self.p, self.base, self.out = p, base, out
        try:
            self.browser = p.chromium.launch(channel="chrome")  # 本机装了 Chrome 就不用下载浏览器
        except Exception:
            self.browser = p.chromium.launch()  # 需要先 python -m playwright install chromium

    def save(self, name, target, **kw):
        path = self.out / name
        target.screenshot(path=path, **kw)
        compress(path)
        print("saved", path)

    def open(self, phase, height=H, wait_dash=True):
        api(self.base, f"/mock/phase/{phase}", "POST")
        page = self.browser.new_page(viewport={"width": W, "height": height}, device_scale_factor=SCALE)
        page.goto(self.base + "/")
        if wait_dash:
            page.wait_for_selector("#dash:not([hidden])", timeout=15000)
        page.wait_for_timeout(1500)  # 等一轮 2 秒的状态推送渲染完
        return page

    def idle(self):
        page = self.open("None", height=700, wait_dash=False)
        self.save("00-idle.png", page)
        page.close()

    def champ(self):
        page = self.open("ChampSelect")
        page.wait_for_selector(".cc-card .cc-head")
        self.save("01-champ-select.png", page, full_page=True)
        page.close()

    def game(self):
        llm = api(self.base, "/api/coach/status").get("enabled")
        page = self.open("InProgress")
        page.wait_for_selector(".cc-shop")
        page.click(".attrs-toggle")
        page.wait_for_timeout(300)
        self.save("02-in-game.png", page, full_page=True)

        # 强化符文：模拟截屏识别到三个选项（和自动识别推送的消息一样），面板自动填好并出推荐
        api(self.base, "/mock/augments?names=" + quote("炼狱导管,坦克引擎,冰寒"), method="POST")
        page.wait_for_selector("#cc-aug .cc-rank")
        page.wait_for_timeout(300)
        aug = page.locator("#cc-aug").bounding_box()
        guide = page.locator(".dash-guide").bounding_box()
        x, y = guide["x"] - 12, max(0, aug["y"] - 24)
        self.save("03-augment.png", page, clip={"x": x, "y": y, "width": W - x, "height": min(aug["height"] + 300, H - y)})
        page.evaluate("AugmentPicker.close()")

        page.click('.guide-tabs button[data-tab="deep"]')
        page.click("#deepBtn")
        page.wait_for_function("document.getElementById('deepBtn').textContent === '重新生成'", timeout=240000)
        # 流式输出会把报告滚到底，截图要从开头看
        page.evaluate("""() => { const r = document.getElementById('deepReport');
            for (const el of [r, r.closest('.ai-scroll')]) if (el) el.scrollTop = 0; }""")
        page.wait_for_timeout(300)
        self.save("04-deep-analysis.png", page.locator(".dash-guide"))

        if not llm:
            print("skip 05-ask-ai.png: 未配置 LLM Key")
        else:
            page.click('.guide-tabs button[data-tab="ai"]')
            page.fill("#aiInput", "对面有奶妈和两个坦克，我复活后先补哪件？")
            page.click("#aiSendBtn")
            page.wait_for_selector(".ai-turn .ai-answer")
            page.wait_for_function(
                "!document.getElementById('aiSendBtn').disabled"
                " && !document.querySelector('.ai-turn .ai-answer.loading')", timeout=240000)
            page.wait_for_timeout(300)
            self.save("05-ask-ai.png", page.locator(".dash-guide"))
        page.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenes", nargs="*", default=["champ", "game"], choices=["idle", "champ", "game"])
    ap.add_argument("--out", default="docs/screenshots")
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        s = Shooter(p, args.base.rstrip("/"), out)
        for name in args.scenes:
            getattr(s, name)()
        s.browser.close()


if __name__ == "__main__":
    main()
