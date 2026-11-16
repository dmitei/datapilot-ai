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
from .import_db import MAX_FILE_BYTES, latest_uploaded_year, read_dataset, replace_dataset
from .storage import SCHEMA


async def report_for(question, pool, client, model, user_id=None):
    year = await latest_uploaded_year(pool, user_id)
    if year is None:
        raise ValueError("Данных пока нет. Отправь боту файл SQLite (.db) или CSV (.csv).")
    name, arguments = await select_tool(question, client, model, default_year=year)
    return await run_tool(pool, name, arguments, user_id)


async def serve_bot(token, pool, client, model):
    dispatcher = Dispatcher()

    @dispatcher.message(Command("start", "help"), F.chat.type == "private")
    async def show_help(message: Message):
        await message.answer(
            HELP + "\n\nПришли .db или .csv как файл. Каждый пользователь видит только свои данные."
        )

    @dispatcher.message(F.document, F.chat.type == "private")
    async def handle_document(message: Message, bot: Bot):
        if message.from_user is None:
            return
        document = message.document
        extension = Path(document.file_name or "").suffix.lower()
        if extension not in {".db", ".csv"}:
            await message.answer("Пришли файл SQLite (.db) или CSV (.csv).")
            return
        if document.file_size is None or not 0 < document.file_size <= MAX_FILE_BYTES:
            await message.answer("Файл должен быть размером до 5 МБ.")
            return
        try:
            with BytesIO() as buffer:
                await bot.download(document, destination=buffer, timeout=30)
                companies, finance = await asyncio.to_thread(read_dataset, buffer.getvalue(), extension)
            await replace_dataset(pool, companies, finance, message.from_user.id)
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
        if message.from_user is None:
            return
        try:
            report = await report_for(message.text, pool, client, model, message.from_user.id)
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
        if args.question is not None or args.import_db is not None:
            await pool.execute(SCHEMA)
        if args.import_db is not None:
            with args.import_db.open("rb") as source:
                companies, finance = await asyncio.to_thread(
                    read_dataset, source.read(MAX_FILE_BYTES + 1), args.import_db.suffix.lower()
                )
            await replace_dataset(pool, companies, finance)
            print(f"База загружена. Компаний: {len(companies)}, строк: {len(finance)}.")
            return
        client = http if mode == "llm" else None
        if args.question is None:
            await serve_bot(token, pool, client, model)
            return
        report = await report_for(args.question, pool, client, model)
        print(report.as_text())
        if report.rows:
            args.png.write_bytes(await asyncio.to_thread(render_png, report))
            print(f"График сохранён в {args.png}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DataPilot AI")
    parser.add_argument("question", nargs="?", help="Вопрос для CLI, без Telegram")
    parser.add_argument("--import-db", type=Path, help="Загрузить локальный файл SQLite или CSV")
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
