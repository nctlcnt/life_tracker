"""
天气查询模块
使用 wttr.in 免费 API，无需 API key
"""
import httpx
from datetime import datetime
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


def _fmt_hourly(slot: dict) -> str:
    """把一个 wttr.in hourly slot 格式化为单行文字"""
    raw_time = int(slot.get("time", 0))  # e.g. 0, 300, 600 ... 2100
    hh = raw_time // 100
    desc_list = slot.get("lang_zh", [])
    desc = desc_list[0].get("value", "") if desc_list else slot.get("weatherDesc", [{}])[0].get("value", "")
    temp = slot.get("tempC", "?")
    feels = slot.get("FeelsLikeC", "?")
    rain = slot.get("chanceofrain", "0")
    uv = slot.get("uvIndex", "0")
    return f"  {hh:02d}:00  {desc}  {temp}°C（体感{feels}°C）  雨{rain}%  UV{uv}"


async def _fetch_wttr() -> dict | None:
    """原始抓取 wttr.in j1 数据，失败返回 None。"""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"https://wttr.in/{CITY}?format=j1",
                headers={"Accept-Language": "zh"}
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception as e:
        logger.warning(f"⚠️ 天气查询失败: {e}")
        return None


async def get_weather_brief() -> str | None:
    """
    获取今日天气简报，返回中文摘要。
    失败时返回 None（不影响主流程）。
    """
    data = await _fetch_wttr()
    if not data:
        return None
    try:
        current = data.get("current_condition", [{}])[0]
        today = data.get("weather", [{}])[0]

        temp = current.get("temp_C", "?")
        feels = current.get("FeelsLikeC", "?")
        desc_list = current.get("lang_zh", [])
        desc = desc_list[0].get("value", "") if desc_list else current.get("weatherDesc", [{}])[0].get("value", "")
        humidity = current.get("humidity", "?")

        max_temp = today.get("maxtempC", "?")
        min_temp = today.get("mintempC", "?")

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
        logger.warning(f"⚠️ 天气数据解析失败: {e}")
        return None


async def get_weather_detailed() -> str | None:
    """
    获取详细天气数据供 /weather 指令使用：
    - 当前实况
    - 过去几小时（今日已过时段的最近 2 个 slot）
    - 未来 24 小时（今日剩余 + 明日，最多 8 个 slot）
    返回结构化中文文本，失败返回 None。
    """
    data = await _fetch_wttr()
    if not data:
        return None
    try:
        now = datetime.now()
        current_slot = now.hour * 100  # e.g. 14:30 → 1400

        current = data.get("current_condition", [{}])[0]
        weather_days = data.get("weather", [])
        today = weather_days[0] if weather_days else {}
        tomorrow = weather_days[1] if len(weather_days) > 1 else {}

        # ── 当前实况 ──────────────────────────────────────────
        temp = current.get("temp_C", "?")
        feels = current.get("FeelsLikeC", "?")
        humidity = current.get("humidity", "?")
        uv = current.get("uvIndex", "0")
        desc_list = current.get("lang_zh", [])
        desc = desc_list[0].get("value", "") if desc_list else current.get("weatherDesc", [{}])[0].get("value", "")
        wind = current.get("windspeedKmph", "?")

        lines = [
            f"【当前实况】{now.strftime('%H:%M')}",
            f"  {desc}  {temp}°C（体感{feels}°C）  湿度{humidity}%  风速{wind}km/h  UV{uv}",
            "",
        ]

        # ── 今日各时段（区分已过 / 未来）────────────────────
        today_hourly = today.get("hourly", [])
        past_slots = [h for h in today_hourly if int(h.get("time", 0)) < current_slot]
        future_slots_today = [h for h in today_hourly if int(h.get("time", 0)) >= current_slot]

        if past_slots:
            recent = past_slots[-2:]  # 最近 2 个已过时段
            lines.append(f"【过去几小时】")
            for s in recent:
                lines.append(_fmt_hourly(s))
            lines.append("")

        max_temp = today.get("maxtempC", "?")
        min_temp = today.get("mintempC", "?")
        lines.append(f"【今日剩余预报】今日全天 {min_temp}~{max_temp}°C")
        if future_slots_today:
            for s in future_slots_today:
                lines.append(_fmt_hourly(s))
        else:
            lines.append("  （今日时段已全部过去）")
        lines.append("")

        # ── 明日预报 ──────────────────────────────────────────
        if tomorrow:
            tom_max = tomorrow.get("maxtempC", "?")
            tom_min = tomorrow.get("mintempC", "?")
            lines.append(f"【明日预报】全天 {tom_min}~{tom_max}°C")
            for s in tomorrow.get("hourly", []):
                lines.append(_fmt_hourly(s))

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"⚠️ 天气详情解析失败: {e}")
        return None


# WEATHER_REPORT_PROMPT 已迁到 bot/prompts.py 与其它 prompt 集中管理
# 历史的直接导入路径 `from bot.weather import WEATHER_REPORT_PROMPT` 改为
# `from bot.prompts import WEATHER_REPORT_PROMPT`。
