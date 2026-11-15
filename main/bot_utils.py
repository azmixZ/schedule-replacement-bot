"""Общее для обоих ботов: логи, хранение JSON, безопасная отправка и рассылка."""
import asyncio
import json
import logging
import os
from datetime import datetime

from telegram.error import Forbidden, RetryAfter

log = logging.getLogger("bot")


def setup_logging():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    # httpx пишет в лог полный адрес каждого запроса, а в адресе — токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class StorageError(Exception):
    pass


def load_json(path: str, strict: bool = False) -> dict:
    """Читает словарь из JSON. Файла нет — пустой словарь.

    Если файл битый: при strict=True — исключение (чтобы потом не записать
    поверх него почти пустой словарь), иначе — пустой словарь и копия файла рядом.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("в файле не словарь")
        return data
    except (OSError, ValueError) as e:
        log.error("Не читается %s: %s", path, e)
        if strict:
            raise StorageError(str(e)) from e
        _keep_broken_copy(path)
        return {}


_broken_saved = set()


def _keep_broken_copy(path: str):
    """Один раз на каждую версию битого файла кладёт рядом его копию для ручного восстановления."""
    try:
        mark = (path, os.path.getmtime(path))
        if mark in _broken_saved:
            return
        _broken_saved.add(mark)
        broken = f"{path}.broken-{datetime.now():%Y%m%d-%H%M%S}"
        with open(path, "rb") as src, open(broken, "wb") as dst:
            dst.write(src.read())
    except OSError:
        pass


def save_json(path: str, data: dict):
    """Запись через временный файл: при сбое старый файл остаётся целым."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def set_user_value(path: str, user_id, value: str):
    data = load_json(path, strict=True)
    data[str(user_id)] = value
    save_json(path, data)


def _seconds(retry_after) -> float:
    total = getattr(retry_after, "total_seconds", None)  # в новых версиях это timedelta
    return float(total() if total else retry_after)


async def send_safe(bot, chat_id, text: str) -> bool:
    """Отправляет HTML-сообщение. Ошибки не выбрасывает, возвращает успех."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            return True
        except RetryAfter as e:
            await asyncio.sleep(_seconds(e.retry_after) + 1)
        except Forbidden:
            return False  # пользователь заблокировал бота
        except Exception as e:
            log.warning("Не отправилось %s: %s", chat_id, e)
            return False
    return False


async def broadcast(bot, items, label: str):
    """Рассылка. items — пары (chat_id, текст); пустой текст пропускается."""
    sent = failed = 0
    for chat_id, text in items:
        if not text:
            continue
        if await send_safe(bot, chat_id, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    log.info("Рассылка «%s»: отправлено %s, не доставлено %s", label, sent, failed)
    return sent, failed
