import json
import re

import httpx

from .analytics import METRICS


HELP = (
    "DataPilot AI · синтетические данные за 2024 и 2025 годы.\n"
    "По умолчанию используется 2025 год.\n\n"
    "/revenue 2025 — выручка\n"
    "/expenses 2025 — расходы\n"
    "/profit 2025 — прибыль\n"
    "/top 2025 — топ клиентов\n"
    "/loans 2025 — кредитный портфель по отраслям\n\n"
    "Пример обычного вопроса: «Покажи выручку по месяцам за 2024»."
)
COMMANDS = {metric.command: name for name, metric in METRICS.items()}
DEMO_PATTERNS = {
    "monthly_revenue": r"выруч|доход",
    "monthly_expenses": r"расход|затрат",
    "monthly_profit": r"прибыл",
    "top_clients": r"топ|крупнейш|лидер",
    "industry_loans": r"кредит|задолж|долг",
}
TOOLS = [
    {"type": "function", "function": {
        "name": name,
        "description": metric.description,
        "parameters": {
            "type": "object",
            "properties": {"year": {"type": "integer", "enum": [2024, 2025]}},
            "required": ["year"],
            "additionalProperties": False,
        },
    }}
    for name, metric in METRICS.items()
]


def requested_year(question: str) -> int:
    years = set(re.findall(r"(?<!\d)\d{4}(?!\d)", question))
    if len(years) > 1 or (years and not years <= {"2024", "2025"}):
        raise ValueError("Укажи один год: 2024 или 2025.")
    return int(next(iter(years), "2025"))


def route_demo(question: str) -> str:
    if re.search(DEMO_PATTERNS["top_clients"], question):
        if not any(re.search(DEMO_PATTERNS[name], question)
                   for name in ("monthly_expenses", "monthly_profit", "industry_loans")):
            return "top_clients"
    matches = [name for name, pattern in DEMO_PATTERNS.items()
               if re.search(pattern, question)]
    if len(matches) != 1:
        raise ValueError(HELP)
    return matches[0]


async def select_tool(question: str, client: httpx.AsyncClient | None,
                      model: str) -> tuple[str, dict]:
    question = question.strip().lower()
    if not question or len(question) > 1000:
        raise ValueError("Введи вопрос длиной до 1000 символов.")
    year = requested_year(question)
    if question.startswith("/"):
        command, *args = question.split()
        command = command[1:].split("@", 1)[0]
        if command not in COMMANDS or (args and args != [str(year)]):
            raise ValueError(HELP)
        return COMMANDS[command], {"year": year}
    if client is None:
        return route_demo(question), {"year": year}

    try:
        response = await client.post("chat/completions", json={
            "model": model,
            "messages": [
                {"role": "system", "content":
                 f"Выбирай ровно один инструмент для понятного запроса. Год {year}. "
                 "При неподдерживаемом или неоднозначном запросе не вызывай инструменты."},
                {"role": "user", "content": question},
            ],
            "tools": TOOLS,
            "tool_choice": "auto",
        })
        response.raise_for_status()
        calls = response.json()["choices"][0]["message"].get("tool_calls") or []
        if len(calls) != 1:
            raise ValueError
        function = calls[0]["function"]
        arguments = json.loads(function["arguments"])
        if calls[0]["type"] != "function" or function["name"] not in METRICS:
            raise ValueError
        if arguments != {"year": year} or type(arguments["year"]) is not int:
            raise ValueError
        return function["name"], arguments
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise ValueError("LLM не выбрала доступный отчёт. Попробуй команду из /help.") from None
