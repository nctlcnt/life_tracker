"""
FastAPI 接口模块
给前端提供数据
"""
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
import config
from bot.database import Database
from bot.merge import merge_events

app = FastAPI(title="Life Tracker API")

# 允许前端跨域请求（开发时 React 在 localhost:3000）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该改为你的前端域名
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
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


@app.get("/api/categories")
async def get_categories():
    """获取所有事件分类"""
    categories = db.get_all_categories()
    return {"categories": categories}


@app.get("/api/memories")
async def get_memories():
    """获取所有记忆"""
    return db.get_all_memories()


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


@app.get("/api/projects/heatmap")
async def get_projects_heatmap(days: int = Query(90, description="统计天数，默认90天")):
    """
    Project Overview 热力图数据。
    返回每个项目在最近 N 天里每天的 Focus 时长（分钟）。
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

    return {"projects": projects, "dates": dates, "data": data}


# ── AI Preset 管理 ──────────────────────────────────────────────────
# 仅在受信任网络（VPN）下暴露；返回包含 api_key 全文，便于编辑回填。

def _preset_to_dict(p: config.Preset) -> dict:
    return {
        "name": p.name,
        "provider": p.provider,
        "api_key": p.api_key,
        "base_url": p.base_url,
        "model": p.model,
        "notes": p.notes,
    }


@app.get("/api/presets")
async def list_api_presets():
    """列出所有 preset 及当前主/备 preset 名称。"""
    return {
        "presets": [_preset_to_dict(p) for p in config.PRESETS.values()],
        "active": config.get_active_name(),
        "fallback": config.get_fallback_name(),
        "supported_providers": list(config.SUPPORTED_PROVIDERS),
    }


@app.post("/api/presets")
async def create_api_preset(body: dict):
    """创建一条 preset；body: { name, provider, api_key, base_url, model, notes }"""
    try:
        p = config.add_preset(
            name=body.get("name", ""),
            provider=body.get("provider", ""),
            api_key=body.get("api_key", ""),
            base_url=body.get("base_url", ""),
            model=body.get("model", ""),
            notes=body.get("notes", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _preset_to_dict(p)


@app.put("/api/presets/{name}")
async def update_api_preset(name: str, body: dict):
    """更新 preset 字段；不允许重命名（要改名请删后再加）。"""
    try:
        p = config.update_preset(
            name,
            provider=body.get("provider"),
            api_key=body.get("api_key"),
            base_url=body.get("base_url"),
            model=body.get("model"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _preset_to_dict(p)


@app.delete("/api/presets/{name}")
async def delete_api_preset(name: str):
    try:
        config.delete_preset(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "name": name}


@app.post("/api/presets/active")
async def set_active_preset(body: dict):
    """切换主 preset；body: { name }"""
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="缺少 name")
    try:
        config.set_active(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "active": config.get_active_name()}


@app.post("/api/presets/fallback")
async def set_fallback_preset(body: dict):
    """切换 fallback preset；body: { name } 或 { name: null } 关闭。"""
    name = body.get("name")  # 可为 None 表示关闭
    try:
        config.set_fallback(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "fallback": config.get_fallback_name()}


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse(url="/app/index.html")


# 挂载前端静态文件（放在路由定义之后，避免拦截 /api 路由）
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.isdir(_frontend_dir):
    app.mount("/app", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
