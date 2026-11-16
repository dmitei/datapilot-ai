import argparse
import asyncio
import logging
import os
from io import BytesIO
from pathlib import Path

import asyncpg
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

from .analytics import run_tool
from .assistant import HELP, select_tool
from .charts import render_png
from .import_db import MAX_FILE_BYTES, SCHEMA, latest_uploaded_year, read_sqlite, replace_dataset


async def report_for(question, pool, client, model):
    year = await latest_uploaded_year(pool)
    if year is None:
        raise ValueError("Данных пока нет. Отправь боту файл SQLite (.db).")
    name, arguments = await select_tool(question, client, model, default_year=year)
    return await run_tool(pool, name, arguments)


async def serve_bot(token, pool, client, model, owner_id):
    dispatcher = Dispatcher()

    def is_owner(message: Message) -> bool:
        return (message.chat.type == "private" and message.from_user is not None
                and message.from_user.id == owner_id)

    @dispatcher.message(Command("id"), F.chat.type == "private")
    async def show_id(message: Message):
        if message.from_user is not None:
            await message.answer(f"Твой Telegram ID: {message.from_user.id}")

    @dispatcher.message(Command("start", "help"), F.chat.type == "private")
    async def show_help(message: Message):
        if is_owner(message):
            await message.answer(HELP + "\n\nПришли .db как файл, чтобы загрузить или заменить данные.")
        elif owner_id is None:
            await message.answer("Для настройки доступа отправь /id и запиши число в BOT_OWNER_ID.")
        else:
            await message.answer("Отчёты и импорт доступны владельцу бота.")

    @dispatcher.message(F.document, F.chat.type == "private")
    async def handle_document(message: Message, bot: Bot):
        if not is_owner(message):
            await message.answer("Для загрузки файла настрой доступ через /start.")
            return
        document = message.document
        if Path(document.file_name or "").suffix.lower() != ".db":
            await message.answer("Пришли SQLite-базу с расширением .db.")
            return
        if document.file_size is None or not 0 < document.file_size <= MAX_FILE_BYTES:
            await message.answer("Файл должен быть размером до 5 МБ.")
            return
        try:
            with BytesIO() as buffer:
                await bot.download(document, destination=buffer, timeout=30)
                companies, finance = await asyncio.to_thread(read_sqlite, buffer.getvalue())
            await replace_dataset(pool, companies, finance)
        except ValueError as error:
            await message.answer(str(error))
            return
        except (TelegramAPIError, asyncpg.PostgresError, OSError, TimeoutError):
            await message.answer("Импорт не завершился. Попробуй отправить файл ещё раз.")
            return
        await message.answer(
            f"База загружена. Компаний: {len(companies)}, строк: {len(finance)}. "
            "Теперь можно задавать вопросы."
        )

    @dispatcher.message(F.text, F.chat.type == "private")
    async def handle_question(message: Message):
        if not is_owner(message):
            return
        try:
            report = await report_for(message.text, pool, client, model)
            png = await asyncio.to_thread(render_png, report) if report.rows else None
        except ValueError as error:
            await message.answer(str(error))
            return
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await message.answer("База данных временно недоступна. Попробуй позже.")
            return

        await message.answer(report.as_text())
        if png:
            await message.answer_photo(BufferedInputFile(png, filename="report.png"))

    async with Bot(token) as bot:
        await dispatcher.start_polling(bot, close_bot_session=False,
                                       tasks_concurrency_limit=8)


async def main(args):
    load_dotenv()
    mode = os.getenv("LLM_MODE", "demo").strip().lower()
    key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    token = os.getenv("BOT_TOKEN", "").strip()
    if mode not in {"demo", "llm"}:
        raise ValueError("LLM_MODE должен быть demo или llm.")
    if mode == "llm" and not (key and model):
        raise ValueError("Для режима llm укажи LLM_API_KEY и LLM_MODEL.")
    raw_owner = os.getenv("BOT_OWNER_ID", "").strip()
    if raw_owner and (not raw_owner.isascii() or not raw_owner.isdecimal() or int(raw_owner) <= 0):
        raise ValueError("BOT_OWNER_ID должен быть числом из команды /id.")
    owner_id = int(raw_owner) if raw_owner else None
    if args.question is None and args.import_db is None and not token:
        raise ValueError("Для Telegram укажи BOT_TOKEN в .env или передай вопрос в CLI.")

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    database_url = os.getenv(
        "DATABASE_URL", "postgresql://datapilot:datapilot@127.0.0.1:5433/datapilot"
    )
    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {key}"} if mode == "llm" else {},
        timeout=15, trust_env=False,
    ) as http, asyncpg.create_pool(
        database_url, min_size=1, max_size=5, timeout=5, command_timeout=5
    ) as pool:
        await pool.execute(SCHEMA)
        if args.import_db is not None:
            with args.import_db.open("rb") as source:
                companies, finance = await asyncio.to_thread(read_sqlite, source.read(MAX_FILE_BYTES + 1))
            await replace_dataset(pool, companies, finance)
            print(f"База загружена. Компаний: {len(companies)}, строк: {len(finance)}.")
            return
        client = http if mode == "llm" else None
        if args.question is None:
            await serve_bot(token, pool, client, model, owner_id)
            return
        report = await report_for(args.question, pool, client, model)
        print(report.as_text())
        if report.rows:
            args.png.write_bytes(await asyncio.to_thread(render_png, report))
            print(f"График сохранён в {args.png}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DataPilot AI")
    parser.add_argument("question", nargs="?", help="Вопрос для CLI, без Telegram")
    parser.add_argument("--import-db", type=Path, help="Загрузить локальный SQLite-файл")
    parser.add_argument("--png", type=Path, default=Path("report.png"))
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(main(parser.parse_args()))
    except TokenValidationError:
        parser.exit(1, "Ошибка: проверь формат BOT_TOKEN.\n")
    except ValueError as error:
        parser.exit(1, f"Ошибка: {error}\n")
    except (asyncpg.PostgresError, OSError, TimeoutError):
        parser.exit(1, "Ошибка: проверь подключение к PostgreSQL и доступ к файлу PNG.\n")
