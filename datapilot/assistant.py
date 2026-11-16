import json
import re

import httpx

from .analytics import METRICS, validate_arguments


HELP = (
    "DataPilot AI · отчёты по загруженной базе SQLite.\n"
    "По умолчанию используется последний год в базе.\n\n"
    "/revenue 2025 — выручка\n"
    "/expenses 2025 — расходы\n"
    "/profit 2025 — прибыль\n"
    "/top 2025 — топ клиентов\n"
    "/loans 2025 — кредитный портфель по отраслям\n\n"
    "Пример: «Покажи общую выручку за первые 6 месяцев 2025»."
)
COMMANDS = {metric.command: name for name, metric in METRICS.items()}
DEMO_PATTERNS = {
    "monthly_revenue": r"выруч|доход",
    "monthly_expenses": r"расход|затрат",
    "monthly_profit": r"прибыл",
    "top_clients": r"топ|крупнейш|лидер",
    "industry_loans": r"кредит|задолж|долг",
}
MONTH_PROPERTIES = {
    "month_from": {"type": "integer", "minimum": 1, "maximum": 12,
                   "description": "Первый месяц периода, включительно. По умолчанию 1."},
    "month_to": {"type": "integer", "minimum": 1, "maximum": 12,
                 "description": "Последний месяц периода, включительно. По умолчанию 12."},
    "show_total": {"type": "boolean", "description": "Показать сумму за указанный период."},
}
TOOLS = [
    {"type": "function", "function": {
        "name": name,
        "description": metric.description,
        "parameters": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "minimum": 1900, "maximum": 2099},
                **(MONTH_PROPERTIES if metric.monthly else {}),
            },
            "required": ["year"],
            "additionalProperties": False,
        },
    }}
    for name, metric in METRICS.items()
]


def requested_year(question: str, default_year: int) -> int:
    years = set(re.findall(r"(?<!\d)\d{4}(?!\d)", question))
    if len(years) > 1 or (years and not all(1900 <= int(year) <= 2099 for year in years)):
        raise ValueError("Укажи один год от 1900 до 2099.")
    return int(next(iter(years), default_year))


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


def demo_arguments(question: str, year: int) -> dict:
    arguments = {"year": year}
    span = re.search(r"(первые|последние)\s+(\d{1,2})\s+месяц", question)
    half = re.search(r"(первое|второе|1[-–]?е|2[-–]?е)\s+полугодие", question)
    quarter = re.search(r"\b([1-4])(?:[-–]?(?:й|ый))?\s+квартал", question)
    if span:
        count = int(span[2])
        if not 1 <= count <= 12:
            raise ValueError("Укажи число месяцев от 1 до 12.")
        if span[1] == "первые":
            arguments.update(month_from=1, month_to=count)
        else:
            arguments.update(month_from=13 - count, month_to=12)
    elif half:
        first = 1 if half[1] in {"первое", "1-е", "1е", "1–е"} else 7
        arguments.update(month_from=first, month_to=first + 5)
    elif quarter:
        last = int(quarter[1]) * 3
        arguments.update(month_from=last - 2, month_to=last)
    elif re.search(r"месяц|квартал|полугод", question) and not re.search(
        r"по месяцам|помесячно|каждый месяц", question
    ):
        raise ValueError("Укажи период, например «первые 6 месяцев» или «2-й квартал».")

    if re.search(r"общ(?:ая|ую|ий|ие)|итог|суммар|сумм[ау]|всего", question):
        arguments["show_total"] = True
    return arguments


async def select_tool(question: str, client: httpx.AsyncClient | None,
                      model: str, default_year: int = 2025) -> tuple[str, dict]:
    question = question.strip().lower()
    if not question or len(question) > 1000:
        raise ValueError("Введи вопрос длиной до 1000 символов.")
    year = requested_year(question, default_year)
    if question.startswith("/"):
        command, *args = question.split()
        command = command[1:].split("@", 1)[0]
        if command not in COMMANDS or (args and args != [str(year)]):
            raise ValueError(HELP)
        return COMMANDS[command], {"year": year}
    if client is None:
        name = route_demo(question)
        arguments = demo_arguments(question, year)
        if not METRICS[name].monthly:
            if "month_from" in arguments:
                raise ValueError("Для этого отчёта доступен только годовой период.")
            arguments.pop("show_total", None)
        validate_arguments(name, arguments)
        return name, arguments

    try:
        response = await client.post("chat/completions", json={
            "model": model,
            "messages": [
                {"role": "system", "content":
                 f"Выбирай ровно один инструмент для понятного запроса. Год {year}. "
                 "Для выручки, расходов и прибыли передавай границы периода в месяцах, "
                 "если пользователь указал период, и show_total=true для общей суммы. "
                 "Первые N месяцев — месяцы с 1 по N включительно. "
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
        if validate_arguments(function["name"], arguments)[0] != year:
            raise ValueError
        return function["name"], arguments
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise ValueError("LLM не выбрала доступный отчёт. Попробуй команду из /help.") from None
