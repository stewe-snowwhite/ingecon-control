"""Explicit standalone aiogram long polling; no import-time startup or migrations."""
import argparse
import asyncio
import logging
import os
import re
import sqlite3
import stat
import traceback
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError, validate_token
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL

from device_safety import disable_device_transports
from models import TELEGRAM_TABLES, db
from operation_locks import resource_lock
from telegram_store import TelegramStore
from telegram_ui import build_dispatcher, notification_loop

BASE_TABLES = {'user','inverter','inverter_status','monitor_config','monitor_heartbeat','state_event'}


class TokenRedactor(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += '\n'+''.join(traceback.format_exception(*record.exc_info))
        message = message.replace(self.token,'[REDACTED]')
        record.msg = re.sub(r'(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}', '[REDACTED]',message)
        record.args,record.exc_info,record.exc_text = (),None,None
        return True


def load_token(token_file=None):
    token = os.environ.get('INGECON_TELEGRAM_TOKEN')
    if token and token_file:
        raise ValueError('Вкажіть або INGECON_TELEGRAM_TOKEN, або --token-file.')
    if token_file:
        path = Path(token_file)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>4096):
            raise ValueError('Файл токена має належати поточному користувачу, бути звичайним файлом із правами 0600.')
        token = path.read_text().strip()
    try:
        if not token:
            raise ValueError
        validate_token(token)
    except (ValueError,TypeError,TokenValidationError):
        raise ValueError('Відсутній або некоректний Telegram-токен.') from None
    return token


def open_engine(database):
    path = Path(database).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('Потрібна наявна SQLite-БД, без символічного посилання.')
    return create_engine(URL.create('sqlite',database=str(path)),connect_args={'timeout':5})


def check_schema(engine):
    tables = set(inspect(engine).get_table_names())
    required = {table.name for table in TELEGRAM_TABLES} | BASE_TABLES
    if 'demo_metadata' in tables:
        raise ValueError('Архівна тестова БД не може використовуватись для production polling.')
    if not required<=tables:
        raise ValueError('Схема Telegram відсутня. Потрібні погоджена резервна копія та явний prepare-db.')


def prepare_database(database, backup):
    """Manual only: consistent SQLite backup BEFORE creating only additive tables."""
    source_path = Path(database).absolute()
    backup_path = Path(backup).absolute()
    engine = open_engine(source_path)
    try:
        existing = set(inspect(engine).get_table_names())
        if not BASE_TABLES<=existing:
            raise ValueError('Спочатку потрібна підготовлена схема TASK-02/03.')
        fd = os.open(backup_path,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600)
        os.close(fd)
        # SQLite backup includes committed WAL data and works with a live writer.
        with sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
        db.metadata.create_all(engine,tables=TELEGRAM_TABLES)
    finally:
        engine.dispose()


async def run_polling(store, token):
    session = AiohttpSession(timeout=35,limit=2)
    bot = Bot(token,session=session)
    notifier = None
    try:
        # Do not silently remove another deployment's webhook or pending updates.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError('Webhook уже налаштовано; узгодьте його вимкнення перед polling.')
        await bot.set_my_commands([BotCommand(command=command,description=description) for command,description in (
            ('start','Меню та прив’язка'),('status','Поточні стани з SQLite'),('inverters','Інвертори'),
            ('groups','Групи'),('history','Історія станів'),('help','Допомога'))])
        dispatcher = build_dispatcher(store)
        notifier = asyncio.create_task(notification_loop(store,bot))
        await dispatcher.start_polling(bot,polling_timeout=25,handle_as_tasks=False,
            allowed_updates=['message','callback_query'],close_bot_session=False)
    finally:
        if notifier:
            notifier.cancel()
            await asyncio.gather(notifier,return_exceptions=True)
        await bot.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Telegram integration: SQLite only, controls blocked.')
    commands = parser.add_subparsers(dest='command',required=True)
    prepare = commands.add_parser('prepare-db',help='Explicit additive schema preparation with mandatory backup')
    prepare.add_argument('--database',required=True)
    prepare.add_argument('--backup',required=True)
    run = commands.add_parser('run',help='Real Telegram long polling; never runs inverter probes')
    run.add_argument('--database',required=True)
    run.add_argument('--token-file')
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.command=='prepare-db':
            prepare_database(args.database,args.backup)
            print('Резервну копію та додаткові таблиці Telegram створено. Наявні дані збережено.')
            return
        token = load_token(args.token_file)
        logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(name)s: %(message)s')
        for handler in logging.getLogger().handlers:
            handler.addFilter(TokenRedactor(token))
        engine = open_engine(args.database)
        check_schema(engine)  # No schema changes or Telegram API calls on an unprepared DB.
        disable_device_transports()
        with resource_lock('telegram-db:'+str(Path(args.database).resolve())), resource_lock('telegram-bot:'+token.split(':')[0]):
            asyncio.run(run_polling(TelegramStore(engine),token))
    except KeyboardInterrupt:
        pass
    except (ValueError,OSError,TelegramAPIError) as error:
        # API exception text can contain a token. Never print it or a traceback.
        if isinstance(error,ValueError):
            parser.exit(1,'Помилка: '+str(error)+'\n')
        parser.exit(1,'Telegram не запущено або зупинено: '+type(error).__name__+'. Перевірте конфігурацію.\n')
    finally:
        if engine is not None:
            engine.dispose()


if __name__=='__main__':
    main()
