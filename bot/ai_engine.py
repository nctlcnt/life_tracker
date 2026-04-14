"""
AI 引擎路由模块
根据 config.AI_PROVIDER 自动选择后端引擎：
- claude: Anthropic 原生 API
- relay:  OpenAI 兼容中转站（用 AI_BASE_URL）
- gemini: Google Gemini 原生 API

支持 fallback 机制：主引擎 API 调用失败（AIProviderError）时自动切换到
config.AI_FALLBACK_PROVIDER 配置的备用引擎。支持跨 provider（如 Claude → Gemini）。

外部只需 from bot.ai_engine import chat, scheduled_action, simple_completion
"""
import types
from contextlib import contextmanager
import config
from bot.ai_provider_error import AIProviderError
from bot.logger import get_logger

logger = get_logger(__name__)


def _load_engine(provider: str) -> types.ModuleType:
    """根据 provider 名称加载对应的引擎模块。"""
    p = provider.lower().strip()
    if p == "gemini":
        import bot.ai_engine_gemini as m
    elif p == "relay":
        import bot.ai_engine_relay as m
    else:
        # 默认 claude（含 antigravity 场景）
        import bot.ai_engine_claude as m
    return m


@contextmanager
def _override_config(**overrides):
    """
    临时覆盖 config 中的配置项，退出 context 后恢复原值。
    用于 fallback 调用时注入不同的 API key / model name。
    """
    originals = {k: getattr(config, k) for k in overrides if hasattr(config, k)}
    for k, v in overrides.items():
        if v:  # 只有非空值才覆盖（空字符串表示"继承主引擎配置"）
            setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in originals.items():
            setattr(config, k, v)


def _fallback_overrides() -> dict:
    """
    构建 fallback 引擎需要的 config 覆盖项。
    留空的字段（如 chat_model = ""）表示继承主引擎的值，不覆盖。
    """
    return {
        "AI_PROVIDER":  config.AI_FALLBACK_PROVIDER,
        "AI_API_KEY":   config.AI_FALLBACK_API_KEY,
        "AI_BASE_URL":  config.AI_FALLBACK_BASE_URL,
        "CHAT_MODEL":   config.AI_FALLBACK_CHAT_MODEL,
        "POLL_MODEL":   config.AI_FALLBACK_POLL_MODEL,
    }


# ── 加载主引擎 ────────────────────────────────────────────────────────────────

_primary = _load_engine(config.AI_PROVIDER)
logger.info(f"🧠 主引擎: {config.AI_PROVIDER} (chat={config.CHAT_MODEL}, poll={config.POLL_MODEL})")

# ── 加载 fallback 引擎（可选）────────────────────────────────────────────────

_fallback = None
if config.AI_FALLBACK_PROVIDER and config.AI_FALLBACK_API_KEY:
    _fallback = _load_engine(config.AI_FALLBACK_PROVIDER)
    fb_chat = config.AI_FALLBACK_CHAT_MODEL or config.CHAT_MODEL
    fb_poll = config.AI_FALLBACK_POLL_MODEL or config.POLL_MODEL
    logger.info(
        f"🔄 Fallback 引擎: {config.AI_FALLBACK_PROVIDER} (chat={fb_chat}, poll={fb_poll})"
    )
else:
    logger.info("⏭️ 未配置 fallback 引擎（ai.fallback.provider / api_key 为空）")


# ── 公共接口 ──────────────────────────────────────────────────────────────────

async def chat(db, messages: list[dict],
               send_callback=None, tool_callback=None) -> str:
    try:
        return await _primary.chat(db, messages, send_callback, tool_callback)
    except AIProviderError as e:
        if _fallback is None:
            raise
        logger.warning(
            f"⚠️ 主引擎 [{config.AI_PROVIDER}] 调用失败: {e}\n"
            f"   → 自动切换到 fallback [{config.AI_FALLBACK_PROVIDER}]"
        )
        with _override_config(**_fallback_overrides()):
            return await _fallback.chat(db, messages, send_callback, tool_callback)


async def scheduled_action(db, prompt: str, timestamp: str,
                           history: list[dict],
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None) -> str | None:
    try:
        return await _primary.scheduled_action(
            db, prompt, timestamp, history, send_callback, allow_silent, trigger
        )
    except AIProviderError as e:
        if _fallback is None:
            raise
        logger.warning(
            f"⚠️ 主引擎 [{config.AI_PROVIDER}] 调用失败: {e}\n"
            f"   → 自动切换到 fallback [{config.AI_FALLBACK_PROVIDER}]"
        )
        with _override_config(**_fallback_overrides()):
            return await _fallback.scheduled_action(
                db, prompt, timestamp, history, send_callback, allow_silent, trigger
            )


async def simple_completion(prompt: str) -> str:
    try:
        return await _primary.simple_completion(prompt)
    except AIProviderError as e:
        if _fallback is None:
            raise
        logger.warning(
            f"⚠️ 主引擎 [{config.AI_PROVIDER}] 调用失败: {e}\n"
            f"   → 自动切换到 fallback [{config.AI_FALLBACK_PROVIDER}]"
        )
        with _override_config(**_fallback_overrides()):
            return await _fallback.simple_completion(prompt)
