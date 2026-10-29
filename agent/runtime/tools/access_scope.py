"""Caller capability ceiling, independent of model schemas and approval grants."""
from contextvars import ContextVar


tool_access_scope: ContextVar[frozenset[str] | None] = ContextVar("tool_access_scope", default=None)
CHAT_TOOLS = frozenset({"current_time", "search_web", "web_extract", "fetch_url"})
