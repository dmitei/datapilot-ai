import json
import os
import unittest

import asyncpg
import httpx

from datapilot.analytics import METRICS, Report, run_tool
from datapilot.assistant import select_tool
from datapilot.charts import render_png


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_routes_all_five_reports(self):
        examples = {
            "Динамика доходов за 2024": "monthly_revenue",
            "Расходы за 2024": "monthly_expenses",
            "Прибыль за 2024": "monthly_profit",
            "Топ клиентов по выручке за 2024": "top_clients",
            "Кредитный портфель за 2024": "industry_loans",
        }
        for question, expected in examples.items():
            with self.subTest(question=question):
                self.assertEqual(await select_tool(question, None, ""), (expected, {"year": 2024}))

    async def test_command_bypasses_llm_and_defaults_to_2025(self):
        self.assertEqual(await select_tool("/profit@mybot", object(), "mock"),
                         ("monthly_profit", {"year": 2025}))

    async def test_rejects_unknown_or_ambiguous_requests(self):
        for question in ("Привет", "Расходы за 2023", "Выручка и прибыль", "/top 2024 2025"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                await select_tool(question, None, "")

    async def test_llm_tool_call_and_rejects_injected_arguments(self):
        cases = [
            ("monthly_revenue", '{"year": 2024}', True),
            ("monthly_revenue", '{"year": 2025}', False),
            ("monthly_revenue", '{"year": 2024, "sql": "DROP TABLE companies"}', False),
            ("run_sql", '{"year": 2024}', False),
        ]
        for name, arguments, accepted in cases:
            with self.subTest(name=name, arguments=arguments):
                def respond(request):
                    self.assertEqual(request.url.path, "/v1/chat/completions")
                    body = json.loads(request.content)
                    self.assertEqual(len(body["tools"]), 5)
                    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
                        {"id": "call_demo", "type": "function",
                         "function": {"name": name, "arguments": arguments}}
                    ]}}]})

                async with httpx.AsyncClient(
                    base_url="https://example.invalid/v1", transport=httpx.MockTransport(respond)
                ) as client:
                    if accepted:
                        self.assertEqual(await select_tool("Выручка за 2024", client, "mock"),
                                         (name, {"year": 2024}))
                    else:
                        with self.assertRaises(ValueError):
                            await select_tool("Выручка за 2024", client, "mock")


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_runs_before_database_access(self):
        for arguments in ({"year": True}, {"year": "2025"}, {"year": 2023}, {"sql": "SELECT 1"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await run_tool(None, "monthly_profit", arguments)

    def test_png_signatures(self):
        for metric in (METRICS["monthly_profit"], METRICS["industry_loans"]):
            report = Report(metric, 2025, [("ИТ", 2000000), ("Логистика", 3000000)])
            self.assertIn("2 000 000", report.as_text())
            self.assertTrue(render_png(report).startswith(b"\x89PNG\r\n\x1a\n"))

    @unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Укажи TEST_DATABASE_URL для проверки PostgreSQL")
    async def test_postgres_queries_for_both_years(self):
        async with asyncpg.create_pool(os.environ["TEST_DATABASE_URL"], min_size=1, max_size=2) as pool:
            for year in (2024, 2025):
                reports = {name: await run_tool(pool, name, {"year": year}) for name in METRICS}
                revenue = dict(reports["monthly_revenue"].rows)
                expenses = dict(reports["monthly_expenses"].rows)
                self.assertEqual(list(revenue), [f"{month:02d}" for month in range(1, 13)])
                self.assertEqual(dict(reports["monthly_profit"].rows),
                                 {month: revenue[month] - expenses[month] for month in revenue})
                top = [value for _, value in reports["top_clients"].rows]
                self.assertEqual(len(top), 5)
                self.assertEqual(top, sorted(top, reverse=True))
                loans = dict(reports["industry_loans"].rows)
                self.assertEqual(set(loans), {"ИТ", "Логистика", "Производство"})
                self.assertEqual(sum(loans.values()), 51_300_000 - 3_000_000 * (year - 2024))
