"""
Read-only Google Calendar integration.

The module is intentionally optional: if credentials, dependencies, or token
files are missing, callers receive a structured "not configured" path instead
of breaking the bot.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from bot.logger import get_logger
from bot.timezone_state import get_timezone

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DEFAULT_CONTEXT_DAYS = 7
DEFAULT_CONTEXT_LIMIT = 20
DEFAULT_CONTEXT_TIMEOUT_SECONDS = 8
DEFAULT_CONTEXT_CACHE_SECONDS = 300

_creds_cache: Any | None = None
_service_cache: Any | None = None
_context_cache: dict[tuple[str, int, int, str], tuple[datetime, str]] = {}
_calendar_list_cache: tuple[datetime, list[dict]] | None = None


class CalendarNotConfigured(Exception):
    """Google Calendar is disabled, unauthenticated, or missing dependencies."""


class CalendarQueryError(Exception):
    """Google Calendar was configured, but the API query failed."""


def _imports():
    try:
        import httplib2
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google_auth_httplib2 import AuthorizedHttp
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as e:
        raise CalendarNotConfigured(
            "Google Calendar dependencies are not installed. Run pip install -r requirements.txt."
        ) from e
    return httplib2, Request, InstalledAppFlow, AuthorizedHttp, Credentials, build, HttpError


def is_authorized() -> bool:
    return bool(config.GCAL_TOKEN_FILE and os.path.exists(config.GCAL_TOKEN_FILE))


def _reset_caches() -> None:
    global _creds_cache, _service_cache, _calendar_list_cache
    _creds_cache = None
    _service_cache = None
    _calendar_list_cache = None
    _context_cache.clear()


def begin_oauth_flow(redirect_port: int = 58679) -> tuple[Any, str]:
    """Create an OAuth flow and return (flow, authorization_url)."""
    if not config.GCAL_CLIENT_SECRET_FILE or not os.path.exists(config.GCAL_CLIENT_SECRET_FILE):
        raise CalendarNotConfigured(
            f"Google Calendar client secret file not found: {config.GCAL_CLIENT_SECRET_FILE}"
        )
    _httplib2, _Request, InstalledAppFlow, _AuthorizedHttp, _Credentials, _build, _HttpError = _imports()
    flow = InstalledAppFlow.from_client_secrets_file(config.GCAL_CLIENT_SECRET_FILE, SCOPES)
    flow.redirect_uri = f"http://localhost:{redirect_port}/"
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return flow, auth_url


def finish_oauth_flow(flow: Any, callback_url: str) -> str:
    """Finish OAuth using the final localhost callback URL and write token file."""
    parsed = urlparse((callback_url or "").strip())
    query = parse_qs(parsed.query)
    if "error" in query:
        raise CalendarNotConfigured(f"Google OAuth error: {query['error'][0]}")
    code = (query.get("code") or [""])[0]
    if not code:
        raise CalendarNotConfigured("No code= parameter found in callback URL.")
    flow.fetch_token(code=code)
    creds = flow.credentials
    os.makedirs(os.path.dirname(config.GCAL_TOKEN_FILE), exist_ok=True)
    with open(config.GCAL_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    _reset_caches()
    return config.GCAL_TOKEN_FILE


def _tz() -> ZoneInfo:
    name = get_timezone() or config.TIMEZONE or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise CalendarQueryError(f"Invalid timezone: {name}") from e


def _parse_local(value: str, *, end_of_date: bool = False) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise CalendarQueryError("Calendar query requires non-empty start and end.")

    tz = _tz()
    try:
        if "T" not in raw and len(raw) == 10:
            d = date.fromisoformat(raw)
            return datetime.combine(d, time.min, tzinfo=tz)

        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise CalendarQueryError(f"Invalid calendar time: {raw}") from e

    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _credentials():
    global _creds_cache
    if not config.GCAL_ENABLED:
        raise CalendarNotConfigured("Google Calendar is disabled in config.json.")
    if not config.GCAL_TOKEN_FILE or not os.path.exists(config.GCAL_TOKEN_FILE):
        raise CalendarNotConfigured(
            "Google Calendar is not authorized. Run scripts/google_calendar_auth.py first."
        )

    _httplib2, Request, _InstalledAppFlow, _AuthorizedHttp, Credentials, _build, _HttpError = _imports()
    if _creds_cache is None:
        _creds_cache = Credentials.from_authorized_user_file(config.GCAL_TOKEN_FILE, SCOPES)

    creds = _creds_cache
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            _creds_cache = None
            raise CalendarNotConfigured(f"Google Calendar token refresh failed: {e}") from e
        os.makedirs(os.path.dirname(config.GCAL_TOKEN_FILE), exist_ok=True)
        with open(config.GCAL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    if not creds or not creds.valid:
        _creds_cache = None
        raise CalendarNotConfigured(
            "Google Calendar token is invalid. Re-run scripts/google_calendar_auth.py."
        )
    return creds


def _service():
    global _service_cache
    if _service_cache is None:
        httplib2, _Request, _InstalledAppFlow, AuthorizedHttp, _Credentials, build, _HttpError = _imports()
        http = AuthorizedHttp(_credentials(), http=httplib2.Http(timeout=DEFAULT_CONTEXT_TIMEOUT_SECONDS))
        _service_cache = build("calendar", "v3", http=http, cache_discovery=False)
    return _service_cache


def _event_time(raw: dict, key: str, tz: ZoneInfo) -> tuple[str, bool]:
    value = raw.get(key, {})
    if "dateTime" in value:
        dt = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(tz)
        return dt.isoformat(timespec="minutes"), False
    if "date" in value:
        return value["date"], True
    return "", False


def _normalize_event(raw: dict) -> dict:
    tz = _tz()
    start, start_all_day = _event_time(raw, "start", tz)
    end, end_all_day = _event_time(raw, "end", tz)
    return {
        "id": raw.get("id", ""),
        "summary": raw.get("summary", "(No title)"),
        "start": start,
        "end": end,
        "all_day": bool(start_all_day or end_all_day),
        "location": raw.get("location", ""),
        "status": raw.get("status", ""),
    }


def list_calendars() -> list[dict]:
    """Return readable calendars from Google Calendar's calendarList."""
    global _calendar_list_cache
    if not config.GCAL_ENABLED:
        raise CalendarNotConfigured("Google Calendar is disabled in config.json.")
    now = datetime.now()
    if (
        _calendar_list_cache
        and (now - _calendar_list_cache[0]).total_seconds() < DEFAULT_CONTEXT_CACHE_SECONDS
    ):
        disabled = set(config.GCAL_DISABLED_CALENDAR_IDS)
        return [{**c, "disabled": c["id"] in disabled} for c in _calendar_list_cache[1]]

    try:
        result = _service().calendarList().list(minAccessRole="reader").execute()
    except CalendarNotConfigured:
        raise
    except Exception as e:
        raise CalendarQueryError(f"Google Calendar list failed: {e}") from e

    calendars = []
    for item in result.get("items", []):
        calendar_id = item.get("id", "")
        if not calendar_id:
            continue
        calendars.append({
            "id": calendar_id,
            "summary": item.get("summary", calendar_id),
            "primary": bool(item.get("primary")),
            "selected": bool(item.get("selected", True)),
            "access_role": item.get("accessRole", ""),
            "disabled": False,
        })
    _calendar_list_cache = (now, calendars)
    disabled = set(config.GCAL_DISABLED_CALENDAR_IDS)
    calendars = [{**c, "disabled": c["id"] in disabled} for c in calendars]
    return calendars


