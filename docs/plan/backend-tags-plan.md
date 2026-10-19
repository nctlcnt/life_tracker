# 实施计划：memo 标签（tags）

> 基于 life_tracker `main` @ `3a8b22a`。请从 `main` 新开分支（比如 `feat/memo-tags`），**不要**基于 `feat/message-cards`。
> 这次只做后端的数据和 API，App 端另外做。请按「边界」一节控制范围。

## 背景

现在 memo 的标签是 App 从正文里解析 `#xxx` 得到的，后端没有标签的概念。这次改成结构化的标签：

1. 标签单独存储，可以设置颜色、父标签（**最多两层**，比如 娱乐 → 电影），可以是计时标签（App 显示为 `电影 | 12天`，天数由 App 用 `today - started_at` 算出，后端不存）
2. 一条 memo 可以挂**多个**标签；标签不再从正文解析
3. 标签可能在 App 离线时创建，所以和 memo 一样，用 App 生成的 `client_id` 做幂等
4. **对外一律用标签的 `client_id` 表示一个标签**：memo 上挂的标签、标签的父标签、标签接口的路径参数都用 `client_id`。原因是 App 里的标签在同步前还没有服务器 id，这样 App 不需要做 id 映射。服务器内部仍然用整数 id 关联。

每个标签下有多少篇 memo 由 App 在本地统计，后端不需要提供计数接口。

---

## 1. 表结构（`bot/database.py`）

在 `_init_tables` 里 memos 建表语句后面加两张新表。都是新表，`CREATE TABLE IF NOT EXISTS` 就够了，不需要 `ALTER TABLE`。

```sql
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL UNIQUE,       -- App 生成的 UUID
    name TEXT NOT NULL,
    parent_id INTEGER,                    -- tags.id；NULL = 一级标签。最多两层
    color TEXT,                           -- "#RRGGBB"；NULL = App 自己从调色板选
    is_timed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,                      -- 计时起点，UTC 固定格式；非计时标签为 NULL
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,             -- 任何修改（含软删除）都要更新
    deleted_at TEXT                       -- 软删除；NULL = 未删除
);

CREATE TABLE IF NOT EXISTS memo_tags (
    memo_id INTEGER NOT NULL,             -- memos.id
    tag_id INTEGER NOT NULL,              -- tags.id
    PRIMARY KEY (memo_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_memo_tags_tag ON memo_tags(tag_id);
```

`_get_conn()` 不开 `PRAGMA foreign_keys`，所以**引用关系都在代码里校验**，不要依赖外键约束。

## 2. Database 方法（`bot/database.py`）

在 Memos 那一段后面加一个 `# ============ Tags ============` 段。

**2a. 标签的读写**

- `_tag_row_to_dict(row)`：`id` 转成字符串（和 memo 一样），`is_timed` 转成 bool，去掉 `parent_id`，加上 `parent_client_id`。查询时用 `SELECT t.*, p.client_id AS parent_client_id FROM tags t LEFT JOIN tags p ON p.id = t.parent_id`，一次拿到。
- `list_tags() -> list[dict]`：返回**全部**标签，包括已软删除的（App 需要知道哪些被删了）。标签数量很少，不做分页和游标。
- `get_tag_by_client_id(client_id) -> dict | None`：包括已软删除的。
- `upsert_tag(*, client_id, name, parent_id, color, is_timed, started_at) -> dict`：`ON CONFLICT(client_id) DO NOTHING`，重复的 `client_id` 返回已有那一行、不覆盖。行为和 `upsert_memo` 一致。
- `update_tag(client_id, **fields) -> dict | None`：`allowed = {"name", "parent_id", "color", "is_timed", "started_at"}`，同时更新 `updated_at`。行不存在或已删除时返回 None。
- `soft_delete_tag(client_id) -> bool`：在**同一个事务**里完成三件事：
  1. 这个标签 `deleted_at = updated_at = now`
  2. 找出挂了这个标签、而且没被删除的 memo，把它们的 `updated_at` 更新为 now（App 靠 `updated_at` 增量拉取，不更新的话手机上这些 memo 还会显示这个标签）
  3. 删除 `memo_tags` 里这个标签的所有行

  已删除或不存在时返回 False，不报错。

**2b. memo 上的标签**

- 每个返回给 API 的 memo dict 都要**始终包含** `tag_client_ids: list[str]`，没有标签时为 `[]`，只包含未删除的标签。顺序不重要，App 自己排序。
- `_memo_row_to_dict` 是 staticmethod，拿不到 conn。加一个 `_attach_memo_tags(conn, items)`，对一批 memo **用一条查询**取出标签，不要每条 memo 查一次：

  ```sql
  SELECT mt.memo_id, t.client_id
  FROM memo_tags mt JOIN tags t ON t.id = mt.tag_id
  WHERE t.deleted_at IS NULL AND mt.memo_id IN (?, ?, ...)
  ```

  在 `upsert_memo`、`get_memo`、`update_memo`、`list_memos_after` 里返回之前调用。`list_memos_after` 一次最多 501 行，在 SQLite 参数个数的限制以内。`list_memos_between`（给 prompt 用）**不需要**加。
