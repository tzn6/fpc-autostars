"""
AutoStars — плагин автовыдачи Telegram Stars для FunPayCardinal (FPC).

Когда покупатель оплачивает лот с Telegram-звёздами «по username», плагин:
  1. ловит новый заказ;
  2. определяет кол-во звёзд и Telegram-юзернейм (из описания заказа или из чата);
  3. проверяет юзернейм через Fragment (searchStarsRecipient);
  4. покупает звёзды на Fragment и получает данные TON-транзакции;
  5. подписывает и отправляет перевод TON с вашего кошелька (W5 -> tonapi.io);
  6. дожидается подтверждения, пишет покупателю и (опционально) делает возврат при ошибке.

Установка:
  1. Положите этот файл в папку plugins/ вашего FunPayCardinal.
  2. Установите зависимость для работы с TON:  pip install pytoniq
     (requests уже входит в зависимости FPC).
  3. Запустите FPC — рядом появится storage/plugins/autostars.json.
     Заполните в нём fragment_cookies, fragment_hash, ton_mnemonic
     (сид-фраза кошелька W5) и, по желанию, ton_api_token.

ВНИМАНИЕ: плагин работает с реальными деньгами (TON). Используйте отдельный
кошелёк под автовыдачу и держите на нём только рабочий баланс.

Покупатель может (пере)задать юзернейм в чате командой:  /stars username
"""

from __future__ import annotations

import os
import re
import json
import time
import base64
import copy
import html
import logging
import tempfile
import threading
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

# --- зависимость pytoniq (для подписи TON-транзакций) ---
try:
    from pytoniq import WalletV5R1
    from pytoniq_core import Address, StateInit, Cell, begin_cell
    from pytoniq_core.crypto.keys import mnemonic_is_valid, mnemonic_to_private_key
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE

    PYTONIQ_AVAILABLE = True
    PYTONIQ_ERROR = None
except Exception as e:  # noqa: BLE001
    PYTONIQ_AVAILABLE = False
    PYTONIQ_ERROR = e


# ============================== МЕТА-ДАННЫЕ ПЛАГИНА ==============================

NAME = "AutoStars"
VERSION = "0.3.1"
DESCRIPTION = (
    "Автовыдача Telegram Stars: покупка звёзд через Fragment и оплата с "
    "TON-кошелька W5. Требует: pip install pytoniq."
)
CREDITS = "@vipzazaa"
UUID = "75645030-dcb7-4d15-881d-efae51369c14"
SETTINGS_PAGE = True

logger = logging.getLogger("FPC.autostars")
LOGGER_PREFIX = "[AUTOSTARS]"


# ============================== КОНСТАНТЫ / НАСТРОЙКИ ==============================

CONFIG_PATH = os.path.join("storage", "plugins", "autostars.json")
ORDERS_PATH = os.path.join("storage", "plugins", "autostars_orders.json")

WALLET_V5R1_ID = 2147483409
TON_NETWORK_GLOBAL_ID = -239
ONE_TON = 1_000_000_000
AD_TEXT = "Stars sent automatically by AutoStars plugin for FunPayCardinal."
PAYMENT_REVIEW_GRACE_SEC = 120
MIN_PAYMENT_TTL_SEC = 15
PAYMENT_REBROADCAST_SEC = 30

DEFAULT_CONFIG = {
    "fragment_cookies": "",
    "fragment_hash": "",
    "ton_mnemonic": "",
    "ton_api_token": "",
    "show_sender": False,
    "show_ad": False,
    "refund_on_error": False,
    "loop_interval_sec": 5,
    "low_balance_threshold": 0.0,
    "low_balance_notify": True,
    "review_reply": True,
    "review_reply_text": "🌟 Спасибо за отзыв!",
    "messages": {
        "transaction_completed": "🌟 {buyer}, {amount} звёзд успешно переведены на аккаунт @{username}.",
        "transaction_failed": "❌ {buyer}, не удалось перевести звёзды.\nПродавец уведомлён и придёт на помощь как только сможет!",
        "invalid_username": "❌ {buyer}, telegram юзернейм по заказу {order_id} невалиден.\n\nПроверьте правильность и отправьте команду:\n/stars ваш_телеграм_юзернейм",
        "username_not_found": "❌ {buyer}, не удалось найти Telegram аккаунт с юзернеймом @{username}.\n\nПроверьте правильность и отправьте команду:\n/stars ваш_телеграм_юзернейм",
        "not_user_username": "❌ {buyer}, telegram тег @{username} принадлежит не пользователю.\nПеревод звёзд каналам/чатам не поддерживается.\n\nУкажите юзернейм пользователя:\n/stars ваш_телеграм_юзернейм",
        "blocked_by_user": "❌ {buyer}, похоже, вы заблокировали мой Telegram аккаунт, поэтому я не могу перевести звёзды.\n\nРазблокируйте аккаунт и отправьте команду:\n/stars {username}",
        "failed_to_fetch_username": "❌ {buyer}, не удалось проверить юзернейм @{username} (ошибка на стороне Telegram).\nПродавец уже уведомлён!\n\nПопробуйте позже, отправив команду:\n/stars {username}",
        "username_received": "✅ Юзернейм @{username} принят для заказа {order_id}. Проверяю аккаунт Telegram.",
        "stars_command_usage": "❌ Укажите Telegram-юзернейм целиком:\n/stars @ваш_юзернейм\n\nЕсли ожидают несколько заказов:\n/stars НОМЕР_ЗАКАЗА @ваш_юзернейм",
        "stars_no_waiting_order": "Нет заказа со звёздами, ожидающего исправления юзернейма. Если заказ только что оплачен, дождитесь его обработки; в остальных случаях напишите продавцу.",
        "stars_multiple_orders": "Ожидают юзернейм несколько заказов: {orders}.\nУкажите нужный заказ:\n/stars НОМЕР_ЗАКАЗА @ваш_юзернейм",
    },
}

# Статусы заказа.
ST_UNPROCESSED = "UNPROCESSED"
ST_WAITING_USERNAME = "WAITING_FOR_USERNAME"
ST_READY = "READY"
ST_TRANSFERRING = "TRANSFERRING"
ST_NEEDS_REVIEW = "NEEDS_REVIEW"
ST_REFUND_PENDING = "REFUND_PENDING"
ST_DONE = "DONE"
ST_ERROR = "ERROR"
ST_REFUNDED = "REFUNDED"

# Типы ошибок.
ERR_INVALID_USERNAME = "INVALID_USERNAME"
ERR_USERNAME_NOT_FOUND = "USERNAME_NOT_FOUND"
ERR_NOT_USER_USERNAME = "NOT_USER_USERNAME"
ERR_BLOCKED_BY_USER = "BLOCKED_BY_USER"
ERR_UNABLE_TO_FETCH_USERNAME = "UNABLE_TO_FETCH_USERNAME"
ERR_FRAGMENT_NOT_PROVIDED = "FRAGMENT_API_NOT_PROVIDED"
ERR_UNABLE_TO_FETCH_LINK = "UNABLE_TO_FETCH_STARS_LINK"
ERR_GET_BALANCE = "GET_BALANCE_ERROR"
ERR_NOT_ENOUGH_TON = "NOT_ENOUGH_TON"
ERR_TRANSFER = "TRANSFER_ERROR"
ERR_TIMEOUT = "TRANSACTION_TIMEOUT_ERROR"
ERR_TRANSACTION_FAILED = "TRANSACTION_FAILED"
ERR_PAYMENT_UNCERTAIN = "PAYMENT_RESULT_UNCERTAIN"

ERROR_DESC = {
    ERR_INVALID_USERNAME: "Невалидный Telegram юзернейм",
    ERR_USERNAME_NOT_FOUND: "Telegram юзернейм не найден",
    ERR_NOT_USER_USERNAME: "Юзернейм принадлежит не пользователю",
    ERR_BLOCKED_BY_USER: "Покупатель заблокировал ваш Telegram",
    ERR_UNABLE_TO_FETCH_USERNAME: "Не удалось проверить юзернейм (ошибка Fragment)",
    ERR_FRAGMENT_NOT_PROVIDED: "Fragment cookies/hash не указаны",
    ERR_UNABLE_TO_FETCH_LINK: "Не удалось получить данные для перевода (Fragment)",
    ERR_GET_BALANCE: "Не удалось получить баланс кошелька",
    ERR_NOT_ENOUGH_TON: "Недостаточно TON",
    ERR_TRANSFER: "Не удалось отправить транзакцию",
    ERR_TIMEOUT: "Таймаут ожидания подтверждения транзакции",
    ERR_TRANSACTION_FAILED: "TON-транзакция завершилась без отправки платежей",
    ERR_PAYMENT_UNCERTAIN: "Результат платежа пока не подтверждён; повтор и возврат заблокированы",
}

