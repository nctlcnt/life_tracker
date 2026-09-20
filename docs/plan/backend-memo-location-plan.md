# 实施计划：memos 增加地点字段

> 基于 life_tracker `main` @ `cf9f893`（Add memos table and API for iOS app note-taking）。
> 这是在已有 memo 功能上的**增量改动**，只给 memo 增加地点。改动范围小，请严格按「边界」一节执行。

## 背景

iOS App（LifeMemo）写 memo 时会附带当时的地点（经纬度 + 地名）。App 已经在请求里发送这三个字段，并且会用服务器返回的 Memo 覆盖本地数据。

当前后端的问题：`memos` 表没有地点列，POST / PATCH 忽略这几个字段，响应里也不返回。因为 App 用响应覆盖本地，所以**每同步一次，带地点的 memo 在手机上的地点就会被清空**，后端也没有保存，地点会彻底丢失。

需求：

1. memo 可以保存、修改、清除地点
2. 地点**只给 App 显示用，不进入任何 AI 上下文**（chat、poll、tool worker 的 prompt 都不包含）

## App 端的请求和期望（已实现，后端需要匹配）

字段名（JSON，snake_case）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `latitude` | number \| null | WGS84 纬度 |
| `longitude` | number \| null | WGS84 经度 |
| `place_name` | string \| null | 地名，由 App 在手机上反向地理编码得到，比如 `"Kensington"`；可能为 null（拿不到地名时 App 显示经纬度） |

- POST：有地点时带上三个字段；没有地点时这三个 key **不出现**。
- PATCH：**每次都带上这三个 key**。没有地点（包括用户在编辑时去掉了地点）时，三个值都是 `null`。
- 响应：App 期望每个 Memo 对象里**始终包含**这三个 key，没有值时为 `null`。

---

## 改动的文件

### 1. `bot/database.py`

**1a. 建表语句**：在 `CREATE TABLE IF NOT EXISTS memos` 里，`source` 之后增加三列（给新建的数据库用）：

```sql
    -- 写 memo 时所在的地点。只给 App 显示用，不注入任何 AI 上下文
    latitude REAL,
    longitude REAL,
    place_name TEXT
```

**1b. 迁移已有数据库**：因为 `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，prod 和 staging 的库需要 `ALTER TABLE`。放在 `_init_tables` 末尾已有的「兼容已有数据库：尝试加列」那一段里，沿用同样的写法。

**每一列单独一个 try**：

```python
        # memos 地点字段（见 docs/codebase/backend-memo-plan.md）
        for ddl in ("ALTER TABLE memos ADD COLUMN latitude REAL",
                    "ALTER TABLE memos ADD COLUMN longitude REAL",
                    "ALTER TABLE memos ADD COLUMN place_name TEXT"):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 列已存在
```

不要把三条 ALTER 放进同一个 try（现有的 `conversation_messages` embedding 迁移就是这么写的）。因为只要第一列已经存在，后面两条就会被跳过；如果某次迁移只执行了一半，剩下的列就永远加不上。

**1c. `upsert_memo`**：增加三个关键字参数，默认 `None`，写进 INSERT。

```python
def upsert_memo(self, *, client_id: str, content: str, occurred_at: str,
                images: Optional[list] = None, source: str = "app",
                latitude: Optional[float] = None,
                longitude: Optional[float] = None,
                place_name: Optional[str] = None) -> dict:
```

`ON CONFLICT(client_id) DO NOTHING` 的行为**保持不变**：重复的 `client_id` 返回已有那一行，地点也不覆盖。

**1d. `update_memo`**：`allowed` 增加 `"latitude", "longitude", "place_name"`，docstring 同步更新。值为 `None` 时写成 NULL，也就是清空。这一点现有代码已经支持（`fields` 里的 `None` 会作为参数原样传给 SQLite），不需要特殊处理。修改地点同样会更新 `updated_at`，这一点必须保留，因为 App 靠 `updated_at` 增量拉取。

**1e. `_memo_row_to_dict`**：不需要修改。它基于 `SELECT *` 的结果，加列之后会自动包含这三个 key，值为 NULL 时返回 `None`，JSON 里就是 `null`。验证的时候确认一下旧行也会返回这三个 key。

### 2. `api/server.py`

**2a. 新增一个校验函数**，放在 memos 那一段里：

```python
def _memo_location_from_body(body: dict) -> dict | None:
    """从请求 body 里取地点字段，作为一个整体处理。

    - 三个 key 都不出现：返回 None，表示「不涉及地点」（POST 时就是没有地点，PATCH 时就是不修改）。
    - 只要出现其中任意一个：三个字段按整体处理，没出现的当作 null。
      App 每次都会发送完整的三个字段；按整体处理可以避免「只改了纬度」这种半更新状态。
    校验：
    - latitude / longitude 要么同时为 null，要么同时是数字（bool 不算数字）
    - latitude ∈ [-90, 90]，longitude ∈ [-180, 180]
    - place_name 是字符串或 null；去掉首尾空白后为空时当作 null；最长 200 字符
    - 经纬度为 null 时，place_name 强制为 null（没有坐标的地名没有意义）
    不合法时抛 HTTPException(400)。
    """
