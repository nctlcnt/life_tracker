"""
FastAPI 接口模块
给前端提供数据
"""
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from bot.database import Database
from bot.merge import merge_events
from bot import trace as ai_trace

app = FastAPI(title="Life Tracker API")

# 允许前端跨域请求（开发时 React 在 localhost:3000）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该改为你的前端域名
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["*"],
)

# 数据库实例会在 main.py 启动时注入
db: Database | None = None


def set_database(database: Database):
    global db
    db = database


@app.get("/api/timeline")
async def get_timeline(
    start: str = Query(..., description="起始时间 ISO 8601，如 2026-04-01T00:00:00"),
    end: str = Query(..., description="结束时间 ISO 8601，如 2026-04-07T23:59:59"),
):
    """查询时间范围内的数据：
    - segments: 真实事件（status IS NULL）的合并时间段
    - planned_events: 该范围内 status='planned' 的原始 event（不合并）
    - cancelled_events: 该范围内 status='cancelled' 的原始 event（不合并）
    前端对三组分别用不同视觉样式叠加渲染。
    """
    raw_events = db.get_events(start, end)
    actual = [e for e in raw_events if e.get("status") is None]
    planned = [e for e in raw_events if e.get("status") == "planned"]
    cancelled = [e for e in raw_events if e.get("status") == "cancelled"]
    segments = merge_events(actual)
    return {
        "segments": segments,
        "planned_events": planned,
        "cancelled_events": cancelled,
        "count": len(segments),
    }


@app.get("/api/events")
async def get_events(
    start: str = Query(..., description="起始时间 ISO 8601，如 2026-04-01T00:00:00"),
    end: str = Query(..., description="结束时间 ISO 8601，如 2026-04-07T23:59:59"),
):
    """查询时间范围内的所有原始事件（调试用）"""
    events = db.get_events(start, end)
    return {"events": events, "count": len(events)}


@app.delete("/api/events/{event_id}")
async def delete_event(event_id: int):
    """硬删一条 event。出于安全考虑，仅允许删除 status 非空的事件
    （planned / cancelled），真实已发生的事件（status IS NULL）拒绝删除——
    AI 侧需要删真实事件时走 delete_timeline_event 工具。"""
    event = db.get_event_by_id(event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"event_id={event_id} not found")
    if event.get("status") is None:
        raise HTTPException(
            status_code=403,
            detail="Refusing to delete actual event via UI. Only planned/cancelled events can be deleted here."
        )
    ok = db.delete_event(event_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Delete failed unexpectedly")
    return {"success": True, "event_id": event_id}


@app.post("/api/events")
async def create_event(body: dict):
    """手动新建一条 timeline event（仪表板 New event 模态框入口）。
    body: {content, category?, start_time?, end_time?, project_name?, notes?}
    缺省: category=Routine, start_time=now ISO, end_time=null（进行中）。
    """
    from datetime import datetime
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    category = body.get("category") or "Routine"
    start_time = body.get("start_time") or datetime.now().isoformat(timespec="seconds")
    end_time = body.get("end_time") or None
    event_id = db.add_event(
        start_time=start_time,
        end_time=end_time,
        content=content,
        category=category,
        notes=body.get("notes"),
        project_name=body.get("project_name"),
    )
    return {"id": event_id}


@app.get("/api/categories")
async def get_categories():
    """获取所有事件分类"""
    categories = db.get_all_categories()
    return {"categories": categories}


@app.get("/api/memories")
async def get_memories():
    """获取所有记忆"""
    return db.get_all_memories()


@app.post("/api/memories")
async def create_memory(body: dict):
    """手动新建一条记忆（Quick note 模态框入口）。"""
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    memory_id = db.add_memory(content, source="user")
    return {"id": memory_id}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: int):
    """删除一条记忆"""
    db.delete_memory(memory_id)
    return {"status": "ok"}


@app.get("/api/reminders")
async def get_reminders(status: str = None, done: int = None):
    """获取提醒列表，支持基于 status 或者旧的 done 过滤"""
    conn = db._get_conn()
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status=? ORDER BY trigger_time DESC LIMIT 100",
            (status,)
        ).fetchall()
    elif done is not None:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE done=? ORDER BY trigger_time DESC LIMIT 100",
            (done,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reminders ORDER BY trigger_time DESC LIMIT 100"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/todos")
async def get_todos(all: bool = False):
    """获取待办列表"""
    todos = db.get_todos(include_done=all)
    return {"todos": todos, "count": len(todos)}


