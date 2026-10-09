"""Time tools backed by the host runtime clock."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.runtime.time_utils import current_datetime, weekday_label
from agent.runtime.tool_failure import ToolFailure

from .registry import ToolDef, ToolRegistry


def register_time_tools(registry: ToolRegistry):
    def _current_time(timezone: str = "") -> str | ToolFailure:
        requested = (timezone or "").strip()
        if requested:
            # current_datetime falls back to local time for an unknown name;
            # a caller that named a zone must not get local time under it.
            try:
                ZoneInfo(requested)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                return ToolFailure(
                    code="invalid_arguments",
                    message=(
                        f"未知时区 {requested!r}：timezone 需为 IANA 时区名，"
                        "如 Asia/Shanghai、America/New_York、UTC。"
                    ),
                    retryable=True,
                )
        now = current_datetime(requested or None)
        offset = now.strftime("%z")
        if len(offset) == 5:
            offset = f"{offset[:3]}:{offset[3:]}"
        # Local time has no IANA name to report; a named zone is always shown.
        zone = getattr(now.tzinfo, "key", None)
        return (
            f"{now.strftime('%Y-%m-%d')} {weekday_label(now)} "
            f"{now.strftime('%H:%M:%S')} UTC{offset}"
            + (f" ({zone})" if zone else "")
        )

    registry.register(ToolDef(
        name="current_time",
        description="获取当前日期、星期、时间及 UTC 偏移（指定或配置了时区时附时区名），用于精确时间或相对日期判断。",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "IANA 时区名，如 Asia/Shanghai、America/New_York；New York、PST、UTC+8 "
                        "这类写法不是 IANA 名称，会报错。默认 AGENT_TIMEZONE 或本地时区。"
                    ),
                    "default": "",
                },
            },
        },
        fn=_current_time,
        sandboxed=True,
    ))
