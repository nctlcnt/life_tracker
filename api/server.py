"""
FastAPI 接口模块
给 React 前端提供数据
"""
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from bot.database import Database
from bot.merge import merge_events

app = FastAPI(title="Life Tracker API")

# 允许前端跨域请求（开发时 React 在 localhost:3000）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该改为你的前端域名
    allow_methods=["GET"],
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
    return {"segments": segments, "count": len(segments)}


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


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}
