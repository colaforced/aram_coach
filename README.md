# ARAM Coach · 英雄联盟大乱斗实时 AI 教练

基于 **LCU（League Client API）** 与 **LCD（Live Client Data）** 的本地实时大乱斗教练。
读取游戏客户端数据，结合版本知识库与本地规则引擎（可选接入大模型），在选人、选装备、对线、团战各阶段给出可执行建议。

- 实时读取，无需截图、无需 OCR
- 170 名英雄的大乱斗专属数据（乱斗强度、出装、符文、雪球/闪现技巧）
- 版本知识库驱动（海克斯大乱斗 `25.24_2025-12`）
- 双模式：配置了 API Key 走 LLM 深度分析，未配置自动降级为本地规则引擎

---

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [界面说明](#界面说明)
- [项目结构](#项目结构)
- [数据文件说明](#数据文件说明)
- [API 接口](#api-接口)
- [工作原理](#工作原理)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 功能特性

### 选人阶段（ChampSelect）

- 实时展示我方/敌方英雄阵容与各自的教练数据
- **骰子重随建议**：结合英雄乱斗强度与阵容契合度给出 `保留 / 考虑 / 强烈建议重随` 三档结论，并说明理由
- 阵容契合度计算：自动识别队伍是否缺前排、缺 AP、缺持续输出，并指出你是否正好补齐缺口

### 对局阶段（InProgress）

- 实时属性面板：等级、金币、HP、AD/AP、护甲/魔抗、攻速、移速、技能急速、暴击、双穿透、全能吸血、韧性
- 已购装备 vs 推荐出装对比，标注缺失的关键装备与替代方案
- 双方阵容对比与对位克制分析（`compute_matchup_analysis`）
- 综合评分：英雄强度 + 阵容契合 + 装备完成度 + 团队战力，给出即时行动项与下一优先级

### 强化符文与升级节点

- 监听等级提升事件，触发强化符文（Hextech Augment）抉择提示
- 知识库内置 T0/T1 梯度、雪球技巧、草丛视野、装备公式、强化符文三选一建议

### 事件流

- WebSocket 实时推送：阶段变化、骰子重随、升级、状态更新
- 每次状态轮询同步落盘 `game_state.md`（Markdown 快照），便于外部工具或 AI 直接读取

---

## 快速开始

### 环境要求

- Python 3.10+
- Windows（依赖 LCU/lockfile 本地端口与客户端进程；脚本已针对 Windows 异步 IO 做兼容处理）
- 已安装并运行《英雄联盟》客户端

### 安装

```bash
git clone https://github.com/colaforced/aram_coach.git
cd aram_coach
pip install -r requirements.txt
```

### 启动

```bat
start.bat
```

或在 PowerShell 中直接运行：

```bash
python run.py
```

启动脚本会自动完成：检查 Python 环境 → 安装缺失依赖 → 启动后端 → 打开浏览器。

访问 <http://127.0.0.1:8765/>

> 端口可在 `config.json` 的 `web_port` 中修改。

### 使用流程

1. 启动 LoL 客户端并登录
2. 启动本项目，浏览器打开 <http://127.0.0.1:8765/>
3. 进入大乱斗选人阶段，页面自动检测并开始推送实时数据
4. 进入对局后，页面持续刷新属性、装备、阵容与建议

---

## 配置说明

`config.json`：

```json
{
    "lol_install_paths": [
        "G:\\Games\\WeGameApps",
        "C:\\Riot Games\\League of Legends",
        "C:\\Program Files (x86)\\League of Legends"
    ],
    "web_port": 8765,
    "dev_mode": false,
    "llm": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "sk-xxxxxxxxxxxxxxxx",
        "model": "kimi-k3",
        "temperature": 1.0,
        "timeout": 60.0
    }
}
```

| 字段 | 说明 |
| --- | --- |
| `lol_install_paths` | 客户端安装目录列表，用于定位 `lockfile`。项目会自动向下递归查找 `*\\LeagueClient\\lockfile` |
| `web_port` | Web 服务端口，默认 `8765` |
| `dev_mode` | 预留的调试模式开关，代码中暂未引用 |
| `llm.base_url` | OpenAI 兼容接口地址，默认月之暗面 Kimi |
| `llm.api_key` | API Key，**留空则自动降级为本地规则引擎**，功能不受影响 |
| `llm.model` | 模型名 |
| `llm.temperature` | 采样温度 |
| `llm.timeout` | 请求超时（秒） |

### 接入其他大模型

任何 OpenAI 兼容的 `/chat/completions` 接口均可，改 `base_url`、`api_key`、`model` 即可，例如：

- 阿里云百炼：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- DeepSeek：`https://api.deepseek.com/v1`
- OpenAI：`https://api.openai.com/v1`

> ⚠️ `config.json` 已被纳入版本管理，正式公开前请移除其中的 `api_key`，或改为从环境变量读取。

---

## 界面说明

| 路径 | 页面 | 说明 |
| --- | --- | --- |
| `/` | 主控台 | WebSocket 实时推送，完整展示属性面板、阵容对比、装备状态、事件流与 LLM 追问 |
| `/coach` | 实时卡片 | 每 2 秒自动刷新的精简卡片，适合放在副屏常驻 |
| `/deep` | 深度分析 | 一次性生成完整对局分析报告（有 Key 走 LLM，否则走本地规则） |

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI 应用主体：路由、状态采集、规则引擎、卡片渲染
│   ├── lcu_client.py    # LCU 连接器：定位 lockfile / 解析日志取认证信息
│   ├── lcd_client.py    # LCD 客户端：读取局内实时数据（https://127.0.0.1:2999）
│   └── llm_client.py    # LLM 客户端：OpenAI 兼容接口调用、流式输出、429 重试
├── data/
│   ├── champions.json       # 170 名英雄的乱斗数据
│   ├── champion_map.json    # championId → 英雄名 映射
│   ├── item_map.json        # itemId → 装备属性 映射
│   └── knowledge_base.json  # 版本知识库（梯度/技巧/装备公式/强化符文）
├── frontend/
│   ├── index.html       # 主控台
│   ├── coach.html       # 实时卡片
│   └── deep.html        # 深度分析报告
├── config.json          # 路径、端口、LLM 配置
├── requirements.txt
├── run.py               # 启动入口（含 Windows 异步 IO 兼容处理）
├── start.bat            # Windows 一键启动
├── start.ps1            # 启动脚本主体（依赖检查 + 拉起 + 开浏览器）
├── check_id.py          # 英雄 ID 与名称映射校验脚本
└── rebuild_db.py        # 依据 champion_map 重建 champions.json 的数据脚本
```

---

## 数据文件说明

| 文件 | 内容 |
| --- | --- |
| `data/champions.json` | 每名英雄包含 `name` / `title` / `role` / `aram_strength` / `build` / `core_cost` / `runes` / `runes_alt` / `primary_details` / `secondary` / `tips` / `snowball_tip` / `flash_tip` / `situational` 等字段 |
| `data/champion_map.json` | `championId → 英雄名`，用于把客户端返回的 ID 还原成名字 |
| `data/item_map.json` | `itemId → {name, gold_total, gold_base, sell}`，用于把局内装备 ID 还原成装备并计算总价 |
| `data/knowledge_base.json` | 版本级知识：`tier_list`（梯度）、`snowball_tips`、`grass_tips`、`item_myths`、`core_build_recipes`、`hextech_augments`（强化符文梯度）以及个别英雄专属数据 |

### 维护数据

```bash
# 校验 championId 与英雄名是否错配
python check_id.py

# 依据 champion_map.json 重建 champions.json（会覆盖原文件，先备份）
python rebuild_db.py
```

---

## API 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 主控台页面 |
| GET | `/coach` | 实时卡片页面 |
| GET | `/deep` | 深度分析页面 |
| GET | `/api/state` | 当前完整对局状态（JSON） |
| GET | `/api/coach/quick` | 实时建议卡片（HTML 片段） |
| GET | `/api/coach/deep` | 深度分析（LLM 或规则引擎，返回 `mode` 字段标识来源） |
| GET | `/api/coach/ask?q=` | 向 LLM 追问，流式返回 |
| GET | `/api/coach/md` | 当前状态的 Markdown 快照 |
| GET | `/api/coach/status` | LLM 是否启用及模型信息 |
| GET | `/api/kb/status` | 知识库加载状态与统计 |
| GET | `/api/lcu/status` | LCU 连接状态 |
| GET | `/api/champion/{id}` | 单个英雄的教练数据 |
| GET | `/api/champions/search?q=` | 英雄搜索 |
| WS | `/ws` | 实时事件推送（`state_update` / `phase_change` / `dice_rolled` / `level_up`） |

---

## 工作原理

```
┌────────────────┐
│  LoL 客户端     │
└───────┬────────┘
        │ ① lockfile / 日志解析出端口与口令
        ▼
┌────────────────┐      ┌─────────────────────┐
│  lcu_client.py  │─────▶│   LCU  API (127.0.0.1)│ 选人/流程/召唤师
└────────────────┘      └─────────────────────┘
        │ ② 局内实时数据
        ▼
┌────────────────┐
│  lcd_client.py  │─────▶  LCD API (127.0.0.1:2999)  属性/装备/队友
└────────────────┘
        │
        ▼
┌────────────────────────────────────────┐
│  main.py  collect_game_state()         │
│  ├─ 合并 LCU + LCD + 本地英雄数据        │
│  ├─ 规则引擎：重随建议 / 综合评分 / 对位   │
│  └─ 渲染卡片 + 推送 WebSocket            │
└───────────────┬────────────────────────┘
                │ ④ 组装 prompt + 知识库
                ▼
┌────────────────────────────┐
│  llm_client.py  →  Kimi 等  │
└────────────────────────────┘
```

**连接建立**：`lcu_client.discover_connection()` 先按配置路径查找 `lockfile`；拿不到则回退到扫描 `*_LeagueClientUx.log` 正则提取 `https://riot:<password>@127.0.0.1:<port>/`，最后用 Basic Auth 建立 httpx 客户端。

**轮询**：后台任务每 2 秒采集一次状态，检测到阶段变化、骰子重随、等级提升时通过 WebSocket 主动推送增量事件。

**双模式**：`llm_client.enabled` 取决于 `config.json` 是否配置了 `api_key`。配置了就走 LLM 生成深度分析，没配置就走 `_deep_analysis()` 本地规则引擎，页面右上角标签会显示当前模式。

---

## 常见问题

**Q：页面一直显示"等待 LoL 客户端连接"？**
A：确认客户端已登录；确认 `config.json` 的 `lol_install_paths` 覆盖了实际安装目录；`/api/lcu/status` 可查看连接细节。

**Q：启动报 `_overlapped` 相关错误？**
A：`run.py` 已内置 Windows 异步 IO 兼容垫片，请通过 `run.py` 或 `start.bat` 启动，不要直接 `uvicorn app.main:app`。

**Q：端口 8765 被占用？**
A：修改 `config.json` 的 `web_port` 后重启。

**Q：LLM 分析没有返回内容？**
A：检查 `config.json` 的 `api_key` 是否有效；`/api/coach/status` 可确认是否启用；月之暗面有并发限制，超时会自动重试 3 次。

**Q：局内数据不刷新？**
A：LCD 端口 `2999` 仅在对局中开放，选人阶段该接口不可用属正常现象。

**Q：想贡献英雄数据？**
A：编辑 `data/champions.json`，字段参考现有条目；改完用 `python check_id.py` 校验 ID 映射。

---

## 免责声明

本项目为个人学习与研究性质的辅助工具，所有建议均由本地规则引擎与第三方大模型生成，**仅供参考，不构成任何游戏竞技层面的保证**。

使用本作时请注意：

- 遵守《英雄联盟》玩家守则与 Riot Games 相关规定，避免违反反作弊条款
- 切勿用于任何形式的自动化游戏操控
- 请勿将 `config.json` 中的 `api_key` 提交到公开仓库

---

## License

仅供个人学习使用。
