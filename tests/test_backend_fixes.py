import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import accounts
import admin_api
import ai_reply
import api_server
import bridge_manager
import config
import database
import message_router
import scheduler
import tenancy


class BackendFixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wa-fixes-"))
        self._prev_data_dir = os.environ.get("WHATSAPP_BOT_DATA_DIR")
        self._prev_llm_key = os.environ.get("LLM_API_KEY")
        os.environ["WHATSAPP_BOT_DATA_DIR"] = str(self.tmp)
        os.environ.pop("LLM_API_KEY", None)

        accounts.reset_for_tests()
        database.close_db()
        config.invalidate_config_cache()
        tenancy._env_cache.clear()
        ai_reply._clients.clear()
        ai_reply._hourly_counters.clear()

        template = Path(__file__).resolve().parent.parent / "config.yaml"
        self.t1_dir = tenancy.ensure_tenant_dir("u1", template=template)
        self.t2_dir = tenancy.ensure_tenant_dir("u2", template=template)
        self.t1 = tenancy.Tenant("u1", "u1@example.com", self.t1_dir)
        self.t2 = tenancy.Tenant("u2", "u2@example.com", self.t2_dir)

    def tearDown(self):
        database.close_db()
        accounts.reset_for_tests()
        config.invalidate_config_cache()
        tenancy._env_cache.clear()
        ai_reply._clients.clear()
        ai_reply._hourly_counters.clear()
        if self._prev_data_dir is None:
            os.environ.pop("WHATSAPP_BOT_DATA_DIR", None)
        else:
            os.environ["WHATSAPP_BOT_DATA_DIR"] = self._prev_data_dir
        if self._prev_llm_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = self._prev_llm_key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ai_client_and_rate_limit_isolated_per_tenant(self):
        (self.t1_dir / ".env").write_text("LLM_API_KEY=sk-tenant-1\n", encoding="utf-8")
        (self.t2_dir / ".env").write_text("LLM_API_KEY=sk-tenant-2\n", encoding="utf-8")

        tok1 = tenancy.set_current(self.t1)
        try:
            c1 = ai_reply._get_client()
            self.assertEqual(c1.api_key, "sk-tenant-1")
            cfg = config.get_config()
            cfg.business.max_auto_replies_per_hour = 1
            c1_cust = database.get_or_create_customer("8613800000001")
            self.assertEqual(ai_reply.check_rate_limits(c1_cust["id"]), (True, "ok"))
            self.assertEqual(ai_reply.check_rate_limits(c1_cust["id"]), (False, "hourly limit"))
        finally:
            tenancy.reset_current(tok1)

        tok2 = tenancy.set_current(self.t2)
        try:
            c2 = ai_reply._get_client()
            self.assertEqual(c2.api_key, "sk-tenant-2")
            self.assertIsNot(c1, c2)
            c2_cust = database.get_or_create_customer("8613800000002")
            self.assertEqual(ai_reply.check_rate_limits(c2_cust["id"]), (True, "ok"))
        finally:
            tenancy.reset_current(tok2)

    def test_save_setup_does_not_leak_to_global_env_and_invalidates_cache(self):
        # Prime empty cache for tenant 1
        self.assertEqual(tenancy.load_tenant_env(self.t1_dir), {})

        tok = tenancy.set_current(self.t1)
        try:
            res = asyncio.run(admin_api.save_setup({"api_key": "sk-only-t1"}))
            self.assertEqual(res, {"ok": True})
        finally:
            tenancy.reset_current(tok)

        self.assertIsNone(os.environ.get("LLM_API_KEY"))
        self.assertEqual(tenancy.load_tenant_env(self.t1_dir).get("LLM_API_KEY"), "sk-only-t1")

    def test_handle_incoming_message_preserves_tenant_in_worker_thread(self):
        seen_tenants = []

        def fake_generate(customer, body):
            seen_tenants.append(tenancy.current().user_id)
            return "Hello from tenant thread"

        def fake_memory(customer_id, body, reply):
            seen_tenants.append(tenancy.current().user_id)

        tok = tenancy.set_current(self.t1)
        try:
            with patch("message_router.generate_reply", side_effect=fake_generate), \
                 patch("message_router.extract_and_store_memory", side_effect=fake_memory):
                result = asyncio.run(
                    message_router.handle_incoming_message("8613911112222", "Hi", "msg-1")
                )
        finally:
            tenancy.reset_current(tok)

        self.assertEqual(result["action"], "reply")
        self.assertEqual(seen_tenants, ["u1", "u1"])

    def test_activity_log_candidates_includes_bridge_log(self):
        log_dir = self.t1_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        bridge_log = log_dir / "bridge.log"
        bridge_log.write_text("2026-11-13 10:00:00 WHATSAPP_READY:8613800\n", encoding="utf-8")

        tok = tenancy.set_current(self.t1)
        try:
            candidates = admin_api._activity_log_candidates()
        finally:
            tenancy.reset_current(tok)

        self.assertIn(bridge_log, candidates)

    def test_set_proxy_restarts_running_bridge(self):
        accounts.create_user("u1@example.com", "pass123456", self.t1_dir, user_id="u1")
        token = accounts.create_session("u1")
        client = TestClient(api_server.app)
        client.cookies.set(accounts.SESSION_COOKIE, token)

        with patch("bridge_manager.status", return_value={"running": True, "online": True, "port": 3101}), \
             patch("bridge_manager.restart", return_value={"online": True, "port": 3101, "started": True}) as m_restart:
            resp = client.post("/admin/api/proxy", json={"proxy": "http://127.0.0.1:7890"})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["restarted"])
        m_restart.assert_called_once()

    def test_delete_customer_cascade_and_merge_customers_fk_safe(self):
        tok = tenancy.set_current(self.t1)
        try:
            c1 = database.get_or_create_customer("8613800000101", "Target")
            c2 = database.get_or_create_customer("8613800000102", "Source")
            conn = database.get_connection()
            conn.execute("INSERT INTO conversations(customer_id, direction, content, status) VALUES(?,?,?,?)",
                         (c2["id"], "inbound", "hi", "received"))
            conn.execute("INSERT INTO follow_up_schedule(customer_id, frequency_days, active) VALUES(?, 3, 1)",
                         (c2["id"],))
            conn.execute("INSERT INTO sent_followups(customer_id, status) VALUES(?, ?)",
                         (c2["id"], "sent"))
            conn.execute("INSERT INTO orders(order_no, customer_id, product) VALUES(?,?,?)",
                         ("TP20260101001", c2["id"], "Gen1"))
            conn.execute("INSERT INTO lead_scores(customer_id, score, segment) VALUES(?,?,?)",
                         (c2["id"], 80, "hot"))
            conn.execute("INSERT INTO agent_decisions(customer_id, decision_type) VALUES(?,?)",
                         (c2["id"], "follow_up"))
            conn.execute("INSERT INTO ai_memory_entries(customer_id, entry_type, content) VALUES(?,?,?)",
                         (c2["id"], "fact", "500kw"))
            conn.commit()

            merged = database.merge_customers(c1["id"], c2["id"])
            self.assertEqual(merged["id"], c2["id"])
            self.assertIsNone(conn.execute("SELECT id FROM customers WHERE id=?", (c2["id"],)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE customer_id=?", (c1["id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lead_scores WHERE customer_id=?", (c1["id"],)).fetchone()[0], 1)

            deleted = database.delete_customer_cascade(c1["id"])
            self.assertEqual(deleted["id"], c1["id"])
            self.assertIsNone(conn.execute("SELECT id FROM customers WHERE id=?", (c1["id"],)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders WHERE customer_id=?", (c1["id"],)).fetchone()[0], 0)
        finally:
            tenancy.reset_current(tok)

    def test_ensure_active_batch_schedule_no_duplicates(self):
        tok = tenancy.set_current(self.t1)
        try:
            c1 = database.get_or_create_customer("8613800000103", "Sched")
            res1 = database.ensure_active_batch_schedule(c1["id"], 5)
            res2 = database.ensure_active_batch_schedule(c1["id"], 5)
            self.assertTrue(res1["ok"])
            self.assertTrue(res2["ok"])
            cnt = database.get_connection().execute(
                "SELECT COUNT(*) FROM follow_up_schedule WHERE customer_id=?",
                (c1["id"],),
            ).fetchone()[0]
            self.assertEqual(cnt, 1)
        finally:
            tenancy.reset_current(tok)

    def test_create_order_record_sequential_order_no(self):
        tok = tenancy.set_current(self.t1)
        try:
            c1 = database.get_or_create_customer("8613800000104", "Buyer")
            o1 = database.create_order_record(c1["id"], {"product": "P1", "order_date": "2026-11-01", "delivery_date": "2026-11-10"})
            o2 = database.create_order_record(c1["id"], {"product": "P2"})
            bad = database.create_order_record(c1["id"], {"order_date": "2026-11-20", "delivery_date": "2026-11-01"})
            self.assertTrue(o1["ok"])
            self.assertTrue(o2["ok"])
            self.assertTrue(o1["order"]["order_no"].endswith("001"))
            self.assertTrue(o2["order"]["order_no"].endswith("002"))
            self.assertIn("error", bad)
        finally:
            tenancy.reset_current(tok)

    def test_reload_current_config_does_not_overwrite_global_config(self):
        template = Path(__file__).resolve().parent.parent / "config.yaml"
        global_cfg = config.load_config(template)
        t1_cfg_file = self.t1_dir / "config.yaml"
        t1_cfg_file.write_text("business:\n  company_name: TenantOneCorp\n", encoding="utf-8")

        tok = tenancy.set_current(self.t1)
        try:
            reloaded = config.reload_current_config()
            self.assertEqual(reloaded.business.company_name, "TenantOneCorp")
        finally:
            tenancy.reset_current(tok)

        self.assertIsNot(config._config, reloaded)
        self.assertEqual(config.get_config().business.company_name, global_cfg.business.company_name)

    def test_webhook_bridge_token_and_missing_tenant_validation(self):
        accounts.create_user("u1@example.com", "pass123456", self.t1_dir, user_id="u1")
        bridge_manager._tokens["u1"] = "secret-tok-1"
        try:
            client = TestClient(api_server.app, client=("127.0.0.1", 50000))
            missing_tenant = client.post("/webhook/qr", json={"status": "ready"})
            self.assertEqual(missing_tenant.status_code, 400)

            wrong_token = client.post(
                "/webhook/qr",
                headers={"X-Tenant-Id": "u1", "X-Bridge-Token": "wrong"},
                json={"status": "ready"},
            )
            self.assertEqual(wrong_token.status_code, 403)

            ok = client.post(
                "/webhook/qr",
                headers={"X-Tenant-Id": "u1", "X-Bridge-Token": "secret-tok-1"},
                json={"status": "ready"},
            )
            self.assertEqual(ok.status_code, 200)
        finally:
            bridge_manager._tokens.pop("u1", None)

    def test_scheduler_per_tenant_locks_and_wal_checkpoint(self):
        accounts.create_user("u1@example.com", "pass123456", self.t1_dir, user_id="u1")
        accounts.create_user("u2@example.com", "pass123456", self.t2_dir, user_id="u2")

        tok1 = tenancy.set_current(self.t1)
        lock1 = scheduler._get_tenant_lock()
        lock1.acquire()
        tenancy.reset_current(tok1)

        try:
            tok2 = tenancy.set_current(self.t2)
            try:
                res2 = asyncio.run(scheduler._run_followup_pass("test", 0))
                # u2 was not blocked by u1's held lock (returns no targets or disabled, not already_running)
                self.assertNotEqual(res2.get("skipped"), "already_running")
            finally:
                tenancy.reset_current(tok2)

            seen_wal = []
            with patch("scheduler._wal_checkpoint_once", side_effect=lambda: seen_wal.append(tenancy.current().user_id)):
                scheduler._wal_checkpoint()
            self.assertEqual(sorted(seen_wal), ["u1", "u2"])
        finally:
            lock1.release()

    def test_public_origin_csrf_allows_configured_domain_and_rejects_foreign(self):
        client = TestClient(api_server.app, client=("127.0.0.1", 50000))
        prev = os.environ.get("WA_PUBLIC_ORIGIN")
        os.environ["WA_PUBLIC_ORIGIN"] = "https://bot.xueai88.xyz"
        try:
            ok_resp = client.post(
                "/api/auth/logout",
                headers={"Host": "127.0.0.1:8000", "Origin": "https://bot.xueai88.xyz"},
            )
            self.assertEqual(ok_resp.status_code, 200)

            bad_resp = client.post(
                "/api/auth/logout",
                headers={"Host": "127.0.0.1:8000", "Origin": "https://evil.example.com"},
            )
            self.assertEqual(bad_resp.status_code, 403)
        finally:
            if prev is None:
                os.environ.pop("WA_PUBLIC_ORIGIN", None)
            else:
                os.environ["WA_PUBLIC_ORIGIN"] = prev

    def test_web_main_no_scheduler_still_starts_bridge_watchdog(self):
        import web_main

        watchdog_calls = []
        scheduler_calls = []
        fake_uvicorn = type("FakeUvicorn", (), {"run": staticmethod(lambda *a, **kw: None)})

        with patch("bridge_manager.start_watchdog", side_effect=lambda: watchdog_calls.append(True)), \
             patch("bridge_manager.shutdown_all"), \
             patch("scheduler.start_scheduler", side_effect=lambda: scheduler_calls.append(True)), \
             patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
            rc = web_main.main([
                "--no-scheduler",
                "--no-browser",
                "--data-dir", str(self.tmp / "webmain_data"),
                "--port", "18765",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(scheduler_calls, [])
        self.assertEqual(watchdog_calls, [True])

    def test_bridge_auth_headers_and_log_rotation(self):
        bridge_manager._tokens["u1"] = "tok-xyz"
        try:
            self.assertEqual(bridge_manager.auth_headers(self.t1), {"X-Bridge-Token": "tok-xyz"})
            self.assertEqual(bridge_manager.auth_headers(self.t2), {})
        finally:
            bridge_manager._tokens.pop("u1", None)

        log_file = self.tmp / "bridge.log"
        log_file.write_text("x" * 128, encoding="utf-8")
        bridge_manager._rotate_bridge_log(log_file, max_bytes=64, backup_count=2)
        self.assertFalse(log_file.exists())
        self.assertTrue((self.tmp / "bridge.log.1").exists())

    def test_accounts_update_password_revokes_sessions_and_throttles_last_seen(self):
        user = accounts.create_user("reset@example.com", "oldpass123", self.t1_dir, user_id="u-reset")
        token = accounts.create_session(user["id"], ip="127.0.0.1", ua="test")
        resolved1 = accounts.resolve_session(token)
        self.assertIsNotNone(resolved1)

        token_hash = accounts._hash_token(token)
        conn = accounts.get_conn()
        before_last_seen = conn.execute(
            "SELECT last_seen FROM sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()["last_seen"]
        # Force a different last_seen within 60s window and confirm resolve_session avoids unnecessary write
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds")
        conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (recent, token_hash))
        conn.commit()
        accounts.resolve_session(token)
        after_last_seen = conn.execute(
            "SELECT last_seen FROM sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()["last_seen"]
        self.assertEqual(after_last_seen, recent)

        changed = accounts.update_password("reset@example.com", "newpass1234")
        self.assertTrue(changed)
        self.assertIsNone(accounts.authenticate("reset@example.com", "oldpass123"))
        self.assertIsNotNone(accounts.authenticate("reset@example.com", "newpass1234"))
        self.assertIsNone(accounts.resolve_session(token))
        self.assertFalse(accounts.update_password("missing@example.com", "newpass1234"))

    def test_admin_bridge_restart_endpoint_uses_tenant_context(self):
        accounts.create_user("owner@example.com", "pass123456", self.t1_dir, user_id="u1")
        token = accounts.create_session("u1")
        client = TestClient(api_server.app, client=("127.0.0.1", 50000))
        client.cookies.set("wa_session", token)
        with patch("bridge_manager.restart", return_value={"online": True, "port": 3101, "started": True}) as mock_restart:
            resp = client.post("/admin/api/bridge/restart", headers={"Host": "127.0.0.1:8000"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["port"], 3101)
        mock_restart.assert_called_once()
        self.assertEqual(mock_restart.call_args[0][0].user_id, "u1")

    def test_security_and_limit_guards_present_in_bridge_and_login(self):
        root = Path(__file__).resolve().parent.parent
        bridge_js = (root / "bridge" / "index.js").read_text(encoding="utf-8")
        self.assertIn("hasValidBridgeToken", bridge_js)
        self.assertIn("limit: '75mb'", bridge_js)
        login_html = (root / "src" / "static" / "login.html").read_text(encoding="utf-8")
        self.assertIn("function safeNextPath", login_html)


if __name__ == "__main__":
    unittest.main()
