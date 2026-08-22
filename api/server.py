"""
FastAPI 接口模块
给前端提供数据
"""
import os
import re
import sqlite3
from html import escape
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from api.auth import (
    API_KEY_ENV,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    ApiAuthMiddleware,
    cookie_is_secure,
    get_api_key,
    request_is_authenticated,
    secure_compare,
    session_token,
)
from bot.database import Database
from bot.memory import MemoryService
from bot.merge import merge_events
from bot import trace as ai_trace

app = FastAPI(title="Life Tracker API")
app.add_middleware(ApiAuthMiddleware)

# 数据库实例会在 main.py 启动时注入
db: Database | None = None
memory: MemoryService | None = None
_check_in_changed_callback = None
_check_in_trigger_callback = None


def set_database(database: Database, memory_service: MemoryService | None = None):
    global db, memory
    db = database
    memory = memory_service or MemoryService(database)


def set_check_in_changed_callback(callback):
    global _check_in_changed_callback
    _check_in_changed_callback = callback


def set_check_in_trigger(callback):
    """注入 Scheduler.trigger_check_in_now，供 Admin 页面手动触发 check-in。"""
    global _check_in_trigger_callback
    _check_in_trigger_callback = callback


# 改动了这些字段就意味着调度参数变了，已排好的触发时刻必须作废重排；
# 改 prompt / instructions / tool_profile 之类的内容则不需要。
# 注意 Admin 页面的编辑弹窗提交的是整份表单，一定会带上这里的字段，
# 所以从界面保存时这个判断恒为真；区分只对直接调 API 的调用方有意义。
_CHECK_IN_SCHEDULE_FIELDS = frozenset({
    "enabled", "schedule_type", "time_start", "time_end", "days_of_week",
    "interval_min_minutes", "interval_max_minutes",
})


def _notify_check_in_changed(reschedule: bool = False):
    if _check_in_changed_callback:
        _check_in_changed_callback(reschedule)


class LoginRequest(BaseModel):
    api_key: str


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest):
    """Exchange the configured API key for an HttpOnly dashboard session."""
    api_key = get_api_key()
    if api_key is None:
        raise HTTPException(
            status_code=503,
            detail=f"API authentication is not configured; set {API_KEY_ENV}",
        )
    candidate = body.api_key
    if not secure_compare(candidate, api_key):
        raise HTTPException(status_code=401, detail="invalid API key")

    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        SESSION_COOKIE,
        session_token(api_key),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=cookie_is_secure(),
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/auth/session")
async def auth_session(request: Request):
    api_key = get_api_key()
    authenticated = bool(api_key) and request_is_authenticated(request, api_key)
    return {"authenticated": authenticated, "configured": api_key is not None}


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=cookie_is_secure(),
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/calendar/oauth/callback", response_class=HTMLResponse)
async def google_calendar_oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
):
    """Receive Google Calendar Web OAuth callbacks."""
    if error:
        return HTMLResponse(
            f"<h1>Google Calendar 授权失败</h1><p>{escape(error)}</p>",
            status_code=400,
        )
    try:
        from bot.google_calendar import finish_web_oauth_flow, refresh_calendar_context
        token_file = finish_web_oauth_flow(state, code)
    except Exception as e:
        return HTMLResponse(
            f"<h1>Google Calendar 授权失败</h1>"
            f"<p>{escape(type(e).__name__)}: {escape(str(e))}</p>",
            status_code=400,
        )

    refresh_line = ""
    try:
        refresh = refresh_calendar_context()
        refresh_line = f"<p>Calendar 缓存已刷新：{refresh.get('count', 0)} events。</p>"
    except Exception as e:
        refresh_line = (
            "<p>Token 已写入，但缓存刷新失败："
            f"{escape(type(e).__name__)}: {escape(str(e))}</p>"
        )

    return HTMLResponse(
        "<h1>Google Calendar 已授权</h1>"
        f"<p>Token 已写入：<code>{token_file}</code></p>"
        f"{refresh_line}"
        "<p>可以关闭这个页面。</p>"
    )


