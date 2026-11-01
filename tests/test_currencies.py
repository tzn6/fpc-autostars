"""Currency selection and real TEP-74 cells, with a simulated TON network."""
import base64
import binascii
import copy
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import test_payments as helpers

p = helpers.p


def address(byte):
    return p.Address((0, bytes([byte]) * 32)).to_str(is_user_friendly=False)


def cell(op, query, amount, owner):
    return (p.begin_cell().store_uint(op, 32).store_uint(query, 64)
            .store_coins(amount).store_address(p.Address(owner)).end_cell())


class CurrencyFragment:
    def __init__(self, owner, jetton_wallet, recipient):
        self.owner, self.jetton_wallet, self.recipient = owner, jetton_wallet, recipient
        self.currencies = []
        self.amount = 2 * p.ONE_USDT
        self.destination_override = None
        self.body_override = None
        self.confirmations = []
        self.confirm_ok = True
        self.accounts = []

    def init_buy_stars_request(self, recipient, quantity, currency="TON"):
        self.currencies.append(currency)
        return {"req_id": str(len(self.currencies))}

    def get_buy_stars_link(self, request_id, show_sender, account=None):
        self.accounts.append(account)
        body = (p.begin_cell().store_uint(0x0F8A7EA5, 32).store_uint(int(request_id), 64)
                .store_coins(self.amount).store_address(p.Address(self.recipient))
                .store_address(p.Address(self.owner)).store_bit(0).store_coins(1)
                .store_bit(1).store_ref(p.begin_cell().store_uint(0, 32)
                                       .store_snake_string("Ref#test").end_cell()).end_cell())
        body = self.body_override or body
        return {"confirm_method": "confirmReq", "confirm_params": {"id": request_id},
                "transaction": {"validUntil": int(p.time.time()) + 300, "messages": [{
            "address": self.destination_override or self.jetton_wallet,
            "amount": str(p.ONE_TON // 20), "payload": base64.b64encode(body.to_boc()).decode(),
        }]}}

    def confirm_stars_payment(self, method, params, boc, account):
        self.confirmations.append((method, params, boc, account))
        if not self.confirm_ok:
            raise p.FragmentError(method, "temporary failure")


class Offline:
    def __init__(self, api):
        self.api = api
        self.seqnos = []

    def address_str(self):
        return self.api.owner

    def tonconnect_account(self):
        return {"address": self.api.owner, "chain": "-239", "walletStateInit": "AA==", "publicKey": "00"}

    def build_external_transfer(self, seqno, transfers):
        self.seqnos.append(seqno)
        self.api.transfers = transfers
        self.api.current_message_hash = f"message-hash-{seqno}"
        return "00aabb", self.api.current_message_hash


class Network:
    def __init__(self):
        self.owner, self.jetton_wallet, self.recipient, self.recipient_jetton = map(address, range(1, 5))
        self.balance = 100 * p.ONE_TON
        self.usdt = 10 * p.ONE_USDT
        self.seqno = 7
        self.cached_seqno = None
        self.broadcasts = []
        self.transactions = {}
        self.traces = {}
        self.tx = None
        self.trace = None
        self.trace_visible = False
        self.fail_recipient = False
        self.invalid_trace_amount = False
        self.notification_skipped = None
        self.notification_bounce = False
        self.transfers = []

    def get_wallet(self, address_):
        return {"balance": self.balance, "is_wallet": True}

    def get_usdt_balance(self, address_):
        return self.usdt, self.jetton_wallet if self.usdt else None

    def get_seqno(self, address_):
        return self.seqno if self.cached_seqno is None else self.cached_seqno

    def send_boc(self, boc):
        self.broadcasts.append(boc)
        root_messages, children = [], []
        for index, transfer in enumerate(self.transfers):
            query, amount, recipient, _ = p._jetton_fields(transfer["body"], 0x0F8A7EA5)
            root_message = {"hash": f"root-{index}", "destination": {"address": transfer["address"]},
                            "source": {"address": self.owner}, "bounce": True,
                            "value": transfer["amount"], "raw_body": transfer["body"].to_boc().hex(),
                            "msg_type": "int_msg", "bounced": False}
            internal = {"hash": f"internal-{index}", "destination": {"address": self.recipient_jetton},
                        "source": {"address": self.jetton_wallet}, "bounce": True, "value": 10_000_000,
                        "raw_body": cell(0x178D4519, query, amount, self.owner).to_boc().hex(),
                        "msg_type": "int_msg", "bounced": False}
            notification = {"hash": f"notification-{index}", "destination": {"address": recipient},
                            "source": {"address": self.recipient_jetton},
                            "bounce": self.notification_bounce, "value": 1,
                            "raw_body": cell(0x7362D09C, query,
                                             amount + int(self.invalid_trace_amount), self.owner).to_boc().hex(),
                            "msg_type": "int_msg", "bounced": False}

            def node(account, incoming, outgoing, children_, success=True):
                return {"transaction": {"account": {"address": account}, "in_msg": incoming,
                                        "success": success, "aborted": not success, "out_msgs": outgoing},
                        "children": children_}

            delivered = node(recipient, notification, [], [], not bool(self.notification_skipped))
            if self.notification_skipped:
                delivered["transaction"].update(
                    orig_status="uninit" if self.notification_skipped == "cskip_no_state" else "active",
                    end_status="uninit" if self.notification_skipped == "cskip_no_state" else "active",
                    compute_phase={"skipped": True, "skip_reason": self.notification_skipped},
                    credit_phase={"credit": 1, "fees_collected": 0})
            # TonAPI places consumed messages in child in_msg entries in the trace.
            receiver = node(self.recipient_jetton, internal, [], [delivered], not self.fail_recipient)
            sender = node(self.jetton_wallet, root_message, [], [receiver])
            root_messages.append(root_message)
            children.append(sender)
        self.tx = {"hash": f"transaction-hash-{len(self.broadcasts)}", "account": {"address": self.owner},
                   "success": True, "aborted": False, "out_msgs": root_messages}
        self.trace = {"transaction": {**self.tx, "out_msgs": []}, "children": children}
        self.transactions[self.current_message_hash] = self.tx
        self.traces[self.tx["hash"]] = self.trace
        self.seqno += 1

    def get_transaction_by_message_hash(self, message_hash):
        return self.transactions.get(message_hash)

    def get_trace(self, transaction_hash):
        return self.traces.get(transaction_hash) if self.trace_visible else None


@unittest.skipUnless(p.PYTONIQ_AVAILABLE, "install requirements.txt for real TON cells")
class CurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.api = Network()
        self.s = p.AutoStarsService.__new__(p.AutoStarsService)
        self.s.cardinal = helpers.FakeCardinal()
        self.s.config = copy.deepcopy(p.DEFAULT_CONFIG)
        self.s.config["payment_currency"] = "USDT"
        self.s.config["show_ad"] = True
        self.s.storage = p.Storage(str(pathlib.Path(temp.name) / "orders.json"))
        self.s.tonapi = self.api
        self.s.wallet = p.Wallet(Offline(self.api), self.api)
        self.s.fragment = CurrencyFragment(self.api.owner, self.api.jetton_wallet, self.api.recipient)
        self.s._provider_lock = threading.RLock()
        self.s._loop_busy = False
        self.s._low_balance_paused = False
        self.s._bot = self.s.cardinal.telegram.bot
        self.s._admin_chat_id = 42

    def start(self, *orders):
        self.s.storage.upsert(*orders)
        self.s._transfer_batch(list(orders))

    def test_fragment_requests_the_selected_payment_method(self):
        fragment = p.FragmentAPI("cookies", "hash")
        with mock.patch.object(fragment, "_post", return_value={"req_id": "ok"}) as post:
            fragment.init_buy_stars_request("recipient", 50)
            self.assertEqual(post.call_args.args[0]["payment_method"], "ton")
            fragment.init_buy_stars_request("recipient", 50, "USDT")
            self.assertEqual(post.call_args.args[0]["payment_method"], "usdt_ton")
            with self.assertRaises(p.FragmentError):
                fragment.init_buy_stars_request("recipient", 50, "BTC")

    def test_base64_decoder_accepts_standard_and_urlsafe_without_losing_bytes(self):
        raw = b"\xfb\xff\xff\x00\xfe"
        for encoded in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
            for payload in (encoded, encoded.rstrip("=")):
                with self.subTest(payload=payload):
                    self.assertEqual(p._decode_fragment_payload(payload), raw)
        for payload in ("a", "bad*chars", "%%%", "AA==junk", "", "☃"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    p._decode_fragment_payload(payload)

    def test_urlsafe_usdt_quote_reproduces_181_character_error(self):
        body = (p.begin_cell().store_uint(0x0F8A7EA5, 32).store_uint(0, 64)
                .store_coins(2 * p.ONE_USDT).store_address(p.Address(self.api.recipient))
                .store_address(p.Address(self.api.owner)).store_bit(0).store_coins(1).store_bit(1)
                .store_ref(p.begin_cell().store_uint(0, 32)
                           .store_snake_string("Ref#" + "X" * 28).end_cell()).end_cell())
        payload = base64.urlsafe_b64encode(body.to_boc()).decode().rstrip("=")
        self.assertIn("-", payload)
        with self.assertRaisesRegex(binascii.Error, "181"):
            base64.b64decode(p._pad_b64(payload))
        self.s.fragment.body_override = body
        original_link = self.s.fragment.get_buy_stars_link

        def urlsafe_link(*args):
            link = original_link(*args)
            link["transaction"]["messages"][0]["payload"] = payload
            return link

        self.s.fragment.get_buy_stars_link = urlsafe_link
        self.api.trace_visible = True
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 1)
        self.assertEqual(self.api.transfers[0]["body"].hash, body.hash)

    def test_urlsafe_ton_quote_preserves_reference_and_payload(self):
        body = p.begin_cell().store_uint(0, 32).store_snake_string("Ref#" + "X" * 116).end_cell()
        payload = base64.urlsafe_b64encode(body.to_boc()).decode().rstrip("=")
        self.assertIn("-", payload)
        with self.assertRaises(binascii.Error):
            base64.b64decode(p._pad_b64(payload))
        self.s.config["show_ad"] = False
        link = {"transaction": {"validUntil": int(p.time.time()) + 300, "messages": [{
            "address": address(9), "amount": str(p.ONE_TON), "payload": payload,
        }]}}
        with mock.patch.object(self.s.fragment, "get_buy_stars_link", return_value=link):
            current = helpers.order()
            transfer = self.s._prepare_transfer(current, "TON")
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer["body"].hash, body.hash)
        self.assertEqual(current["ref"], "Ref#" + "X" * 116)

    def test_invalid_base64_does_not_broadcast_or_damage_payment_history(self):
        original_link = self.s.fragment.get_buy_stars_link

        def invalid_link(*args):
            link = original_link(*args)
            link["transaction"]["messages"][0]["payload"] = "broken*payload"
            return link

        self.s.fragment.get_buy_stars_link = invalid_link
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["error"], p.ERR_UNABLE_TO_FETCH_LINK)
        self.assertNotIn("payment_attempt", self.s.storage.get("O1"))
        self.assertEqual(self.api.broadcasts, [])

    def test_confirmation_sends_saved_boc_and_wallet_account(self):
        fragment = p.FragmentAPI("cookies", "hash")
        account = {"address": self.api.owner}
        with mock.patch.object(fragment, "_post", return_value={"ok": True}) as post:
            fragment.confirm_stars_payment("confirmReq", {"id": "r1"}, "00aabb", account)
            payload = post.call_args.args[0]
            self.assertEqual(payload["boc"], "AKq7")
            self.assertEqual(payload["id"], "r1")
            self.assertIn(self.api.owner, payload["account"])
            with self.assertRaises(p.FragmentError):
                fragment.confirm_stars_payment("unknown", {}, "00aabb", account)

    def test_usdt_link_request_includes_paying_wallet(self):
        fragment = p.FragmentAPI("cookies", "hash")
        account = {"address": self.api.owner}
        response = {"transaction": {"messages": [{"address": self.api.jetton_wallet}]}}
        with mock.patch.object(fragment, "_post", return_value=response) as post:
            fragment.get_buy_stars_link("r1", account=account)
            payload = post.call_args.args[0]
            self.assertIn(self.api.owner, payload["account"])
            self.assertIn("SendTransaction", payload["device"])

    def test_waits_for_usdt_delivery_and_survives_currency_switch(self):
        self.start(helpers.order())
        saved = self.s.storage.get("O1")
        self.assertEqual(saved["status"], p.ST_TRANSFERRING)
        self.assertEqual(saved["expected_message"]["currency"], "USDT")
        self.assertEqual(saved["expected_message"]["jetton_amount"], 2 * p.ONE_USDT)
        self.assertEqual(self.s.fragment.currencies, ["USDT"])
        self.assertEqual(self.s.fragment.accounts[0]["address"], self.api.owner)
        self.assertEqual(self.s.cardinal.messages, [])
        p.switch_payment_currency(self.s.config, "TON")
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 1)
        self.assertEqual(len(self.s.cardinal.messages), 1)
        self.assertEqual(self.s.fragment.confirmations[0][:3], ("confirmReq", {"id": "1"}, "00aabb"))

    def test_fragment_confirmation_failure_keeps_paid_order_pending(self):
        self.api.trace_visible = True
        self.s.fragment.confirm_ok = False
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertTrue(self.s._has_blocking_pending())
        self.s.fragment.confirm_ok = True
        self.s._reconcile_pending()
        self.assertEqual(len(self.s.fragment.confirmations), 1)
        retry_at = self.s.storage.get("O1")["payment_attempt"]["fragment_confirm_retry_at"]
        with mock.patch.object(p.time, "time", return_value=retry_at + 1):
            self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 1)

    def test_unknown_confirmation_error_blocks_and_does_not_log_server_secrets(self):
        self.api.trace_visible = True
        secret = "cookie_or_hash_should_not_be_logged"
        self.s.fragment.confirm_stars_payment = mock.Mock(
            side_effect=p.FragmentError("confirmReq", f"error {secret}"))
        with mock.patch.object(p.logger, "warning") as warning:
            self.start(helpers.order())
        self.assertTrue(self.s._has_blocking_pending())
        self.assertNotIn("fragment_confirmation_expired", self.s.storage.get("O1")["payment_attempt"])
        self.assertNotIn(secret, str(warning.call_args_list))
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def expire_first_confirmation(self):
        self.api.trace_visible = True

        def confirm(method, params, boc, account):
            self.s.fragment.confirmations.append((method, params, boc, account))
            if params["id"] == "1":
                raise p.FragmentError(method, "Session expired")

        self.s.fragment.confirm_stars_payment = confirm
        self.start(helpers.order())

    def run_cycle(self, service=None):
        service = service or self.s
        service._stop = helpers.Ticks(1)
        service._loop()

    def test_expired_confirmation_does_not_block_next_order_or_retry_old_one(self):
        self.s.config["refund_on_error"] = True
        self.expire_first_confirmation()
        old = self.s.storage.get("O1")
        self.assertEqual(old["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(old["payment_attempt"]["state"], "PENDING")
        self.assertTrue(old["payment_attempt"]["fragment_confirmation_expired"])
        self.assertIn("Session expired", self.s.cardinal.notifications[0][1])
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

        self.s.storage.upsert(helpers.order("O2"))
        self.run_cycle()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(self.s.storage.get("O1")["payment_attempt"]["state"], "PENDING")
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_DONE)
        self.assertEqual(self.api.broadcasts, ["00aabb", "00aabb"])
        self.assertEqual(self.s.wallet.offline.seqnos, [7, 8])
        self.assertEqual(self.s.fragment.currencies, ["USDT", "USDT"])
        self.assertEqual([message[0] for message in self.s.cardinal.messages], ["O2"])
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertEqual(len(self.s.fragment.confirmations), 2)

    def test_expired_confirmation_restores_seqno_after_restart_with_stale_tonapi(self):
        self.expire_first_confirmation()
        restarted = copy.copy(self.s)
        restarted.storage = p.Storage(self.s.storage.path)
        restarted.wallet = p.Wallet(Offline(self.api), self.api)
        restarted.wallet._min_seqno = restarted.storage.get_confirmed_seqno(restarted.wallet.address)
        self.api.cached_seqno = 7
        restarted.storage.upsert(helpers.order("O2"))
        self.run_cycle(restarted)
        self.assertEqual(restarted.wallet.offline.seqnos, [8])
        self.assertEqual(restarted.storage.get("O1")["payment_attempt"]["state"], "PENDING")
        self.assertEqual(restarted.storage.get("O2")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 2)

    def test_expired_old_order_can_confirm_later_without_another_purchase(self):
        self.expire_first_confirmation()
        self.s.storage.upsert(helpers.order("O2"))
        self.run_cycle()
        retry_at = self.s.storage.get("O1")["payment_attempt"]["fragment_confirm_retry_at"]
        self.s.fragment.confirm_stars_payment = lambda *args: None
        with mock.patch.object(p.time, "time", return_value=retry_at + 1):
            self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual([message[0] for message in self.s.cardinal.messages], ["O2", "O1"])
        self.assertEqual(len(self.api.broadcasts), 2)
        self.assertEqual(len(self.s.fragment.currencies), 2)

    def test_direct_batch_cannot_rebuy_expired_order_even_from_stale_copy(self):
        stale_copy = helpers.order()
        self.expire_first_confirmation()
        next_order = helpers.order("O2")
        self.s.storage.upsert(next_order)
        self.s._transfer_batch([stale_copy, next_order])
        self.assertEqual(self.s.storage.get("O1")["payment_attempt"]["state"], "PENDING")
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 2)
        self.assertEqual(len(self.s.fragment.currencies), 2)

    def test_expired_confirmation_with_missing_evidence_still_blocks(self):
        self.expire_first_confirmation()
        valid = self.s.storage.get("O1")
        mutations = [
            lambda a, e: a.pop("in_msg_hash"),
            lambda a, e: a.pop("transaction_hash"),
            lambda a, e: a.update(wallet_address="different-wallet"),
            lambda a, e: a.update(wallet_executed=False),
            lambda a, e: a.update(seqno=None),
            lambda a, e: a.update(seqno=True),
            lambda a, e: a.update(seqno=-1),
            lambda a, e: a.pop("fragment_confirmation_expired"),
            lambda a, e: e.update(currency="TON"),
            lambda a, e: e.update(confirm_params=None),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                broken = copy.deepcopy(valid)
                mutate(broken["payment_attempt"], broken["expected_message"])
                self.s.storage.upsert(broken)
                self.assertTrue(self.s._has_blocking_pending())
        self.s.storage.upsert(valid)
        self.assertFalse(self.s._has_blocking_pending())
        self.s.wallet._min_seqno = valid["payment_attempt"]["seqno"]
        self.assertTrue(self.s._has_blocking_pending())

    def test_other_unresolved_payment_blocks_even_if_expired_one_is_safe(self):
        self.expire_first_confirmation()
        other = helpers.order("O2")
        other["status"] = p.ST_TRANSFERRING
        self.s.storage.upsert(other)
        self.assertTrue(self.s._has_blocking_pending())
        self.s.storage.upsert(helpers.order("O3"))
        self.run_cycle()
        self.assertEqual(len(self.api.broadcasts), 1)
        self.assertEqual(self.s.storage.get("O3")["retries_left"], 3)

    def test_fragment_confirmation_backoff_and_reconnect(self):
        self.expire_first_confirmation()
        first = self.s.storage.get("O1")["payment_attempt"]
        self.s._reconcile_pending()
        self.assertEqual(len(self.s.fragment.confirmations), 1)
        with mock.patch.object(p.time, "time", return_value=first["fragment_confirm_retry_at"] + 1):
            self.s._reconcile_pending()
        second = self.s.storage.get("O1")["payment_attempt"]
        self.assertEqual(second["fragment_confirm_retries"], 2)
        self.assertEqual(second["fragment_confirm_retry_at"] - first["fragment_confirm_retry_at"], 61)
        self.assertEqual(len(self.s.cardinal.notifications), 1)

        self.s.reload_providers()
        reloaded = self.s.storage.get("O1")["payment_attempt"]
        self.assertNotIn("fragment_confirm_retry_at", reloaded)
        self.assertTrue(reloaded["fragment_confirmation_expired"])

    def test_failed_jetton_execution_requires_review_without_refund(self):
        self.api.trace_visible = True
        self.api.fail_recipient = True
        self.s.config["refund_on_error"] = True
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(self.s.cardinal.refunds, [])
        self.assertEqual(self.s.cardinal.messages, [])

    def test_credited_nonbounce_notification_with_no_gas_completes_order(self):
        self.api.trace_visible = True
        self.api.notification_skipped = "cskip_no_gas"
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.s.cardinal.messages), 1)
        self.assertEqual(len(self.api.broadcasts), 1)

    def test_credited_uninitialized_notification_completes_order(self):
        self.api.trace_visible = True
        self.api.notification_skipped = "cskip_no_state"
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.s.cardinal.messages), 1)

    def test_bounceable_failed_notification_never_confirms_order(self):
        self.api.trace_visible = True
        self.api.notification_skipped = "cskip_no_gas"
        self.api.notification_bounce = True
        self.start(helpers.order())
        self.assertNotEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.cardinal.messages, [])

    def test_skipped_notification_without_full_credit_does_not_confirm(self):
        self.api.notification_skipped = "cskip_no_gas"
        self.start(helpers.order())
        delivered = self.api.trace["children"][0]["children"][0]["children"][0]
        delivered["transaction"]["credit_phase"]["credit"] = 0
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertNotEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_skipped_bad_state_does_not_confirm(self):
        self.api.notification_skipped = "cskip_bad_state"
        self.api.trace_visible = True
        self.start(helpers.order())
        self.assertNotEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_emulated_child_is_not_confirmation(self):
        self.start(helpers.order())
        self.api.trace["children"][0]["children"][0]["emulated"] = True
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_skipped_jetton_wallet_execution_does_not_confirm(self):
        self.start(helpers.order())
        receiver = self.api.trace["children"][0]["children"][0]
        receiver["transaction"]["compute_phase"] = {"skipped": True, "skip_reason": "cskip_no_state"}
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertNotEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_missing_notification_keeps_payment_pending(self):
        self.start(helpers.order())
        self.api.trace["children"][0]["children"][0]["children"] = []
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_duplicate_notification_is_not_used_as_proof(self):
        self.start(helpers.order())
        receiver = self.api.trace["children"][0]["children"][0]
        receiver["children"].append(copy.deepcopy(receiver["children"][0]))
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_forged_source_does_not_confirm_or_refund(self):
        self.start(helpers.order())
        sender = self.api.trace["children"][0]
        sender["children"][0]["transaction"]["in_msg"]["source"]["address"] = address(9)
        self.api.trace_visible = True
        self.s._reconcile_pending()
        self.assertNotEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.cardinal.messages, [])
        self.assertEqual(self.s.cardinal.refunds, [])

    def test_wrong_notification_does_not_confirm_payment(self):
        self.api.trace_visible = True
        self.api.invalid_trace_amount = True
        self.start(helpers.order())
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_TRANSFERRING)
        self.assertEqual(self.s.cardinal.messages, [])

    def test_pending_usdt_recovers_after_restart_and_late_trace(self):
        self.api.notification_skipped = "cskip_no_gas"
        self.start(helpers.order())
        restarted = copy.copy(self.s)
        restarted.storage = p.Storage(self.s.storage.path)
        restarted.wallet = p.Wallet(Offline(self.api), self.api)
        attempt = restarted.storage.get("O1")["payment_attempt"]
        with mock.patch.object(p.time, "time", return_value=attempt["valid_until"] + p.PAYMENT_REVIEW_GRACE_SEC + 1):
            restarted._reconcile_pending()
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_NEEDS_REVIEW)
        self.assertEqual(self.s.cardinal.refunds, [])
        self.api.trace_visible = True
        restarted._reconcile_pending()
        self.assertEqual(restarted.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(len(self.api.broadcasts), 1)
        self.assertEqual(len(self.s.cardinal.messages), 1)
        restarted._reconcile_pending()
        self.assertEqual(len(self.s.cardinal.messages), 1)
        self.assertEqual(len(self.api.broadcasts), 1)

    def test_rejects_other_jetton_wallet_and_non_jetton_payload(self):
        self.s.fragment.destination_override = address(9)
        self.start(helpers.order())
        self.assertEqual(self.api.broadcasts, [])
        self.assertEqual(self.s.storage.get("O1")["error"], p.ERR_UNABLE_TO_FETCH_LINK)
        self.s.fragment.destination_override = None
        self.s.fragment.body_override = p.begin_cell().store_uint(0, 32).end_cell()
        self.start(helpers.order("O2"))
        self.assertEqual(self.api.broadcasts, [])
        self.assertEqual(self.s.storage.get("O2")["error"], p.ERR_UNABLE_TO_FETCH_LINK)

    def test_usdt_and_ton_balances_are_checked_independently(self):
        self.api.usdt = 3 * p.ONE_USDT
        self.api.trace_visible = True
        self.start(helpers.order(), helpers.order("O2"))
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.storage.get("O2")["error"], p.ERR_NOT_ENOUGH_USDT)
        self.assertEqual(len(self.api.transfers), 1)

    def test_multiple_usdt_orders_confirm_with_the_same_saved_boc(self):
        self.api.trace_visible = True
        self.start(helpers.order(), helpers.order("O2"))
        self.assertEqual(self.s.storage.get("O1")["status"], p.ST_DONE)
        self.assertEqual(self.s.storage.get("O2")["status"], p.ST_DONE)
        self.assertEqual(len(self.s.fragment.confirmations), 2)
        self.assertEqual({item[2] for item in self.s.fragment.confirmations}, {"00aabb"})
        self.assertEqual(len(self.api.broadcasts), 1)

    def test_offline_wallet_builds_a_real_tonconnect_account(self):
        from pytoniq_core.crypto.keys import mnemonic_new
        wallet = p.OfflineWallet(" ".join(mnemonic_new()))
        account = wallet.tonconnect_account()
        self.assertEqual(p.Address(account["address"]), wallet.address)
        self.assertEqual(account["chain"], "-239")
        self.assertEqual(len(bytes.fromhex(account["publicKey"])), 32)
        state = p.Cell.one_from_boc(base64.b64decode(account["walletStateInit"]))
        self.assertEqual(state.hash, wallet.address.hash_part)

    def test_not_enough_ton_for_gas_does_not_send(self):
        self.api.balance = p.ONE_TON // 10
        self.start(helpers.order())
        self.assertEqual(self.api.broadcasts, [])
        self.assertEqual(self.s.storage.get("O1")["error"], p.ERR_NOT_ENOUGH_TON)

    def test_thresholds_and_notifications_use_selected_currency(self):
        self.s.config["low_balance_threshold"] = 2.0
        self.assertTrue(self.s._check_low_balance(1_500_000))
        self.assertIn("1.5000 USDT", self.s.cardinal.notifications[-1][1])
        p.switch_payment_currency(self.s.config, "TON")
        self.assertEqual(self.s.config["low_balance_threshold"], 0)
        self.s.config["low_balance_threshold"] = 5
        p.switch_payment_currency(self.s.config, "USDT")
        self.assertEqual(self.s.config["low_balance_threshold"], 2.0)
        self.assertFalse(self.s._check_low_balance(3_000_000))
        self.assertIn("3.0000 USDT", self.s.cardinal.notifications[-1][1])

    def test_balance_endpoint_requires_official_master_and_six_decimals(self):
        api = p.TonAPI()
        good = {"balance": "1234567", "wallet_address": {"address": self.api.jetton_wallet},
                "jetton": {"address": p.USDT_MASTER, "decimals": 6}}
        with mock.patch.object(api, "_request", return_value=good):
            self.assertEqual(api.get_usdt_balance(self.api.owner), (1234567, self.api.jetton_wallet))
        bad = copy.deepcopy(good)
        bad["jetton"]["address"] = address(8)
        with mock.patch.object(api, "_request", return_value=bad):
            with self.assertRaises(p.TonAPIError):
                api.get_usdt_balance(self.api.owner)
        with mock.patch.object(api, "_request", return_value=None):
            self.assertEqual(api.get_usdt_balance(self.api.owner), (0, None))

    def test_telegram_switch_updates_balance_and_keeps_thresholds(self):
        class Button:
            def __init__(self, text, callback_data):
                self.text, self.callback_data = text, callback_data

        class Keyboard:
            def __init__(self):
                self.buttons = []

            def add(self, button):
                self.buttons.append(button)
                return self

        class Bot:
            def __init__(self):
                self.rendered = []

            def answer_callback_query(self, *args):
                pass

            def edit_message_text(self, text, chat_id, msg_id, reply_markup):
                self.rendered.append((text, reply_markup))

            def send_message(self, *args, **kwargs):
                raise AssertionError("unexpected error message")

        class Telegram:
            def __init__(self):
                self.bot, self.callbacks = Bot(), []

            def cbq_handler(self, callback, predicate):
                self.callbacks.append(callback)

            def msg_handler(self, *args, **kwargs):
                pass

        tg = Telegram()
        telebot = types.ModuleType("telebot")
        telebot_types = types.ModuleType("telebot.types")
        telebot_types.InlineKeyboardMarkup, telebot_types.InlineKeyboardButton = Keyboard, Button
        self.s.config["payment_currency"] = "TON"
        self.s.config["low_balance_threshold"] = 4.0
        c = types.SimpleNamespace(data="ascurrency:0", id="callback",
                                  message=types.SimpleNamespace(chat=types.SimpleNamespace(id=42), id=1))
        with mock.patch.dict(sys.modules, {"telebot": telebot, "telebot.types": telebot_types}), \
             mock.patch.object(p, "SERVICE", self.s), \
             mock.patch.object(p, "_ensure_config", return_value=self.s.config), \
             mock.patch.object(p, "save_config") as save, \
             mock.patch.object(p.threading, "Thread", helpers.ImmediateThread):
            p.register_settings(types.SimpleNamespace(telegram=tg))
            switch = next(callback for callback in tg.callbacks if callback.__name__ == "toggle_currency")
            switch(c)
            self.assertEqual(self.s.config["payment_currency"], "USDT")
            self.assertEqual(self.s.config["low_balance_threshold"], 0)
            self.assertIn("Баланс USDT: 10.000000", tg.bot.rendered[-1][0])
            self.assertTrue(any(button.text == "💱 Валюта оплаты: USDT"
                                for button in tg.bot.rendered[-1][1].buttons))
            switch(c)
            self.assertEqual(self.s.config["payment_currency"], "TON")
            self.assertEqual(self.s.config["low_balance_threshold"], 4.0)
            self.assertIn("Баланс TON: 100.000000", tg.bot.rendered[-1][0])
            self.assertEqual(save.call_count, 2)


if __name__ == "__main__":
    unittest.main()
