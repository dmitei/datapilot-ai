from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    command: str
    title: str
    description: str
    query: str
    monthly: bool = True


YEAR_FILTER = "month >= make_date($1, 1, 1) AND month < make_date($1 + 1, 1, 1)"

METRICS = {
    "monthly_revenue": Metric("revenue", "Выручка по месяцам",
        "Динамика выручки или доходов компаний по месяцам.",
        f"""SELECT to_char(month, 'MM') AS label, sum(revenue_rub) AS value
            FROM monthly_finance WHERE {YEAR_FILTER} GROUP BY month ORDER BY month"""),
    "monthly_expenses": Metric("expenses", "Расходы по месяцам",
        "Динамика расходов или затрат по месяцам.",
        f"""SELECT to_char(month, 'MM') AS label, sum(expenses_rub) AS value
            FROM monthly_finance WHERE {YEAR_FILTER} GROUP BY month ORDER BY month"""),
    "monthly_profit": Metric("profit", "Прибыль по месяцам",
        "Динамика прибыли по месяцам. Прибыль равна выручке минус расходы.",
        f"""SELECT to_char(month, 'MM') AS label,
                sum(revenue_rub - expenses_rub) AS value
            FROM monthly_finance WHERE {YEAR_FILTER} GROUP BY month ORDER BY month"""),
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

    def as_text(self) -> str:
        lines = [f"{self.metric.title} · {self.year} (₽)"]
        lines.extend(f"{label} — {value:,}".replace(",", " ") for label, value in self.rows)
        return "\n".join(lines) if self.rows else f"{lines[0]}\nДанных нет."


async def run_tool(pool, name: str, arguments: dict) -> Report:
    if name not in METRICS:
        raise ValueError("Неизвестный инструмент.")
    if not isinstance(arguments, dict) or set(arguments) != {"year"}:
        raise ValueError("Инструмент принимает только год.")
    year = arguments["year"]
    if type(year) is not int or year not in (2024, 2025):
        raise ValueError("Данные доступны за 2024 и 2025 годы.")

    metric = METRICS[name]
    async with pool.acquire() as connection:
        async with connection.transaction(readonly=True):
            records = await connection.fetch(metric.query, year)
    return Report(metric, year, [(row["label"], int(row["value"])) for row in records])
