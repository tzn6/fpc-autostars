"""
UnconfirmedOrders — плагин для FunPayCardinal.

Записывает время каждого нового заказа. По команде /unconfirmed в Telegram-боте
выводит список заказов, которые всё ещё не подтверждены покупателем
и с момента оплаты которых прошло более 24 часов.

Текст можно скопировать и отправить в поддержку FunPay.
"""

from __future__ import annotations

import os
import json
import time
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent

NAME = "UnconfirmedOrders"
VERSION = "1.0.0"
DESCRIPTION = "Команда /unconfirmed — список заказов без подтверждения покупателя (>24ч)."
CREDITS = "@vipzazaa"
UUID = "b3f7e120-94c2-4d88-a061-dc3f8e5a1b29"
SETTINGS_PAGE = False

logger = logging.getLogger("FPC.unconfirmed")
LOGGER_PREFIX = "[UNCONFIRMED]"

STORAGE_PATH = os.path.join("storage", "plugins", "unconfirmed_orders.json")
THRESHOLD_SEC = 24 * 3600  # 24 часа

_lock = threading.Lock()
_orders: dict[str, float] = {}  # order_id -> unix timestamp получения


# ============================== Хранилище ==============================

def _load() -> None:
    global _orders
    try:
        with open(STORAGE_PATH, "r", encoding="utf-8") as f:
            _orders = json.load(f)
    except FileNotFoundError:
        _orders = {}
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка чтения хранилища: {e}")
        _orders = {}


def _save() -> None:
    os.makedirs(os.path.dirname(STORAGE_PATH), exist_ok=True)
    with open(STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(_orders, f, ensure_ascii=False, indent=2)


# ============================== FunPay API ==============================

def _get_paid_ids(cardinal: "Cardinal") -> set[str]:
    """Возвращает ID заказов в статусе PAID (не подтверждены покупателем)."""
    try:
        from FunPayAPI.types import OrderStatuses
        result = cardinal.account.get_orders(
            include_paid=True,
            include_closed=False,
            include_refunded=False,
        )
        # get_orders возвращает (cursor, list) или просто list — обрабатываем оба варианта.
        orders = result[1] if isinstance(result, tuple) else result
        return {o.id for o in orders if o.status == OrderStatuses.PAID}
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка получения заказов с FunPay: {e}")
        return set()


# ============================== Команда /unconfirmed ==============================

def _send_report(cardinal: "Cardinal", bot, chat_id: int) -> None:
    try:
        bot.send_message(chat_id, "⏳ Получаю список заказов…")
        paid_ids = _get_paid_ids(cardinal)
        now = time.time()

        with _lock:
            # Удаляем из хранилища заказы, которые уже закрыты.
            closed = [oid for oid in list(_orders) if oid not in paid_ids]
            for oid in closed:
                del _orders[oid]
            if closed:
                _save()

            old_ids = [
                oid for oid, ts in _orders.items()
                if oid in paid_ids and (now - ts) >= THRESHOLD_SEC
            ]

        if not old_ids:
            bot.send_message(chat_id, "✅ Нет неподтверждённых заказов старше 24 часов.")
            return

        lines = ["Покупатели забыли подтвердить получение,прошло больше суток"]
        for i, oid in enumerate(old_ids):
            prefix = "  " if i == 0 else ""
            lines.append(f"{prefix}#{oid}")
            lines.append("")  # пустая строка между ID

        # Убираем последнюю лишнюю пустую строку.
        while lines and lines[-1] == "":
            lines.pop()

        bot.send_message(chat_id, "\n".join(lines))
        logger.info(f"{LOGGER_PREFIX} Отправлен список из {len(old_ids)} неподтверждённых заказов.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка при формировании отчёта: {e}")
        try:
            bot.send_message(chat_id, f"❌ Ошибка: {e}")
        except Exception:  # noqa: BLE001
            pass


# ============================== Точки входа FPC ==============================

def register_command(cardinal: "Cardinal", *args) -> None:
    tg = getattr(cardinal, "telegram", None)
    if tg is None:
        return
    bot = tg.bot

    def handle(m):
        threading.Thread(
            target=_send_report, args=(cardinal, bot, m.chat.id), daemon=True
        ).start()

    tg.msg_handler(handle, commands=["unconfirmed"])
    logger.info(f"{LOGGER_PREFIX} Команда /unconfirmed зарегистрирована.")


def init(cardinal: "Cardinal", *args) -> None:
    _load()
    # Добавляем уже существующие PAID-заказы с текущим временем,
    # чтобы не потерять их если плагин только что установлен.
    # Они появятся в отчёте не раньше чем через 24ч — поведение корректное.
    try:
        paid_ids = _get_paid_ids(cardinal)
        added = 0
        now = time.time()
        with _lock:
            for oid in paid_ids:
                if oid not in _orders:
                    _orders[oid] = now
                    added += 1
            if added:
                _save()
        if added:
            logger.info(f"{LOGGER_PREFIX} Добавлено {added} существующих PAID-заказов в хранилище.")
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка инициализации: {e}")
    logger.info(f"{LOGGER_PREFIX} Плагин загружен. Заказов в хранилище: {len(_orders)}.")


def on_new_order(cardinal: "Cardinal", event: "NewOrderEvent", *args) -> None:
    order = event.order
    with _lock:
        if order.id not in _orders:
            _orders[order.id] = time.time()
            _save()
            logger.info(f"{LOGGER_PREFIX} Записан новый заказ {order.id}.")


BIND_TO_PRE_INIT = [register_command]
BIND_TO_POST_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
