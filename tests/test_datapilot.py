import json
import os
import unittest
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx

from datapilot.analytics import METRICS, Report, run_tool
from datapilot.assistant import select_tool
from datapilot.charts import render_png
from datapilot.import_db import latest_uploaded_year, read_dataset, replace_dataset
from datapilot.storage import SCHEMA, telegram_schema


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DB = ROOT / "examples" / "example.db"
CSV_HEADER = "company_id,name,industry,month,revenue_rub,expenses_rub,loan_balance_rub"
CSV_ROW = "1,Вектор Софт,ИТ,2026-01-01,2500000,1800000,4000000"


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

    async def test_demo_routes_partial_year_total(self):
        self.assertEqual(
            await select_tool("покажи общую выручку за первые 6 месяцев 2025", None, ""),
            ("monthly_revenue", {"year": 2025, "month_from": 1, "month_to": 6,
                                 "show_total": True}),
        )

    async def test_command_bypasses_llm_and_defaults_to_2025(self):
        self.assertEqual(await select_tool("/profit@mybot", object(), "mock"),
                         ("monthly_profit", {"year": 2025}))

    async def test_rejects_unknown_or_ambiguous_requests(self):
        for question in ("Привет", "Расходы за 1800", "Выручка и прибыль", "/top 2024 2025"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                await select_tool(question, None, "")

    async def test_llm_tool_call_and_rejects_injected_arguments(self):
        cases = [
            ("monthly_revenue", '{"year": 2024}', True),
            ("monthly_revenue", '{"year": 2024, "month_from": 1, "month_to": 6, "show_total": true}', True),
            ("monthly_revenue", '{"year": 2024, "month_from": 7, "month_to": 6}', False),
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
                                         (name, json.loads(arguments)))
                    else:
                        with self.assertRaises(ValueError):
                            await select_tool("Выручка за 2024", client, "mock")


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_runs_before_database_access(self):
        for arguments in ({"year": True}, {"year": "2025"}, {"year": 1800}, {"sql": "SELECT 1"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await run_tool(None, "monthly_profit", arguments)

    def test_png_signatures(self):
        for metric in (METRICS["monthly_profit"], METRICS["industry_loans"]):
            report = Report(metric, 2025, [("ИТ", 2000000), ("Логистика", 3000000)])
            self.assertIn("2 000 000", report.as_text())
            self.assertTrue(render_png(report).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_reads_sqlite_example_and_rejects_invalid_file(self):
        companies, rows = read_dataset(EXAMPLE_DB.read_bytes(), ".db")
        self.assertEqual((len(companies), len(rows)), (6, 144))
        with self.assertRaisesRegex(ValueError, "SQLite"):
            read_dataset(b"not a database", ".db")

    def test_reads_csv_with_both_delimiters_and_utf8_bom(self):
        csv_text = f"{CSV_HEADER}\n{CSV_ROW}\n"
        for delimiter in (",", ";"):
            with self.subTest(delimiter=delimiter):
                payload = csv_text.replace(",", delimiter).encode("utf-8-sig")
                companies, rows = read_dataset(payload, ".csv")
                self.assertEqual(companies, [(1, "Вектор Софт", "ИТ")])
                self.assertEqual(rows[0][1].isoformat(), "2026-01-01")

    def test_rejects_inconsistent_csv_rows(self):
        for rows in (f"{CSV_ROW}\n{CSV_ROW}", CSV_ROW.replace("2500000", "abc"),
                     f"{CSV_ROW}\n{CSV_ROW.replace('Вектор Софт', 'Другая').replace('01-01', '02-01')}"):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                read_dataset(f"{CSV_HEADER}\n{rows}\n".encode(), ".csv")

    @unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Укажи TEST_DATABASE_URL для проверки PostgreSQL")
    async def test_import_and_postgres_queries_for_both_years(self):
        url = os.environ["TEST_DATABASE_URL"]
        schema = "test_datapilot_" + uuid4().hex
        first_user = uuid4().int % (2**62) + 1
        second_user = first_user + 1
        admin = await asyncpg.connect(url)
        try:
            await admin.execute(f"CREATE SCHEMA {schema}")
            async with asyncpg.create_pool(
                url, min_size=1, max_size=2, server_settings={"search_path": schema}
            ) as pool:
                await self.verify_import_and_reports(pool)
                await self.verify_telegram_isolation(pool, first_user, second_user)
        finally:
            try:
                for name in (schema, telegram_schema(first_user), telegram_schema(second_user)):
                    await admin.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
            finally:
                await admin.close()

    async def verify_import_and_reports(self, pool):
        await pool.execute((ROOT / "init.sql").read_text())
        await pool.execute(SCHEMA)
        self.assertIsNone(await latest_uploaded_year(pool))
        self.assertEqual(await pool.fetchval("SELECT count(*) FROM companies"), 0)
        companies, rows = read_dataset(EXAMPLE_DB.read_bytes(), ".db")
        await replace_dataset(pool, companies, rows)
        self.assertEqual(await latest_uploaded_year(pool), 2025)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await replace_dataset(pool, companies[:1], rows)
        self.assertEqual(await pool.fetchval("SELECT count(*) FROM monthly_finance"), 144)
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

        first_half = await run_tool(pool, "monthly_revenue", {
            "year": 2025, "month_from": 1, "month_to": 6, "show_total": True,
        })
        self.assertEqual([label for label, _ in first_half.rows],
                         ["01", "02", "03", "04", "05", "06"])
        self.assertIn("Итого за период — 196 780 000 ₽", first_half.as_text())
        csv_dataset = read_dataset(f"{CSV_HEADER}\n{CSV_ROW}\n".encode(), ".csv")
        await replace_dataset(pool, *csv_dataset)
        self.assertEqual(await pool.fetchval("SELECT count(*) FROM monthly_finance"), 1)
        self.assertEqual(await latest_uploaded_year(pool), 2026)
        report = await run_tool(pool, "monthly_revenue", {"year": 2026})
        self.assertEqual(report.rows, [("01", 2_500_000)])

    async def verify_telegram_isolation(self, pool, first_user, second_user):
        question = {"year": 2026}
        first_csv = f"{CSV_HEADER}\n{CSV_ROW}\n"
        second_csv = first_csv.replace("2500000", "7500000")
        first_data = read_dataset(first_csv.encode(), ".csv")
        second_data = read_dataset(second_csv.encode(), ".csv")

        self.assertIsNone(await latest_uploaded_year(pool, first_user))
        self.assertIsNone(await latest_uploaded_year(pool, second_user))
        await replace_dataset(pool, *first_data, user_id=first_user)
        self.assertIsNone(await latest_uploaded_year(pool, second_user))
        report = await run_tool(pool, "monthly_revenue", question, second_user)
        self.assertEqual(report.rows, [])
        await replace_dataset(pool, *second_data, user_id=second_user)

        report = await run_tool(pool, "monthly_revenue", question, first_user)
        self.assertEqual(report.rows, [("01", 2_500_000)])
        report = await run_tool(pool, "monthly_revenue", question, second_user)
        self.assertEqual(report.rows, [("01", 7_500_000)])

        sqlite_data = read_dataset(EXAMPLE_DB.read_bytes(), ".db")
        await replace_dataset(pool, *sqlite_data, user_id=first_user)
        self.assertEqual(await latest_uploaded_year(pool, first_user), 2025)
        self.assertEqual(await latest_uploaded_year(pool, second_user), 2026)
        report = await run_tool(pool, "monthly_revenue", question, second_user)
        self.assertEqual(report.rows, [("01", 7_500_000)])