@app.post("/api/todos")
async def create_todo(body: dict):
    """手动新建一条待办（New todo 模态框入口）。"""
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    todo_id = db.add_todo(content)
    return {"id": todo_id}


@app.patch("/api/todos/{todo_id}/done")
async def set_todo_done(todo_id: int, body: dict):
    """更新待办完成状态"""
    done = body.get("done", True)
    db.set_todo_done(todo_id, bool(done))
    return {"status": "ok"}


@app.get("/api/deadlines")
async def get_deadlines():
    """获取所有 active 的 deadline（含倒计时）"""
    from bot.prompts import format_countdown
    db.expire_past_deadlines()
    deadlines = db.get_active_deadlines()
    result = []
    for d in deadlines:
        result.append({
            **d,
            "countdown": format_countdown(d["due_time"]),
        })
    return {"deadlines": result, "count": len(result)}


@app.get("/api/weather")
async def get_weather():
    """今日天气结构化数据。无配置或失败时返回 {available: false}。"""
    from bot.weather import get_weather_struct
    data = await get_weather_struct()
    if data is None:
        return {"available": False}
    return {"available": True, **data}


@app.get("/api/projects/heatmap")
async def get_projects_heatmap(days: int = Query(90, description="统计天数，默认90天")):
    """
    Project Overview 热力图数据。
    返回每个项目在最近 N 天里每天的 Focus 时长（分钟）。
    含已归档项目（projects 字段全量），同时返回 archived 列表，
    前端按需隐藏/淡化——避免归档动作往返之间的列表错位。
    """
    from datetime import datetime, timedelta

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days - 1)
    start_str = start_dt.strftime("%Y-%m-%dT00:00:00")
    end_str = end_dt.strftime("%Y-%m-%dT23:59:59")

    events = db.get_events(start_str, end_str)

    # 构建日期列表（YYYY-MM-DD，最近 days 天）
    dates = [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    # 按 project_name 和日期聚合 Focus 时长
    from collections import defaultdict
    project_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for ev in events:
        if ev.get("category") != "Focus" or not ev.get("project_name"):
            continue
        # 只统计真实事件——planned / cancelled 是 dummy，不算已投入时间
        if ev.get("status") is not None:
            continue
        proj = ev["project_name"]
        day = ev["start_time"][:10]  # YYYY-MM-DD
        start = datetime.fromisoformat(ev["start_time"])
        if ev.get("end_time"):
            end_t = datetime.fromisoformat(ev["end_time"])
        else:
            # 使用与 start 时区一致的 now，避免 naive/aware 相减报错
            tz = start.tzinfo
            end_t = datetime.now(tz=tz) if tz else datetime.now()
        minutes = max(0, (end_t - start).total_seconds() / 60)
        project_day[proj][day] += minutes

    # 按总时长排序项目（降序）
    projects = sorted(project_day.keys(), key=lambda p: sum(project_day[p].values()), reverse=True)

    data = {
        proj: {date: round(project_day[proj].get(date, 0)) for date in dates}
        for proj in projects
    }

    archived = db.get_archived_project_names()
    return {"projects": projects, "dates": dates, "data": data, "archived": archived}


@app.post("/api/projects/archive")
async def archive_project(body: dict):
    """归档一个项目（按名）。幂等——已归档再调返回 changed=False。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    changed = db.archive_project(name)
    return {"ok": True, "name": name, "changed": changed}


@app.post("/api/projects/unarchive")
async def unarchive_project(body: dict):
    """取消归档。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    changed = db.unarchive_project(name)
    return {"ok": True, "name": name, "changed": changed}


# ── AI Trace 观测 ───────────────────────────────────────────────


@app.get("/api/traces/dates")
async def list_trace_dates():
    """所有有 trace 的日期，最新在前。"""
    return {"dates": ai_trace.list_dates()}


@app.get("/api/traces")
async def list_traces(
    date: str = Query(..., description="日期 YYYY-MM-DD"),
    trigger: str | None = Query(None, description="按触发源过滤：chat/poll/reminder/bedtime/morning/oneshot"),
    limit: int = Query(100, ge=1, le=500),
):
    """读取某天的 trace 列表，最新在前。"""
    entries = ai_trace.read_day(date, trigger=trigger, limit=limit)
    return {"traces": entries, "count": len(entries)}


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}


@app.get("/api/version")
async def get_version():
    """返回当前镜像版本（由 Dockerfile 的 APP_VERSION build-arg 注入）"""
    return {"version": os.environ.get("APP_VERSION", "dev")}


