"""Structured, validated questions for pausing a model turn for the user."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ..tool_failure import ToolFailure
from .registry import ToolDef, ToolRegistry

MIN_QUESTIONS = 1
MAX_QUESTIONS = 3
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_ID_CHARS = 80
MAX_HEADER_CHARS = 80
MAX_QUESTION_CHARS = 1_000
MAX_LABEL_CHARS = 120
MAX_DESCRIPTION_CHARS = 500
MAX_CUSTOM_CHARS = 2_000
_CALLS_PER_TURN = 6

_QUESTION_KEYS = {"id", "header", "question", "options", "multi_select"}
_OPTION_KEYS = {"label", "description"}
_ANSWER_KEYS = {"id", "selected", "custom"}


def _single_line(value: object) -> str:
    text = "".join(char if char == "\t" or ord(char) >= 32 else " " for char in str(value or ""))
    return " ".join(text.split())


def _clean_text(value: object, *, limit: int) -> str:
    return _single_line(value)[:limit].strip()


def _question_text(value: object, *, limit: int, name: str) -> str:
    """Text the model wrote for the user: refused when too long, so the user never reads a cut one."""
    text = _single_line(value)
    if len(text) > limit:
        raise ValueError(f"{name} is {len(text):,} characters; the limit is {limit:,}")
    return text


def normalize_questions(raw: object) -> list[dict[str, Any]]:
    """Validate and detach a model question request into its UI contract."""

    if not isinstance(raw, list) or not MIN_QUESTIONS <= len(raw) <= MAX_QUESTIONS:
        raise ValueError(f"questions must contain {MIN_QUESTIONS} to {MAX_QUESTIONS} questions")

    normalized: list[dict[str, Any]] = []
    question_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"question {index + 1} must be an object")
        unknown = set(item) - _QUESTION_KEYS
        if unknown:
            raise ValueError(f"unknown question key: {min(unknown)}")

        place = f"question {index + 1}"
        question_id = _question_text(item.get("id"), limit=MAX_ID_CHARS, name=f"{place} id")
        if not question_id:
            raise ValueError("non-empty id is required")
        if question_id in question_ids:
            raise ValueError("question ids must be unique")
        question_ids.add(question_id)

        question = _question_text(item.get("question"), limit=MAX_QUESTION_CHARS, name=f"{place} text")
        if not question:
            raise ValueError("non-empty question is required")

        result: dict[str, Any] = {"id": question_id, "question": question}
        if "header" in item:
            header = _question_text(item.get("header"), limit=MAX_HEADER_CHARS, name=f"{place} header")
            if header:
                result["header"] = header

        if "options" in item:
            options = item["options"]
            if not isinstance(options, list) or not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
                raise ValueError(f"options must contain {MIN_OPTIONS} to {MAX_OPTIONS} options")
            normalized_options: list[dict[str, str]] = []
            labels: set[str] = set()
            for option_index, option in enumerate(options):
                if not isinstance(option, dict):
                    raise TypeError(f"option {option_index + 1} must be an object")
                unknown_option_keys = set(option) - _OPTION_KEYS
                if unknown_option_keys:
                    raise ValueError(f"unknown option key: {min(unknown_option_keys)}")
                option_place = f"{place} option {option_index + 1}"
                label = _question_text(option.get("label"), limit=MAX_LABEL_CHARS, name=f"{option_place} label")
                if not label:
                    raise ValueError("option label must be non-empty")
                if label in labels:
                    raise ValueError("option labels must be unique")
                labels.add(label)
                normalized_option = {"label": label}
                if "description" in option:
                    description = _question_text(
                        option.get("description"), limit=MAX_DESCRIPTION_CHARS, name=f"{option_place} description",
                    )
                    if description:
                        normalized_option["description"] = description
                normalized_options.append(normalized_option)
            result["options"] = normalized_options

        multi_select = item.get("multi_select", False)
        if not isinstance(multi_select, bool):
            raise TypeError("multi_select must be a boolean")
        result["multi_select"] = multi_select
        normalized.append(result)
    return normalized


def normalize_answers(
    questions: list[dict[str, Any]],
    raw: object,
) -> dict[str, list[dict[str, Any]]]:
    """Validate answers against the offered questions and return request order."""

    if not isinstance(raw, dict):
        raise TypeError("answers must be an object")
    unknown = set(raw) - {"answers"}
    if unknown:
        raise ValueError(f"unknown answer key: {min(unknown)}")
    answers = raw.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("answers must contain one answer for every question")

    by_id: dict[str, dict[str, Any]] = {}
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            raise TypeError(f"answer {index + 1} must be an object")
        unknown_answer_keys = set(answer) - _ANSWER_KEYS
        if unknown_answer_keys:
            raise ValueError(f"unknown answer key: {min(unknown_answer_keys)}")
        answer_id = _clean_text(answer.get("id"), limit=MAX_ID_CHARS)
        if not answer_id:
            raise ValueError("answer id must be non-empty")
        if answer_id in by_id:
            raise ValueError("answer ids must be unique")
        by_id[answer_id] = answer

    expected_ids = [str(question.get("id") or "") for question in questions]
    if set(by_id) != set(expected_ids):
        raise ValueError("answer ids must match question ids")

    normalized_answers: list[dict[str, Any]] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        answer = by_id[question_id]
        selected = answer.get("selected", [])
        if not isinstance(selected, list):
            raise TypeError("selected must be a list")
        if any(not isinstance(label, str) for label in selected):
            raise ValueError("selected labels must be strings")
        if len(selected) != len(set(selected)):
            raise ValueError("selected labels must be unique")
        offered = {
            option.get("label")
            for option in question.get("options", [])
            if isinstance(option, dict)
        }
        for label in selected:
            if label not in offered:
                raise ValueError(f"selected label {label!r} was not offered")

        custom = _clean_text(answer.get("custom"), limit=MAX_CUSTOM_CHARS)
        if "custom" in answer and len(_clean_text(answer.get("custom"), limit=MAX_CUSTOM_CHARS + 1)) > MAX_CUSTOM_CHARS:
            raise ValueError(f"custom answer must be at most {MAX_CUSTOM_CHARS:,} characters")
        if not question.get("multi_select", False) and len(selected) > 1:
            raise ValueError("single-select questions allow at most one selected label")
        if not question.get("multi_select", False) and selected and custom:
            raise ValueError("single-select answers cannot include both selected and custom")
        result: dict[str, Any] = {"id": question_id, "selected": list(selected)}
        if custom:
            result["custom"] = custom
        normalized_answers.append(result)
    return {"answers": normalized_answers}


class UserQuestionUnavailable(RuntimeError):
    """The current channel cannot pause for top-level user input."""


class UserQuestionCancelled(RuntimeError):
    """The top-level user dismissed the structured question request."""


def register_user_question_tools(
    registry: ToolRegistry,
    ask_questions: Callable[..., Awaitable[dict]],
) -> None:
    """Register the model-facing user-question tool."""

    supports_wait_mode = "mode" in inspect.signature(ask_questions).parameters

    async def _ask_user_question(questions: list[dict], mode: str = "blocking", optional: bool = False,
                                 timeout_seconds: float = 30, _permission_call_id: str = "") -> str | ToolFailure:
        from ..user_questions import validate_wait
        validate_wait(mode, optional, timeout_seconds)
        request = normalize_questions(questions)
        try:
            raw_answer = (await ask_questions(request, mode=mode, optional=optional,
                                              timeout_seconds=timeout_seconds, call_id=_permission_call_id)
                          if supports_wait_mode or mode == "timed" else await ask_questions(request))
        except UserQuestionUnavailable as exc:
            return ToolFailure(
                code="user_question_unavailable",
                message=str(exc),
                retryable=True,
                recovery_hint="Ask the user in ordinary prose or continue with a safe documented assumption.",
            )
        except UserQuestionCancelled as exc:
            return ToolFailure(
                code="user_question_cancelled",
                message=str(exc),
                retryable=True,
                recovery_hint="Wait for the user's next message or ask a revised question.",
            )
        answer = raw_answer if raw_answer.get("state") == "pending" else normalize_answers(request, raw_answer)
        return json.dumps(answer, ensure_ascii=False, separators=(",", ":"))

    registry.register(ToolDef(
        name="ask_user_question",
        description=(
            "Ask the top-level user a structured question. This call pauses for the top-level user. "
            "Use mode=timed with optional=true only for optional preferences or information: after timeout it returns "
            "pending and you may continue independent work while the question remains answerable. "
            "Required answers and authorization must remain blocking. Unanswered never means approval. "
            "This must be the only tool call in its assistant step; it may be repeated, at most "
            f"{_CALLS_PER_TURN} calls per turn (a further call is not run and ends the turn). "
            "Only one question request can be open at a time: while a timed-out optional question is still "
            "pending, another call fails. It stops blocking when the user answers or dismisses it, or when a "
            "new task, a session change or a backend restart expires it. "
            "The result has one answer per question id: selected holds the chosen option labels and custom "
            "holds text the user typed; an answer with neither means the user left that question unanswered. "
            "It does not grant risky-tool approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string", "enum": ["blocking", "timed"], "default": "blocking",
                    "description": (
                        "blocking waits until the user answers or dismisses the request. timed waits "
                        "timeout_seconds and then returns pending; it requires optional=true."
                    ),
                },
                "optional": {
                    "type": "boolean", "default": False,
                    "description": "true only when the work can go on without the answer. Required for mode=timed.",
                },
                "timeout_seconds": {
                    "type": "number", "exclusiveMinimum": 0, "maximum": 300, "default": 30,
                    "description": (
                        "mode=timed: seconds to wait before returning pending (more than 0, at most 300); "
                        "the wait is extended while the user is typing an answer. Not used by mode=blocking."
                    ),
                },
                "questions": {
                    "type": "array",
                    "minItems": MIN_QUESTIONS,
                    "maxItems": MAX_QUESTIONS,
                    "description": (
                        f"{MIN_QUESTIONS} to {MAX_QUESTIONS} questions shown together. Every text is shown "
                        "on one line (line breaks become spaces); a text over its length limit is refused."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string", "maxLength": MAX_ID_CHARS,
                                "description": "Your key for this question, unique in the call; its answer carries the same id.",
                            },
                            "header": {
                                "type": "string", "maxLength": MAX_HEADER_CHARS,
                                "description": "Optional short title shown above the question.",
                            },
                            "question": {
                                "type": "string", "maxLength": MAX_QUESTION_CHARS,
                                "description": f"The question, at most {MAX_QUESTION_CHARS:,} characters.",
                            },
                            "options": {
                                "type": "array",
                                "minItems": MIN_OPTIONS,
                                "maxItems": MAX_OPTIONS,
                                "description": (
                                    f"{MIN_OPTIONS} to {MAX_OPTIONS} choices with distinct labels; omit for a "
                                    "free-text question. The user can always type an answer instead of choosing."
                                ),
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string", "maxLength": MAX_LABEL_CHARS,
                                            "description": "The choice as shown; a chosen label comes back in selected.",
                                        },
                                        "description": {
                                            "type": "string", "maxLength": MAX_DESCRIPTION_CHARS,
                                            "description": "Optional explanation shown with the choice.",
                                        },
                                    },
                                    "required": ["label"],
                                    "additionalProperties": False,
                                },
                            },
                            "multi_select": {
                                "type": "boolean",
                                "description": "true lets the user pick several options; default false, one option.",
                            },
                        },
                        "required": ["id", "question"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
        fn=_ask_user_question,
        timeout=None,
        risk="read",
        approval="never",
        group="core",
        max_calls_per_turn=_CALLS_PER_TURN,
    ))
