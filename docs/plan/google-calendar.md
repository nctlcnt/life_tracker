# LT-68 日和读取 Google Calendar（只读追踪 event）

## Context

日和（Life Tracker bot）目前能记 timeline、deadline、reminder，但看不到我的日程安排。LT-68 要让它能读 Google Calendar，回答「今天/这周有什么安排」「临近的 event」这类问题，并在对话里和 timeline / deadline 协同。

已定方案：**Google Calendar API + OAuth（只读）**，而不是 ICS 订阅链接——为拿到完整字段，并为以后 Hedwig 的「写入日历」(LT-56/62) 复用同一套凭证打基础。本期 **只读，不写、不自动入 timeline**。

代码架构已确认可顺接：工具 schema 集中在 `bot/tools.py` 的 `TOOLS`（`to_anthropic_tools` 自动转 Claude 格式，无需逐 provider 改）；执行集中在 `bot/ai_engine_base.py::_execute_tool(db, name, args)`；时区用 `bot/timezone_state.get_timezone()`；`config.json` 与 `data/` 均已 gitignore，凭证/token 放这里不进仓库。

## Step 0 — 分支准备（按你选的 stash）

1. `git stash push -m "LT-107 reflection WIP"`（暂存 3 个 LT-107 改动：`ai_engine_base.py` / `prompts.py` / `tools.py`）
2. `git checkout main && git checkout -b feat/LT-68-google-calendar-read`
3. ⚠️ 回 LT-107 时记得 `git stash pop` 恢复

## 需要你做的前置（我无法代劳）

在 Google Cloud Console：建项目 → 启用 **Google Calendar API** → 创建 **OAuth client（Desktop app 类型）** → 下载 client secret JSON，放到 `data/google_oauth_client.json`。授权范围用 `calendar.readonly`。

## 实现

### 1. 依赖 `requirements.txt`
新增：`google-api-python-client`、`google-auth`、`google-auth-oauthlib`。

### 2. 配置 `config.json` / `config.py` / `config.example.json`
新增 section（值放 gitignore 过的 `config.json`，并在 committed 的 `config.example.json` 里加文档）：
```json
"google_calendar": {
  "enabled": true,
  "client_secret_file": "data/google_oauth_client.json",
  "token_file": "data/google_calendar_token.json",
  "calendar_id": "primary"
}
```
`config.py` 按现有 `_cfg.get(...)` 模式导出 `GCAL_ENABLED / GCAL_CLIENT_SECRET_FILE / GCAL_TOKEN_FILE / GCAL_CALENDAR_ID`（路径相对仓库根解析）。不加入必填校验（缺失时优雅降级）。

### 3. 一次性授权脚本 `scripts/google_calendar_auth.py`（新建）
用 `google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(client_secret_file, ["https://www.googleapis.com/auth/calendar.readonly"])` 跑授权，把含 refresh_token 的凭证写入 `data/google_calendar_token.json`。设计成 headless 友好（打印授权 URL，接收回调 code）；也可在有浏览器的机器上跑完把 token.json 拷到 VPS 的 `data/`。只跑一次。

### 4. 日历读取模块 `bot/google_calendar.py`（新建）
- `class CalendarNotConfigured(Exception)`：未配置/未授权时抛。
- `_credentials()`：从 `GCAL_TOKEN_FILE` 加载 `Credentials.from_authorized_user_file`；过期且有 refresh_token 则 `creds.refresh(Request())` 并回写 token 文件；token 文件不存在或 `enabled=False` → 抛 `CalendarNotConfigured`。模块级缓存。
- `list_events(start, end, query=None, max_results=50) -> list[dict]`：
  - 把 AI 传入的本地 ISO（`YYYY-MM-DD` 或带时间，参照 `query_timeline` 的入参风格）用 `ZoneInfo(get_timezone())` 转成 RFC3339 offset-aware 的 `timeMin/timeMax`。
  - `service.events().list(calendarId=GCAL_CALENDAR_ID, timeMin, timeMax, singleEvents=True, orderBy="startTime", q=query, maxResults=..., timeZone=<tz>)`。
  - 归一化每条：`{id, summary, start, end, all_day, location, status}`（区分 `dateTime` 计时 vs `date` 全天，时间按本地时区格式化）。

### 5. 工具 schema `bot/tools.py`
在 `TOOLS` 加 `query_calendar`（入参 `start` / `end` 必填、`query` 可选），description 说明它是「计划中的日程」、与 `query_timeline`（已发生轨迹）区分。**只读**，不加入 `SET_TOOL_NAMES`（与 `query_timeline` 一致），随全量 TOOLS 在 chat/poll 流都可用。

### 6. 执行分发 `bot/ai_engine_base.py::_execute_tool`
加 `elif tool_name == "query_calendar"`：调用 `google_calendar.list_events(...)`，捕获 `CalendarNotConfigured` → 返回 `{"success": False, "message": "Google Calendar 未配置/未授权，先跑 scripts/google_calendar_auth.py"}`；成功返回 `{"success": True, "events": [...], "count": n}`。`_execute_tool` 只收 `db`，日历模块自管凭证，**无需改签名**。工具调用摘要日志（`_TOOL_DESC_MAP`）会从 schema 自动生成，无需额外改。

### 7. 使用策略 prompt
在运行库的 `tools` prompt section（Hiyori 实际用的那条，通过 admin UI 编辑）补一段：何时查日历、与 timeline/deadline 的区别、时间范围按当前时间推算。同步更新 `docs/default-prompts.json` 的 `sections.tools`，让新装也带上（必要时用 `scripts/import_prompts.py` 应用到 DB）。

## 关键文件
- 新建：`bot/google_calendar.py`、`scripts/google_calendar_auth.py`
- 改：`requirements.txt`、`config.py`、`config.example.json`、`bot/tools.py`、`bot/ai_engine_base.py`、`docs/default-prompts.json`
- 复用：`bot/timezone_state.get_timezone()`、`bot/tools.py::to_anthropic_tools`、`_execute_tool` 分发模式、config.py 的 `_cfg.get` 模式

## 验证
1. 完成 Google Cloud 前置 + 跑授权脚本 → 确认 `data/google_calendar_token.json` 生成。
2. 模块级冒烟：`python3 -c "from bot import google_calendar as g; print(g.list_events('2026-06-17','2026-06-24'))"`（先 `export TZ=...` 或走进程时区）→ 返回真实 event，时间为本地时区。
3. 端到端：staging（8081）里对日和说「我这周有什么安排」→ 触发 `query_calendar` → 回真实日程。
4. 降级路径：把 token 文件移走/`enabled=false`，再问日历 → 返回「未配置」提示且 bot 不崩。
5. 安全核查：`git status` 确认 `data/` 下的 token / client secret 未被跟踪。

## Non-goals（本期不做）
写入/创建/改 event（留给 LT-56/62）；多日历筛选 UI；把 event 自动落进 timeline / daily briefing（后续接 LT-107）。