```

返回值是 `{"latitude": ..., "longitude": ..., "place_name": ...}`。

**2b. `create_memo`（POST）**：调用 `_memo_location_from_body(body)`，结果不为 None 时，把三个字段作为关键字参数传给 `db.upsert_memo`。

**2c. `update_memo`（PATCH）**：结果不为 None 时，`fields.update(location)`。

注意：现有代码在 `fields` 为空时返回 400 "no fields to update"。一个只包含地点的 PATCH（比如只去掉地点）加入地点字段后 `fields` 就不为空了，所以这个判断不需要改，但要写一条测试覆盖这种情况。

**2d.** 更新 `update_memo` 和 `create_memo` 的 docstring，写上地点字段。

### 3. `bot/memos.py`

`get_today_memos_for_prompt` 现在返回 `{**m, "local_time": ...}`，会把整行数据（现在也包括地点）传给 prompt 的格式化函数。`_format_today_memos` 目前只用到 `local_time` 和 `content`，所以地点实际上不会出现在 prompt 里。但为了防止以后有人修改格式化函数时不小心把地点带进去，这里改成**只传注入需要的字段**：

```python
    return [
        {"id": m["id"], "content": m["content"], "occurred_at": m["occurred_at"],
         "local_time": _local_time_label(m["occurred_at"])}
        for m in memos
    ]
```

并在 docstring 里写明：「地点字段故意不传：地点只给 App 显示用，不进入 AI 上下文。」

先确认 `_format_today_memos` 和其他调用 `get_today_memos_for_prompt` 的地方（`bot/ai_engine_base.py`、`bot/async_pipeline/worker_prompts.py`）没有用到这四个字段以外的 key。

### 4. `tests/test_memos_api.py`

新增测试，沿用文件里现有的写法（Database 层用 pytest 函数 + `tmp_path`，HTTP 层写在 `MemosApiTests` 里）：

Database 层：
- `upsert_memo` 带地点，返回的 dict 里有这三个值
- `upsert_memo` 不带地点，三个 key **存在**且值为 `None`
- 重复 `client_id` 并带上不同的地点，返回的仍然是第一次的地点
- `update_memo(latitude=None, longitude=None, place_name=None)` 能清空地点，并且 `updated_at` 变大
- **迁移**：先用旧的建表语句（不含地点列）手动建一个库，插入一行，再用 `Database(path)` 打开它，确认三列被加上，旧行返回的三个值都是 `None`；再打开一次，确认不会报错（迁移可以重复执行）

HTTP 层：
- POST 带地点 → 200，响应里三个值正确
- POST 不带地点 → 响应里三个 key 存在且为 `null`
- POST 只带 `latitude` 不带 `longitude` → 400
- POST `latitude: 91` → 400；`longitude: -181` → 400；`latitude: true` → 400
- POST 有地名但经纬度为 null → 响应里 `place_name` 为 `null`
- PATCH 只发送 `{"latitude": null, "longitude": null, "place_name": null}` → 200，地点被清空，`updated_at` 变大
- PATCH 只发送 `{"content": "..."}` → 地点保持不变
- GET 增量拉取返回的 item 里包含地点字段

Prompt：
- 带地点的 memo 经过 `get_today_memos_for_prompt` 后，返回的 dict 里没有 `latitude` / `longitude` / `place_name`
- `_format_today_memos(get_today_memos_for_prompt(...))` 的文本里不包含地名字符串和经纬度数字

### 5. 文档

- `docs/codebase/API.md`：Memos 表格里，POST 和 PATCH 的 body 加上 `latitude?, longitude?, place_name?`，并注明「地点三个字段作为整体处理；传 null 表示清空；不注入 AI」。
- `docs/codebase/backend-memo-plan.md`：在表结构和 API 两节里补上地点字段（内容和本文件一致）。

---

## 边界（这次不要做的）

- **不把地点放进任何 AI 上下文。** 不修改 `_format_today_memos` 的输出格式，不在 `bot/prompts.py` 里加新的占位符，不把地点传给 tool worker、compact、curator，也不传给 embedding。
- **不修改已有的 memo 行为**：cursor 编码和分页、软删除、DELETE 幂等（204）、POST 按 `client_id` 幂等且不覆盖、PATCH 对已删除行返回 404、时间格式，这些都保持现状。
- **不在后端做地理编码。** 地名完全由 App 提供，后端不调用任何地图或地理编码服务，也不引入新的依赖。
- **不回填旧数据。** 已有 memo 的地点保持 NULL。
- **不修改其他表**（`events`、`conversation_messages` 等），不修改认证逻辑，也不修改现有 `conversation_messages` embedding 迁移的写法（它的问题只在这里说明，不在这次修复）。
- **不处理附件**（`/api/attachments`），也不实现 `/api/insights`。
- 现有测试 `test_list_memos_after_can_miss_an_update_that_lands_behind_an_already_issued_cursor` 记录的是一个已知的限制，这次不要修改它，也不要修改它对应的逻辑。

## 验证

1. `pytest tests/test_memos_api.py`，然后跑一次完整的 `pytest`，确认其他测试没有受影响。
2. 先部署到 staging（8081），**不要**直接部署到 8080。重启两次容器，确认迁移可以重复执行、不会报错。
3. 用 curl 在 staging 上验证：

```bash
KEY=<LIFE_TRACKER_API_KEY>; BASE=http://localhost:8081

# 带地点新建
curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"client_id":"loc-test-1","content":"地点测试","occurred_at":"2026-09-19T04:20:00.000Z","latitude":-33.9173,"longitude":151.2313,"place_name":"Kensington"}' \
  $BASE/api/memos

# 清空地点（把 <id> 换成上一步返回的 id）
curl -s -X PATCH -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"latitude":null,"longitude":null,"place_name":null}' \
  $BASE/api/memos/<id>

# 增量拉取，确认旧行也有这三个 key
curl -s -H "X-API-Key: $KEY" "$BASE/api/memos?limit=5"
```

4. 在 Admin 的 prompt preview 里确认【她今天亲手写的 memo】这一段里没有地名。
5. 确认没问题之后再部署到 prod。
