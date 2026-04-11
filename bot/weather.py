"""
天气查询模块
使用 wttr.in 免费 API，无需 API key
"""
import httpx
from datetime import datetime
from typing import Optional
from bot.logger import get_logger

logger = get_logger(__name__)

# 悉尼，AEST
CITY = "Sydney"
# 早上时段：6:00 - 10:00，在此期间注入天气数据
MORNING_START = 6
MORNING_END = 10


def is_morning() -> bool:
    """判断当前是否在早上时段"""
    hour = datetime.now().hour
    return MORNING_START <= hour < MORNING_END


async def get_weather_brief() -> str | None:
    """
    获取今日天气简报，返回中文摘要。
    失败时返回 None（不影响主流程）。
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # wttr.in JSON 格式，只取当天
            resp = await client.get(
                f"https://wttr.in/{CITY}?format=j1",
                headers={"Accept-Language": "zh"}
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            current = data.get("current_condition", [{}])[0]
            today = data.get("weather", [{}])[0]

            temp = current.get("temp_C", "?")
            feels = current.get("FeelsLikeC", "?")
            desc_list = current.get("lang_zh", [])
            desc = desc_list[0].get("value", "") if desc_list else current.get("weatherDesc", [{}])[0].get("value", "")
            humidity = current.get("humidity", "?")

            max_temp = today.get("maxtempC", "?")
            min_temp = today.get("mintempC", "?")

            # 降雨概率（取白天时段）
            hourly = today.get("hourly", [])
            rain_chances = [int(h.get("chanceofrain", 0)) for h in hourly if h]
            max_rain = max(rain_chances) if rain_chances else 0

            brief = (
                f"悉尼今日天气：{desc}，"
                f"当前 {temp}°C（体感 {feels}°C），"
                f"今日 {min_temp}~{max_temp}°C，"
                f"湿度 {humidity}%"
            )
            if max_rain > 30:
                brief += f"，降雨概率最高 {max_rain}%，记得带伞"

            return brief
    except Exception as e:
        logger.warning(f"⚠️ 天气查询失败: {e}")
        return None


# WEATHER_REPORT_PROMPT 已迁到 bot/prompts.py 与其它 prompt 集中管理
# 历史的直接导入路径 `from bot.weather import WEATHER_REPORT_PROMPT` 改为
# `from bot.prompts import WEATHER_REPORT_PROMPT`。