CHECK_USERNAME_ERRORS = {
    "no telegram users found.": ERR_USERNAME_NOT_FOUND,
    "please enter a username assigned to a user.": ERR_NOT_USER_USERNAME,
    "you can't gift telegram stars to this account at this moment.": ERR_BLOCKED_BY_USER,
    "you can&#39;t gift telegram stars to this account at this moment.": ERR_BLOCKED_BY_USER,
}

# --- регулярные выражения разбора заказа ---
STARS_AMOUNT_RE = re.compile(r"(\d+)\s*(?:звёзд|звезд|Stars)", re.IGNORECASE)
PCS_RE = re.compile(r",\s*(\d+)\s*(?:шт|pcs)\.?", re.IGNORECASE)
BY_USERNAME_RE = re.compile(r"(?:по\s*username|by\s*username)", re.IGNORECASE)
USERNAME_FULL_RE = re.compile(r"^@?[a-zA-Z0-9_]{4,32}$")
STARS_COMMAND_RE = re.compile(r"^[/!]stars(?:\s|$)", re.IGNORECASE)
TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,32})/?", re.IGNORECASE
)
STARS_CATEGORY_RE = re.compile(r"Telegram.*(?:Звёзд|Звезд|Stars)", re.IGNORECASE)


# ============================== Fragment API ==============================

class FragmentError(Exception):
    def __init__(self, method: str, text: str):
        super().__init__(f"Fragment '{method}': {text}")
        self.method = method
        self.error_text = text