def _configured_calendar_ids() -> list[str]:
    if config.GCAL_INCLUDE_ALL_READABLE:
        return [
            c["id"]
            for c in list_calendars()
            if not c.get("disabled")
        ]
    configured = config.GCAL_CALENDAR_IDS or [config.GCAL_CALENDAR_ID or "primary"]
    disabled = set(config.GCAL_DISABLED_CALENDAR_IDS)
    ids = []
    seen = set()
    for calendar_id in configured:
        cid = str(calendar_id).strip()
        if cid and cid not in disabled and cid not in seen:
            ids.append(cid)
            seen.add(cid)
    return ids or ["primary"]


def _calendar_summaries() -> dict[str, str]:
    """Map calendar id -> friendly name, including a `primary` alias."""
    summaries: dict[str, str] = {}
    for c in list_calendars():
        name = c.get("summary") or c["id"]
        summaries[c["id"]] = name
        if c.get("primary"):
            summaries["primary"] = name
    return summaries


def disable_calendar(calendar_id: str) -> list[str]:
    cid = (calendar_id or "").strip()
    if not cid:
        raise CalendarQueryError("calendar_id is required.")
    disabled = list(config.GCAL_DISABLED_CALENDAR_IDS)
    if cid not in disabled:
        disabled.append(cid)
        config.set_gcal_disabled_calendar_ids(disabled)
    _reset_caches()
    return disabled


def enable_calendar(calendar_id: str) -> list[str]:
    cid = (calendar_id or "").strip()
    if not cid:
        raise CalendarQueryError("calendar_id is required.")
    disabled = [x for x in config.GCAL_DISABLED_CALENDAR_IDS if x != cid]
    config.set_gcal_disabled_calendar_ids(disabled)
    _reset_caches()
    return disabled


