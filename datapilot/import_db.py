import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
MAX_AMOUNT = 10**12
SCHEMA = """CREATE TABLE IF NOT EXISTS dataset_import (
    id boolean PRIMARY KEY CHECK (id),
    imported_at timestamptz NOT NULL DEFAULT now()
)"""


def read_sqlite(payload: bytes):
    if not 0 < len(payload) <= MAX_FILE_BYTES:
        raise ValueError("Размер файла должен быть от 1 байта до 5 МБ.")
    if not payload.startswith(b"SQLite format 3\x00"):
        raise ValueError("Файл должен быть базой SQLite.")

    try:
        with TemporaryDirectory(prefix="datapilot-") as directory:
            path = Path(directory) / "upload.db"
            path.write_bytes(payload)
            connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
                tables = {row[1] for row in connection.execute("PRAGMA main.table_list")
                          if row[2] == "table"}
                if not {"companies", "monthly_finance"} <= tables:
                    raise ValueError("В файле нужны таблицы companies и monthly_finance.")
                companies = connection.execute(
                    "SELECT id, name, industry FROM main.companies LIMIT ?", (MAX_ROWS + 1,)
                ).fetchall()
                finance = connection.execute(
                    """SELECT company_id, month, revenue_rub, expenses_rub, loan_balance_rub
                       FROM main.monthly_finance LIMIT ?""", (MAX_ROWS + 1,)
                ).fetchall()
            finally:
                connection.close()
    except sqlite3.Error:
        raise ValueError("Не удалось прочитать таблицы SQLite. Сверь столбцы с README.") from None

    if not companies or not finance or max(len(companies), len(finance)) > MAX_ROWS:
        raise ValueError("В каждой таблице должно быть от 1 до 20 000 строк.")

    company_ids = set()
    for company_id, name, industry in companies:
        if (type(company_id) is not int or not 1 <= company_id <= 2**31 - 1
                or company_id in company_ids or not valid_label(name) or not valid_label(industry)):
            raise ValueError("Проверь уникальность ID компаний, названия и отрасли.")
        company_ids.add(company_id)

    rows = []
    keys = set()
    for company_id, raw_month, revenue, expenses, loans in finance:
        try:
            month = date.fromisoformat(raw_month)
        except (TypeError, ValueError):
            raise ValueError("Месяц должен быть записан как ГГГГ-ММ-01.") from None
        if (month.isoformat() != raw_month or month.day != 1 or not 1900 <= month.year <= 2099):
            raise ValueError("Месяц должен быть записан как ГГГГ-ММ-01, год от 1900 до 2099.")
        key = (company_id, month)
        if type(company_id) is not int or company_id not in company_ids or key in keys:
            raise ValueError("Проверь ID компаний и уникальность строки для каждого месяца.")
        if any(type(amount) is not int or not 0 <= amount <= MAX_AMOUNT
               for amount in (revenue, expenses, loans)):
            raise ValueError("Денежные показатели должны быть целыми рублями от 0 до 1 трлн.")
        keys.add(key)
        rows.append((company_id, month, revenue, expenses, loans))

    return companies, rows


def valid_label(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 100 and value.isprintable()


async def replace_dataset(pool, companies, finance):
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("LOCK TABLE dataset_import IN EXCLUSIVE MODE")
            await connection.execute("DELETE FROM monthly_finance")
            await connection.execute("DELETE FROM companies")
            await connection.copy_records_to_table(
                "companies", records=companies, columns=("id", "name", "industry")
            )
            await connection.copy_records_to_table(
                "monthly_finance", records=finance,
                columns=("company_id", "month", "revenue_rub", "expenses_rub", "loan_balance_rub")
            )
            await connection.execute(
                """INSERT INTO dataset_import (id) VALUES (TRUE)
                   ON CONFLICT (id) DO UPDATE SET imported_at = now()"""
            )


async def latest_uploaded_year(pool):
    return await pool.fetchval(
        """SELECT EXTRACT(YEAR FROM max(month))::integer
           FROM monthly_finance WHERE EXISTS (SELECT 1 FROM dataset_import)"""
    )
