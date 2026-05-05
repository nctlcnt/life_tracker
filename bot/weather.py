"""
天气查询模块
使用 tomorrow.io API（需 config.weather.api_key；免费档 500 calls/day）

tomorrow.io `/v4/weather/forecast` 返回 timelines.{minutely, hourly, daily}，
从当前时刻开始往前看 5 天 hourly / 6 天 daily。无"过去几小时"数据。
"""
import httpx
from datetime import datetime
from bot.logger import get_logger
import config

logger = get_logger(__name__)

LOCATION_LABEL = "悉尼"
TOMORROW_API = "https://api.tomorrow.io/v4/weather/forecast"

# 早上时段：6:00 - 10:00，在此期间注入天气数据
MORNING_START = 6
MORNING_END = 10

# tomorrow.io weatherCode → 中文描述
# 官方码表：https://docs.tomorrow.io/reference/data-layers-weather-codes
_WEATHER_CODE: dict[int, str] = {
    0: "未知",
    1000: "晴", 1100: "大部晴朗", 1101: "局部多云", 1102: "多云", 1001: "阴",
    2000: "雾", 2100: "轻雾",
    4000: "毛毛雨", 4001: "降雨", 4200: "小雨", 4201: "大雨",
    5000: "降雪", 5001: "阵雪", 5100: "小雪", 5101: "大雪",
    6000: "冻毛雨", 6001: "冻雨", 6200: "小冻雨", 6201: "大冻雨",
    7000: "冰粒", 7101: "大冰粒", 7102: "小冰粒",
    8000: "雷暴",
}


def _desc(code) -> str:
    try:
        return _WEATHER_CODE.get(int(code), f"天气码{code}")
    except (TypeError, ValueError):
        return "未知"


def is_morning() -> bool:
    """判断当前是否在早上时段"""
    hour = datetime.now().hour
    return MORNING_START <= hour < MORNING_END


