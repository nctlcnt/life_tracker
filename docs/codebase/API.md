# API 参考

来源：`api/server.py`（唯一的路由文件，没有拆分子路由）。这是现状快照，接口改动请同步更新本文件。

## 鉴权

除 `PUBLIC_API_PATHS`（`/api/auth/login`、`/api/auth/logout`、`/api/auth/session`、`/api/calendar/oauth/callback`）外，所有 `/api/*` 路径以及 `/docs`、`/redoc`、`/openapi.json` 都被 `ApiAuthMiddleware` 拦截，需要下面两种方式之一：

- 请求头 `x-api-key: <LIFE_TRACKER_API_KEY>`
- Cookie `life_tracker_session`（由 `POST /api/auth/login` 用 API key 换取，30 天有效）

`LIFE_TRACKER_API_KEY` 未配置（或长度小于 32 位）时，所有受保护接口一律返回 503。

## Auth

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | body `{api_key}`，校验通过后种下 session cookie |
| GET | `/api/auth/session` | 返回 `{authenticated, configured}` |
| POST | `/api/auth/logout` | 清除 session cookie |
| GET | `/api/calendar/oauth/callback` | Google Calendar OAuth 回调，返回 HTML 页面（非 JSON） |

## Timeline / Events

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/timeline?start=&end=` | 时间范围内的事件，合并相邻段后返回 `{segments, count}` |
| GET | `/api/events?start=&end=` | 同范围的原始事件（调试用），`{events, count}` |
| POST | `/api/events` | body `{content, category?, start_time?, end_time?, project_name?, notes?}`；`category=Focus` 时 `project_name` 必填且必须是已建项目 |
| GET | `/api/categories` | 全部事件分类 |

## Memory

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memories` | 全部记忆（含已过期） |
| POST | `/api/memories` | body `{content, memory_type?, valid_until?}` |
| PATCH | `/api/memories/{memory_id}` | 只更新 body 中出现的字段；字段传 `null`/空串代表清空 |
| DELETE | `/api/memories/{memory_id}` | 删除一条记忆 |
| GET | `/api/memory-document` | 返回 Markdown 记忆文档全文 + 用量统计（未启用 Markdown 记忆时 501） |
| PUT | `/api/memory-document` | body `{content}`，整份原子替换 |

## Memos（App 亲手写的 memo）

见 `docs/codebase/backend-memo-plan.md`。`memos` 是独立表，不进 `conversation_messages`/`events`；AI 只通过 prompt 注入只读今天的 memo。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memos?cursor=&limit=200` | 增量拉取，含已软删除的行；`{items, next_cursor, has_more}`，`limit` 上限 500 |
| POST | `/api/memos` | body `{client_id?, content, occurred_at, images?, source?, latitude?, longitude?, place_name?}`；`occurred_at` 必须带时区，否则 400；地点三个字段作为整体处理；传 null 表示清空；不注入 AI；重复 `client_id` 幂等，返回已有内容 |
| PATCH | `/api/memos/{memo_id}` | body `{content?, occurred_at?, images?, latitude?, longitude?, place_name?}`，只更新出现的字段；地点三个字段作为整体处理；传 null 表示清空；不注入 AI；不存在或已软删除 404 |
| DELETE | `/api/memos/{memo_id}` | 软删除；不存在或已删除也返回 204（保证 App 同步队列幂等） |

响应里的 `id` 是字符串，`created_at`/`updated_at`/`occurred_at`/`deleted_at` 统一是 UTC 毫秒精度 `...Z` 格式，游标是 `base64url("{updated_at}|{id}")`。

## Reminders / Todos / Deadlines

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reminders?status=&done=` | 最近 100 条，`status` 优先于旧参数 `done` |
| GET | `/api/todos?all=` | `all=true` 含已完成 |
| POST | `/api/todos` | body `{content}` |
| PATCH | `/api/todos/{todo_id}/done` | body `{done?}`（默认 true） |
| GET | `/api/deadlines` | 所有 active deadline，附加 `countdown` 倒计时字段 |

## Check-ins（可配置定时对话）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/check-ins` | 列出全部 |
| POST | `/api/check-ins` | body 需要 `name/schedule_type/prompt_template`，其余字段见 `_check_in_fields_from_body` |
| PATCH | `/api/check-ins/{check_in_id}` | 局部更新；改到排程相关字段会触发重新排程 |
| DELETE | `/api/check-ins/{check_in_id}` | 内置 check-in 不可删 |
| POST | `/api/check-ins/{check_in_id}/test` | 立即触发一次真实执行（会真发 Discord 消息），不影响当天定时触发 |

## Projects

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/projects` | 项目清单，含 `archived` 标记 |
| POST | `/api/projects` | body `{name}` |
| PATCH | `/api/projects/{old_name}` | body `{name}`，重命名并同步历史事件 |
| DELETE | `/api/projects/{name}` | 只删清单项，不删历史事件 |
| POST | `/api/projects/archive` / `/api/projects/unarchive` | body `{name}`，幂等 |
| GET | `/api/projects/heatmap?days=90` | 每个项目每天的 Focus check-in 次数热力图数据 |

## Traces（AI 调用观测）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/traces/dates` | 有记录的日期列表 |
| GET | `/api/traces?date=&trigger=&limit=` | 某天的 trace 列表 |
| GET | `/api/traces/tools?limit=&date=&trigger=&name=` | 最近的 AI 工具调用日志 |

## 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 需鉴权的健康检查 |
| GET | `/internal/health` | 不鉴权，仅供容器探活 |
| GET | `/api/version` | 当前镜像版本 |
| GET | `/api/weather` | 今日天气结构化数据，无配置/失败时 `{available: false}` |

## Admin：AI Preset

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/presets` | 列出全部 preset，标记 active / fallback（API key 掩码显示） |
| POST | `/api/admin/presets` | body `{name, provider, api_key, base_url, model, note?, use_v1_suffix?}` |
| PATCH | `/api/admin/presets/{name}` | 可改 provider/api_key/base_url/model/note/use_v1_suffix；`api_key` 留空表示不改 |
| DELETE | `/api/admin/presets/{name}` | 当前 active 禁止删除 |
| POST | `/api/admin/presets/active` | body `{name}`，切换主 preset |
| POST | `/api/admin/presets/fallback` | body `{name}`（`null` 关闭 fallback） |
| POST | `/api/admin/presets/test` | body `{name}`，对该 preset 发一条 "hello" 纯连接测试 |
| GET | `/api/admin/compact-preset` | 当前 compact 摘要用的 preset |
| PUT | `/api/admin/compact-preset` | body `{name}`（空/null 表示回落 active） |

## Admin：Prompt

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/prompts` | 列出可编辑 prompt sections（正文存 DB，不进 Git） |
| GET | `/api/admin/prompts/preview?check_in_id=` | 渲染全部运行时 prompt 轨道预览，不改数据 |
| PUT | `/api/admin/prompts/{key}` | body `{value}`，保存单个 section |

---

未列出的路径 `GET /` 重定向到 `/app/index.html`（前端静态文件挂载在 `/app`）。所有响应体均为 JSON，除 OAuth 回调和根路径外。