@app.get("/api/timeline")
async def get_timeline(
    start: str = Query(..., description="起始时间 ISO 8601，如 2026-04-01T00:00:00"),
    end: str = Query(..., description="结束时间 ISO 8601，如 2026-04-07T23:59:59"),
):
    """查询时间范围内的真实事件，返回合并后的时间段。"""
    raw_events = db.get_events(start, end)
    segments = merge_events(raw_events)
    return {
        "segments": segments,
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
    project_name = (body.get("project_name") or "").strip() or None
    if category == "Focus":
        if not project_name:
            raise HTTPException(status_code=400, detail="project_name required for Focus events")
        if not db.project_exists(project_name):
            raise HTTPException(status_code=400, detail=f"unknown project: {project_name}")
    else:
        project_name = None
    start_time = body.get("start_time") or datetime.now().isoformat(timespec="seconds")
    end_time = body.get("end_time") or None
    event_id = db.add_event(
        start_time=start_time,
        end_time=end_time,
        content=content,
        category=category,
        notes=body.get("notes"),
        project_name=project_name,
    )
    return {"id": event_id}


@app.get("/api/categories")
async def get_categories():
    """获取所有事件分类"""
    categories = db.get_all_categories()
    return {"categories": categories}


@app.get("/api/memories")
async def get_memories():
    """获取所有记忆，包括已过期的——Memory tab 手动整理需要看到全部。"""
    return memory.list_durable(include_expired=True)


@app.get("/api/memory-document")
async def get_memory_document():
    """Return the canonical Markdown document and prompt-budget stats."""
    repository = memory.durable_repository
    if not hasattr(repository, "read_document"):
        raise HTTPException(status_code=501, detail="Markdown memory is not enabled")
    return {"content": repository.read_document(), **memory.durable_stats()}


@app.put("/api/memory-document")
async def replace_memory_document(body: dict):
    """Atomically replace the canonical Markdown document."""
    repository = memory.durable_repository
    if not hasattr(repository, "replace_document"):
        raise HTTPException(status_code=501, detail="Markdown memory is not enabled")
    try:
        repository.replace_document(body.get("content") or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **memory.durable_stats()}


@app.post("/api/memories")
async def create_memory(body: dict):
    """手动新建一条记忆（Quick note 模态框入口）。"""
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    memory_id = memory.save_durable(
        content,
        source="user",
        memory_type=(body.get("memory_type") or None),
        valid_until=(body.get("valid_until") or None),
    )
    return {"id": memory_id}


@app.patch("/api/memories/{memory_id}")
async def update_memory(memory_id: int, body: dict):
    """手动编辑一条记忆的 content/memory_type/valid_until。

    只更新 body 里实际出现的字段；某个字段传 null（或前端传空字符串）表示清空，
    比如把 valid_until 清空 = 改回永久有效。
    """
    fields = {}
    if "content" in body:
        content = (body.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        fields["content"] = content
    if "memory_type" in body:
        fields["memory_type"] = body.get("memory_type") or None
    if "valid_until" in body:
        fields["valid_until"] = body.get("valid_until") or None
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    memory.update_durable(memory_id, **fields)
    return {"status": "ok"}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: int):
    """删除一条记忆"""
    memory.delete_durable(memory_id)
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


# ── Configurable check-ins ─────────────────────────────────────────────

_CHECK_IN_TEMPLATE_TOKENS = {"timestamp", "name", "label", "instructions"}
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


def _validate_hhmm(value: str | None, field: str) -> None:
    if value is None:
        return
    if not _HHMM_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{field} must be HH:MM")
    hour, minute = value.split(":", 1)
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise HTTPException(status_code=400, detail=f"{field} must be a valid HH:MM")


def _validate_check_in_template(value: str) -> None:
    import string
    try:
        used = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(value)
            if field_name
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid format braces: {e}")
    unknown = used - _CHECK_IN_TEMPLATE_TOKENS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown placeholder(s): {', '.join(sorted(unknown))}",
        )


