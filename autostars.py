"""
AutoStars — плагин автовыдачи Telegram Stars для FunPayCardinal (FPC).

Когда покупатель оплачивает лот с Telegram-звёздами «по username», плагин:
  1. ловит новый заказ;
  2. определяет кол-во звёзд и Telegram-юзернейм (из описания заказа или из чата);
  3. проверяет юзернейм через Fragment (searchStarsRecipient);
  4. покупает звёзды на Fragment и получает данные TON-транзакции;
  5. подписывает и отправляет перевод TON с вашего кошелька (Wallet V5R1 -> tonapi.io);
  6. дожидается подтверждения, пишет покупателю и (опционально) делает возврат при ошибке.

Установка:
  1. Положите этот файл в папку plugins/ вашего FunPayCardinal.
  2. Установите зависимость для работы с TON:  pip install pytoniq
     (requests уже входит в зависимости FPC).
  3. Запустите FPC — рядом появится storage/plugins/autostars.json.
     Заполните в нём fragment_cookies, fragment_hash, ton_mnemonic
     (сид-фраза кошелька версии V5R1) и, по желанию, ton_api_token.

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
import logging
import threading
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

# --- зависимость pytoniq (для подписи TON-транзакций) ---
try:
    from pytoniq import WalletV5R1
    from pytoniq_core import Address, StateInit, Cell
    from pytoniq_core.crypto.keys import mnemonic_is_valid, mnemonic_to_private_key
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE

    PYTONIQ_AVAILABLE = True
    PYTONIQ_ERROR = None
except Exception as e:  # noqa: BLE001
    PYTONIQ_AVAILABLE = False
    PYTONIQ_ERROR = e


# ============================== МЕТА-ДАННЫЕ ПЛАГИНА ==============================

NAME = "AutoStars"
VERSION = "0.2.0"
DESCRIPTION = (
    "Автовыдача Telegram Stars: покупка звёзд через Fragment и оплата с "
    "TON-кошелька (Wallet V5R1). Требует: pip install pytoniq."
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
    },
}

# Статусы заказа.
ST_UNPROCESSED = "UNPROCESSED"
ST_WAITING_USERNAME = "WAITING_FOR_USERNAME"
ST_READY = "READY"
ST_TRANSFERRING = "TRANSFERRING"
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
TRAILING_USERNAME_RE = re.compile(r",\s*@?([a-zA-Z0-9_]{4,32})\s*$")
USERNAME_RE = re.compile(r"@?([a-zA-Z0-9_]{4,32})")
USERNAME_FULL_RE = re.compile(r"^@?[a-zA-Z0-9_]{4,32}$")
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
        return self._request("GET", f"/v2/blockchain/messages/{message_hash}/transaction")

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

    def transfer(self, transfers: list[dict], wait_seconds: int = 60) -> dict:
        deadline = int(time.time() + wait_seconds)
        last_err = None
        for attempt in range(8):
            seqno = self.tonapi.get_seqno(self.address)
            boc, in_hash = self.offline.build_external_transfer(seqno, transfers)
            try:
                self.tonapi.send_boc(boc)
            except TonAPIError as e:
                last_err = e
                if "seqno" in str(e).lower() and attempt < 7:
                    wait = min(5 * (attempt + 1), 30)
                    logger.warning(
                        f"{LOGGER_PREFIX} Устаревший seqno ({seqno}), "
                        f"попытка {attempt + 1}/8, повтор через {wait}с…"
                    )
                    time.sleep(wait)
                    continue
                raise
            tx = self.tonapi.wait_for_transfer(in_hash, deadline)
            return {"hash": tx["hash"], "in_msg_hash": in_hash}
        raise last_err or TonAPIError("transfer failed")


# ============================== Хранилище заказов ==============================

class Storage:
    def __init__(self, path: str = ORDERS_PATH):
        self.path = path
        self.orders: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.orders = json.load(f)
        except FileNotFoundError:
            self.orders = {}
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка чтения {self.path}: {e}")
            self.orders = {}

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.orders, f, ensure_ascii=False, indent=2)

    def has(self, order_id: str) -> bool:
        return order_id in self.orders

    def get(self, order_id: str) -> dict | None:
        return self.orders.get(order_id)

    def upsert(self, *orders: dict) -> None:
        for o in orders:
            self.orders[o["order_id"]] = o
        self.save()

    def find_by_chat(self, chat_id: Any, status: str | None = None) -> list[dict]:
        return [
            o for o in self.orders.values()
            if str(o.get("chat_id")) == str(chat_id) and (status is None or o["status"] == status)
        ]

    def find_by_buyer(self, buyer_id: Any, status: str | None = None) -> list[dict]:
        return [
            o for o in self.orders.values()
            if str(o.get("buyer_id")) == str(buyer_id) and (status is None or o["status"] == status)
        ]

    def get_ready_orders(self, limit: int = 65) -> list[dict]:
        result = []
        for o in self.orders.values():
            if o["status"] == ST_READY or (o["status"] == ST_ERROR and o["retries_left"] > 0):
                result.append(o)
            if len(result) >= limit:
                break
        return result


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
    m = TRAILING_USERNAME_RE.search(clean)
    if m:
        return m.group(1)
    m = USERNAME_RE.search(clean)
    return m.group(1) if m else None


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
                balance = self.wallet.get_balance()
                logger.info(f"{LOGGER_PREFIX} TON кошелёк подключён: {self.wallet.address} "
                            f"(баланс {balance / ONE_TON} TON).")
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Не удалось подключить TON кошелёк: {e}")
        else:
            logger.warning(f"{LOGGER_PREFIX} Сид-фраза TON кошелька не указана.")

        self._loop_busy = False
        self._checking = set()
        self._stop = threading.Event()
        self._low_balance_paused = False
        self._bot = None
        self._admin_chat_id: int | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AutoStarsLoop")
        self._thread.start()
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
        username = order.get("telegram_username")
        if not username or not USERNAME_FULL_RE.match(username):
            order["status"], order["error"] = ST_WAITING_USERNAME, ERR_INVALID_USERNAME
        elif self.fragment is None:
            order["status"], order["error"] = ST_WAITING_USERNAME, ERR_FRAGMENT_NOT_PROVIDED
        else:
            self._checking.add(order["order_id"])
            self._do_check(order, username.lstrip("@"))
            self._checking.discard(order["order_id"])

        self.storage.upsert(order)
        if order["status"] == ST_WAITING_USERNAME:
            self._notify_username_error(order)

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
            # Не реагируем на собственные сообщения продавца.
            if author_id is not None and author_id == self.cardinal.account.id:
                return
            text = _strip(getattr(message, "text", "") or "")
            if not text:
                return

            # Ищем заказ покупателя по его ID (надёжно: chat_id у заказа и у
            # сообщения имеют разный формат). Запасной вариант — по chat_id.
            waiting = []
            if author_id is not None:
                waiting = self.storage.find_by_buyer(author_id, ST_WAITING_USERNAME)
            if not waiting and getattr(message, "chat_id", None) is not None:
                waiting = self.storage.find_by_chat(message.chat_id, ST_WAITING_USERNAME)
            if not waiting:
                return

            username = self._parse_username(text)
            if not username:
                return
            to_recheck = []
            for order in waiting:
                if order["order_id"] in self._checking:
                    continue
                order["telegram_username"] = username
                order["error"] = None
                # Запоминаем активный node чата, чтобы ответы точно дошли.
                if getattr(message, "chat_id", None) is not None:
                    order["chat_id"] = message.chat_id
                to_recheck.append(order)
            if to_recheck:
                self.storage.upsert(*to_recheck)
                for order in to_recheck:
                    threading.Thread(
                        target=self._check_username, args=(order,), daemon=True
                    ).start()
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка обработки сообщения: {e}")

    def _parse_username(self, text: str) -> str | None:
        if text.lower().startswith(("/stars", "!stars")):
            parts = text.split()
            if len(parts) >= 2:
                m = USERNAME_RE.search(parts[1])
                return m.group(1) if m else None
            return None
        m = re.match(r"^@?([a-zA-Z0-9_]{4,32})$", text)
        return m.group(1) if m else None

    # ---------- цикл перевода ----------

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(2, int(self.config.get("loop_interval_sec", 5)))
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            if self._loop_busy or not self.fragment or not self.wallet:
                continue
            self._loop_busy = True
            try:
                # Проверяем баланс на пороговое значение до обработки заказов.
                # Если включён low_balance_threshold или цикл уже на паузе — делаем запрос.
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
        prepared = []
        for order in orders:
            transfer = self._prepare_transfer(order)
            if transfer:
                prepared.append((order, transfer))
        self.storage.upsert(*orders)
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
            result = self.wallet.transfer([t for _, t in fit])
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка перевода TON: {e}")
            self._fail(fit_orders, ERR_TRANSFER)
            return

        for order in fit_orders:
            order["status"] = ST_DONE
            order["error"] = None
            order["transaction_hash"] = result["hash"]
        self.storage.upsert(*fit_orders)
        logger.info(f"{LOGGER_PREFIX} Звёзды переведены. Хэш: {result['hash']}.")
        for order in fit_orders:
            self._on_success(order)

    def _prepare_transfer(self, order: dict) -> dict | None:
        try:
            req = self.fragment.init_buy_stars_request(order["recipient_id"], order["stars_amount"])
            link = self.fragment.get_buy_stars_link(req["req_id"], self.config.get("show_sender", False))
            msg = link["transaction"]["messages"][0]
            order["ref"] = extract_ref(msg.get("payload", ""))
            return {
                "address": msg["address"],
                "amount": int(msg["amount"]),
                "body": self._build_body(order, msg),
                "valid_until": int(link["transaction"]["validUntil"]),
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"{LOGGER_PREFIX} Ошибка получения ссылки Fragment "
                         f"по заказу {order['order_id']}: {e}")
            order["status"], order["error"] = ST_ERROR, ERR_UNABLE_TO_FETCH_LINK
            return None

    def _build_body(self, order: dict, msg: dict):
        # По умолчанию используем payload Fragment как есть (надёжнее всего).
        if self.config.get("show_ad") and order.get("ref"):
            return f"{AD_TEXT}\n\n{order['ref']}"
        return Cell.one_from_boc(base64.b64decode(_pad_b64(msg["payload"])))

    def _fail(self, orders: list[dict], error: str) -> None:
        for order in orders:
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
        if self.config.get("refund_on_error"):
            self._refund(order)

    def _refund(self, order: dict) -> None:
        old_status = order["status"]
        order["status"] = ST_REFUNDED
        self.storage.upsert(order)
        for _ in range(3):
            try:
                self.cardinal.account.refund(order["order_id"])
                logger.info(f"{LOGGER_PREFIX} Возврат по заказу {order['order_id']} выполнен.")
                return
            except Exception as e:  # noqa: BLE001
                logger.error(f"{LOGGER_PREFIX} Не удалось вернуть средства "
                             f"по заказу {order['order_id']}: {e}")
                time.sleep(1)
        order["status"] = old_status
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
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
        kb.add(B(f"🔐 Сид-фраза V5R1: {is_set(cfg.get('ton_mnemonic'))}",
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
            status = f"Fragment {fr} · Кошелёк {wl}{pause}"
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
BIND_TO_POST_STOP = [on_stop]
BIND_TO_DELETE = None
