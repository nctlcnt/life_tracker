# 后端 memo 表与 API 实施计划

> 对应后端版本：life_tracker `main` @ `2fadbfb`（2026-09-11），即 LifeMemo 里 `backend/` submodule 指向的 commit。
> App 端的接口约定见 `docs/API.md`，本文件是后端怎么实现它。

## 背景

- App 里亲手写的内容（memo）单独存一张新表 `memos`，**不**放进 `conversation_messages`，也**不**放进 `events`。
  - 不放 `conversation_messages`：它是 append-only 的 source of truth，compact 摘要、embedding、`personal_memory_sources` 都从它派生，编辑和删除无法清理干净。
  - 不放 `events`：`events` 的每一行被当作「有时长、有分类的活动」。`end_time IS NULL` 表示进行中、`merge_events` 会给它补结束时间、`category` 是三分法枚举、AI 有 `update/delete_timeline_event` 工具可以改它。
- 「时间线」是 App 层的概念：App 首页把 `memos` 和 `/api/timeline` 的 events 合并显示。
- AI 通过 prompt 注入读取今天的 memo，只读，不从 memo 派生数据。因此 App 里删除（软删除）后，只要注入时过滤 `deleted_at IS NULL`，AI 就看不到。

## 范围

本次做：

1. `memos` 表
2. `GET/POST/PATCH/DELETE /api/memos`
3. 今天的 memo 注入 chat / poll / tool worker 的 prompt

本次不做（见文末「未决问题」）：`/api/attachments`、`/api/insights/{day}`、后端解析 #标签、tool worker 根据 memo 自动记活动。

---

## 1. 表结构

在 `bot/database.py::_init_tables` 的 `executescript` 里追加（`CREATE TABLE IF NOT EXISTS`，旧库启动时自动建表，不需要额外迁移）：

```sql
-- 用户亲手写的 memo（App / 快捷指令等入口）。
-- AI 只通过 prompt 注入读取，不写、不从它派生数据。
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL UNIQUE,      -- App 生成的 UUID；无 client_id 的来源由后端生成 uuid4
    content TEXT NOT NULL,               -- 原文，原样保存
    occurred_at TEXT NOT NULL,           -- memo 所属时间，用户可改
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,            -- 任何修改（含软删除）都要更新，增量同步靠它
    deleted_at TEXT,                     -- 软删除；NULL = 未删除
    images_json TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，附件 URL
    source TEXT NOT NULL DEFAULT 'app'   -- app / shortcut / discord / mcp ...
);

CREATE INDEX IF NOT EXISTS idx_memos_sync ON memos(updated_at, id);
CREATE INDEX IF NOT EXISTS idx_memos_occurred ON memos(occurred_at) WHERE deleted_at IS NULL;
```

## 2. 时间约定

`memos` 表的四个时间字段**统一存成 UTC、固定格式**：`YYYY-MM-DDTHH:MM:SS.mmmZ`，例如 `2026-09-18T04:20:00.123Z`。

- 因为同步 cursor 和按天查询都用 SQLite 字符串比较，只有同一时区、同一宽度的字符串，字典序才等于时间顺序。
- 这和 `events` 表（进程本地时区的 naive 时间）不同。`memos` 是新表，没有历史数据的兼容负担，所以直接用带时区的格式；App 端两种都能解析。
- App 发来的时间一定带时区（`...Z`）。不带时区的输入返回 400，避免猜时区。

建议放在 `bot/database.py` 顶部或单独的小模块：

```python
from datetime import datetime, timezone

def utc_now_iso() -> str:
    return _fmt(datetime.now(timezone.utc))

def normalize_utc_iso(raw: str) -> str:
    """把带时区的 ISO 8601 转成 UTC 固定格式；naive 时间抛 ValueError。"""
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return _fmt(dt)

def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
```

## 3. Database 方法

放在 `Database` 类里，新增一个 `# ============ Memos ============` 段。返回的 dict 由一个 `_memo_row_to_dict` 统一转换（见第 4 节的响应格式）。

| 方法 | 行为 |
|---|---|
| `upsert_memo(*, client_id, content, occurred_at, images, source) -> dict` | `client_id` 已存在时**不修改**，直接返回已有行（重复 POST 幂等）；不存在则插入，`created_at = updated_at = now` |
| `get_memo(memo_id) -> dict \| None` | 按 id 取，含已软删除的 |
| `update_memo(memo_id, **fields) -> dict \| None` | 只更新传入的 `content / occurred_at / images`，同时 `updated_at = now`；行不存在或已软删除返回 `None` |
| `soft_delete_memo(memo_id) -> bool` | `deleted_at = updated_at = now`；已删除或不存在返回 `False`（不报错） |
| `list_memos_after(cursor_updated_at, cursor_id, limit) -> list[dict]` | 增量同步，见第 4 节 |
| `list_memos_between(start_utc, end_utc) -> list[dict]` | `deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?`，按 `occurred_at` 升序；给 prompt 注入用 |

