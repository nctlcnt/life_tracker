"""
FastAPI 接口模块
给前端提供数据
"""
import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from bot.database import Database
from bot.merge import merge_events

app = FastAPI(title="Life Tracker API")

# 允许前端跨域请求（开发时 React 在 localhost:3000）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该改为你的前端域名
    allow_methods=["GET", "DELETE"],
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
    """查询时间范围内的合并后时间段（前端主要用这个）"""
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