def list_events(
    start: str,
    end: str,
    query: str | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Return normalized calendar events for [start, end)."""
    if not config.GCAL_ENABLED:
        raise CalendarNotConfigured("Google Calendar is disabled in config.json.")
    start_dt = _parse_local(start)
    end_dt = _parse_local(end)
    if end_dt <= start_dt:
        raise CalendarQueryError("Calendar query end must be after start.")

    tz_name = get_timezone() or config.TIMEZONE or "UTC"
    try:
        max_results = max(1, min(int(max_results or 50), 100))
    except (TypeError, ValueError):
        max_results = 50

    base_params = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
        "timeZone": tz_name,
    }
    if query:
        base_params["q"] = query

    calendar_ids = _configured_calendar_ids()
    try:
        summaries = _calendar_summaries()
    except CalendarNotConfigured:
        raise
    except Exception as e:
        logger.warning(f"⚠️ Google Calendar 名称查询失败，回退到原始 id: {e}")
        summaries = {}
    events: list[dict] = []
    service = _service()
    for calendar_id in calendar_ids:
        params = {"calendarId": calendar_id, **base_params}
        try:
            result = service.events().list(**params).execute()
        except CalendarNotConfigured:
            raise
        except Exception as e:
            logger.warning(f"⚠️ Google Calendar query failed for {calendar_id}: {e}")
            continue
        for item in result.get("items", []):
            event = _normalize_event(item)
            event["calendar_id"] = calendar_id
            event["calendar_summary"] = summaries.get(calendar_id, calendar_id)
            events.append(event)

    return sorted(events, key=lambda e: (e.get("start") or "", e.get("summary") or ""))


def _format_event_line(event: dict, show_calendar: bool = True) -> str:
    title = event.get("summary") or "(No title)"
    calendar = event.get("calendar_summary") or ""
    location = event.get("location") or ""
    status = event.get("status") or ""
    suffix = ""
    if show_calendar and calendar:
        suffix += f" [{calendar}]"
    if location:
        suffix += f" @ {location}"
    if status and status != "confirmed":
        suffix += f" [{status}]"

    if event.get("all_day"):
        start = event.get("start", "")
        end = event.get("end", "")
        if end and end != start:
            try:
                end_label = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                when = start if end_label == start else f"{start}..{end_label}"
            except ValueError:
                when = start
        else:
            when = start
        return f"- {when} all-day | {title}{suffix}"

    start_raw = event.get("start", "")
    end_raw = event.get("end", "")
    try:
        start_dt = datetime.fromisoformat(start_raw)
        end_dt = datetime.fromisoformat(end_raw) if end_raw else None
        start_label = start_dt.strftime("%Y-%m-%d %H:%M")
        end_label = end_dt.strftime("%H:%M") if end_dt and end_dt.date() == start_dt.date() else (
            end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else ""
        )
        when = f"{start_label}-{end_label}" if end_label else start_label
    except ValueError:
        when = start_raw
    return f"- {when} | {title}{suffix}"


def build_calendar_context(days: int = DEFAULT_CONTEXT_DAYS, limit: int = DEFAULT_CONTEXT_LIMIT) -> str:
    """Build prompt-ready context for today plus the next `days` days."""
    tz = _tz()
    today = datetime.now(tz).date()
    start = today.isoformat()
    end = (today + timedelta(days=days + 1)).isoformat()
    events = list_events(start, end, max_results=max(limit + 1, limit))
    if not events:
        return ""
    shown = events[:limit]
    show_calendar = len({e.get("calendar_id") for e in shown}) > 1
    lines = [_format_event_line(e, show_calendar=show_calendar) for e in shown]
    if len(events) > limit:
        lines.append(f"- ...还有 {len(events) - limit} 条未展开，可用 query_calendar 精查")
    return "\n".join(lines)


async def get_calendar_context(days: int = DEFAULT_CONTEXT_DAYS, limit: int = DEFAULT_CONTEXT_LIMIT) -> str | None:
    """Async wrapper for prompt construction; failures are logged and ignored."""
    try:
        tz_name = get_timezone() or config.TIMEZONE or "UTC"
        today = datetime.now(_tz()).date().isoformat()
        key = (tz_name, days, limit, today)
        cached = _context_cache.get(key)
        now = datetime.now()
        if cached and (now - cached[0]).total_seconds() < DEFAULT_CONTEXT_CACHE_SECONDS:
            return cached[1] or None

        context = build_calendar_context(days, limit)
        _context_cache[key] = (now, context)
        return context or None
    except CalendarNotConfigured as e:
        logger.info(f"⚠️ Google Calendar 未启用/未授权，跳过日历上下文: {e}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Google Calendar 上下文查询失败: {e}")
        return None