`client_id` 缺失时（Discord、快捷指令等）由调用方传 `str(uuid.uuid4())`。

## 4. API

放在 `api/server.py`，风格和现有接口一致（`body: dict` + `HTTPException`）。所有路径在 `/api/` 下，现有的 `ApiAuthMiddleware` 自动要求 `X-API-Key`，不需要额外处理认证。

### 响应里的 Memo 对象

```json
{
  "id": "42",
  "client_id": "8F1C...-UUID",
  "content": "今天去了代官山 #咖啡",
  "occurred_at": "2026-09-18T04:20:00.000Z",
  "created_at": "2026-09-18T04:21:03.120Z",
  "updated_at": "2026-09-18T04:21:03.120Z",
  "deleted_at": null,
  "images": [],
  "source": "app"
}
```

- `id` 以**字符串**返回（`str(row["id"])`），因为 App 的 `MemoDTO.id` 是 `String`，返回整数会解码失败。路径参数仍按 `int` 接收。
- `images` 由 `images_json` 反序列化。

### `GET /api/memos?cursor=<opaque>&limit=200`

增量拉取，**包含已软删除的行**（App 需要知道哪些被删了）。

- cursor 编码：`base64url("{updated_at}|{id}")`。不带 cursor = 从头拉取。
- 查询：
  ```sql
  SELECT * FROM memos
  WHERE updated_at > :u OR (updated_at = :u AND id > :id)
  ORDER BY updated_at, id
  LIMIT :limit + 1
  ```
  多取一条用来判断 `has_more`，返回时去掉。用 `(updated_at, id)` 一起比较，是为了避免多条 `updated_at` 相同时被跳过。
- `limit` 上限 500。
- 响应：`{"items": [...], "next_cursor": "...", "has_more": false}`
  - 有数据：`next_cursor` 指向本页最后一条。
  - 没有数据：原样返回请求里的 cursor（没带 cursor 就返回 `null`）。
- cursor 无法解码时返回 400。

### `POST /api/memos`

body：`{client_id, content, occurred_at, images?, source?}`

- `content` 去掉首尾空白后为空 → 400
- `occurred_at` 缺失或不带时区 → 400
- `client_id` 缺失 → 后端生成 uuid4
- 返回完整 Memo（200）。重复的 `client_id` 返回已有那条，**不**覆盖内容。

### `PATCH /api/memos/{id}`

body：`{content?, occurred_at?, images?}`，只更新出现的字段。

- 行不存在或已软删除 → 404
- 返回更新后的完整 Memo，`updated_at` 已更新

### `DELETE /api/memos/{id}`

- 设置 `deleted_at` 和 `updated_at`
- **不存在或已删除也返回 204**，保证幂等

之所以 DELETE 要幂等，是因为 App 的 `pushPending()` 一旦某一条抛错，会中断整轮同步（后面的推送和 pull 都不会执行）。如果重复删除返回 404，这条删除会一直卡在队列里。

---

## 5. 注入 prompt

目标：AI 在 chat / poll / tool worker 里都能看到**今天**、**未删除**的 memo，并且能和它自己记的 timeline 区分开。

### 5.1 「今天」的范围

和 App 的 `LogicalDay` 一致：一天从本地凌晨 4 点开始，到第二天凌晨 4 点结束。本地时区用 `bot.timezone_state.get_timezone()`（用户可以通过 `/tz` 切换，所以不要写死）。

```python
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from bot import timezone_state

DAY_CUTOFF_HOUR = 4

def today_memo_range_utc(now: datetime | None = None) -> tuple[str, str]:
    tz = ZoneInfo(timezone_state.get_timezone())
    local_now = (now or datetime.now(tz)).astimezone(tz)
    day = (local_now - timedelta(hours=DAY_CUTOFF_HOUR)).date()
    start = datetime.combine(day, time(DAY_CUTOFF_HOUR), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(DAY_CUTOFF_HOUR), tzinfo=tz)
    return _fmt(start), _fmt(end)
```

用 `datetime.combine(..., tzinfo=ZoneInfo)` 而不是「当前时间的 offset」，是因为悉尼有夏令时（10 月和 4 月切换），用 ZoneInfo 才能在切换当天得到正确的 offset。

注意：现有的 `get_today_events()` 用的是 0 点到 23:59，和这里的 4 点切分不一致。本次不改它，只是两者在凌晨 0–4 点之间看到的「今天」会不同。

### 5.2 新占位符 `{today_memos}`

`bot/prompts.py`：