# ── Admin: AI Preset 管理 ───────────────────────────────────────────────
# 给前端 admin 页面用，不走 Discord 斜杠命令通道。复用 config.set_active/
# set_fallback；测试端点直接对指定 preset 发 hello，绕过 system prompt 和工具。
def _mask_api_key(key: str) -> str:
    """API key 不全文上 UI；保留尾 4 位帮人辨认是哪条 key。"""
    if not key:
        return ""
    if len(key) <= 4:
        return "•" * len(key)
    return "••••" + key[-4:]


def _preset_view(name: str, p) -> dict:
    return {
        "name": name,
        "provider": p.provider,
        "model": p.model,
        "base_url": p.base_url or "",
        "note": getattr(p, "note", "") or "",
        "api_key_masked": _mask_api_key(p.api_key),
    }


@app.get("/api/admin/presets")
async def admin_list_presets():
    """列出所有 preset，标记当前 active / fallback。"""
    import config
    try:
        active_name = config.get_active().name
    except Exception:
        active_name = None
    fb = config.get_fallback()
    presets = [_preset_view(name, p) for name, p in config.PRESETS.items()]
    return {
        "presets": presets,
        "active": active_name,
        "fallback": fb.name if fb else None,
    }


@app.post("/api/admin/presets/active")
async def admin_set_active(body: dict):
    """切换主 preset。"""
    import config
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        config.set_active(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "active": name}


@app.post("/api/admin/presets/fallback")
async def admin_set_fallback(body: dict):
    """切换 fallback preset；name=null 关闭 fallback。"""
    import config
    name = body.get("name")
    if isinstance(name, str):
        name = name.strip() or None
    try:
        config.set_fallback(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "fallback": name}


@app.post("/api/admin/presets")
async def admin_create_preset(body: dict):
    """新增 preset。重名 → 409，校验失败 → 400。"""
    import config
    name = (body.get("name") or "").strip()
    try:
        config.add_preset(
            name=name,
            provider=body.get("provider") or "",
            api_key=body.get("api_key") or "",
            base_url=body.get("base_url") or "",
            model=body.get("model") or "",
            note=body.get("note") or "",
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "name": name}


@app.patch("/api/admin/presets/{name}")
async def admin_update_preset(name: str, body: dict):
    """更新 preset 字段。允许：provider / api_key / base_url / model / note。
    api_key 传空字符串或不传 = 不动；不支持改名。"""
    import config
    fields: dict = {}
    for k in ("provider", "base_url", "model", "note"):
        if k in body and body[k] is not None:
            fields[k] = body[k]
    # api_key 留空 = 不变（避免误清空）
    new_key = body.get("api_key")
    if isinstance(new_key, str) and new_key.strip():
        fields["api_key"] = new_key
    try:
        config.update_preset(name, **fields)
    except ValueError as e:
        msg = str(e)
        if "unknown preset" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "name": name}


@app.delete("/api/admin/presets/{name}")
async def admin_delete_preset(name: str):
    """删 preset。当前 active 拒删（400）；是 fallback 的会自动 clear。"""
    import config
    try:
        config.delete_preset(name)
    except ValueError as e:
        msg = str(e)
        if "unknown preset" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "deleted": name}


@app.post("/api/admin/presets/test")
async def admin_test_preset(body: dict):
    """对指定 preset 发一条 'hello'，绕过 system prompt + 工具，纯连接测试。"""
    import time
    import config
    from bot.ai_provider_error import AIProviderError
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    p = config.PRESETS.get(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"unknown preset: {name}")

    provider = p.provider.lower().strip()
    if provider == "gemini":
        from bot import ai_engine_gemini as engine
    elif provider == "relay":
        from bot import ai_engine_relay as engine
    elif provider == "openai":
        from bot import ai_engine_openai as engine
    else:
        from bot import ai_engine_claude as engine

    t0 = time.monotonic()
    try:
        reply = await engine.simple_completion("hello", p)
        latency = round((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "reply": reply,
            "latency_ms": latency,
            "provider": p.provider,
            "model": p.model,
        }
    except AIProviderError as e:
        return {
            "ok": False,
            "error": str(e),
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "provider": p.provider,
            "model": p.model,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "provider": p.provider,
            "model": p.model,
        }


@app.get("/")
async def root():
    return RedirectResponse(url="/app/index.html")


# 挂载前端静态文件（放在路由定义之后，避免拦截 /api 路由）
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