class FragmentAPI:
    BASE_URL = "https://fragment.com/api"
    HEADERS = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://fragment.com",
        "Referer": "https://fragment.com/stars/buy",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, cookies: str, hash_: str):
        self.cookies = cookies
        self.hash = hash_

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = dict(self.HEADERS)
        headers["Cookie"] = self.cookies
        resp = requests.post(
            self.BASE_URL, params={"hash": self.hash}, headers=headers, data=payload, timeout=30
        )
        if resp.status_code != 200:
            raise FragmentError(payload.get("method", "?"), f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise FragmentError(payload.get("method", "?"), "invalid JSON") from e
        if data and data.get("error"):
            raise FragmentError(payload.get("method", "?"), str(data["error"]))
        return data

    def search_stars_recipient(self, username: str, quantity: int = 0) -> dict[str, Any]:
        data = self._post({
            "method": "searchStarsRecipient",
            "query": username,
            "quantity": str(quantity) if quantity else "",
        })
        found = data.get("found")
        if not found or not found.get("recipient"):
            raise FragmentError("searchStarsRecipient", "recipient not found")
        return found

    def init_buy_stars_request(self, recipient: str, quantity: int) -> dict[str, Any]:
        if quantity < 50 or quantity > 1_000_000:
            raise FragmentError("initBuyStarsRequest", f"invalid quantity {quantity}")
        data = self._post({
            "method": "initBuyStarsRequest",
            "recipient": recipient,
            "quantity": str(quantity),
            "payment_method": "ton",
        })
        if not data.get("req_id"):
            raise FragmentError("initBuyStarsRequest", "no req_id")
        return data

    def get_buy_stars_link(self, request_id: str, show_sender: bool = False) -> dict[str, Any]:
        data = self._post({
            "method": "getBuyStarsLink",
            "id": request_id,
            "show_sender": "1" if show_sender else "0",
            "transaction": "1",
        })
        tr = data.get("transaction")
        if not tr or not isinstance(tr.get("messages"), list) or not tr["messages"]:
            raise FragmentError("getBuyStarsLink", "no transaction in response")
        return data


# ============================== tonapi.io ==============================

class TonAPIError(Exception):
    pass


class TonAPI:
    BASE_URL = "https://tonapi.io"

    def __init__(self, token: str | None = None):
        self.token = token or None
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def _interval(self) -> float:
        return 1.1 if self.token else 4.1

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        with self._lock:
            wait = self._interval() - (time.monotonic() - self._last_ts)
            if wait > 0:
                time.sleep(wait)
            headers = {"Accept": "*/*"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                if method == "GET":
                    resp = requests.get(self.BASE_URL + path, headers=headers, timeout=30)
                else:
                    headers["Content-Type"] = "application/json"
                    resp = requests.post(self.BASE_URL + path, headers=headers, json=body, timeout=30)
            finally:
                self._last_ts = time.monotonic()

        data = None
        if resp.text:
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            err = (data or {}).get("error") if isinstance(data, dict) else resp.text
            raise TonAPIError(f"tonapi {path}: {err}")
        if isinstance(data, dict) and data.get("error"):
            raise TonAPIError(f"tonapi {path}: {data['error']}")
        return data

    def get_wallet(self, address: str) -> dict:
        return self._request("GET", f"/v2/wallet/{address}")

    def get_seqno(self, address: str) -> int:
        data = self._request("GET", f"/v2/wallet/{address}/seqno")
        return int((data or {}).get("seqno", 0))

    def send_boc(self, boc: str) -> None:
        self._request("POST", "/v2/blockchain/message", {"boc": boc})

    def get_transaction_by_message_hash(self, message_hash: str) -> dict | None:
        data = self._request("GET", f"/v2/blockchain/messages/{message_hash}/transaction")
        if data is not None and (not isinstance(data, dict) or not data.get("hash")):
            raise TonAPIError("invalid transaction response")
        return data

    def wait_for_transfer(self, message_hash: str, valid_until: int) -> dict:
        while time.time() < valid_until:
            tx = self.get_transaction_by_message_hash(message_hash)
            if tx:
                return tx
            time.sleep(3)
        raise TonAPIError(f"timeout waiting for transfer {message_hash}")


# ============================== TON Wallet V5R1 ==============================

def _pad_b64(b64: str) -> str:
    pad = len(b64) % 4
    return b64 + "=" * (4 - pad) if pad else b64


def extract_ref(payload_b64: str) -> str | None:
    """Достаёт реф-код 'Ref#...' из payload Fragment (для альтернативного комментария)."""
    if not payload_b64:
        return None
    try:
        cell = Cell.one_from_boc(base64.b64decode(_pad_b64(payload_b64)))
        text = cell.begin_parse().load_snake_string()
        m = re.search(r"Ref#.+", text)
        if m:
            return re.sub(r"[^A-Za-z0-9:#]", "", m.group())
    except Exception:  # noqa: BLE001
        pass
    raw = base64.b64decode(_pad_b64(payload_b64)).decode("latin1", "ignore")
    m = re.search(r"Ref#[A-Za-z0-9:#]+", raw)
    return re.sub(r"[^A-Za-z0-9:#]", "", m.group()) if m else None


class OfflineWallet:
    """Офлайн-кошелёк V5R1: создаёт и подписывает внешние сообщения (порт ton/wallet.py)."""

    def __init__(self, mnemonic: str):
        words = mnemonic.strip().split()
        if not mnemonic_is_valid(words):
            raise ValueError("Невалидная сид-фраза.")
        self.public_key, self.private_key = mnemonic_to_private_key(words)
        data_cell = WalletV5R1.create_data_cell(
            self.public_key, wallet_id=WALLET_V5R1_ID, network_global_id=TON_NETWORK_GLOBAL_ID
        )
        state_init = StateInit(code=WALLET_V5_R1_CODE, data=data_cell)
        self.address = Address((0, state_init.serialize().hash))

    def address_str(self, bounceable: bool = True) -> str:
        return self.address.to_str(is_user_friendly=True, is_bounceable=bounceable)

    def build_external_transfer(self, seqno: int, transfers: list[dict]) -> tuple[str, str]:
        """transfers: [{address, amount, body(Cell|str), valid_until}] -> (boc_hex, msg_hash_hex)."""
        messages = [
            WalletV5R1.create_wallet_internal_message(
                destination=Address(t["address"]), value=t["amount"], body=t["body"]
            )
            for t in transfers
        ]
        valid_until = min(t["valid_until"] for t in transfers)
        transfer_msg = WalletV5R1.raw_create_transfer_msg(
            WalletV5R1,
            private_key=self.private_key,
            seqno=seqno,
            wallet_id=WALLET_V5R1_ID,
            messages=messages,
            valid_until=valid_until,
        )
        ext = WalletV5R1.create_external_msg(dest=self.address, body=transfer_msg).serialize()
        return ext.to_boc().hex(), ext.hash.hex()


class Wallet:
    def __init__(self, offline: OfflineWallet, tonapi: TonAPI):
        self.offline = offline
        self.tonapi = tonapi
        # Локально отслеживаем минимально ожидаемый seqno.
        # tonapi иногда кэширует устаревшее значение — используем max().
        self._min_seqno: int = 0

    @classmethod
    def from_mnemonic(cls, mnemonic: str, tonapi: TonAPI) -> "Wallet":
        offline = OfflineWallet(mnemonic)
        info = tonapi.get_wallet(offline.address_str())
        if info and info.get("is_wallet") is False:
            raise ValueError("Адрес не является кошельком (проверьте сид-фразу).")
        return cls(offline, tonapi)

    @property
    def address(self) -> str:
        return self.offline.address_str()

    def get_balance(self) -> int:
        return int(self.tonapi.get_wallet(self.address)["balance"])

    def prepare_transfer(self, transfers: list[dict]) -> dict:
        """Подписывает платёж. Вызывающий код обязан сохранить его до отправки."""
        valid_until = min(t["valid_until"] for t in transfers)
        if valid_until <= time.time() + MIN_PAYMENT_TTL_SEC:
            raise ValueError("Срок действия платежа Fragment истекает; платёж не отправлен.")
        # Берём максимум: tonapi может вернуть устаревшее значение,
        # но мы точно знаем что следующий seqno не меньше _min_seqno.
        tonapi_seqno = self.tonapi.get_seqno(self.address)
        seqno = max(tonapi_seqno, self._min_seqno)
        if seqno != tonapi_seqno:
            logger.warning(
                f"{LOGGER_PREFIX} tonapi вернул устаревший seqno {tonapi_seqno}, "
                f"используем локальный {seqno}."
            )
        boc, in_hash = self.offline.build_external_transfer(seqno, transfers)
        return {
            "wallet_address": self.address,
            "seqno": seqno,
            "boc": boc,
            "in_msg_hash": in_hash,
            "valid_until": valid_until,
            "created_at": int(time.time()),
            "state": "PENDING",
        }

    def broadcast_transfer(self, attempt: dict) -> None:
        self.tonapi.send_boc(attempt["boc"])

    def confirm_seqno(self, attempt: dict) -> None:
        # Сохраняем защиту от кэшированного seqno только после успешного исполнения.
        self._min_seqno = max(self._min_seqno, int(attempt["seqno"]) + 1)


# ============================== Хранилище заказов ==============================

class Storage:
    def __init__(self, path: str = ORDERS_PATH):
        self.path = path
        self.orders: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.orders = json.load(f)
            if not isinstance(self.orders, dict) or any(
                not isinstance(o, dict) or o.get("order_id") != order_id or "status" not in o
                for order_id, o in self.orders.items()
            ):
                raise ValueError("invalid orders storage")
            migrated = False
            for order in self.orders.values():
                if (not order.get("payment_attempt") and order["status"] == ST_ERROR
                        and order.get("error") in (ERR_TRANSFER, ERR_TIMEOUT)
                        and order.get("retries_left", 0) > 0):
                    # В 0.2.0 таймаут после send_boc сохранялся как обычный TRANSFER_ERROR.
                    order["status"], order["error"] = ST_NEEDS_REVIEW, ERR_PAYMENT_UNCERTAIN
                    order["payment_review_reason"] = "Ошибка старой версии без сохранённого хеша платежа."
                    migrated = True
            if migrated:
                self.save()
        except FileNotFoundError:
            self.orders = {}
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка чтения {self.path}: {e}")
            raise RuntimeError("История заказов повреждена; автовыдача остановлена.") from e

    def save(self) -> None:
        with self._lock:
            _atomic_write_json(self.path, self.orders)

    def has(self, order_id: str) -> bool:
        with self._lock:
            return order_id in self.orders

    def get(self, order_id: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self.orders.get(order_id))

    def upsert(self, *orders: dict) -> None:
        with self._lock:
            previous = self.orders
            self.orders = dict(previous)
            try:
                for o in orders:
                    self.orders[o["order_id"]] = copy.deepcopy(o)
                self.save()
            except Exception:
                self.orders = previous
                raise

    def find_by_chat(self, chat_id: Any, status: str | None = None) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if str(o.get("chat_id")) == str(chat_id) and (status is None or o["status"] == status)
            ])

    def find_by_buyer(self, buyer_id: Any, status: str | None = None) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if str(o.get("buyer_id")) == str(buyer_id) and (status is None or o["status"] == status)
            ])

    def find_by_buyer_name(self, buyer_name: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if buyer_name and str(o.get("buyer_name") or "").casefold() == buyer_name.casefold()
            ])

    def get_ready_orders(self, limit: int = 65) -> list[dict]:
        with self._lock:
            result = []
            for o in self.orders.values():
                if o.get("payment_attempt", {}).get("state") == "PENDING":
                    continue
                if o["status"] == ST_READY or (o["status"] == ST_ERROR and o["retries_left"] > 0):
                    result.append(copy.deepcopy(o))
                if len(result) >= limit:
                    break
            return result

    def get_pending_orders(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if o.get("payment_attempt", {}).get("state") == "PENDING"
                or o["status"] == ST_TRANSFERRING
                or (o["status"] == ST_NEEDS_REVIEW and o.get("error") == ERR_PAYMENT_UNCERTAIN)
            ])

    def get_unchecked_orders(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if (o["status"] == ST_UNPROCESSED
                    or (o["status"] == ST_WAITING_USERNAME and o.get("error") is None))
                and o.get("payment_attempt", {}).get("state") not in ("PENDING", "CONFIRMED")
            ])

    def get_confirmed_seqno(self, wallet_address: str) -> int:
        """Восстанавливает нижнюю границу seqno из подтверждённых платежей."""
        with self._lock:
            attempts = [o.get("payment_attempt", {}) for o in self.orders.values()]
            return max((
                int(a["seqno"]) + 1 for a in attempts
                if a.get("wallet_address") == wallet_address and a.get("wallet_executed")
            ), default=0)

    def get_attempt_orders(self, wallet_address: str, message_hash: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy([
                o for o in self.orders.values()
                if o.get("payment_attempt", {}).get("wallet_address") == wallet_address
                and o.get("payment_attempt", {}).get("in_msg_hash") == message_hash
            ])


def _atomic_write_json(path: str, data: dict) -> None:
    """Запись в той же ФС: flush/fsync, затем атомарная замена файла."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".autostars-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _outgoing_key(message: dict) -> tuple[str, int, str]:
    """Сверяем адрес, сумму и hash payload, а не только факт существования tx."""
    address = message["destination"]
    if isinstance(address, dict):
        address = address["address"]
    raw_body = message["raw_body"]
    body_hash = Cell.one_from_boc(bytes.fromhex(raw_body)).hash.hex()
    return Address(address).to_str(is_user_friendly=False), int(message["value"]), body_hash


# ============================== Разбор заказа ==============================

def _strip(text: str) -> str:
    text = re.sub(r"<[^>]*>", " ", str(text or ""))
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def is_stars_order(description: str, subcategory: str = "") -> bool:
    text = _strip(description) + " " + _strip(subcategory)
    return bool(STARS_CATEGORY_RE.search(text) and STARS_AMOUNT_RE.search(text) and BY_USERNAME_RE.search(text))


def extract_username(text: str) -> str | None:
    clean = _strip(text)
    # Получатель — отдельное поле в конце описания, а не слово Telegram/username.
    username = _normalize_username(clean.rsplit(",", 1)[-1].strip())
    if username:
        return username
    explicit = re.search(r"(?:^|\s)(@\S+)\s*$", clean)
    return _normalize_username(explicit.group(1)) if explicit else None


def _command_text(text: str) -> str:
    text = html.unescape(_strip(text))
    # Символы форматирования часто попадают в команду при копировании.
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", text).strip()


def _normalize_username(text: str) -> str | None:
    text = _command_text(text)
    if USERNAME_FULL_RE.fullmatch(text):
        return text.lstrip("@")
    link = TELEGRAM_LINK_RE.fullmatch(text)
    return link.group(1) if link else None


def build_stars_order(order) -> dict | None:
    desc = _strip(order.description)
    m = STARS_AMOUNT_RE.search(desc)
    if not m:
        return None
    stars_per_item = int(m.group(1))
    count = order.amount if getattr(order, "amount", None) else 1
    pcs = PCS_RE.search(desc)
    if pcs and not getattr(order, "amount", None):
        count = int(pcs.group(1))
    return {
        "order_id": order.id,
        "chat_id": order.chat_id,
        "buyer_id": order.buyer_id,
        "buyer_name": order.buyer_username,
        "order_name": desc,
        "stars_amount": stars_per_item * count,
        "telegram_username": extract_username(desc),
        "recipient_id": None,
        "ref": None,
        "transaction_hash": None,
        "status": ST_UNPROCESSED,
        "error": None,
        "retries_left": 3,
    }


def format_message(template: str, order: dict, **extra) -> str:
    data = {
        "amount": order.get("stars_amount", ""),
        "username": order.get("telegram_username") or "",
        "recipient": order.get("recipient_id") or "",
        "hash": order.get("transaction_hash") or "",
        "order_id": order.get("order_id", ""),
        "buyer": order.get("buyer_name") or "",
    }
    data.update(extra)

    class _D(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return str(template or "").format_map(_D(data))
    except Exception:  # noqa: BLE001
        return str(template or "")


# ============================== Сервис перевода ==============================

class AutoStarsService:
    def __init__(self, cardinal: "Cardinal", config: dict):
        self.cardinal = cardinal
        self.config = config
        self._provider_lock = threading.RLock()
        self.storage = Storage()
        self.tonapi = TonAPI(config.get("ton_api_token") or None)

        self.fragment = None
        if config.get("fragment_cookies") and config.get("fragment_hash"):
            self.fragment = FragmentAPI(config["fragment_cookies"], config["fragment_hash"])
            logger.info(f"{LOGGER_PREFIX} Fragment API настроен.")
        else:
            logger.warning(f"{LOGGER_PREFIX} Fragment cookies/hash не указаны.")

        self.wallet = None
        if config.get("ton_mnemonic"):
            try:
                self.wallet = Wallet.from_mnemonic(config["ton_mnemonic"], self.tonapi)
                self.wallet._min_seqno = self.storage.get_confirmed_seqno(self.wallet.address)
                balance = self.wallet.get_balance()
                logger.info(f"{LOGGER_PREFIX} TON кошелёк подключён: {self.wallet.address} "
                            f"(баланс {balance / ONE_TON} TON).")
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Не удалось подключить TON кошелёк: {e}")
        else:
            logger.warning(f"{LOGGER_PREFIX} Сид-фраза TON кошелька не указана.")

        self._loop_busy = False
        self._checking = set()
        self._checking_lock = threading.Lock()
        self._stop = threading.Event()
        self._low_balance_paused = False
        self._bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
        self._admin_chat_id: int | None = config.get("admin_chat_id")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AutoStarsLoop")
        self._thread.start()
        for order in self.storage.get_unchecked_orders():
            threading.Thread(target=self._check_username, args=(order,), daemon=True).start()
        logger.info(f"{LOGGER_PREFIX} Сервис запущен.")

    # ---------- отправка сообщений ----------

    def _send(self, order: dict, text: str) -> None:
        if not text:
            return
        try:
            self.cardinal.send_message(order["chat_id"], text, order.get("buyer_name"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка отправки сообщения покупателю: {e}")

    # ---------- обработка нового заказа ----------

    def handle_new_order(self, order) -> None:
        try:
            if not is_stars_order(order.description, getattr(order, "subcategory_name", "")):
                return
            if self.storage.has(order.id):
                return
            stars_order = build_stars_order(order)
            if not stars_order:
                return
            self.storage.upsert(stars_order)
            logger.info(f"{LOGGER_PREFIX} Новый звёздный заказ {order.id} "
                        f"({stars_order['stars_amount']}⭐).")
            threading.Thread(
                target=self._check_username, args=(stars_order,), daemon=True
            ).start()
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка обработки заказа: {e}")

    # ---------- проверка username ----------

    def _check_username(self, order: dict) -> None:
        order_id = order["order_id"]
        with self._checking_lock:
            if order_id in self._checking:
                return
            order = self.storage.get(order_id)
            if not self._awaits_username(order):
                return
            self._checking.add(order_id)
        result = None
        try:
            while True:
                username = order.get("telegram_username")
                revision = order.get("username_revision", 0)
                order["recipient_id"] = None
                if not username or not USERNAME_FULL_RE.fullmatch(username):
                    order["status"], order["error"] = ST_WAITING_USERNAME, ERR_INVALID_USERNAME
                elif self.fragment is None:
                    order["status"], order["error"] = ST_WAITING_USERNAME, ERR_FRAGMENT_NOT_PROVIDED
                else:
                    self._do_check(order, username.lstrip("@"))
                with self._checking_lock:
                    current = self.storage.get(order_id)
                    if not self._awaits_username(current):
                        return
                    if (current.get("username_revision", 0) != revision
                            or current.get("telegram_username") != username):
                        # Команда во время запроса не теряется: проверяем последний ответ.
                        order = current
                        continue
                    for key in ("status", "error", "recipient_id"):
                        current[key] = order.get(key)
                    self.storage.upsert(current)
                    result = current
                    break
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка проверки юзернейма заказа {order_id}: {e}")
        finally:
            with self._checking_lock:
                self._checking.discard(order_id)
        # Освобождаем проверку до отправки подсказки: покупатель может ответить сразу.
        if result and result["status"] == ST_WAITING_USERNAME:
            current = self.storage.get(order_id)
            if (self._awaits_username(current)
                    and current.get("username_revision", 0) == result.get("username_revision", 0)):
                self._notify_username_error(result)

    @staticmethod
    def _awaits_username(order: dict | None) -> bool:
        return bool(order and order["status"] in (ST_UNPROCESSED, ST_WAITING_USERNAME)
                    and order.get("payment_attempt", {}).get("state") not in ("PENDING", "CONFIRMED"))

    def _do_check(self, order: dict, username: str) -> None:
        for attempt in range(3):
            try:
                found = self.fragment.search_stars_recipient(username)
                order["status"] = ST_READY
                order["recipient_id"] = found["recipient"]
                order["error"] = None
                return
            except FragmentError as e:
                order["status"] = ST_WAITING_USERNAME
                order["error"] = CHECK_USERNAME_ERRORS.get(
                    e.error_text.lower(), ERR_UNABLE_TO_FETCH_USERNAME
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{LOGGER_PREFIX} Ошибка проверки @{username} ({attempt + 1}): {e}")
                time.sleep(1)
        order["status"], order["error"] = ST_WAITING_USERNAME, ERR_UNABLE_TO_FETCH_USERNAME

    def _notify_username_error(self, order: dict) -> None:
        msgs = self.config.get("messages", {})
        mapping = {
            ERR_INVALID_USERNAME: msgs.get("invalid_username"),
            ERR_USERNAME_NOT_FOUND: msgs.get("username_not_found"),
            ERR_NOT_USER_USERNAME: msgs.get("not_user_username"),
            ERR_BLOCKED_BY_USER: msgs.get("blocked_by_user"),
            ERR_UNABLE_TO_FETCH_USERNAME: msgs.get("failed_to_fetch_username"),
            ERR_FRAGMENT_NOT_PROVIDED: msgs.get("failed_to_fetch_username"),
        }
        template = mapping.get(order["error"])
        if template:
            self._send(order, format_message(template, order))

    # ---------- команда покупателя /stars username ----------

    def handle_new_message(self, message) -> None:
        try:
            author_id = getattr(message, "author_id", None)
            # Системные сообщения и сообщения продавца не меняют получателя.
            if (author_id is None or not str(author_id).isdigit() or int(author_id) <= 0
                    or str(author_id) == str(self.cardinal.account.id)):
                return
            chat_id = getattr(message, "chat_id", None)
            text = _command_text(getattr(message, "text", "") or "")
            if not text or chat_id is None:
                return
            is_command = bool(STARS_COMMAND_RE.match(text))
            username = self._parse_username(text)
            if not username:
                if is_command:
                    self._command_reply(message, "stars_command_usage")
                return

            # ID покупателя надёжен, даже если chat_id заказа — users-A-B,
            # а в сообщении указан числовой node чата.
            buyer_orders = self.storage.find_by_buyer(author_id)
            if not buyer_orders:
                buyer_orders = [o for o in self.storage.find_by_chat(chat_id)
                                if o.get("buyer_id") in (None, 0, "")]
            message_id = getattr(message, "id", None)
            message_id = int(message_id) if str(message_id).isdigit() else 0
            if message_id and any(
                str(o.get("last_username_chat_id")) == str(chat_id)
                and o.get("last_username_message_id", 0) >= message_id for o in buyer_orders
            ):
                return
            parts = text.split()
            target_id = parts[1].lstrip("#").upper() if is_command and len(parts) == 3 else None
            reply, accepted, needs_worker = None, None, False
            with self._checking_lock:
                waiting = [self.storage.get(o["order_id"]) for o in buyer_orders]
                waiting = [o for o in waiting if self._awaits_username(o)]
                if target_id:
                    waiting = [o for o in waiting if str(o["order_id"]).upper() == target_id]
                if not waiting:
                    reply = "stars_no_waiting_order" if is_command else None
                elif len(waiting) > 1:
                    reply = "stars_multiple_orders"
                else:
                    accepted = waiting[0]
                    if (message_id and str(accepted.get("last_username_chat_id")) == str(chat_id)
                            and accepted.get("last_username_message_id", 0) >= message_id):
                        return
                    accepted["telegram_username"] = username
                    accepted["recipient_id"] = None
                    accepted["status"], accepted["error"] = ST_WAITING_USERNAME, None
                    accepted["username_revision"] = int(accepted.get("username_revision", 0)) + 1
                    accepted["chat_id"] = chat_id
                    accepted["buyer_id"] = author_id
                    if message_id:
                        accepted["last_username_message_id"] = message_id
                        accepted["last_username_chat_id"] = chat_id
                    self.storage.upsert(accepted)
                    needs_worker = accepted["order_id"] not in self._checking
            if reply:
                self._command_reply(message, reply, orders=", ".join(o["order_id"] for o in waiting))
            if accepted:
                self._command_reply(message, "username_received", accepted)
                if needs_worker:
                    threading.Thread(target=self._check_username, args=(accepted,), daemon=True).start()
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка обработки сообщения: {e}")

    def _parse_username(self, text: str) -> str | None:
        text = _command_text(text)
        if STARS_COMMAND_RE.match(text):
            parts = text.split()
            if len(parts) not in (2, 3):
                return None
            if len(parts) == 3 and not re.fullmatch(r"#?[a-zA-Z0-9]{1,64}", parts[1]):
                return None
            text = parts[-1]
        return _normalize_username(text)

    def _command_reply(self, message, key: str, order: dict | None = None, **extra) -> None:
        context = dict(order or {})
        context["chat_id"] = message.chat_id
        context.setdefault("buyer_name", getattr(message, "author", None) or "")
        template = self.config.get("messages", {}).get(key, DEFAULT_CONFIG["messages"][key])
        self._send(context, format_message(template, context, **extra))

    def handle_last_chat_message(self, chat) -> None:
        """В старом режиме FPC присылает только изменение последнего сообщения."""
        text = _command_text(getattr(chat, "last_message_text", "") or "")
        if not STARS_COMMAND_RE.match(text):
            if not self._parse_username(text):
                return
            # Для обычных реплик не запрашиваем историю чужих/закрытых заказов.
            related = self.storage.find_by_chat(chat.id)
            related += self.storage.find_by_buyer_name(getattr(chat, "name", None) or "")
            if not any(self._awaits_username(o) for o in related):
                return

        def worker():
            try:
                # По unread нельзя определять автора: читаем настоящее сообщение.
                messages = self.cardinal.account.get_chat_history(chat.id)
                node_message_id = getattr(chat, "node_msg_id", None)
                if node_message_id is not None:
                    messages = [m for m in messages if str(m.id) == str(node_message_id)]
                elif messages:
                    messages = messages[-1:]
                    if _command_text(messages[0].text or "") != text:
                        return
                for message in messages:
                    self.handle_new_message(message)
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Не удалось прочитать команду в чате {chat.id}: {e}")

        threading.Thread(target=worker, daemon=True).start()

    # ---------- цикл перевода ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(2, int(self.config.get("loop_interval_sec", 5)))
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            if self._loop_busy:
                continue
            self._loop_busy = True
            try:
                with self._provider_lock:
                    # Сначала восстанавливаем старые попытки, даже при смене ключей/низком балансе.
                    self._reconcile_pending()
                    if self.storage.get_pending_orders() or not self.fragment or not self.wallet:
                        continue
                    if float(self.config.get("low_balance_threshold", 0)) > 0 or self._low_balance_paused:
                        try:
                            bal = self.wallet.get_balance()
                            if self._check_low_balance(bal):
                                continue
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"{LOGGER_PREFIX} Не удалось проверить баланс: {e}")
                            if self._low_balance_paused:
                                continue
                    orders = self.storage.get_ready_orders()
                    if not orders:
                        continue
                    for o in orders:
                        o["retries_left"] -= 1
                    self.storage.upsert(*orders)
                    logger.info(f"{LOGGER_PREFIX} Перевод TON по заказам: "
                                f"{', '.join(o['order_id'] for o in orders)}.")
                    self._transfer_batch(orders)
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Ошибка в цикле перевода: {e}")
            finally:
                self._loop_busy = False

    def _transfer_batch(self, orders: list[dict]) -> None:
        if self.storage.get_pending_orders():
            return
        prepared = []
        for order in orders:
            transfer = self._prepare_transfer(order)
            if transfer:
                prepared.append((order, transfer))
        self.storage.upsert(*orders)
        for order in orders:
            if order["status"] == ST_ERROR and order["retries_left"] <= 0:
                self._on_fail(order)
        if not prepared:
            return

        try:
            balance = self.wallet.get_balance() - ONE_TON // 10  # резерв 0.1 TON
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка получения баланса: {e}")
            self._fail([o for o, _ in prepared], ERR_GET_BALANCE)
            return

        prepared.sort(key=lambda p: p[1]["amount"])
        fit, total = [], 0
        for order, transfer in prepared:
            if total + transfer["amount"] > balance:
                break
            total += transfer["amount"]
            fit.append((order, transfer))

        not_enough = [o for (o, _) in prepared if (o, _) not in fit]
        if not_enough:
            for o in not_enough:
                o["retries_left"] = 0
            self._fail(not_enough, ERR_NOT_ENOUGH_TON)
        if not fit:
            return

        fit_orders = [o for o, _ in fit]
        try:
            attempt = self.wallet.prepare_transfer([t for _, t in fit])
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка подготовки TON (платёж не отправлен): {e}")
            self._fail(fit_orders, ERR_TRANSFER)
            return

        for order, transfer in fit:
            previous = order.get("payment_attempt")
            if previous:
                order.setdefault("payment_history", []).append(previous)
            order["payment_attempt"] = copy.deepcopy(attempt)
            order["payment_attempt"]["last_broadcast_at"] = int(time.time())
            order.pop("payment_review_notified", None)
            order.pop("payment_review_reason", None)
            order["expected_message"] = {
                "address": transfer["address"],
                "amount": transfer["amount"],
                "body_hash": transfer["body"].hash.hex(),
                "request_id": transfer["request_id"],
            }
            order["status"] = ST_TRANSFERRING
            order["error"] = None
        # Write-ahead: если запись не удалась, send_boc никогда не вызывается.
        self.storage.upsert(*fit_orders)
        try:
            self.wallet.broadcast_transfer(attempt)
        except Exception as e:  # noqa: BLE001
            # HTTP timeout не доказывает, что TON не был отправлен.
            logger.warning(f"{LOGGER_PREFIX} Отправка {attempt['in_msg_hash']} не подтверждена: {e}")
        self._reconcile_pending()

    def _notify_admin(self, text: str) -> bool:
        if not self._bot or not self._admin_chat_id:
            return False
        try:
            self._bot.send_message(self._admin_chat_id, text)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Не удалось уведомить продавца: {e}")
            return False

    def _mark_for_review(self, orders: list[dict], reason: str) -> None:
        changed = any(o["status"] != ST_NEEDS_REVIEW for o in orders)
        for order in orders:
            order["status"], order["error"] = ST_NEEDS_REVIEW, ERR_PAYMENT_UNCERTAIN
            order["payment_review_reason"] = reason
        self.storage.upsert(*orders)
        if changed:
            logger.warning(f"{LOGGER_PREFIX} Нужна проверка платежа: {reason}")
        if orders and any(not o.get("payment_review_notified") for o in orders):
            message_hash = orders[0].get("payment_attempt", {}).get("in_msg_hash", "не сохранён")
            notified = self._notify_admin(
                "⚠️ <b>AutoStars: требуется проверка платежа</b>\n\n"
                f"Заказы: <code>{html.escape(', '.join(o['order_id'] for o in orders))}</code>\n"
                f"Сообщение TON: <code>{html.escape(message_hash)}</code>\n"
                f"Причина: {html.escape(reason)}\n\n"
                "Повторная покупка и автоматический возврат заблокированы. "
                "Плагин продолжает проверять прежнюю транзакцию."
            )
            if notified:
                for order in orders:
                    order["payment_review_notified"] = True
                self.storage.upsert(*orders)

    def _reconcile_pending(self) -> None:
        groups = {}
        for order in self.storage.get_pending_orders():
            attempt = order.get("payment_attempt", {})
            if not attempt.get("in_msg_hash") or not attempt.get("wallet_address"):
                self._mark_for_review([order], "У незавершённого платежа отсутствуют данные попытки.")
                continue
            groups[(attempt["wallet_address"], attempt["in_msg_hash"])] = attempt
        for (wallet_address, message_hash), attempt in groups.items():
            orders = self.storage.get_attempt_orders(wallet_address, message_hash)
            pending = [o for o in orders if o.get("payment_attempt", {}).get("state") == "PENDING"]
            if not pending:
                continue
            try:
                tx = self.tonapi.get_transaction_by_message_hash(message_hash)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{LOGGER_PREFIX} Пока не удалось проверить {message_hash}: {e}")
                tx = None
            if tx is None:
                # Повторяем только те же подписанные байты и только в пределах срока.
                # Это также восстанавливает остановку между записью журнала и send_boc.
                if self.wallet and self.wallet.address == wallet_address and (
                    time.time() < attempt["valid_until"] - MIN_PAYMENT_TTL_SEC
                    and time.time() - attempt.get("last_broadcast_at", 0) >= PAYMENT_REBROADCAST_SEC
                ):
                    for order in pending:
                        order["payment_attempt"]["last_broadcast_at"] = int(time.time())
                    self.storage.upsert(*pending)
                    try:
                        self.wallet.broadcast_transfer(attempt)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"{LOGGER_PREFIX} Повторная отправка прежнего BOC {message_hash}: {e}")
                if time.time() > attempt["valid_until"] + PAYMENT_REVIEW_GRACE_SEC:
                    self._mark_for_review(pending, "Транзакция не найдена после истечения срока платежа.")
                continue
            self._resolve_transaction(orders, tx)

    def _resolve_transaction(self, orders: list[dict], tx: dict) -> None:
        pending = [o for o in orders if o["payment_attempt"].get("state") == "PENDING"]
        attempt = pending[0]["payment_attempt"]
        try:
            tx_address = tx["account"]["address"]
            if Address(tx_address).to_str(is_user_friendly=False) != Address(
                attempt["wallet_address"]
            ).to_str(is_user_friendly=False):
                raise ValueError("Адрес транзакции не совпадает с кошельком попытки.")
            outgoing = tx["out_msgs"]
            if not isinstance(outgoing, list) or not isinstance(tx["success"], bool) or not isinstance(tx["aborted"], bool):
                raise ValueError("Неполный ответ tonapi о результате транзакции.")
        except Exception as e:  # noqa: BLE001
            self._mark_for_review(pending, str(e))
            return

        if (tx["success"] is False or tx["aborted"] is True) and not outgoing:
            # Подтверждённая неуспешная транзакция без исходящих сообщений: выплаты не было.
            for order in pending:
                order["payment_attempt"]["state"] = "FAILED"
                order["payment_attempt"].pop("boc", None)
                order["payment_attempt"]["transaction_hash"] = tx["hash"]
                order["transaction_hash"] = tx["hash"]
            self._fail(pending, ERR_TRANSACTION_FAILED)
            return

        if tx["success"] is not True or tx["aborted"] is not False or (
            (tx.get("compute_phase") or {}).get("success") is False
            or (tx.get("action_phase") or {}).get("success") is False
        ):
            self._mark_for_review(pending, "TON-транзакция неуспешна, но результат исходящих платежей неоднозначен.")
            return

        keys = []
        for message in outgoing:
            try:
                if message.get("msg_type") == "int_msg" and message.get("bounced") is False:
                    keys.append(_outgoing_key(message))
            except Exception:  # noqa: BLE001 - некорректный BOC не подтверждает платёж
                continue
        completed, uncertain = [], []
        for order in orders:
            expected = order.get("expected_message", {})
            try:
                key = (Address(expected["address"]).to_str(is_user_friendly=False),
                       int(expected["amount"]), expected["body_hash"])
                matched = key in keys
                if matched:
                    keys.remove(key)
            except Exception:  # noqa: BLE001
                matched = False
            # Учитываем уже завершённые заказы, чтобы не использовать одно сообщение дважды.
            if order["payment_attempt"].get("state") != "PENDING":
                continue
            order["payment_attempt"]["wallet_executed"] = True
            order["payment_attempt"]["transaction_hash"] = tx["hash"]
            if matched:
                order["status"], order["error"] = ST_DONE, None
                order["transaction_hash"] = tx["hash"]
                order["payment_attempt"]["state"] = "CONFIRMED"
                order["payment_attempt"].pop("boc", None)
                completed.append(order)
            else:
                uncertain.append(order)
        # Подтверждение и seqno сохраняются атомарно до сообщений покупателю.
        self.storage.upsert(*completed, *uncertain)
        if self.wallet and self.wallet.address == attempt["wallet_address"]:
            self.wallet.confirm_seqno(attempt)
        if completed:
            logger.info(f"{LOGGER_PREFIX} TON-платежи подтверждены по заказам "
                        f"{', '.join(o['order_id'] for o in completed)}. Хэш: {tx['hash']}.")
        for order in completed:
            self._on_success(order)
        if uncertain:
            self._mark_for_review(uncertain, "Не найдены исходящие сообщения с ожидаемыми адресом, суммой и payload.")

    def _prepare_transfer(self, order: dict) -> dict | None:
        try:
            req = self.fragment.init_buy_stars_request(order["recipient_id"], order["stars_amount"])
            link = self.fragment.get_buy_stars_link(req["req_id"], self.config.get("show_sender", False))
            if len(link["transaction"]["messages"]) != 1:
                raise FragmentError("getBuyStarsLink", "expected exactly one payment message")
            msg = link["transaction"]["messages"][0]
            if int(msg["amount"]) <= 0:
                raise FragmentError("getBuyStarsLink", "payment amount must be positive")
            order["ref"] = extract_ref(msg.get("payload", ""))
            return {
                "address": Address(msg["address"]).to_str(is_user_friendly=False),
                "amount": int(msg["amount"]),
                "body": self._build_body(order, msg),
                "valid_until": int(link["transaction"]["validUntil"]),
                "request_id": req["req_id"],
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка получения ссылки Fragment "
                         f"по заказу {order['order_id']}: {e}")
            order["status"], order["error"] = ST_ERROR, ERR_UNABLE_TO_FETCH_LINK
            return None

    def _build_body(self, order: dict, msg: dict):
        # По умолчанию используем payload Fragment как есть (надёжнее всего).
        if self.config.get("show_ad") and order.get("ref"):
            return begin_cell().store_uint(0, 32).store_snake_string(
                f"{AD_TEXT}\n\n{order['ref']}"
            ).end_cell()
        return Cell.one_from_boc(base64.b64decode(_pad_b64(msg["payload"])))

    def _fail(self, orders: list[dict], error: str) -> None:
        for order in orders:
            if order.get("payment_attempt", {}).get("state") in ("PENDING", "CONFIRMED"):
                raise RuntimeError("Нельзя повторить или вернуть платёж с неизвестным результатом.")
            order["status"], order["error"] = ST_ERROR, error
        self.storage.upsert(*orders)
        for order in orders:
            if order["retries_left"] <= 0:
                self._on_fail(order)

    # ---------- колбэки результата ----------

    def _on_success(self, order: dict) -> None:
        msgs = self.config.get("messages", {})
        self._send(order, format_message(msgs.get("transaction_completed", ""), order))

    def _on_fail(self, order: dict) -> None:
        msgs = self.config.get("messages", {})
        self._send(order, format_message(msgs.get("transaction_failed", ""), order))
        logger.error(f"{LOGGER_PREFIX} Заказ {order['order_id']} провалился: "
                     f"{ERROR_DESC.get(order['error'], order['error'])}.")
        self._notify_admin(
            f"❌ <b>AutoStars: ошибка заказа {html.escape(order['order_id'])}</b>\n"
            f"{html.escape(ERROR_DESC.get(order['error'], order['error']))}"
        )
        if self.config.get("refund_on_error"):
            self._refund(order)

    def _refund(self, order: dict) -> None:
        if order.get("payment_attempt", {}).get("state") in ("PENDING", "CONFIRMED"):
            logger.error(f"{LOGGER_PREFIX} Возврат {order['order_id']} заблокирован: результат платежа неизвестен.")
            return
        order["status"] = ST_REFUND_PENDING
        self.storage.upsert(order)
        try:
            self.cardinal.account.refund(order["order_id"])
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Результат возврата {order['order_id']} неизвестен: {e}")
            order["status"] = ST_NEEDS_REVIEW
            self._notify_admin(
                f"⚠️ <b>AutoStars: проверьте возврат по заказу {html.escape(order['order_id'])}</b>\n"
                "FunPay не подтвердил результат. Автоматический повтор возврата заблокирован."
            )
        else:
            order["status"] = ST_REFUNDED
            logger.info(f"{LOGGER_PREFIX} Возврат по заказу {order['order_id']} выполнен.")
        self.storage.upsert(order)

    # ---------- автовыключение при низком балансе ----------

    def _check_low_balance(self, balance_nanoton: int) -> bool:
        """Возвращает True и приостанавливает цикл, если баланс ниже порога."""
        threshold = float(self.config.get("low_balance_threshold", 0))
        if threshold <= 0:
            if self._low_balance_paused:
                self._low_balance_paused = False
            return False
        if balance_nanoton < int(threshold * ONE_TON):
            if not self._low_balance_paused:
                self._low_balance_paused = True
                logger.warning(
                    f"{LOGGER_PREFIX} Низкий баланс: {balance_nanoton / ONE_TON:.4f} TON "
                    f"(порог {threshold} TON). Автовыдача приостановлена."
                )
                self._notify_low_balance(balance_nanoton, threshold)
            return True
        if self._low_balance_paused:
            self._low_balance_paused = False
            logger.info(
                f"{LOGGER_PREFIX} Баланс восстановлен: {balance_nanoton / ONE_TON:.4f} TON. "
                f"Автовыдача возобновлена."
            )
            self._notify_balance_ok(balance_nanoton)
        return False

    def _notify_low_balance(self, balance: int, threshold: float) -> None:
        if not self.config.get("low_balance_notify", True):
            return
        if not self._bot or not self._admin_chat_id:
            return
        try:
            self._bot.send_message(
                self._admin_chat_id,
                f"⚠️ <b>AutoStars: низкий баланс!</b>\n\n"
                f"Текущий баланс: <b>{balance / ONE_TON:.4f} TON</b>\n"
                f"Порог: <b>{threshold} TON</b>\n\n"
                f"Автовыдача <b>приостановлена</b>. Пополните кошелёк — "
                f"плагин возобновит работу автоматически."
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Не удалось уведомить о низком балансе: {e}")

    def _notify_balance_ok(self, balance: int) -> None:
        if not self.config.get("low_balance_notify", True):
            return
        if not self._bot or not self._admin_chat_id:
            return
        try:
            self._bot.send_message(
                self._admin_chat_id,
                f"✅ <b>AutoStars: баланс восстановлен!</b>\n\n"
                f"Текущий баланс: <b>{balance / ONE_TON:.4f} TON</b>\n\n"
                f"Автовыдача <b>возобновлена</b>."
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Не удалось уведомить о восстановлении баланса: {e}")

    def reload_providers(self) -> str:
        with self._provider_lock:
            return self._reload_providers_locked()

    def _reload_providers_locked(self) -> str:
        """Пересоздаёт Fragment/кошелёк/tonapi по текущему конфигу. Возвращает статус-текст."""
        lines = []
        self.tonapi.token = self.config.get("ton_api_token") or None

        if self.config.get("fragment_cookies") and self.config.get("fragment_hash"):
            self.fragment = FragmentAPI(self.config["fragment_cookies"], self.config["fragment_hash"])
            lines.append("✅ Fragment настроен.")
        else:
            self.fragment = None
            lines.append("⚠️ Fragment cookies/hash не указаны.")

        if self.config.get("ton_mnemonic"):
            try:
                self.wallet = Wallet.from_mnemonic(self.config["ton_mnemonic"], self.tonapi)
                self.wallet._min_seqno = self.storage.get_confirmed_seqno(self.wallet.address)
                balance = self.wallet.get_balance()
                lines.append(f"✅ Кошелёк: <code>{self.wallet.address}</code>\n"
                             f"💰 Баланс: {balance / ONE_TON} TON")
            except Exception as e:  # noqa: BLE001
                self.wallet = None
                lines.append(f"❌ Ошибка кошелька: {e}")
        else:
            self.wallet = None
            lines.append("⚠️ Сид-фраза не указана.")
        return "\n".join(lines)

    def stop(self) -> None:
        self._stop.set()


# ============================== Загрузка конфига ==============================

def load_config() -> dict:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        logger.warning(f"{LOGGER_PREFIX} Создан файл настроек {CONFIG_PATH}. "
                       f"Заполните его через Telegram-бот или вручную.")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Дополняем недостающие ключи дефолтами.
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["messages"].items():
        cfg.setdefault("messages", {}).setdefault(k, v)
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write_json(CONFIG_PATH, cfg)


CONFIG: dict | None = None


def _ensure_config() -> dict:
    global CONFIG
    if CONFIG is None:
        CONFIG = load_config()
    return CONFIG


# ============================== Настройки в Telegram-боте FPC ==============================

# Понятные подписи для редактируемых сообщений покупателю.
MESSAGE_LABELS = {
    "transaction_completed": "✅ Успешный перевод",
    "transaction_failed": "❌ Ошибка перевода",
    "invalid_username": "🤡 Невалидный юзернейм",
    "username_not_found": "🔍 Юзернейм не найден",
    "not_user_username": "📢 Юзернейм не пользователя",
    "blocked_by_user": "🚫 Заблокирован покупателем",
    "failed_to_fetch_username": "👤 Ошибка проверки юзернейма",
    "username_received": "✅ Юзернейм принят",
    "stars_command_usage": "✍️ Формат команды /stars",
    "stars_no_waiting_order": "📦 Нет заказа для исправления",
    "stars_multiple_orders": "📦 Выбор заказа для исправления",
}
PROVIDER_KEYS = {"fragment_cookies", "fragment_hash", "ton_mnemonic", "ton_api_token"}
SECRET_KEYS = {"fragment_cookies", "fragment_hash", "ton_mnemonic", "ton_api_token"}
STATE_EDIT = "autostars_edit_value"


def register_settings(cardinal: "Cardinal", *args) -> None:
    """Регистрирует страницу настроек плагина в Telegram-боте FPC (BIND_TO_PRE_INIT)."""
    tg = getattr(cardinal, "telegram", None)
    if tg is None:  # Telegram-бот выключен в настройках FPC.
        return

    _ensure_config()
    bot = tg.bot

    try:
        from tg_bot import CBT
        cbt_settings, cbt_edit_plugin = CBT.PLUGIN_SETTINGS, CBT.EDIT_PLUGIN
    except Exception:  # noqa: BLE001
        cbt_settings, cbt_edit_plugin = "47", "45"

    from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B

    def on_off(value) -> str:
        return "✅ вкл" if value else "❌ выкл"

    def is_set(value) -> str:
        return "✅ задано" if value else "❌ пусто"

    # ---------- рендер главной страницы настроек ----------

    def settings_kb(offset: int) -> K:
        cfg = _ensure_config()
        kb = K()
        kb.add(B(f"🍪 Fragment cookies: {is_set(cfg.get('fragment_cookies'))}",
                 callback_data=f"asedit:fragment_cookies:{offset}"))
        kb.add(B(f"#️⃣ Fragment hash: {is_set(cfg.get('fragment_hash'))}",
                 callback_data=f"asedit:fragment_hash:{offset}"))
        kb.add(B(f"🔐 Сид-фраза W5: {is_set(cfg.get('ton_mnemonic'))}",
                 callback_data=f"asedit:ton_mnemonic:{offset}"))
        kb.add(B(f"🔑 tonapi токен: {is_set(cfg.get('ton_api_token'))}",
                 callback_data=f"asedit:ton_api_token:{offset}"))
        kb.add(B(f"👤 Показывать отправителя: {on_off(cfg.get('show_sender'))}",
                 callback_data=f"astgl:show_sender:{offset}"))
        kb.add(B(f"📢 Реклама в комментарии: {on_off(cfg.get('show_ad'))}",
                 callback_data=f"astgl:show_ad:{offset}"))
        kb.add(B(f"💸 Возврат при ошибке: {on_off(cfg.get('refund_on_error'))}",
                 callback_data=f"astgl:refund_on_error:{offset}"))
        kb.add(B(f"⏱ Интервал цикла: {cfg.get('loop_interval_sec', 5)} сек",
                 callback_data=f"asedit:loop_interval_sec:{offset}"))
        lbt = cfg.get("low_balance_threshold", 0.0)
        lbt_label = f"{lbt} TON" if lbt else "выкл"
        kb.add(B(f"🪫 Мин. баланс: {lbt_label}",
                 callback_data=f"asedit:low_balance_threshold:{offset}"))
        kb.add(B(f"🔔 Уведомить о низком балансе: {on_off(cfg.get('low_balance_notify', True))}",
                 callback_data=f"astgl:low_balance_notify:{offset}"))
        kb.add(B(f"📝 Ответ на отзыв: {on_off(cfg.get('review_reply'))}",
                 callback_data=f"astgl:review_reply:{offset}"))
        kb.add(B("📝 Текст ответа на отзыв", callback_data=f"asedit:review_reply_text:{offset}"))
        kb.add(B("💬 Сообщения покупателю", callback_data=f"asmsgs:{offset}"))
        kb.add(B("♻️ Переподключить (применить ключи)", callback_data=f"asreload:{offset}"))
        kb.add(B("◀️ Назад", callback_data=f"{cbt_edit_plugin}:{UUID}:{offset}"))
        return kb

    def settings_text() -> str:
        cfg = _ensure_config()
        status = "не запущен"
        if SERVICE is not None:
            fr = "✅" if SERVICE.fragment else "❌"
            wl = "✅" if SERVICE.wallet else "❌"
            pause = " · ⏸ пауза: низкий баланс" if SERVICE._low_balance_paused else ""
            pending = len(SERVICE.storage.get_pending_orders())
            payment_pause = f" · ⏳ ждём результат платежа ({pending} заказов)" if pending else ""
            status = f"Fragment {fr} · Кошелёк {wl}{pause}{payment_pause}"
        elif not PYTONIQ_AVAILABLE:
            status = "❌ не установлен pytoniq (pip install pytoniq)"
        return (f"<b>⭐ AutoStars — настройки</b>\n\n"
                f"<i>Состояние:</i> {status}\n\n"
                f"Нажмите на пункт, чтобы изменить. Секретные значения "
                f"(cookies, hash, сид-фраза) скрыты и показываются как «задано».\n"
                f"После изменения ключей нажмите «♻️ Переподключить».\n"
                f"Мин. баланс = 0 → автовыключение отключено.")

    def render(c, offset: int) -> None:
        bot.edit_message_text(settings_text(), c.message.chat.id, c.message.id,
                              reply_markup=settings_kb(offset))

    def messages_kb(offset: int) -> K:
        kb = K()
        for key, label in MESSAGE_LABELS.items():
            kb.add(B(label, callback_data=f"asedit:messages.{key}:{offset}"))
        kb.add(B("◀️ Назад", callback_data=f"asopen:{offset}"))
        return kb

    # ---------- обработчики ----------

    def open_settings(c) -> None:
        # Открытие из меню плагина (47:UUID:offset) или возврат (asopen:offset).
        parts = c.data.split(":")
        offset = int(parts[-1]) if parts[-1].lstrip("-").isdigit() else 0
        if SERVICE is not None:
            SERVICE._bot = bot
            SERVICE._admin_chat_id = c.message.chat.id
            cfg = _ensure_config()
            cfg["admin_chat_id"] = c.message.chat.id
            save_config(cfg)
        render(c, offset)
        bot.answer_callback_query(c.id)

    def open_messages(c) -> None:
        offset = int(c.data.split(":")[1])
        bot.edit_message_text(
            "<b>💬 Сообщения покупателю</b>\n\nВыберите сообщение для редактирования.\n\n"
            "Переменные: <code>{buyer}</code>, <code>{amount}</code>, "
            "<code>{username}</code>, <code>{order_id}</code>, <code>{hash}</code>.",
            c.message.chat.id, c.message.id, reply_markup=messages_kb(offset))
        bot.answer_callback_query(c.id)

    def toggle(c) -> None:
        _, key, offset = c.data.split(":")
        cfg = _ensure_config()
        cfg[key] = not cfg.get(key)
        save_config(cfg)
        render(c, int(offset))
        bot.answer_callback_query(c.id, "Сохранено.")

    def reload_cb(c) -> None:
        offset = int(c.data.split(":")[1])
        bot.answer_callback_query(c.id, "Переподключаю…")
        chat_id = c.message.chat.id

        def worker():
            if SERVICE is None:
                bot.send_message(chat_id, "⚠️ Сервис не запущен (перезапустите FPC).")
                return
            try:
                status = SERVICE.reload_providers()
            except Exception as e:  # noqa: BLE001
                status = f"❌ Ошибка: {e}"
            bot.send_message(chat_id, f"<b>⭐ AutoStars</b>\n\n{status}",
                             reply_markup=K().add(B("◀️ К настройкам",
                                                    callback_data=f"asopen:{offset}")))

        threading.Thread(target=worker, daemon=True).start()

    def edit_value(c) -> None:
        _, key, offset = c.data.split(":", 2)
        offset = int(offset)
        cfg = _ensure_config()

        if key.startswith("messages."):
            sub = key.split(".", 1)[1]
            current = cfg.get("messages", {}).get(sub, "")
            title = MESSAGE_LABELS.get(sub, sub)
            shown = f"\n\n<i>Текущее:</i>\n<code>{current}</code>" if current else ""
        elif key in SECRET_KEYS:
            title = key
            shown = "\n\n<i>(текущее значение скрыто)</i>"
        else:
            title = key
            cur = cfg.get(key, "")
            shown = f"\n\n<i>Текущее:</i> <code>{cur}</code>"

        prompt = bot.send_message(
            c.message.chat.id,
            f"✏️ Отправьте новое значение для <b>{title}</b>.{shown}",
            reply_markup=K().add(B("❌ Отмена", callback_data=f"asopen:{offset}")))
        tg.set_state(c.message.chat.id, prompt.id, c.from_user.id, STATE_EDIT,
                     {"key": key, "offset": offset})
        bot.answer_callback_query(c.id)

    def receive_value(m) -> None:
        state = tg.get_state(m.chat.id, m.from_user.id)
        data = (state or {}).get("data", {})
        key, offset = data.get("key"), int(data.get("offset", 0))
        tg.clear_state(m.chat.id, m.from_user.id, True)
        if not key:
            return

        value = (m.text or "").strip()
        cfg = _ensure_config()

        if key.startswith("messages."):
            cfg.setdefault("messages", {})[key.split(".", 1)[1]] = value
        elif key == "loop_interval_sec":
            try:
                cfg[key] = max(2, int(value))
            except ValueError:
                bot.reply_to(m, "❌ Нужно число (секунды).",
                             reply_markup=K().add(B("◀️ К настройкам",
                                                    callback_data=f"asopen:{offset}")))
                return
        elif key == "low_balance_threshold":
            try:
                val = float(value.replace(",", "."))
                if val < 0:
                    raise ValueError
                cfg[key] = val
            except ValueError:
                bot.reply_to(m, "❌ Нужно число ≥ 0 (например: 1.5). 0 — отключить.",
                             reply_markup=K().add(B("◀️ К настройкам",
                                                    callback_data=f"asopen:{offset}")))
                return
        else:
            cfg[key] = value
        save_config(cfg)

        back = K().add(B("◀️ К настройкам", callback_data=f"asopen:{offset}"))
        bot.reply_to(m, "✅ Сохранено.", reply_markup=back)

        # Применяем ключи провайдеров на лету.
        if key in PROVIDER_KEYS and SERVICE is not None:
            def worker():
                try:
                    status = SERVICE.reload_providers()
                except Exception as e:  # noqa: BLE001
                    status = f"❌ Ошибка: {e}"
                bot.send_message(m.chat.id, f"<b>⭐ AutoStars</b>\n\n{status}", reply_markup=back)

            threading.Thread(target=worker, daemon=True).start()

    tg.cbq_handler(open_settings,
                   lambda c: c.data.startswith(f"{cbt_settings}:{UUID}") or c.data.startswith("asopen:"))
    tg.cbq_handler(open_messages, lambda c: c.data.startswith("asmsgs:"))
    tg.cbq_handler(toggle, lambda c: c.data.startswith("astgl:"))
    tg.cbq_handler(reload_cb, lambda c: c.data.startswith("asreload:"))
    tg.cbq_handler(edit_value, lambda c: c.data.startswith("asedit:"))
    tg.msg_handler(receive_value,
                   func=lambda m: tg.check_state(m.chat.id, m.from_user.id, STATE_EDIT))
    logger.info(f"{LOGGER_PREFIX} Страница настроек в Telegram зарегистрирована.")


# ============================== Точки входа FPC ==============================

SERVICE: AutoStarsService | None = None


def init(cardinal: "Cardinal", *args) -> None:
    global SERVICE
    if not PYTONIQ_AVAILABLE:
        logger.error(f"{LOGGER_PREFIX} Не установлена библиотека pytoniq "
                     f"(pip install pytoniq). Перевод звёзд недоступен, но настройки работают. "
                     f"Причина: {PYTONIQ_ERROR}")
        return
    try:
        SERVICE = AutoStarsService(cardinal, _ensure_config())
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка инициализации плагина: {e}")


def on_new_order(cardinal: "Cardinal", event: "NewOrderEvent", *args) -> None:
    if SERVICE is not None:
        SERVICE.handle_new_order(event.order)


def on_new_message(cardinal: "Cardinal", event: "NewMessageEvent", *args) -> None:
    if SERVICE is not None:
        SERVICE.handle_new_message(event.message)


def on_last_chat_message_changed(cardinal: "Cardinal", event, *args) -> None:
    if SERVICE is not None and getattr(cardinal, "old_mode_enabled", False):
        SERVICE.handle_last_chat_message(event.chat)


def _format_review(template: str, order) -> str:
    review = getattr(order, "review", None)
    data = {
        "buyer": getattr(order, "buyer_username", "") or "",
        "stars": getattr(review, "stars", "") or "",
        "order_id": getattr(order, "id", "") or "",
    }

    class _D(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return str(template or "").format_map(_D(data))
    except Exception:  # noqa: BLE001
        return str(template or "")


def on_new_review(cardinal: "Cardinal", event: "NewMessageEvent", *args) -> None:
    """Авто-ответ на новый/изменённый отзыв покупателя."""
    try:
        message = event.message
        # Отзыв приходит как системное сообщение с типом NEW_FEEDBACK / FEEDBACK_CHANGED.
        type_name = getattr(getattr(message, "type", None), "name", "")
        if type_name not in ("NEW_FEEDBACK", "FEEDBACK_CHANGED"):
            return
        if getattr(message, "i_am_buyer", False):  # отзыв должен быть к нашей продаже
            return

        cfg = _ensure_config()
        if not cfg.get("review_reply"):
            return
        text = cfg.get("review_reply_text") or ""
        if not text:
            return

        def worker():
            try:
                order = cardinal.get_order_from_object(message)
                if order is None or not getattr(order, "review", None) or not order.review.stars:
                    return
                reply = _format_review(text, order)[:980]
                cardinal.account.send_review(order.id, reply)
                logger.info(f"{LOGGER_PREFIX} Ответил на отзыв по заказу {order.id} "
                            f"({order.review.stars}⭐).")
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Ошибка ответа на отзыв: {e}")

        threading.Thread(target=worker, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.error(f"{LOGGER_PREFIX} Ошибка обработки отзыва: {e}")


def on_stop(cardinal: "Cardinal", *args) -> None:
    if SERVICE is not None:
        SERVICE.stop()


BIND_TO_PRE_INIT = [register_settings]
BIND_TO_POST_INIT = [init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message, on_new_review]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_last_chat_message_changed]
BIND_TO_POST_STOP = [on_stop]
BIND_TO_DELETE = None
