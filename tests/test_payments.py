"""Payment recovery tests. Network requests and TON signing are always simulated."""
import base64
import copy
import hashlib
import importlib.util
import json
import logging
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


if importlib.util.find_spec("requests") is None:
    sys.modules["requests"] = types.ModuleType("requests")

SPEC = importlib.util.spec_from_file_location("autostars_test", pathlib.Path(__file__).parents[1] / "autostars.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)
p.logger.setLevel(logging.CRITICAL)


class FakeAddress:
    def __init__(self, value):
        self.value = str(value).removeprefix("friendly:")

    def to_str(self, **kwargs):
        return self.value


class FakeCell:
    def __init__(self, raw):
        self.raw = raw
        self.hash = hashlib.sha256(raw).digest()

    @classmethod
    def one_from_boc(cls, raw):
        return cls(raw)

    def begin_parse(self):
        return types.SimpleNamespace(load_snake_string=lambda: self.raw.decode())


class FakeBuilder:
    def __init__(self):
        self.raw = b""

    def store_uint(self, value, bits):
        self.raw += value.to_bytes(bits // 8, "big")
        return self

    def store_snake_string(self, value):
        self.raw += value.encode()
        return self

    def end_cell(self):
        return FakeCell(self.raw)


class FakeOffline:
    def __init__(self, address="wallet"):
        self.address = address
        self.seqnos = []

    def address_str(self):
        return self.address

    def build_external_transfer(self, seqno, transfers):
        self.seqnos.append(seqno)
        data = {
            "seqno": seqno,
            "wallet": self.address,
            "outgoing": [{
                "destination": {"address": t["address"]},
                "value": t["amount"],
                "raw_body": t["body"].raw.hex(),
                "msg_type": "int_msg", "bounced": False,
            } for t in transfers],
        }
        boc = json.dumps(data, sort_keys=True).encode().hex()
        return boc, hashlib.sha256(bytes.fromhex(boc)).hexdigest()


class FakeTonAPI(p.TonAPI):
    def __init__(self):
        super().__init__("mock")
        self.balance = 100 * p.ONE_TON
        self.seqno = 10
        self.cached_seqno = None
        self.broadcasts = []
        self.transactions = {}
        self.visible = True
        self.post_timeout = False
        self.lookup_error = False
        self.failed_tx = False
        self.before_broadcast = None

    def _request(self, method, path, body=None):
        if path.startswith("/v2/wallet/"):
            if path.endswith("/seqno"):
                return {"seqno": self.seqno if self.cached_seqno is None else self.cached_seqno}
            return {"balance": self.balance, "is_wallet": True}
        if path == "/v2/blockchain/message":
            if self.before_broadcast:
                self.before_broadcast()
            boc = body["boc"]
            self.broadcasts.append(boc)
            message_hash = hashlib.sha256(bytes.fromhex(boc)).hexdigest()
            if message_hash not in self.transactions:
                data = json.loads(bytes.fromhex(boc))
                self.transactions[message_hash] = {
                    "hash": "tx-" + message_hash,
                    "account": {"address": data["wallet"]},
                    "success": not self.failed_tx, "aborted": self.failed_tx,
                    "compute_phase": {"success": not self.failed_tx},
                    "action_phase": {"success": not self.failed_tx},
                    "out_msgs": [] if self.failed_tx else data["outgoing"],
                }
                if not self.failed_tx:
                    self.seqno = data["seqno"] + 1
            if self.post_timeout:
                raise p.TonAPIError("HTTP timeout after accepted broadcast")
            return {}
        if path.startswith("/v2/blockchain/messages/"):
            if self.lookup_error:
                raise p.TonAPIError("temporary indexer outage")
            message_hash = path.split("/")[-2]
            return self.transactions.get(message_hash) if self.visible else None
        raise AssertionError(path)


class FakeFragment:
    def __init__(self):
        self.calls = []
        self.broken = False
        self.multiple_messages = False

    def init_buy_stars_request(self, recipient, quantity):
        self.calls.append((recipient, quantity))
        if self.broken:
            raise p.FragmentError("initBuyStarsRequest", "HTTP 503")
        return {"req_id": "r" + str(len(self.calls))}

    def get_buy_stars_link(self, request_id, show_sender):
        message = {
            "address": "friendly:fragment", "amount": str(2 * p.ONE_TON),
            "payload": base64.b64encode(("Ref#" + request_id).encode()).decode(),
        }
        return {"transaction": {
            "validUntil": int(p.time.time()) + 300,
            "messages": [message, message] if self.multiple_messages else [message],
        }}


class FakeCardinal:
    def __init__(self):
        self.messages = []
        self.refunds = []
        self.notifications = []
        self.account = types.SimpleNamespace(id=999, refund=self.refunds.append)
        self.telegram = types.SimpleNamespace(bot=types.SimpleNamespace(
            send_message=lambda *args: self.notifications.append(args)))

    def send_message(self, chat_id, text, buyer_name):
        self.messages.append((chat_id, text))


class Ticks:
    def __init__(self, count):
        self.left = count
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, interval):
        if self.left:
            self.left -= 1
        else:
            self.stopped = True


def order(order_id="O1"):
    return {
        "order_id": order_id, "chat_id": order_id, "buyer_id": 2, "buyer_name": "buyer",
        "stars_amount": 50, "telegram_username": "validuser", "recipient_id": "recipient",
        "status": p.ST_READY, "error": None, "retries_left": 3,
        "transaction_hash": None, "ref": None,
    }


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(pathlib.Path(self.temp.name) / "orders.json")
        for name, value in [("Address", FakeAddress), ("Cell", FakeCell), ("begin_cell", FakeBuilder)]:
            patcher = mock.patch.object(p, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s = self.service()

    def service(self, tonapi=None, address="wallet"):
        s = p.AutoStarsService.__new__(p.AutoStarsService)
        s.cardinal = FakeCardinal()
        s.config = copy.deepcopy(p.DEFAULT_CONFIG)
        s.config["refund_on_error"] = True
        s.config["admin_chat_id"] = 42
        s.storage = p.Storage(self.path)
        s.tonapi = tonapi or FakeTonAPI()
        s.wallet = p.Wallet(FakeOffline(address), s.tonapi)
        s.wallet._min_seqno = s.storage.get_confirmed_seqno(address)
        s.fragment = FakeFragment()
        s._provider_lock = threading.RLock()
        s._checking_lock = threading.Lock()
        s._checking = set()
        s._loop_busy = False
        s._low_balance_paused = False
        s._bot = s.cardinal.telegram.bot
        s._admin_chat_id = 42
        return s

    def run_cycles(self, count=1, service=None):
        s = service or self.s
        s._stop = Ticks(count)
        s._loop()

    def start_pending(self):
        self.s.tonapi.visible = False
        self.s.storage.upsert(order())
        self.run_cycles()
        return self.s.storage.get("O1")

    def test_journal_is_saved_before_broadcast(self):
        def check():
            saved = p.Storage(self.path).get("O1")
            self.assertEqual(saved["status"], p.ST_TRANSFERRING)
            self.assertEqual(saved["payment_attempt"]["state"], "PENDING")
            self.assertTrue(saved["payment_attempt"]["boc"])
            self.assertTrue(saved["payment_attempt"]["in_msg_hash"])
            self.assertEqual(saved["expected_message"]["request_id"], "r1")
        self.s.tonapi.before_broadcast = check
        self.s.storage.upsert(order())
        self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)

    def test_post_timeout_does_not_create_second_purchase(self):
        self.s.tonapi.post_timeout = True
        saved = self.start_pending()
        self.run_cycles(3)
        self.assertEqual(len(self.s.fragment.calls), 1)
        self.assertEqual(len(self.s.wallet.offline.seqnos), 1)
        self.assertEqual(self.s.storage.get("O1")["retries_left"], saved["retries_left"])
        self.assertEqual(self.s.cardinal.refunds, [])
        self.s.tonapi.visible = True
        self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.s.cardinal.messages), 1)

    def test_indexer_outage_keeps_payment_pending(self):
        self.s.tonapi.lookup_error = True
        self.s.storage.upsert(order())
        self.run_cycles(3)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(len(self.s.fragment.calls), 1)
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_pending_payment_blocks_new_orders(self):
        self.start_pending()
        self.s.storage.upsert(order("O2"))
        self.run_cycles(3)
        self.assertEqual(self.s.storage.get("O2")["retries_left"], 3)
        self.assertEqual(len(self.s.fragment.calls), 1)

    def test_same_boc_rebroadcast_cannot_create_second_chain_payment(self):
        saved = self.start_pending()
        with mock.patch.object(p.time, "time", return_value=saved["payment_attempt"]["created_at"] + 40):
            self.run_cycles()
        self.assertEqual(len(self.s.tonapi.broadcasts), 2)
        self.assertEqual(len(set(self.s.tonapi.broadcasts)), 1)
        self.assertEqual(len(self.s.tonapi.transactions), 1)
        self.assertEqual(len(self.s.fragment.calls), 1)

    def test_restart_after_accepted_payment_uses_saved_hash(self):
        saved = self.start_pending()
        restarted = self.service(self.s.tonapi)
        restarted.tonapi.visible = True
        self.run_cycles(service=restarted)
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(restarted.wallet.offline.seqnos, [])
        self.assertEqual(len(restarted.tonapi.broadcasts), 1)
        self.assertEqual(restarted.storage.get("O1")["payment_attempt"]["in_msg_hash"], saved["payment_attempt"]["in_msg_hash"])

    def test_restart_before_broadcast_reuses_saved_signed_bytes(self):
        def crash():
            raise SystemExit("process death before broadcast")
        self.s.tonapi.before_broadcast = crash
        self.s.storage.upsert(order())
        with self.assertRaises(SystemExit):
            self.run_cycles()
        saved = self.s.storage.get("O1")
        self.assertEqual(self.s.tonapi.broadcasts, [])
        self.s.tonapi.before_broadcast = None
        restarted = self.service(self.s.tonapi)
        with mock.patch.object(p.time, "time", return_value=saved["payment_attempt"]["created_at"] + 40):
            self.run_cycles(2, restarted)
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(restarted.tonapi.broadcasts, [saved["payment_attempt"]["boc"]])
        self.assertEqual(restarted.fragment.calls, [])

    def test_failed_transaction_is_not_done_or_success_message(self):
        self.s.tonapi.failed_tx = True
        self.s.storage.upsert(order())
        self.run_cycles()
        saved = self.s.storage.get("O1")
        self.assertEqual(saved["status"], p.ST_ERROR)
        self.assertEqual(saved["payment_attempt"]["state"], "FAILED")
        self.assertEqual(self.s.wallet._min_seqno, 0)
        self.assertEqual(self.s.cardinal.messages, [])

    def test_failed_transaction_can_retry_only_after_confirmed_no_outputs(self):
        self.s.tonapi.failed_tx = True
        self.s.storage.upsert(order())
        self.run_cycles()
        self.s.tonapi.failed_tx = False
        self.run_cycles()
        saved = self.s.storage.get("O1")
        self.assertEqual(saved["status"], p.ST_DONE)
        self.assertEqual(saved["payment_history"][0]["state"], "FAILED")
        self.assertEqual(len(self.s.fragment.calls), 2)

    def test_unsuccessful_transaction_with_outputs_requires_review(self):
        saved = self.start_pending()
        tx = self.s.tonapi.transactions[saved["payment_attempt"]["in_msg_hash"]]
        tx["success"] = False
        self.s.tonapi.visible = True
        self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_wrong_address_amount_or_payload_does_not_confirm(self):
        for field, value in [("destination", {"address": "someone-else"}), ("value", 1), ("raw_body", b"other-ref".hex())]:
            with self.subTest(field=field):
                self.s = self.service()
                self.s.storage.orders = {}
                saved = self.start_pending()
                tx = self.s.tonapi.transactions[saved["payment_attempt"]["in_msg_hash"]]
                tx["out_msgs"][0][field] = value
                self.s.tonapi.visible = True
                self.run_cycles()
                self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
                self.assertEqual(self.s.cardinal.messages, [])
                self.assertEqual(self.s.cardinal.refunds, [])

    def test_invalid_boc_decoder_error_requires_review(self):
        self.start_pending()
        self.s.tonapi.visible = True
        with mock.patch.object(FakeCell, "one_from_boc", side_effect=RuntimeError("invalid BOC")):
            self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_wrong_wallet_and_incomplete_result_require_review(self):
        for mutation in (lambda tx: tx.update(account={"address": "other-wallet"}), lambda tx: tx.pop("success")):
            with self.subTest(mutation=mutation):
                self.s = self.service()
                self.s.storage.orders = {}
                saved = self.start_pending()
                mutation(self.s.tonapi.transactions[saved["payment_attempt"]["in_msg_hash"]])
                self.s.tonapi.visible = True
                self.run_cycles()
                self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
                self.assertEqual(self.s.cardinal.refunds, [])

    def test_partial_batch_confirms_only_matched_order(self):
        self.s.tonapi.visible = False
        self.s.storage.upsert(order("O1"), order("O2"))
        self.run_cycles()
        saved = self.s.storage.get("O1")
        self.s.tonapi.transactions[saved["payment_attempt"]["in_msg_hash"]]["out_msgs"].pop()
        self.s.tonapi.visible = True
        self.run_cycles(2)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(len(self.s.cardinal.messages), 1)
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_one_output_cannot_confirm_two_identical_expectations_across_polls(self):
        self.s.tonapi.visible = False
        self.s.storage.upsert(order("O1"), order("O2"))
        self.run_cycles()
        one, two = self.s.storage.get("O1"), self.s.storage.get("O2")
        two["expected_message"] = copy.deepcopy(one["expected_message"])
        self.s.storage.upsert(two)
        self.s.tonapi.transactions[one["payment_attempt"]["in_msg_hash"]]["out_msgs"].pop()
        self.s.tonapi.visible = True
        self.run_cycles(2)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_NEEDS_REVIEW)

    def test_expired_unknown_payment_does_not_retry_or_refund(self):
        saved = self.start_pending()
        with mock.patch.object(p.time, "time", return_value=saved["payment_attempt"]["valid_until"] + p.PAYMENT_REVIEW_GRACE_SEC + 1):
            self.run_cycles(3)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(len(self.s.tonapi.broadcasts), 1)
        self.assertEqual(len(self.s.fragment.calls), 1)
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertEqual(len(self.s.cardinal.notifications), 1)

    def test_late_confirmation_resolves_review(self):
        saved = self.start_pending()
        with mock.patch.object(p.time, "time", return_value=saved["payment_attempt"]["valid_until"] + p.PAYMENT_REVIEW_GRACE_SEC + 1):
            self.run_cycles()
            self.s.tonapi.visible = True
            self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.storage.get_pending_orders(), [])

    def test_legacy_unfinished_payment_without_hash_stays_blocked(self):
        legacy = order()
        legacy["status"] = p.ST_TRANSFERRING
        self.s.storage.upsert(legacy, order("O2"))
        self.run_cycles(3)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(len(self.s.storage.get_pending_orders()), 1)
        self.assertEqual(self.s.fragment.calls, [])

    def test_disk_write_failure_prevents_broadcast(self):
        self.s.storage.upsert(order())
        real_save = self.s.storage.save
        def save():
            if any(o["status"] == p.ST_TRANSFERRING for o in self.s.storage.orders.values()):
                raise OSError("disk full")
            real_save()
        with mock.patch.object(self.s.storage, "save", side_effect=save):
            self.run_cycles()
        self.assertEqual(self.s.tonapi.broadcasts, [])
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_READY)
        self.assertNotIn("payment_attempt", p.Storage(self.path).get("O1"))

    def test_atomic_replace_failure_preserves_old_history(self):
        self.s.storage.upsert(order())
        with mock.patch.object(p.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.s.storage.upsert(order("O2"))
        self.assertFalse(self.s.storage.has("O2"))
        self.assertFalse(p.Storage(self.path).has("O2"))
        self.assertTrue(p.Storage(self.path).has("O1"))

    def test_corrupt_history_fails_closed(self):
        pathlib.Path(self.path).write_text('{"O1":', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            p.Storage(self.path)

    def test_legacy_transfer_error_is_not_automatically_retried(self):
        legacy = order()
        legacy["status"], legacy["error"] = p.ST_ERROR, p.ERR_TRANSFER
        self.s.storage.upsert(legacy)
        restarted = self.service()
        self.run_cycles(3, restarted)
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(restarted.fragment.calls, [])
        self.assertEqual(restarted.cardinal.refunds, [])

    def test_stale_seqno_fix_is_preserved(self):
        self.s.tonapi.cached_seqno = 10
        self.s.storage.upsert(order())
        self.run_cycles()
        self.s.storage.upsert(order("O2"))
        self.run_cycles()
        self.assertEqual(self.s.wallet.offline.seqnos, [10, 11])
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_DONE)

    def test_confirmed_seqno_is_restored_after_restart(self):
        self.s.tonapi.cached_seqno = 10
        self.s.storage.upsert(order())
        self.run_cycles()
        restarted = self.service(self.s.tonapi)
        restarted.storage.upsert(order("O2"))
        self.run_cycles(service=restarted)
        self.assertEqual(restarted.wallet.offline.seqnos, [11])

    def test_reload_restores_confirmed_seqno(self):
        self.s.storage.upsert(order())
        self.run_cycles()
        self.s.config["ton_mnemonic"] = "mock phrase"
        new_wallet = p.Wallet(FakeOffline(), self.s.tonapi)
        with mock.patch.object(p.Wallet, "from_mnemonic", return_value=new_wallet):
            self.s.reload_providers()
        self.assertEqual(self.s.wallet._min_seqno, 11)

    def test_service_initialization_restores_seqno_and_notification_chat(self):
        self.s.storage.upsert(order())
        self.run_cycles()
        config = copy.deepcopy(self.s.config)
        config["ton_mnemonic"] = "mock phrase"
        wallet = p.Wallet(FakeOffline(), self.s.tonapi)
        with mock.patch.object(p, "Storage", return_value=self.s.storage), \
             mock.patch.object(p, "TonAPI", return_value=self.s.tonapi), \
             mock.patch.object(p.Wallet, "from_mnemonic", return_value=wallet), \
             mock.patch.object(p.threading, "Thread"):
            restarted = p.AutoStarsService(self.s.cardinal, config)
        self.assertEqual(restarted.wallet._min_seqno, 11)
        self.assertEqual(restarted._admin_chat_id, 42)
        self.assertIs(restarted._bot, self.s.cardinal.telegram.bot)

    def test_expiring_fragment_quote_is_not_broadcast(self):
        original = self.s.fragment.get_buy_stars_link
        def expiring(*args):
            link = original(*args)
            link["transaction"]["validUntil"] = int(p.time.time()) + 1
            return link
        self.s.fragment.get_buy_stars_link = expiring
        self.s.storage.upsert(order())
        self.run_cycles()
        self.assertEqual(self.s.tonapi.broadcasts, [])
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_ERROR)

    def test_wallet_change_does_not_rebroadcast_from_previous_wallet(self):
        saved = self.start_pending()
        restarted = self.service(self.s.tonapi, address="different-wallet")
        with mock.patch.object(p.time, "time", return_value=saved["payment_attempt"]["created_at"] + 40):
            self.run_cycles(service=restarted)
        self.assertEqual(len(restarted.tonapi.broadcasts), 1)
        restarted.tonapi.visible = True
        self.run_cycles(service=restarted)
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(restarted.wallet._min_seqno, 0)

    def test_recovery_runs_without_fragment_or_wallet(self):
        self.start_pending()
        self.s.fragment = None
        self.s.wallet = None
        self.s.tonapi.visible = True
        self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)

    def test_multiple_fragment_messages_are_rejected_before_payment(self):
        self.s.fragment.multiple_messages = True
        self.s.storage.upsert(order())
        self.run_cycles()
        self.assertEqual(self.s.tonapi.broadcasts, [])

    def test_fragment_errors_notify_and_finish_after_last_attempt(self):
        self.s.fragment.broken = True
        self.s.storage.upsert(order())
        self.run_cycles(3)
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_REFUNDED)
        self.assertEqual(self.s.cardinal.refunds, ["O1"])
        self.assertEqual(len(self.s.cardinal.messages), 1)
        self.assertEqual(len(self.s.cardinal.notifications), 1)

    def test_refund_timeout_is_not_retried_automatically(self):
        def refund(order_id):
            self.s.cardinal.refunds.append(order_id)
            raise TimeoutError("response lost after refund")
        self.s.cardinal.account.refund = refund
        self.s.fragment.broken = True
        self.s.storage.upsert(order())
        self.run_cycles(5)
        self.assertEqual(self.s.cardinal.refunds, ["O1"])
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)

    def test_refund_guard_rejects_unresolved_payment(self):
        saved = self.start_pending()
        self.s._refund(saved)
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)

    def test_refund_guard_rejects_confirmed_payment(self):
        self.s.storage.upsert(order())
        self.run_cycles()
        self.s._refund(self.s.storage.get("O1"))
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)

    def test_ad_payload_matches_the_signed_comment(self):
        self.s.config["show_ad"] = True
        self.s.storage.upsert(order())
        self.run_cycles()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)

    def test_ready_orders_are_copies_not_shared_mutable_state(self):
        self.s.storage.upsert(order())
        ready = self.s.storage.get_ready_orders()
        ready[0]["status"] = p.ST_DONE
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_READY)


if __name__ == "__main__":
    unittest.main()