- `upsert_memo` 增加关键字参数 `tag_ids: list[int] | None = None`。**只有真正插入了新行时**（`cursor.rowcount == 1`）才写入 `memo_tags`；`client_id` 重复时什么都不改，保持现有的幂等行为。
- `update_memo` 增加对 `tag_ids` 的处理：传入时**整体替换**这条 memo 的标签（先删后插），在同一个事务里完成，并且**一定要更新 `updated_at`**。注意现在的代码在 `updates` 为空时会直接 `return self.get_memo(...)`，不会更新 `updated_at`；只改标签的 PATCH 不能走这条路径。

## 3. API（`api/server.py`）

### 3a. 标签接口

标签对象（所有标签接口的响应格式）：

```json
{
  "id": "12",
  "client_id": "6F1C…",
  "name": "电影",
  "parent_client_id": "A93B…",
  "color": "#E8A0BF",
  "is_timed": false,
  "started_at": null,
  "created_at": "…", "updated_at": "…", "deleted_at": null
}
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tags` | `{"items": [...]}`，全部标签，含已软删除的 |
| POST | `/api/tags` | body: `{client_id?, name, parent_client_id?, color?, is_timed?, started_at?}`；`client_id` 重复时幂等，返回已有的、不覆盖 |
| PATCH | `/api/tags/{client_id}` | 只更新出现的字段；不存在或已删除返回 404 |
| DELETE | `/api/tags/{client_id}` | 204，幂等（不存在或已删除也返回 204，原因同 memo 的 DELETE）；**还有未删除的子标签时返回 409** |

**校验**（写一个 `_tag_fields_from_body(body, existing=None)`，POST 和 PATCH 共用；PATCH 时用「已有值 + 本次修改」合并后的结果来校验）：

- `name`：字符串，去掉首尾空白后不能为空，最长 30 个字符，**不能包含 `/`**（以前 `#娱乐/电影` 用 `/` 表示层级，以后迁移旧数据时要靠它区分）
- 同一个父标签下（一级标签之间也算），未删除的标签不能重名，重名返回 409
- `parent_client_id`：null，或者一个**存在、未删除、本身是一级标签**的标签，而且不能是自己。另外，如果这个标签自己还有未删除的子标签，就不能再给它设父标签（否则会变成三层）。不满足时返回 400
- `color`：null，或者匹配 `^#[0-9A-Fa-f]{6}$`
- `is_timed`：bool（不接受 0/1 这类数字）
- `started_at`：`is_timed` 为 true 时必填，用 `normalize_utc_iso` 规范化；`is_timed` 为 false 时强制写成 null

### 3b. memo 接口增加 `tag_client_ids`

写一个 `_memo_tag_ids_from_body(body) -> list[int] | None`：

- 没有 `tag_client_ids` 这个 key：返回 None，表示「不涉及标签」（POST 时就是没有标签，PATCH 时就是不修改）
- 有这个 key：必须是字符串数组（可以是空数组，表示清空），去重，最多 20 个
- 每个 `client_id` 都必须对应一个**存在**的标签，否则返回 400，并在 detail 里写出是哪一个。App 会先推送标签、再推送 memo，所以正常情况下不会出现引用不存在的标签；标签从不硬删除，所以「不存在」只可能是 App 的 bug
- 引用**已软删除**的标签时，**静默忽略、不报错**。原因：App 的 `pushPending()` 只要某一条请求返回错误，就会中断整轮同步。如果 memo 在离线期间、或者在重试一条「服务器已写入但响应丢失」的 memo 时，它引用的标签已经在另一台设备上被删除，此时返回 400 会让这条 memo 一直卡在推送队列里
- 返回对应的整数 `tags.id` 列表（不含被忽略的已删除标签）

POST `/api/memos`：结果不为 None 时，传给 `db.upsert_memo(tag_ids=...)`。
PATCH `/api/memos/{id}`：结果不为 None 时传给 `update_memo`。只包含 `tag_client_ids` 的 PATCH 也算有字段，不能返回 400 "no fields to update"。

所有返回 memo 的接口（POST、PATCH、GET 增量拉取）里，每个 memo 都**始终包含** `tag_client_ids`。

更新 `create_memo` 和 `update_memo` 的 docstring。

## 4. 测试（新建 `tests/test_tags_api.py`，memo 相关的测试加在 `tests/test_memos_api.py`）

沿用 `test_memos_api.py` 的写法（Database 层用 pytest 函数 + `tmp_path`，HTTP 层用 unittest 类）。

