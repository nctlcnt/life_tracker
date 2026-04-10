"""
AI 引擎路由模块
根据 config.AI_PROVIDER 自动选择后端引擎：
- claude: Anthropic 原生 API
- relay:  OpenAI 兼容中转站
- gemini: Google Gemini 原生 API

外部只需 from bot.ai_engine import chat, scheduled_action, simple_completion
"""
import config

_provider = config.AI_PROVIDER.lower().strip()

if _provider == "gemini":
    from bot.ai_engine_gemini import chat, scheduled_action, simple_completion
elif _provider == "relay":
    from bot.ai_engine_relay import chat, scheduled_action, simple_completion
else:
    # 默认 claude
    from bot.ai_engine_claude import chat, scheduled_action, simple_completion

print(f"🧠 AI 引擎: {_provider} (chat={config.CHAT_MODEL}, poll={config.POLL_MODEL})")
