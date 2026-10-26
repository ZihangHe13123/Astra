"""`/usage`: what the models cost today and in this conversation, the daily budget, and the person's prices.

    /usage                                   today's tokens (and cost, for priced models), and this conversation
    /usage budget 500000 | ¥5 | off          a daily budget in tokens or money
    /usage price MODEL INPUT OUTPUT [CACHED] prices per million tokens (off removes them)

The desktop's activity page runs the same through ``python -m agent.cli.usage_commands`` and reads JSON.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from agent.runtime.json_preferences import update_preferences
from agent.runtime.usage import UsageLedger, UsagePreferences, default_settings_path, installed_usage_ledger


def _ledger() -> UsageLedger:
    return installed_usage_ledger() or UsageLedger(preferences=UsagePreferences(default_settings_path()))


def _tokens(value: int) -> str:
    return f"{value / 1_000_000:.2f}M" if value >= 1_000_000 else f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def _money(value: float | None, currency: str) -> str:
    return "" if value is None else f"{currency}{value:.2f}" if value >= 0.01 else f"{currency}{value:.4f}"


def budget_text(budget: dict[str, float], currency: str) -> str:
    if budget.get("daily_cost"):
        return _money(budget["daily_cost"], currency)
    if budget.get("daily_tokens"):
        return f"{_tokens(int(budget['daily_tokens']))} token"
    return ""


def over_budget_message(summary: dict[str, Any]) -> str:
    spent = _tokens(summary["tokens"]) + " token"
    if summary.get("cost") is not None:
        spent += f"（约 {_money(summary['cost'], summary['currency'])}）"
    return (f"今天的模型用量 {spent} 已超过每日预算 {budget_text(summary['budget'], summary['currency'])}。"
            "定时检查暂停到明天，对话照常；可以用 /usage budget 调整预算。")


def summary_text(summary: dict[str, Any], session: dict[str, int] | None = None) -> str:
    currency = summary["currency"]
    head = f"今天（{summary['day']}）：{_tokens(summary['tokens'])} token"
    if summary.get("cost") is not None:
        head += f"，约 {_money(summary['cost'], currency)}"
        if summary.get("unpriced"):
            head += f"（未定价：{', '.join(summary['unpriced'])}）"
    budget = budget_text(summary["budget"], currency)
    if budget:
        head += f" · 每日预算 {budget}" + ("，已超过" if summary["over_budget"] else "")
    lines = [head]
    for entry in summary["models"]:
        line = (f"- {entry['model']}：输入 {_tokens(entry['prompt'])}（缓存命中 {_tokens(entry['cached'])}）"
                f"，输出 {_tokens(entry['completion'])}，{entry['requests']} 次请求")
        if entry.get("cost") is not None:
            line += f"，约 {_money(entry['cost'], currency)}"
        lines.append(line)
    if summary["sources"]:
        names = {"chat": "对话", "channel": "微信/QQ", "job": "定时任务"}
        lines.append("来源：" + "，".join(f"{names.get(source, source)} {_tokens(tokens)}"
                                        for source, tokens in sorted(summary["sources"].items(), key=lambda item: -item[1])))
    if session is not None and session["requests"]:
        lines.append(f"本会话：输入 {_tokens(session['prompt'])}，输出 {_tokens(session['completion'])}，"
                     f"{session['requests']} 次请求")
    if not summary["models"]:
        lines.append("今天还没有模型请求。")
    if not budget:
        lines.append("设预算：/usage budget 500000（token）或 /usage budget ¥5（需要先用 /usage price 填单价）。")
    return "\n".join(lines)


def set_budget(value: str) -> dict[str, float]:
    text = value.strip().lower()
    if text in {"off", "关", "关闭", "0"}:
        budget: dict[str, float] = {}
    else:
        money = re.fullmatch(r"(?:[¥$€£]|rmb|cny|usd)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|块|rmb|cny|usd|dollars?)?", text)
        if money is None:
            raise ValueError("预算写成 token 数（如 500000）或金额（如 ¥5），关闭用 off。")
        amount = float(money.group(1))
        is_money = bool(re.search(r"[¥$€£]|元|块|rmb|cny|usd|dollar", text)) or "." in text
        if amount <= 0:
            raise ValueError("预算要大于 0；关闭用 off。")
        budget = {"daily_cost": amount} if is_money else {"daily_tokens": float(int(amount))}

    def change(data: dict) -> None:
        if budget:
            data["usage_budget"] = budget
        else:
            data.pop("usage_budget", None)

    update_preferences(default_settings_path(), change)
    return budget


def set_price(model: str, numbers: list[str]) -> dict[str, float] | None:
    model = model.strip()
    if not model or len(model) > 200:
        raise ValueError("写上模型名，如 /usage price deepseek-chat 2 8 0.5。")
    if numbers in (["off"], ["关"]):
        price = None
    else:
        try:
            values = [float(number) for number in numbers]
        except ValueError:
            raise ValueError("单价按每百万 token 写数字：输入 输出 [缓存命中]。") from None
        if len(values) not in {2, 3} or any(value < 0 for value in values):
            raise ValueError("单价按每百万 token 写数字：输入 输出 [缓存命中]。")
        price = {"input": values[0], "output": values[1], **({"cached_input": values[2]} if len(values) == 3 else {})}

    def change(data: dict) -> None:
        stored = data.get("model_prices")
        prices: dict[str, Any] = stored if isinstance(stored, dict) else {}
        if price is None:
            prices.pop(model, None)
        else:
            prices[model] = price
        data["model_prices"] = prices

    update_preferences(default_settings_path(), change)
    return price


def execute_usage_command(args: str, *, session: str = "") -> tuple[str, str]:
    """The text a person reads for /usage, and an error if the arguments were not understood."""
    parts = args.split()
    try:
        if parts and parts[0] == "budget" and len(parts) >= 2:
            budget = set_budget(" ".join(parts[1:]))
            ledger = _ledger()
            return ("已关闭每日预算。" if not budget else
                    f"每日预算设为 {budget_text(budget, ledger.preferences.currency() if ledger.preferences else '¥')}。"
                    "超过后定时检查暂停到第二天，对话照常。"), ""
        if parts and parts[0] == "price" and len(parts) >= 3:
            price = set_price(parts[1], parts[2:])
            return (f"已删除 {parts[1]} 的单价。" if price is None else
                    f"{parts[1]} 的单价（每百万 token）：输入 {price['input']}，输出 {price['output']}"
                    + (f"，缓存命中 {price['cached_input']}" if "cached_input" in price else "") + "。"), ""
        if parts:
            return "", "用法：/usage、/usage budget 500000|¥5|off、/usage price 模型 输入 输出 [缓存命中]"
        ledger = _ledger()
        return summary_text(ledger.day(), ledger.session(session) if session else None), ""
    except (ValueError, OSError) as exc:
        return "", str(exc)


def main(argv: list[str] | None = None) -> int:
    """For the desktop: `today` prints the day's JSON; `budget VALUE` and `price MODEL ...` change settings."""
    args = sys.argv[1:] if argv is None else argv
    try:
        if args in ([], ["today"]):
            print(json.dumps({"ok": True, "usage": _ledger().day()}))
            return 0
        if len(args) >= 2 and args[0] == "budget":
            print(json.dumps({"ok": True, "budget": set_budget(" ".join(args[1:]))}))
            return 0
        if len(args) >= 3 and args[0] == "price":
            print(json.dumps({"ok": True, "price": set_price(args[1], args[2:])}))
            return 0
        print(json.dumps({"ok": False, "error": "Unsupported usage command."}))
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
