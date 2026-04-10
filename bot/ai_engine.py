"""
AI 引擎路由模块
根据 config.AI_PROVIDER 自动选择后端引擎：
- claude: Anthropic 原生 API
- relay:  OpenAI 兼容中转站
- gemini: Google Gemini 原生 API

外部只需 from bot.ai_engine import chat, proactive_check, reminder_action
"""
import config

_provider = config.AI_PROVIDER.lower().strip()

if _provider == "gemini":
    from bot.ai_engine_gemini import chat, proactive_check, reminder_action
elif _provider == "relay":
    from bot.ai_engine_relay import chat, proactive_check, reminder_action
else:
    # 默认 claude
    from bot.ai_engine_claude import chat, proactive_check, reminder_action

print(f"🧠 AI 引擎: {_provider} (chat={config.CHAT_MODEL}, poll={config.POLL_MODEL})")