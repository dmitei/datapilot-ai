from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    command: str
    title: str
    description: str
    query: str
    monthly: bool = True


YEAR_FILTER = "month >= make_date($1, 1, 1) AND month < make_date($1 + 1, 1, 1)"
MONTH_FILTER = "month BETWEEN make_date($1, $2, 1) AND make_date($1, $3, 1)"

METRICS = {
    "monthly_revenue": Metric("revenue", "Выручка по месяцам",
        "Динамика выручки или доходов компаний по месяцам.",
        f"""SELECT to_char(month, 'MM') AS label, sum(revenue_rub) AS value
            FROM monthly_finance WHERE {MONTH_FILTER} GROUP BY month ORDER BY month"""),
    "monthly_expenses": Metric("expenses", "Расходы по месяцам",
        "Динамика расходов или затрат по месяцам.",
        f"""SELECT to_char(month, 'MM') AS label, sum(expenses_rub) AS value
            FROM monthly_finance WHERE {MONTH_FILTER} GROUP BY month ORDER BY month"""),
    "monthly_profit": Metric("profit", "Прибыль по месяцам",
        "Динамика прибыли по месяцам. Прибыль равна выручке минус расходы.",
        f"""SELECT to_char(month, 'MM') AS label,
                sum(revenue_rub - expenses_rub) AS value
            FROM monthly_finance WHERE {MONTH_FILTER} GROUP BY month ORDER BY month"""),
    "top_clients": Metric("top", "Топ-5 клиентов по выручке",
        "Пять клиентов с наибольшей суммарной выручкой за год.",
        f"""SELECT c.name AS label, sum(f.revenue_rub) AS value
            FROM monthly_finance f JOIN companies c ON c.id = f.company_id
            WHERE {YEAR_FILTER} GROUP BY c.id
            ORDER BY value DESC, c.id LIMIT 5""", monthly=False),
    "industry_loans": Metric("loans", "Кредитный портфель по отраслям на конец года",
        "Остаток кредитов по отраслям на конец декабря указанного года.",
        """SELECT c.industry AS label, sum(f.loan_balance_rub) AS value
           FROM monthly_finance f JOIN companies c ON c.id = f.company_id
           WHERE month = make_date($1, 12, 1)
           GROUP BY c.industry ORDER BY value DESC, c.industry""", monthly=False),
}


@dataclass(frozen=True)
class Report:
    metric: Metric
    year: int
    rows: list[tuple[str, int]]

    month_from: int = 1
    month_to: int = 12
    show_total: bool = False

    @property
    def period(self) -> str:
        if (self.month_from, self.month_to) == (1, 12):
            return str(self.year)
        return f"{self.month_from:02d}–{self.month_to:02d}.{self.year}"

    def as_text(self) -> str:
        lines = [f"{self.metric.title} · {self.period} (₽)"]
        if self.show_total and self.rows:
            total = f"{sum(value for _, value in self.rows):,}".replace(",", " ")
            lines.append(f"Итого за период — {total} ₽")
        lines.extend(f"{label} — {value:,}".replace(",", " ") for label, value in self.rows)
        return "\n".join(lines) if self.rows else f"{lines[0]}\nДанных нет."


def validate_arguments(name: str, arguments: dict) -> tuple[int, int, int, bool]:
    if name not in METRICS:
        raise ValueError("Неизвестный инструмент.")
    allowed = {"year"}
    if METRICS[name].monthly:
        allowed.update({"month_from", "month_to", "show_total"})
    if not isinstance(arguments, dict) or "year" not in arguments or arguments.keys() - allowed:
        raise ValueError("Недопустимые параметры отчёта.")
    year = arguments["year"]
    if type(year) is not int or not 1900 <= year <= 2099:
        raise ValueError("Укажи год от 1900 до 2099.")
    first = arguments.get("month_from", 1)
    last = arguments.get("month_to", 12)
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= 12:
        raise ValueError("Укажи корректный диапазон месяцев от 1 до 12.")
    show_total = arguments.get("show_total", False)
    if type(show_total) is not bool:
        raise ValueError("Недопустимый параметр итоговой суммы.")
    return year, first, last, show_total


async def run_tool(pool, name: str, arguments: dict) -> Report:
    year, first, last, show_total = validate_arguments(name, arguments)
    metric = METRICS[name]
    params = (year, first, last) if metric.monthly else (year,)
    async with pool.acquire() as connection:
        async with connection.transaction(readonly=True):
            records = await connection.fetch(metric.query, *params)
    rows = [(row["label"], int(row["value"])) for row in records]
    return Report(metric, year, rows, first, last, show_total)
