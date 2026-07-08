"""
AI 引擎路由模块
每次请求按当前 active preset 动态选后端：
- claude: Anthropic 原生 API
- openai: OpenAI 官方 API（用 openai 官方 SDK）
- relay:  OpenAI 兼容中转站（用 preset.base_url）
- gemini: Google Gemini 原生 API

运行时由 /model、/fallback 斜杠命令切换 preset（见 config.set_active / set_fallback）。
主 preset 调用失败（AIProviderError）时自动切 fallback preset；fallback 为空则直接抛出。

外部只需 from bot.ai_engine import chat, scheduled_action, simple_completion
"""
import types
import config
from config import Preset
from bot.ai_provider_error import AIProviderError
from bot.logger import get_logger

logger = get_logger(__name__)


def _load_engine(provider: str) -> types.ModuleType:
    """根据 provider 名称加载对应的引擎模块（import 缓存，反复调用零成本）。"""
    p = provider.lower().strip()
    if p == "gemini":
        import bot.ai_engine_gemini as m
    elif p == "relay":
        import bot.ai_engine_relay as m
    elif p == "openai":
        import bot.ai_engine_openai as m
    else:
        import bot.ai_engine_claude as m
    return m


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
    engine = _load_engine(active.provider)
    method = getattr(engine, method_name)
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
        fb_engine = _load_engine(fallback.provider)
        fb_method = getattr(fb_engine, method_name)
        return await fb_method(*args, preset=fallback, **kwargs)


async def chat(db, messages: list[dict],
               send_callback=None, tool_callback=None) -> str:
    return await _run_with_fallback(
        "chat", db, messages,
        send_callback=send_callback, tool_callback=tool_callback,
    )


async def scheduled_action(db, prompt: str, timestamp: str,
                           history: list[dict],
                           send_callback=None, allow_silent: bool = False,
                           trigger: str | None = None,
                           tool_profile: str | None = None,
                           check_in_name: str | None = None,
                           context_config: dict | None = None) -> str | None:
    return await _run_with_fallback(
        "scheduled_action", db, prompt, timestamp, history,
        send_callback=send_callback, allow_silent=allow_silent, trigger=trigger,
        tool_profile=tool_profile, check_in_name=check_in_name,
        context_config=context_config,
    )


async def simple_completion(prompt: str) -> str:
    return await _run_with_fallback("simple_completion", prompt)
