from contextlib import asynccontextmanager
from pathlib import Path


SCHEMA = """CREATE TABLE IF NOT EXISTS dataset_import (
    id boolean PRIMARY KEY CHECK (id),
    imported_at timestamptz NOT NULL DEFAULT now()
)"""


def telegram_schema(user_id: int) -> str:
    if type(user_id) is not int or not 1 <= user_id < 2**63:
        raise ValueError("Некорректный Telegram ID.")
    return f"telegram_{user_id}"


@asynccontextmanager
async def dataset_connection(pool, user_id=None, *, writing=False):
    async with pool.acquire() as connection:
        async with connection.transaction(readonly=not writing):
            if user_id is not None:
                schema = telegram_schema(user_id)
                if writing:
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", schema
                    )
                    await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                elif not await connection.fetchval("SELECT to_regnamespace($1) IS NOT NULL", schema):
                    yield None
                    return

                # Limit name resolution to this user's schema for the current transaction.
                await connection.execute("SELECT set_config('search_path', $1, true)", schema)
                if writing:
                    init_sql = Path(__file__).resolve().parents[1] / "init.sql"
                    await connection.execute(init_sql.read_text(encoding="utf-8"))
                    await connection.execute(SCHEMA)

            yield connection