Database 层：
- `upsert_tag` 重复 `client_id` 时返回第一次的内容
- `list_tags` 包含已软删除的标签，`parent_client_id` 正确
- `soft_delete_tag`：标签的 `deleted_at` 有值；挂了它的 memo 的 `updated_at` 变大，`tag_client_ids` 里不再有它；没挂它的 memo 的 `updated_at` **不变**；重复删除返回 False、不报错
- `upsert_memo` 带 `tag_ids` 时写入；`client_id` 重复、`tag_ids` 不同时，标签保持第一次的
- `update_memo` 只传 `tag_ids`：标签被整体替换，`updated_at` 变大
- 旧库兼容：用一个已有 memos 表、没有 tags 表的库打开 `Database(path)`，新表被建好，旧 memo 返回 `tag_client_ids == []`；再打开一次也不报错

HTTP 层：
- POST 标签 → 200；同一个 `client_id` 再 POST 一次，返回第一次的内容
- 名字为空、超过 30 字、包含 `/` → 400；同一父标签下重名 → 409；子标签下面再挂子标签 → 400；`color: "red"` → 400；`is_timed: true` 但没有 `started_at` → 400
- PATCH 不存在的标签 → 404
- DELETE 还有子标签的父标签 → 409；先删子标签、再删父标签 → 两次都是 204；再删一次 → 204
- POST memo 带 `tag_client_ids` → 响应里有这些标签；不带 → 响应里是 `[]`
- POST memo 引用不存在的标签 → 400，detail 里包含这个 `client_id`
- POST、PATCH memo 引用已软删除的标签 → 200，响应里不含这个标签；对同一个 `client_id` 重试 POST，返回已有的 memo、不覆盖
- PATCH memo 只发送 `{"tag_client_ids": []}` → 200，标签被清空，`updated_at` 变大
- PATCH memo 只发送 `{"content": "…"}` → 标签不变
- GET `/api/memos` 增量拉取的每个 item 都有 `tag_client_ids`
- 删除一个标签之后，用删除前的 cursor 增量拉取，能拉到挂过这个标签的 memo，而且里面已经没有这个标签

Prompt：
- 带标签的 memo 经过 `get_today_memos_for_prompt` 后，返回的 dict 里没有 `tag_client_ids`（现在的实现只挑选字段，应该本来就满足，加一条测试固定这个行为）

## 5. 文档

- `docs/codebase/API.md`：增加 Tags 一节（四个接口 + 标签对象），memo 的 POST、PATCH body 和响应里加上 `tag_client_ids`
- `docs/codebase/backend-memo-plan.md`：在表结构和 API 两节里补上 `tags`、`memo_tags` 和 `tag_client_ids`

---

## 边界（这次不要做的）

- **不解析正文里的 `#xxx`**，不论来源是 App 还是快捷指令。正文原样保存，和标签无关。
- **不迁移旧数据**：已有 memo 正文里的 `#xxx` 不处理，以后单独写迁移脚本。
- **不把标签放进任何 AI 上下文**：不修改 `get_today_memos_for_prompt` 和 `_format_today_memos`，不传给 tool worker、compact、curator，也不传给 embedding。
- **不做计数或统计接口**，计数由 App 统计。
- **不做「删除父标签时连子标签一起删」或「子标签升为一级」**：有子标签时直接返回 409，由 App 处理。
- **不修改已有的 memo 行为**：cursor 编码和分页、软删除、DELETE 幂等（204）、POST 按 `client_id` 幂等且不覆盖、PATCH 对已删除行返回 404、时间格式、地点字段的处理，这些都保持现状。
- 现有测试 `test_list_memos_after_can_miss_an_update_that_lands_behind_an_already_issued_cursor` 记录的是一个已知的限制，这次不要修改它，也不要修改它对应的逻辑。
- 不修改其他表，不修改认证逻辑，不引入新的依赖。

## 验证

1. `pytest tests/test_tags_api.py tests/test_memos_api.py`，然后跑一次完整的 `pytest`，确认其他测试没有受影响。
2. 先部署到 staging（8081），**不要**直接部署到 8080。重启两次容器，确认建表可以重复执行、不会报错。
3. 用 curl 在 staging 上验证：

```bash
KEY=<LIFE_TRACKER_API_KEY>; BASE=http://localhost:8081
H=(-H "X-API-Key: $KEY" -H "Content-Type: application/json")

# 建一个父标签和一个子标签
curl -s -X POST "${H[@]}" -d '{"client_id":"tag-ent","name":"娱乐","color":"#E8A0BF"}' $BASE/api/tags
curl -s -X POST "${H[@]}" -d '{"client_id":"tag-movie","name":"电影","parent_client_id":"tag-ent"}' $BASE/api/tags

# 带标签新建 memo
curl -s -X POST "${H[@]}" \
  -d '{"client_id":"tag-test-1","content":"标签测试","occurred_at":"2026-10-06T08:00:00.000Z","tag_client_ids":["tag-movie"]}' \
  $BASE/api/memos

# 删除父标签 → 应该是 409
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "${H[@]}" $BASE/api/tags/tag-ent

# 全部标签、增量拉取 memo
curl -s "${H[@]}" $BASE/api/tags
curl -s "${H[@]}" "$BASE/api/memos?limit=5"
```

4. 在 Admin 的 prompt preview 里确认【她今天亲手写的 memo】这一段里没有标签。
5. 确认没问题之后再部署到 prod。
