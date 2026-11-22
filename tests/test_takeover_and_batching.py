"""Human takeover, media placeholders, message batching and webhook hardening.

Run from the repository root:
    PYTHONPATH=src python -m unittest tests.test_takeover_and_batching -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_reliability import ReliabilityTestCase  # noqa: E402

import ai_reply  # noqa: E402
import bridge_manager  # noqa: E402
import config as config_module  # noqa: E402
import message_router  # noqa: E402
from database import (  # noqa: E402
    customer_ai_paused, get_connection, get_or_create_customer, save_message,
    set_customer_ai_pause,
)
from send_service import check_proactive_eligibility  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class HumanTakeoverTests(ReliabilityTestCase):
    def setUp(self):
        super().setUp()
        config_module._config.business.human_takeover_minutes = 120

    def test_phone_reply_is_recorded_and_pauses_ai_for_that_customer(self):
        result = run(message_router.handle_outgoing_from_phone(
            "+8613911110001", "I'll call you tomorrow", "PHONE-1"))
        self.assertTrue(result["stored"])
        self.assertIsNotNone(result["ai_paused_until"])
        row = dict(get_connection().execute(
            "SELECT * FROM conversations WHERE id=?", (result["conversation_id"],)).fetchone())
        self.assertEqual(row["direction"], "outbound")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["send_kind"], "manual")
        self.assertEqual(row["message_type"], "phone")
        self.assertEqual(row["ai_generated"], 0)

        with patch("message_router.generate_reply") as gen:
            inbound = run(message_router.handle_incoming_message(
                "+8613911110001", "ok what time?", "IN-1"))
        self.assertTrue(inbound["stored"])
        self.assertEqual(inbound["reason"], "human_takeover")
        gen.assert_not_called()

        # Another customer is unaffected.
        with patch("message_router.generate_reply", return_value="Hi!"), \
             patch("message_router.extract_and_store_memory"):
            other = run(message_router.handle_incoming_message(
                "+8613911110002", "Need a quote", "IN-2"))
        self.assertEqual(other["action"], "reply")

    def test_duplicate_phone_message_and_bot_echo_are_ignored(self):
        cid = get_or_create_customer("+8613911110003")["id"]
        save_message(cid, "outbound", "bot text", whatsapp_msg_id="BOT-1",
                     ai_generated=True, status="sent", send_kind="instant_reply")
        echo = run(message_router.handle_outgoing_from_phone("+8613911110003", "bot text", "BOT-1"))
        self.assertFalse(echo["stored"])
        self.assertFalse(customer_ai_paused(get_or_create_customer("+8613911110003")))

        first = run(message_router.handle_outgoing_from_phone("+8613911110003", "hello", "PH-2"))
        again = run(message_router.handle_outgoing_from_phone("+8613911110003", "hello", "PH-2"))
        self.assertTrue(first["stored"])
        self.assertFalse(again["stored"])

    def test_takeover_zero_minutes_records_without_pausing(self):
        config_module._config.business.human_takeover_minutes = 0
        result = run(message_router.handle_outgoing_from_phone("+8613911110004", "hi", "PH-3"))
        self.assertTrue(result["stored"])
        self.assertIsNone(result["ai_paused_until"])

    def test_pause_expires_and_can_be_cleared(self):
        cid = get_or_create_customer("+8613911110005")["id"]
        set_customer_ai_pause(cid, datetime.now() - timedelta(minutes=1))
        self.assertFalse(customer_ai_paused(get_or_create_customer("+8613911110005")))
        set_customer_ai_pause(cid, datetime.now() + timedelta(minutes=30))
        self.assertTrue(customer_ai_paused(get_or_create_customer("+8613911110005")))
        set_customer_ai_pause(cid, None)
        self.assertFalse(customer_ai_paused(get_or_create_customer("+8613911110005")))

    def test_takeover_blocks_proactive_followups(self):
        cid = self.add_customer("+8613911110006")
        sid = self.add_schedule(cid)
        set_customer_ai_pause(cid, datetime.now() + timedelta(hours=1))
        customer = dict(get_connection().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone())
        schedule = dict(get_connection().execute(
            "SELECT * FROM follow_up_schedule WHERE id=?", (sid,)).fetchone())
        result = check_proactive_eligibility(customer, schedule)
        self.assertFalse(result["allowed"])
        self.assertIn("human_takeover", [b["code"] for b in result["blockers"]])


    def test_workspace_list_exposes_takeover_and_last_message_type(self):
        from workspace_service import conversation_workspace
        run(message_router.handle_outgoing_from_phone("+8613911110007", "on my way", "PH-7"))
        rows = {r["phone"]: r for r in conversation_workspace()}
        row = rows["+8613911110007"]
        self.assertIsNotNone(row["ai_paused_until"])
        self.assertEqual(row["last_message_type"], "phone")
        self.assertEqual(row["last_ai_generated"], 0)


class MediaAndBatchingTests(ReliabilityTestCase):
    def test_media_is_stored_but_not_answered_by_ai(self):
        with patch("message_router.generate_reply") as gen:
            result = run(message_router.handle_incoming_message(
                "+8613922220001", "[图片]", "IMG-1", message_type="image"))
        self.assertTrue(result["stored"])
        self.assertEqual(result["reason"], "media_needs_human")
        gen.assert_not_called()
        row = get_connection().execute(
            "SELECT message_type, content FROM conversations WHERE whatsapp_msg_id='IMG-1'").fetchone()
        self.assertEqual(row["message_type"], "image")
        self.assertEqual(row["content"], "[图片]")

    def test_unknown_message_type_falls_back_to_text(self):
        with patch("message_router.generate_reply", return_value="ok"), \
             patch("message_router.extract_and_store_memory"):
            result = run(message_router.handle_incoming_message(
                "+8613922220002", "hello there", "T-1", message_type="<script>"))
        self.assertEqual(result["action"], "reply")

    def test_batch_defers_then_answers_combined_text(self):
        with patch("message_router.generate_reply", return_value="Sure") as gen, \
             patch("message_router.extract_and_store_memory"):
            first = run(message_router.handle_incoming_message(
                "+8613922220003", "hi", "B-1", defer_reply=True))
            last = run(message_router.handle_incoming_message(
                "+8613922220003", "need 1MW genset price", "B-2",
                batch_text="hi\nneed 1MW genset price"))
        self.assertEqual(first["reason"], "batched")
        self.assertTrue(first["stored"])
        self.assertEqual(last["action"], "reply")
        gen.assert_called_once()
        self.assertEqual(gen.call_args[0][1], "hi\nneed 1MW genset price")

    def test_batch_ending_with_media_still_answers_the_text(self):
        with patch("message_router.generate_reply", return_value="Got it") as gen, \
             patch("message_router.extract_and_store_memory"):
            run(message_router.handle_incoming_message(
                "+8613922220004", "what model is this?", "BM-1", defer_reply=True))
            last = run(message_router.handle_incoming_message(
                "+8613922220004", "[图片]", "BM-2", message_type="image",
                batch_text="what model is this?"))
        self.assertEqual(last["action"], "reply")
        gen.assert_called_once()

    def test_trivial_batch_is_not_answered(self):
        with patch("message_router.generate_reply") as gen:
            result = run(message_router.handle_incoming_message(
                "+8613922220005", "ok", "TR-1", batch_text="ok"))
        self.assertEqual(result["reason"], "no_reply_needed")
        gen.assert_not_called()


class PromptHistoryTests(ReliabilityTestCase):
    def _capture_messages(self, customer, incoming):
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="reply"))])
        with patch("ai_reply._get_client", return_value=fake_client), \
             patch("ai_reply._build_system_prompt", return_value="sys"), \
             patch("ai_reply._build_specs_message", return_value="specs"):
            ai_reply.generate_reply(customer, incoming)
        return fake_client.chat.completions.create.call_args.kwargs["messages"]

    def test_current_message_is_not_sent_twice_and_unanswered_ones_are_kept(self):
        customer = get_or_create_customer("+8613933330001")
        cid = customer["id"]
        save_message(cid, "inbound", "earlier question", status="received", send_kind="inbound")
        save_message(cid, "outbound", "earlier answer", ai_generated=True, status="sent",
                     send_kind="instant_reply")
        save_message(cid, "inbound", "hi", status="received", send_kind="inbound")
        save_message(cid, "inbound", "need a price", status="received", send_kind="inbound")
        messages = self._capture_messages(customer, "need a price")
        chat = [m for m in messages if m["role"] != "system"]
        self.assertEqual(chat[-1], {"role": "user", "content": "hi\nneed a price"})
        self.assertEqual(sum(1 for m in chat if "need a price" in m["content"]), 1)
        self.assertEqual(chat[-2]["role"], "assistant")


class WebhookHardeningTests(unittest.TestCase):
    def test_named_tenant_without_issued_token_is_rejected(self):
        with patch.dict(os.environ, {"WA_WEBHOOK_SECRET": ""}):
            bridge_manager._tokens.pop("never-started", None)
            self.assertFalse(bridge_manager.verify_bridge_token("never-started", ""))
            self.assertFalse(bridge_manager.verify_bridge_token("never-started", "guess"))
            # Legacy single-user mode (no tenant id) keeps working.
            self.assertTrue(bridge_manager.verify_bridge_token("", ""))

    def test_issued_token_and_global_secret(self):
        bridge_manager._tokens["t-x"] = "tok"
        try:
            with patch.dict(os.environ, {"WA_WEBHOOK_SECRET": ""}):
                self.assertTrue(bridge_manager.verify_bridge_token("t-x", "tok"))
                self.assertFalse(bridge_manager.verify_bridge_token("t-x", "bad"))
            with patch.dict(os.environ, {"WA_WEBHOOK_SECRET": "global"}):
                self.assertTrue(bridge_manager.verify_bridge_token("anyone", "global"))
                self.assertFalse(bridge_manager.verify_bridge_token("t-x", "tok"))
        finally:
            bridge_manager._tokens.pop("t-x", None)


class ClientIpTests(unittest.TestCase):
    def _req(self, peer, headers):
        return SimpleNamespace(client=SimpleNamespace(host=peer),
                               headers={k.lower(): v for k, v in headers.items()})

    def test_spoofed_leftmost_forwarded_for_is_ignored(self):
        import auth
        req = self._req("127.0.0.1", {"X-Forwarded-For": "1.1.1.1, 203.0.113.9"})
        self.assertEqual(auth._client_ip(req), "203.0.113.9")

    def test_cf_connecting_ip_preferred(self):
        import auth
        req = self._req("127.0.0.1", {"CF-Connecting-IP": "198.51.100.7",
                                      "X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(auth._client_ip(req), "198.51.100.7")

    def test_untrusted_peer_headers_ignored(self):
        import auth
        with patch.dict(os.environ, {"WA_TRUST_PROXY": ""}):
            req = self._req("192.0.2.50", {"X-Forwarded-For": "1.1.1.1",
                                           "CF-Connecting-IP": "2.2.2.2"})
            self.assertEqual(auth._client_ip(req), "192.0.2.50")


if __name__ == "__main__":
    unittest.main()
