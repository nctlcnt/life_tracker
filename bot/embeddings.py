"""
对话日志 embedding + 相似度 helper（memory v3 Part B2）。

设计原则（对应 LT-136）：
- 原始消息先无条件写入 conversation_messages；只有 compact 已折叠的区间
  才由后台 worker 调用 embed_and_store() 补 embedding。当前明文尾巴不入索引。
- 不 embed 孤立的一句话：embedding 前拼接同 channel 最近几条消息作为上下文
  （"弄好了"三个字单独 embed 捕捉不到主题），拼接文本同时存进
  embedding_context 列，便于调试和检索命中后原样展示给 AI。
- 所有失败路径返回 None / False，永远不抛异常——embedding 是增强功能，
  失败只意味着那条消息检索不到，不能影响正常聊天和入库。

Provider 由 config.json 的 ai.embedding 决定（OpenAI 兼容 /embeddings 端点，
当前默认智谱 embedding-3）；换 OpenAI 官方或本地 Ollama 只改 config，不改这里。
"""
import math
from datetime import datetime, timezone

from openai import AsyncOpenAI

import config
from bot.logger import get_logger

logger = get_logger(__name__)

# 拼接上下文的消息条数（当前消息之前的最近 N 条，约 1-2 轮对话）
CONTEXT_MESSAGES = 4

# 输入截断保护（字符数）。embedding 模型输入有限，超长消息取前段即可，
# 检索场景不需要完整全文。
MAX_EMBED_CHARS = 2000

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.EMBEDDING_API_KEY,
            base_url=config.EMBEDDING_BASE_URL or None,
            timeout=10.0,
            max_retries=1,
        )
    return _client


async def embed_text(text: str) -> list[float] | None:
    """调 embedding API 返回向量。禁用/空文本/任何失败都返回 None，不抛异常。"""
    if not config.EMBEDDING_ENABLED:
        return None
    if not text or not text.strip():
        return None
    try:
        kwargs: dict = {
            "model": config.EMBEDDING_MODEL,
            "input": text[:MAX_EMBED_CHARS],
        }
        if config.EMBEDDING_DIMENSIONS:
            kwargs["dimensions"] = config.EMBEDDING_DIMENSIONS
        resp = await _get_client().embeddings.create(**kwargs)
        return list(resp.data[0].embedding)
    except Exception as e:
        logger.warning(f"⚠️ embedding 调用失败（该条消息将检索不到）: {type(e).__name__}: {e}")
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度。维度不匹配/零向量返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def recency_weight(created_at: str, now: datetime | None = None) -> float:
    """
    时间衰减项，0.995^距今小时数（Park et al. Generative Agents 的系数）：
    1 天前 ≈ 0.89，1 周前 ≈ 0.43，1 月前 ≈ 0.03。
    解析失败按"很久以前"处理返回 0.0。
    """
    try:
        ts = datetime.fromisoformat(created_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        hours = max((now - ts).total_seconds() / 3600.0, 0.0)
        return 0.995 ** hours
    except (ValueError, TypeError):
        return 0.0


def format_context(rows: list[dict]) -> str:
    """
    把若干条 conversation_messages 行拼成 embedding 输入文本。
    格式和检索命中后展示给 AI 的片段一致：[MM-DD HH:MM] 用户/助手: 内容
    """
    lines = []
    for row in rows:
        role = "用户" if row.get("role") == "user" else "助手"
        content = (row.get("content") or "").strip()
        if not content:
            continue
        try:
            ts = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, TypeError, KeyError):
            ts = str(row.get("created_at", ""))[:16]
        lines.append(f"[{ts}] {role}: {content}")
    return "\n".join(lines)


async def embed_and_store(db, row_id: int, channel_id: str) -> bool:
    """
    给 conversation_messages 的一行补 embedding（后台任务入口）。

    取该行及其前 CONTEXT_MESSAGES 条消息拼上下文 → embed → 写回
    embedding / embedding_context / embedding_model 三列。
    返回是否成功；任何失败只记 warning，不抛异常。
    """
    if not config.EMBEDDING_ENABLED:
        return False
    try:
        rows = db.get_conversation_messages_upto(
            channel_id, row_id, limit=CONTEXT_MESSAGES + 1
        )
        if not rows or rows[-1]["id"] != row_id:
            return False
        context = format_context(rows)
        if not context:
            return False
        embedding = await embed_text(context)
        if embedding is None:
            return False
        db.update_conversation_embedding(
            row_id, embedding, context, config.EMBEDDING_MODEL
        )
        return True
    except Exception as e:
        logger.warning(f"⚠️ embed_and_store 失败 (row_id={row_id}): {type(e).__name__}: {e}")
        return False