1. `PLACEHOLDER_TIERS` 加 `"today_memos": 4`（和 `today_timeline` 同层，每轮都会变化）
2. 新增 `LABEL_TODAY_MEMOS = "【她今天亲手写的 memo（原文）】"`
3. 新增 `_format_today_memos(memos)`：没有数据时**返回空串**，有数据时自己拼标题。这样和 `memories` / `weather` 那一类一致，没有 memo 时整段消失，不会留下空标题
   ```python
   def _format_today_memos(memos: list[dict] | None) -> str:
       if not memos:
           return ""
       lines = [f"- {m['local_time']} | {m['content']}" for m in memos]
       return f"{LABEL_TODAY_MEMOS}\n" + "\n".join(lines)
   ```
   `local_time` 在注入前由调用方把 `occurred_at` 转成本地 `HH:MM`，因为 AI 需要的是本地时间，而表里存的是 UTC。
4. `build_prompt(...)` 加参数 `today_memos: list[dict] | None = None`，`values` 里加 `"today_memos": _format_today_memos(today_memos)`
5. `synthesize_main_template()` 在 `{today_timeline}` 之后加 `"{today_memos}"`

调用方：

- `bot/ai_engine_base.py` 的 `build_prompt_parts`：`include("include_today_memos")` 为真时取 `db.list_memos_between(*today_memo_range_utc())`，传给 `build_prompt`
- `bot/async_pipeline/worker_prompts.py`：同样传入
- `bot/database.py` 的 check-in `default_context` 加 `"include_today_memos": True`。`include()` 的默认值是 `True`，所以已存在的 check-in 不改配置也会注入

### 5.3 DB 里的模板要手动加占位符

**这一步容易漏。** 运行时用的是数据库里的 `main_template` 和 `tool_worker_template`，`synthesize_main_template()` 只在模板为空时才会使用。所以代码改完后，需要在 Admin 的 prompt 编辑页面，把 `{today_memos}` 加到这两个模板里（建议放在 `{today_timeline}` 后面）。`_validate_prompt_template` 用的是 `MAIN_TEMPLATE_PLACEHOLDERS` 白名单，第 1 步加进 `PLACEHOLDER_TIERS` 之后，保存时就不会被拒绝。

---

## 6. 测试

放在 `tests/test_memos_api.py`，参考 `tests/test_api_auth.py` 用 `httpx` + 临时 SQLite 文件的写法：

- POST 同一个 `client_id` 两次 → 只有一行，第二次返回的内容是第一次的
- POST 不带时区的 `occurred_at` → 400
- 返回的 `id` 是字符串，时间字段以 `Z` 结尾、毫秒 3 位
- PATCH 后 `updated_at` 变大；PATCH 已删除的 → 404
- DELETE 两次都是 204；删除后 GET 仍能拉到这一行，`deleted_at` 非空
- 分页：插入多条 `updated_at` 相同的行，`limit=1` 逐页拉取，不重复、不遗漏
- `list_memos_between` 不返回已删除的行
- `today_memo_range_utc`：本地 03:59 属于前一天，04:00 属于当天；夏令时切换日的范围正确
- `_format_today_memos([])` 返回空串；有数据时带标题

## 7. 部署后验证

```bash
KEY=<LIFE_TRACKER_API_KEY>; BASE=http://localhost:8081   # 先在 staging 验证

curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"client_id":"test-1","content":"测试 #咖啡","occurred_at":"2026-09-18T04:20:00.000Z","source":"app"}' \
  $BASE/api/memos

curl -s -H "X-API-Key: $KEY" "$BASE/api/memos?limit=10"
```

然后：

1. App 的 Setting 里 404 消失，「待上传」变成 0，memo 卡片上的 🔄 图标消失
2. Admin 的 prompt preview 里能看到【她今天亲手写的 memo】段落
3. 在 App 里删掉一条 → 再看 preview，那条不再出现

---

## 未决问题

- **附件**：App 推送 memo 前会先上传本地图片到 `POST /api/attachments`。这个接口没有之前，**带图片的 memo 会在上传这一步抛错，并中断整轮同步**（包括后面没有图片的 memo 和 pull）。所以在附件接口实现之前，App 里先不要给 memo 加图片。
- **App 端对 PATCH 404 的处理**：App 目前不处理「服务器上已删除」的情况，PATCH 返回 404 会让这条修改一直卡在队列里。这需要改 App 端（收到 404 时删掉本地这条），不需要改后端。
- **AI 根据 memo 记活动**：tool worker 目前只由 `conversation_messages` 的新消息触发，所以只在 App 里写 memo 时，AI 不会据此调用 `log_timeline_event`。它只会在下一次被 Discord 消息或 check-in 触发时，从注入的内容里看到 memo。
- **后端解析 #标签**：`requirements.txt` 里没有 `regex` 包，Python 标准库 `re` 不支持 `\p{L}`。等后端真的需要按标签统计（比如回顾）时再加。
- **compact 摘要里的残留**：如果 AI 在 Discord 回复里提到了某条 memo，这段回复会进入 `conversation_messages`，进而被 compact 进摘要。删除 memo 不会清理这些内容。目前只能接受这一点。