def _check_in_fields_from_body(body: dict, *, partial: bool = False) -> dict:
    fields = {}
    allowed = {
        "name", "label", "enabled", "schedule_type", "time_start", "time_end",
        "days_of_week", "interval_min_minutes", "interval_max_minutes",
        "prompt_template", "instructions", "context_config",
        "tool_profile", "allow_silent",
    }
    for key in allowed:
        if key in body:
            fields[key] = body[key]
    if not partial:
        for required in ("name", "schedule_type", "prompt_template"):
            if not (fields.get(required) or "").strip():
                raise HTTPException(status_code=400, detail=f"{required} required")

    if "schedule_type" in fields and fields["schedule_type"] not in Database.CHECK_IN_SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid schedule_type: {fields['schedule_type']}")
    if "tool_profile" in fields and fields["tool_profile"] not in Database.CHECK_IN_TOOL_PROFILES:
        raise HTTPException(status_code=400, detail=f"invalid tool_profile: {fields['tool_profile']}")
    for key in ("time_start", "time_end"):
        _validate_hhmm(fields.get(key), key)
    if "days_of_week" in fields and fields["days_of_week"] is not None:
        days = fields["days_of_week"]
        if not isinstance(days, list) or any(day not in range(7) for day in days):
            raise HTTPException(status_code=400, detail="days_of_week must be a list of integers 0-6")
    for key in ("interval_min_minutes", "interval_max_minutes"):
        if key in fields and fields[key] is not None:
            try:
                fields[key] = int(fields[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be an integer")
            if fields[key] < 1:
                raise HTTPException(status_code=400, detail=f"{key} must be positive")
    if "prompt_template" in fields:
        fields["prompt_template"] = (fields.get("prompt_template") or "").strip()
        if not fields["prompt_template"]:
            raise HTTPException(status_code=400, detail="prompt_template required")
        _validate_check_in_template(fields["prompt_template"])
    return fields


@app.get("/api/check-ins")
async def list_check_ins():
    return {"check_ins": db.list_check_ins()}


@app.post("/api/check-ins")
async def create_check_in(body: dict):
    fields = _check_in_fields_from_body(body)
    try:
        check_in_id = db.create_check_in(**fields)
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _notify_check_in_changed(reschedule=True)
    return {"ok": True, "id": check_in_id}


@app.patch("/api/check-ins/{check_in_id}")
async def update_check_in(check_in_id: str, body: dict):
    fields = _check_in_fields_from_body(body, partial=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        changed = db.update_check_in(check_in_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not changed:
        raise HTTPException(status_code=404, detail=f"check-in not found: {check_in_id}")
    _notify_check_in_changed(
        reschedule=bool(_CHECK_IN_SCHEDULE_FIELDS & fields.keys())
    )
    return {"ok": True, "changed": changed}


@app.delete("/api/check-ins/{check_in_id}")
async def delete_check_in(check_in_id: str):
    changed = db.delete_check_in(check_in_id)
    if not changed:
        raise HTTPException(
            status_code=404,
            detail=f"custom check-in not found or built-in cannot be deleted: {check_in_id}",
        )
    _notify_check_in_changed(reschedule=True)
    return {"ok": True, "deleted": check_in_id}


@app.post("/api/check-ins/{check_in_id}/test")
async def test_check_in(check_in_id: str):
    """立刻触发一次 check-in，走真实链路（会真的发 Discord 消息、真的执行工具）。

    与定时触发的唯一区别：不写 last_fired_at，因此不会抑制当天真正的定时触发。
    """
    if _check_in_trigger_callback is None:
        raise HTTPException(
            status_code=503,
            detail="调度器未运行（--api-only 模式下没有 Scheduler），无法触发 check-in",
        )
    result = await _check_in_trigger_callback(check_in_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"check-in not found: {check_in_id}")
    return {
        "ok": bool(result.get("ok")),
        "reply": result.get("reply"),
        "error": result.get("error"),
        "latency_ms": result.get("latency_ms"),
        "name": result.get("name"),
        "label": result.get("label"),
    }


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
    返回每个项目在最近 N 天里每天的 Focus check-in 次数。
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

    # 按 project_name 和日期聚合 Focus check-in 次数。
    # GitHub contributions 的核心语义是"当天发生了几次贡献"；
    # 这里每条 Focus event 视为一次项目 check-in。
    # 图片附件（LT-78）只是 event 的产出证据，不单独增加热度；
    # 如果未来图片上传流程创建了新的 Focus event，那条 event 会自然计入一次。
    from collections import defaultdict
    project_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for ev in events:
        if ev.get("category") != "Focus" or not ev.get("project_name"):
            continue
        proj = ev["project_name"]
        day = ev["start_time"][:10]  # YYYY-MM-DD
        project_day[proj][day] += 1

    project_rows = db.get_all_project_names(include_archived=True)
    managed_projects = [p["project_name"] for p in project_rows]

    # 按总 check-in 次数排序项目（降序），无历史数据的手动项目也展示。
    projects = sorted(
        managed_projects,
        key=lambda p: sum(project_day[p].values()),
        reverse=True,
    )

    data = {
        proj: {date: round(project_day[proj].get(date, 0)) for date in dates}
        for proj in projects
    }

    archived = db.get_archived_project_names()
    return {
        "projects": projects,
        "dates": dates,
        "data": data,
        "archived": archived,
        "metric": "check_ins",
    }


@app.get("/api/projects")
async def list_projects():
    """手动管理的项目清单。"""
    projects = db.get_all_project_names(include_archived=True)
    archived = set(db.get_archived_project_names())
    return {
        "projects": [
            {**p, "archived": p["project_name"] in archived}
            for p in projects
        ]
    }


@app.post("/api/projects")
async def create_project(body: dict):
    """创建一个项目。AI 只会读取这里建立过的项目。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    changed = db.add_project(name)
    return {"ok": True, "name": name, "changed": changed}


@app.patch("/api/projects/{old_name}")
async def rename_project(old_name: str, body: dict):
    """重命名项目，并同步历史事件。"""
    new_name = (body.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="name required")
    if db.project_exists(new_name):
        raise HTTPException(status_code=409, detail=f"project already exists: {new_name}")
    changed = db.rename_project(old_name, new_name)
    if not changed:
        raise HTTPException(status_code=404, detail=f"project not found: {old_name}")
    return {"ok": True, "old_name": old_name, "name": new_name}


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    """删除项目清单项，不删除历史事件。"""
    changed = db.delete_project(name)
    if not changed:
        raise HTTPException(status_code=404, detail=f"project not found: {name}")
    return {"ok": True, "name": name}


@app.post("/api/projects/archive")
async def archive_project(body: dict):
    """归档一个项目（按名）。幂等——已归档再调返回 changed=False。"""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not db.project_exists(name):
        raise HTTPException(status_code=404, detail=f"project not found: {name}")
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


@app.get("/api/traces/tools")
async def list_recent_trace_tool_calls(
    limit: int = Query(50, ge=1, le=500),
    date: str | None = Query(None, description="可选日期 YYYY-MM-DD；不传则从最新 trace 文件往回扫"),
    trigger: str | None = Query(None, description="按触发源过滤：chat/poll/reminder/bedtime/morning/oneshot"),
    name: str | None = Query(None, description="按工具名过滤，例如 set_reminder"),
):
    """最近 AI 工具调用日志，含工具名和参数。"""
    calls = ai_trace.list_recent_tool_calls(
        limit=limit,
        date=date,
        trigger=trigger,
        name=name,
    )
    return {"tool_calls": calls, "count": len(calls)}


@app.get("/api/health")
async def health_check():
    """Authenticated health check for API clients."""
    return {"status": "ok"}


@app.get("/internal/health", include_in_schema=False)
async def internal_health_check():
    """Minimal unauthenticated endpoint for the local container probe."""
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
        "use_v1_suffix": getattr(p, "use_v1_suffix", True),
        "api_key_masked": _mask_api_key(p.api_key),
    }


_PROMPT_REQUIRED_PLACEHOLDERS = {
    "proactive_gemini": {"timestamp"},
    "proactive_claude": {"timestamp"},
    "reminder": {"timestamp", "action"},
    "bedtime": {"timestamp"},
    "morning": {"timestamp"},
    "weather_report": {"weather_data"},
}

# main_template 疑似占位符 token：只把"小写字母+下划线"的花括号当拼写错误拦，
# 其他字面 {}（颜文字、JSON 示例、大写/中文/带空格）一律放行——渲染器本来
# 就只替换白名单 token
_MAIN_TEMPLATE_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")


def _validate_prompt_template(key: str, value: str):
    import string
    if key == "main_template":
        from bot.prompts import MAIN_TEMPLATE_PLACEHOLDERS
        unknown = set(_MAIN_TEMPLATE_TOKEN_RE.findall(value)) - MAIN_TEMPLATE_PLACEHOLDERS
        if unknown:
            raise HTTPException(
                status_code=400,
                detail="unknown placeholder(s): "
                       f"{', '.join(sorted(unknown))}（可用: "
                       f"{', '.join(sorted(MAIN_TEMPLATE_PLACEHOLDERS))}）",
            )
        return  # 没有必需占位符：删掉某个占位符 = 主动选择不注入该知识
    required = _PROMPT_REQUIRED_PLACEHOLDERS.get(key, set())
    try:
        used = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(value)
            if field_name
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid format braces: {e}")
    unknown = used - required
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown placeholder(s): {', '.join(sorted(unknown))}",
        )
    missing = required - used
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing required placeholder(s): {', '.join(sorted(missing))}",
        )


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
            use_v1_suffix=body.get("use_v1_suffix", True),
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "name": name}


@app.patch("/api/admin/presets/{name}")
async def admin_update_preset(name: str, body: dict):
    """更新 preset 字段。允许：provider / api_key / base_url / model / note / use_v1_suffix。
    api_key 传空字符串或不传 = 不动；不支持改名。"""
    import config
    fields: dict = {}
    for k in ("provider", "base_url", "model", "note", "use_v1_suffix"):
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


@app.get("/api/admin/compact-preset")
async def admin_get_compact_preset():
    """当前 compact 摘要用的 preset。configured=null 表示未设置（回落 active）。"""
    import config
    from bot.memory.compact import get_compact_preset, get_compact_preset_name
    effective = get_compact_preset(db)
    return {
        "configured": get_compact_preset_name(db),
        "effective": effective.name,
        "active": config.get_active().name,
    }


@app.put("/api/admin/compact-preset")
async def admin_set_compact_preset(body: dict):
    """设置 compact preset。name 为空/null = 清空，回落 active。"""
    from bot.memory.compact import get_compact_preset, set_compact_preset
    name = (body.get("name") or "").strip() or None
    try:
        set_compact_preset(db, name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "configured": name,
            "effective": get_compact_preset(db).name}


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

    from bot import ai_engine_openai_compat as engine

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


# ── Admin: Prompt 管理 ───────────────────────────────────────────────


@app.get("/api/admin/prompts")
async def admin_list_prompts():
    """列出可编辑 prompt sections。正文来自 DB，不提交到 Git。"""
    from bot.prompts import LEGACY_STRUCTURED_KEYS, PROMPT_SECTION_LABELS
    rows = {row["key"]: row for row in db.list_prompt_sections()}
    sections = []
    for key, label in PROMPT_SECTION_LABELS.items():
        row = rows.get(key)
        value = row["value"] if row else ""
        sections.append({
            "key": key,
            "label": row["label"] if row else label,
            "value": value,
            "current_value": value,
            "updated_at": row["updated_at"] if row else None,
            "empty": not bool(value.strip()),
            # 已内联进 main_template 的旧散文 section：UI 隐藏，API 保留读写
            # 作为回滚/急救通道
            "hidden": key in LEGACY_STRUCTURED_KEYS,
        })
    return {"sections": sections}


@app.put("/api/admin/prompts/{key}")
async def admin_save_prompt(key: str, body: dict):
    """保存单个 prompt section。"""
    from bot.prompts import PROMPT_SECTION_LABELS
    if key not in PROMPT_SECTION_LABELS:
        raise HTTPException(status_code=404, detail=f"unknown prompt section: {key}")
    value = (body.get("value") or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="value required")
    _validate_prompt_template(key, value)
    changed = db.set_prompt_section(key, value)
    return {"ok": True, "key": key, "changed": changed}


@app.get("/")
async def root():
    return RedirectResponse(url="/app/index.html")


# 挂载前端静态文件（放在路由定义之后，避免拦截 /api 路由）
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
