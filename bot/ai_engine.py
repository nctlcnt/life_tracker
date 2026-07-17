"""
AI 引擎入口模块
LT-134 引擎合并后只有一个引擎实现（bot/ai_engine_openai_compat，
OpenAI Chat Completions 格式），preset 只区分 base_url + model + api_key。

运行时由 /model、/fallback 斜杠命令切换 preset（见 config.set_active / set_fallback）。
主 preset 调用失败（AIProviderError）时自动换 fallback preset 重试同一引擎；
fallback 为空则直接抛出。

外部只需 from bot.ai_engine import chat, scheduled_action, simple_completion
"""
import config
from config import Preset
from bot import ai_engine_openai_compat as _engine
from bot.ai_provider_error import AIProviderError
from bot.logger import get_logger

logger = get_logger(__name__)


# ── 启动日志（展示当前激活状态，便于排查）────────────────────────────────
_active = config.get_active()
logger.info(f"🧠 主 preset: {_active.name} (provider={_active.provider}, model={_active.model})")
_fb = config.get_fallback()
if _fb:
    logger.info(f"🔄 Fallback preset: {_fb.name} (provider={_fb.provider}, model={_fb.model})")
else:
    logger.info("⏭️ 未配置 fallback preset")


# ── 公共接口 ──────────────────────────────────────────────────────────────────

async def _run_with_fallback(method_name: str, *args, **kwargs):
    """
    统一的调用壳：按当前 active preset 跑；抛 AIProviderError 就换 fallback preset 再跑。
    method_name 是引擎模块里的 chat / scheduled_action / simple_completion。
    """
    active: Preset = config.get_active()
    method = getattr(_engine, method_name)
    try:
        return await method(*args, preset=active, **kwargs)
    except AIProviderError as e:
        fallback: Preset | None = config.get_fallback()
        if fallback is None:
            raise
        logger.warning(
            f"⚠️ 主 preset [{active.name}] 调用失败: {e}\n"
            f"   → 自动切换到 fallback preset [{fallback.name}]"
        )
        return await method(*args, preset=fallback, **kwargs)


async def chat(db, messages: list[dict],
               send_callback=None, tool_callback=None, memory_service=None) -> str:
    return await _run_with_fallback(
        "chat", db, messages,
        send_callback=send_callback, tool_callback=tool_callback,
        memory_service=memory_service,
    )


async def scheduled_action(db, prompt: str, timestamp: str,
                           history: list[dict],
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None,
                           tool_profile: str | None = None,
                           check_in_name: str | None = None,
                           context_config: dict | None = None,
                           memory_service=None) -> str | None:
    return await _run_with_fallback(
        "scheduled_action", db, prompt, timestamp, history,
        send_callback=send_callback, allow_silent=allow_silent, trigger=trigger,
        tool_profile=tool_profile, check_in_name=check_in_name,
        context_config=context_config,
        memory_service=memory_service,
    )


async def simple_completion(prompt: str) -> str:
    return await _run_with_fallback("simple_completion", prompt)
