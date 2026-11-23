---
name: run-aram-coach
description: 在没有 LoL 客户端的机器上启动 ARAM Coach（用模拟的 LCU/LCD 数据进入选人、对局阶段），驱动界面并截图。用于确认界面改动在真实页面里生效、更新 README 的界面截图。
---

# 启动并截图 ARAM Coach（模拟对局数据）

应用平时要连 LoL 客户端（LCU）和局内接口（LCD, 127.0.0.1:2999）才有内容。`mock_run.py` 只把这两处换成假数据，
规则引擎、数据同步、LLM 调用都是真的，所以看到的就是用户在对局里会看到的界面。

本目录的文件：

- `mock_run.py`：用模拟数据启动后端（默认端口 8799，不占正式端口 8765），多了一个切换场景的接口
- `shots.py`：用 Playwright 打开页面，按场景截图

下面的命令都在仓库根目录执行。

## 1. 环境（一次性）

```bash
python3 -m venv .venv            # .venv 已在 .gitignore
.venv/bin/pip install -r requirements.txt playwright pillow
```

- 浏览器：`shots.py` 优先用本机的 Google Chrome（`channel="chrome"`），没有就用 Playwright 自带的 Chromium，
  这时要先执行 `.venv/bin/python -m playwright install chromium`
- `pillow` 可选：装了会把截图量化到 256 色，体积降到约 1/5

## 2. 启动

```bash
nohup .venv/bin/python .claude/skills/run-aram-coach/mock_run.py > /tmp/aram-mock.log 2>&1 &
until curl -sf -o /dev/null http://127.0.0.1:8799/api/lcu/status; do sleep 0.5; done
```

- 启动日志 `LLM:` 那一行写明 Key 是否配置、来自哪里
- 启动时会后台同步大乱斗数据（写 `data/aram_meta.json`，已 gitignore），等日志出现 `大乱斗数据同步结束` 再截图，
  页面才带上平衡修正等同步数据。OP.GG 在部分网络（如公司网络）会 `ConnectError`，这时出装/符文来自
  `champions.json`，符文按钮显示「本地」，属正常
- 可选环境变量：`MOCK_PORT`（端口）、`MOCK_PHASE`（初始阶段，默认 `ChampSelect`）

手动看页面：浏览器打开 <http://127.0.0.1:8799/>，切换场景：

```bash
curl -X POST http://127.0.0.1:8799/mock/phase/ChampSelect   # 选人
curl -X POST http://127.0.0.1:8799/mock/phase/InProgress    # 对局中（我方阵亡，复活前购物卡）
curl -X POST http://127.0.0.1:8799/mock/phase/None          # 空闲
```

页面每 2 秒推送一次状态，切换后最多等 2 秒生效。

## 3. 截图

```bash
.venv/bin/python .claude/skills/run-aram-coach/shots.py                    # champ + game，输出到 docs/screenshots/
.venv/bin/python .claude/skills/run-aram-coach/shots.py --out /tmp/shots idle champ game
```

| 场景 | 文件 | 内容 |
| --- | --- | --- |
| `idle` | `00-idle.png` | 已连接、未进对局的空闲页（模拟的 LCU 一直是已连接，拍不到「等待 LoL 客户端连接」） |
| `champ` | `01-champ-select.png` | 选人结论（换 / 骰 / 留）、阵容对比、英雄指导，整页 |
| `game` | `02-in-game.png` | 阵亡购物卡、属性面板（展开）、实时战况、事件流，整页 |
| | `03-augment.png` | 强化符文三选一（填入 炼狱导管 / 坦克引擎 / 冰寒 后点推荐），右列区域 |
| | `04-deep-analysis.png` | 深度分析页签，右列 |
| | `05-ask-ai.png` | 问 AI 页签（问一个问题），右列；没配 LLM Key 时跳过 |

**`game` 场景会花 LLM token**：配置了 Key 时深度分析和问 AI 各发一次真实请求。只想验证界面、不想花钱：
Key 在环境变量里就用 `env -u ARAM_COACH_LLM_API_KEY` 启动 `mock_run.py`（深度分析会走本地规则引擎）；
Key 在 `config.local.json` 里则没法这样关掉，只跑 `champ`。

截完一定要打开 PNG 看一眼：空白或只有顶栏，说明页面没进入对应阶段（看 `/tmp/aram-mock.log` 里的 `Poll error`）。

视口是 1680×1050、1.5 倍缩放：宽度 ≥ 1600 才是三列布局，窄了会变成两列 / 一列。

## 4. 关闭

```bash
pkill -f run-aram-coach/mock_run.py
```

运行期间会写 `game_state.md`（已 gitignore），内容是模拟数据，下次真实运行会覆盖。

## 改模拟数据

都在 `mock_run.py` 顶部：

- 选人：`CHAMP_SELECT`（我方 5 人、`benchChampions` 备选席）、`SUMMONER.rerollPoints`（剩余骰子）
- 对局：`MY_TEAM` / `ENEMY`（puuid、英雄 ID、名字），`lcd_data()` 里的金币、等级、属性、各人装备、事件
- 英雄 ID 要在 `data/champion_map.json` 里，装备 ID 要在 `data/item_map.json` 里，并把中文名加到 `ITEM_NAMES`
- 当前场景：选人随到盖伦，备选席有火男 → 建议「换成 复仇焰魂」；对局中我是火男、阵亡 15 秒、1730G，
  敌方有索拉卡（触发重伤建议）和奥恩、赛恩两个坦克（触发法穿建议）

不要用 `uvicorn app.main:app` 直接起：入口必须先 `import win_compat`（`run.py` 和 `mock_run.py` 都做了）。