def _parse_time(iso: str) -> datetime:
    """tomorrow.io 返回 UTC ISO，转本地 naive datetime（用于和 datetime.now() 比较）。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone().replace(tzinfo=None)


def _fmt_hourly(slot: dict) -> str:
    """把一个 tomorrow.io hourly slot 格式化为单行文字"""
    t = _parse_time(slot["time"])
    v = slot.get("values", {})
    desc = _desc(v.get("weatherCode"))
    temp = round(v.get("temperature", 0), 1)
    feels = round(v.get("temperatureApparent", 0), 1)
    rain = round(v.get("precipitationProbability", 0))
    uv = v.get("uvIndex", 0)
    return f"  {t.strftime('%H:%M')}  {desc}  {temp}°C（体感{feels}°C）  雨{rain}%  UV{uv}"


async def _fetch_forecast() -> dict | None:
    """抓取 tomorrow.io forecast（hourly + daily 一次拿齐），失败返 None。"""
    if not config.WEATHER_API_KEY:
        logger.info("⚠️ 未配置 weather.api_key，跳过天气查询")
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                TOMORROW_API,
                params={
                    "location": config.WEATHER_LOCATION,
                    "apikey": config.WEATHER_API_KEY,
                    "units": "metric",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    f"⚠️ tomorrow.io 返回 {resp.status_code}: {resp.text[:200]}"
                )
                return None
            return resp.json()
    except Exception as e:
        logger.warning(f"⚠️ 天气查询失败: {e}")
        return None


async def get_weather_struct() -> dict | None:
    """
    获取今日天气结构化数据（前端 dashboard 用）。
    返回 {temp, condition, max_temp, min_temp, location} 或 None。
    """
    data = await _fetch_forecast()
    if not data:
        return None
    try:
        timelines = data.get("timelines", {})
        hourly = timelines.get("hourly", [])
        daily = timelines.get("daily", [])
        minutely = timelines.get("minutely", [])
        if not hourly or not daily:
            return None
        current = (minutely[0] if minutely else hourly[0]).get("values", {})
        today_values = daily[0].get("values", {})
        return {
            "temp": round(current.get("temperature", 0), 1),
            "condition": _desc(current.get("weatherCode")),
            "max_temp": round(today_values.get("temperatureMax", 0), 1),
            "min_temp": round(today_values.get("temperatureMin", 0), 1),
            "location": LOCATION_LABEL,
        }
    except Exception as e:
        logger.warning(f"⚠️ 天气结构化解析失败: {e}")
        return None


async def get_weather_brief() -> str | None:
    """
    获取今日天气简报，返回中文摘要。
    失败时返回 None（不影响主流程）。
    """
    data = await _fetch_forecast()
    if not data:
        return None
    try:
        timelines = data.get("timelines", {})
        hourly = timelines.get("hourly", [])
        daily = timelines.get("daily", [])
        minutely = timelines.get("minutely", [])
        if not hourly or not daily:
            return None

        # 当前实况：优先用 minutely[0]（最精准），否则用 hourly[0]
        current = (minutely[0] if minutely else hourly[0]).get("values", {})
        desc = _desc(current.get("weatherCode"))
        temp = round(current.get("temperature", 0), 1)
        feels = round(current.get("temperatureApparent", 0), 1)
        humidity = round(current.get("humidity", 0))

        today_values = daily[0].get("values", {})
        max_temp = round(today_values.get("temperatureMax", 0), 1)
        min_temp = round(today_values.get("temperatureMin", 0), 1)

        # 今日剩余 hourly 的最大降雨概率
        today_date = datetime.now().date()
        rain_chances = [
            round(h["values"].get("precipitationProbability", 0))
            for h in hourly
            if _parse_time(h["time"]).date() == today_date
        ]
        max_rain = max(rain_chances) if rain_chances else 0

        brief = (
            f"{LOCATION_LABEL}今日天气：{desc}，"
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
    - 今日剩余时段（每 3 小时取一格）
    - 明日预报（每 3 小时取一格）

    注：tomorrow.io forecast 不返回过去数据，"过去几小时"段已去除。
    失败返回 None。
    """
    data = await _fetch_forecast()
    if not data:
        return None
    try:
        now = datetime.now()
        timelines = data.get("timelines", {})
        hourly = timelines.get("hourly", [])
        daily = timelines.get("daily", [])
        minutely = timelines.get("minutely", [])
        if not hourly or not daily:
            return None

        # ── 当前实况 ──────────────────────────────────────────
        current = (minutely[0] if minutely else hourly[0]).get("values", {})
        temp = round(current.get("temperature", 0), 1)
        feels = round(current.get("temperatureApparent", 0), 1)
        humidity = round(current.get("humidity", 0))
        uv = current.get("uvIndex", 0)
        desc = _desc(current.get("weatherCode"))
        wind_kmh = round(current.get("windSpeed", 0) * 3.6)  # m/s → km/h

        lines = [
            f"【当前实况】{now.strftime('%H:%M')}",
            f"  {desc}  {temp}°C（体感{feels}°C）  湿度{humidity}%  风速{wind_kmh}km/h  UV{uv}",
            "",
        ]

        # ── 今日剩余（每 3 小时取一格：0/3/6/9/12/15/18/21）──────
        today_date = now.date()
        today_max = round(daily[0]["values"].get("temperatureMax", 0), 1)
        today_min = round(daily[0]["values"].get("temperatureMin", 0), 1)
        lines.append(f"【今日剩余预报】今日全天 {today_min}~{today_max}°C")
        future_today = [
            h for h in hourly
            if _parse_time(h["time"]).date() == today_date
            and _parse_time(h["time"]).hour % 3 == 0
        ]
        if future_today:
            for s in future_today:
                lines.append(_fmt_hourly(s))
        else:
            lines.append("  （今日时段已全部过去）")
        lines.append("")

        # ── 明日预报 ──────────────────────────────────────────
        if len(daily) > 1:
            tom_date = _parse_time(daily[1]["time"]).date()
            tom_values = daily[1].get("values", {})
            tom_max = round(tom_values.get("temperatureMax", 0), 1)
            tom_min = round(tom_values.get("temperatureMin", 0), 1)
            lines.append(f"【明日预报】全天 {tom_min}~{tom_max}°C")
            tom_slots = [
                h for h in hourly
                if _parse_time(h["time"]).date() == tom_date
                and _parse_time(h["time"]).hour % 3 == 0
            ]
            for s in tom_slots:
                lines.append(_fmt_hourly(s))

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"⚠️ 天气详情解析失败: {e}")
        return None


# WEATHER_REPORT_PROMPT 已迁到 bot/prompts.py 与其它 prompt 集中管理
# 历史的直接导入路径 `from bot.weather import WEATHER_REPORT_PROMPT` 改为
# `from bot.prompts import WEATHER_REPORT_PROMPT`。
