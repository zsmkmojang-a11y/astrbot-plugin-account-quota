import asyncio
import copy
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_quota import Event, Plugin, Context
from astrbot_plugin_account_quota import reset_monitor as reset


def forecast(p24=40, signal=None, p48=None):
    return reset.Forecast.parse({
        "probabilities": {"rounded_24h": p24, "rounded_48h": p24 if p48 is None else p48},
        "confidence": "medium", "last_reset_at": "2026-10-01T00:00:00Z",
        "age_days": 1, "official_signal": signal, "updated_at": "2026-10-02T00:00:00Z",
    })


def timeline(day=1, event_id=None):
    return {"events": [{"id": event_id or str(day), "group": "reset", "announcement_state": "announced",
                        "announced_at": f"2026-10-{day:02d}T00:00:00Z", "summary": "Reset landed", "url": "https://example.com/reset"}]}


class ResetFormatTests(unittest.TestCase):
    def test_forecast_percentages_timezone_credit(self):
        text = reset.credited(forecast().render("Asia/Shanghai"))
        for fragment in ("40%", "中 (medium)", "08:00:00", "无官方信号", "第三方预测", "https://codex-reset.com/"):
            self.assertIn(fragment, text)
        self.assertTrue(text.endswith(reset.CREDIT))

    def test_invalid_fields_are_not_zero_or_confirmed(self):
        for value in (True, -1, 101, float("nan"), "75"):
            with self.subTest(value=value), self.assertRaises(reset.ResetError):
                forecast(value)
        for value in ("2026-10-01", "bad", None):
            with self.assertRaises(reset.ResetError):
                reset.timestamp(value)
        with self.assertRaises(reset.ResetError):
            reset.Forecast.parse({})
        with self.assertRaises(reset.ResetError):
            reset.timezone_info("not/a/timezone")

    def test_strict_timeline_sorted_and_not_banked(self):
        rows = timeline(2)["events"] + timeline(1)["events"]
        rows += [{**rows[0], "id": "preview", "announcement_state": "none"},
                 {**rows[0], "id": "banked", "group": "banked"}]
        self.assertEqual([e.id for e in reset.confirmed_events({"events": rows})], ["2", "1"])
        with self.assertRaises(reset.ResetError):
            reset.confirmed_events({"events": [{**rows[0], "announced_at": None}]})

    def test_signal_hash_ignores_probability_and_dedups_id(self):
        a = {"summary": "Reset soon", "at": "2026-10-01T00:00:00Z", "probability": 50}
        b = {**a, "probability": 75}
        self.assertEqual(reset.signal_info(a), reset.signal_info(b))
        self.assertEqual(reset.signal_info({"id": "post", "summary": "hello"})[0], "post")
        self.assertEqual(reset.signal_info({"active": False}), ("", "无官方信号"))


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "reset_state.json"
        self.sent, self.logs = [], []
        async def send(origin, text):
            self.sent.append((origin, text))
        self.monitor = reset.ResetMonitor(self.path, {}, send, self.logs.append)

    async def asyncTearDown(self):
        await self.monitor.close()
        self.temp.cleanup()

    def evaluate(self, p=40, events=None, now=100000, signal=None):
        return self.monitor._evaluate(forecast(p, signal), reset.confirmed_events(events or timeline()), now)

    def test_first_boot_and_crossings(self):
        self.assertEqual(self.evaluate(40), [])
        for p, expected in [(77, 75), (80, None), (85, 83), (90, None), (95, 93)]:
            messages = self.evaluate(p)
            self.assertEqual(len(messages), 0 if expected is None else 1)
            if messages:
                self.assertIn(f"{expected}% 提醒档位", messages[0])
                self.assertIn("尚未确认", messages[0])

    def test_initial_high_probability_is_baseline(self):
        self.assertEqual(self.evaluate(95), [])
        self.assertEqual(self.evaluate(96), [])

    def test_either_window_default_hourly_and_strong_watch(self):
        self.assertEqual(self.monitor.interval, 3600)
        self.assertEqual(self.monitor.thresholds, [75, 83, 93])
        events = reset.confirmed_events(timeline())
        self.monitor._evaluate(forecast(15, p48=28), events, 100000)
        self.assertEqual(self.monitor._evaluate(forecast(40, p48=74), events, 100001), [])
        messages = self.monitor._evaluate(forecast(15, p48=75), events, 100002)
        self.assertEqual(len(messages), 1)
        self.assertIn("未来很有可能会重置", messages[0])
        messages = self.monitor._evaluate(forecast(83, p48=40), events, 100003)
        self.assertNotIn("官方很有可能已明确表示", messages[0])
        messages = self.monitor._evaluate(forecast(15, p48=93), events, 100004)
        self.assertEqual(len(messages), 1)
        self.assertIn("Strong Watch", messages[0])
        self.assertIn("尚未确认到账", messages[0])
        self.assertNotIn("官方给出了", messages[0])

    async def test_polling_minutes_and_legacy_seconds(self):
        for config, expected in [
            ({"poll_interval_minutes": 15}, 900),
            ({"poll_interval_minutes": 0}, 60),
            ({"poll_interval_minutes": 2000}, 86400),
            ({"poll_interval": 1800}, 1800),
            ({"poll_interval_minutes": 10, "poll_interval": 1800}, 600),
        ]:
            monitor = reset.ResetMonitor(self.path, config, self.monitor.send, self.logs.append)
            try:
                self.assertEqual(monitor.interval, expected)
                self.assertIn(f"轮询间隔：{expected / 60:g} 分钟", monitor.status("qq"))
            finally:
                await monitor.close()

    def test_signal_below_threshold_does_not_notify(self):
        self.evaluate()
        self.assertEqual(self.evaluate(28, signal={"id": "low", "summary": "hint"}), [])

    def test_confirmation_supersedes_same_poll_prediction(self):
        self.evaluate()
        messages = self.evaluate(95, events=timeline(2))
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("✅"))

    def test_cooldown_does_not_block_higher_level(self):
        self.evaluate(40)
        self.assertEqual(len(self.evaluate(77)), 1)
        self.evaluate(40)
        self.assertEqual(self.evaluate(77, now=100100), [])
        self.assertEqual(len(self.evaluate(85, now=100101)), 1)
        self.evaluate(40)
        self.assertEqual(len(self.evaluate(77, now=121701)), 1)

    def test_empty_timeline_and_regressions_do_not_alert(self):
        self.monitor._evaluate(None, [], 1)
        self.assertFalse(self.monitor.state["timeline_initialized"])
        self.assertEqual(self.evaluate(events=timeline(3)), [])
        self.assertEqual(self.evaluate(events=timeline(1)), [])
        messages = self.evaluate(events=timeline(4))
        self.assertEqual(len(messages), 1)
        self.assertIn("✅", messages[0])
        self.assertEqual(self.evaluate(events=timeline(4)), [])

    def test_signals_distinct_from_confirmations_and_never_replay(self):
        self.evaluate(signal={"id": "old", "summary": "initial"})
        self.assertEqual(self.evaluate(signal={"id": "old", "summary": "initial"}), [])
        messages = self.evaluate(85, signal={"id": "new", "summary": "soon"})
        self.assertEqual(len(messages), 1)
        self.assertIn("尚未确认到账", messages[0])
        self.evaluate(signal=None)
        self.assertEqual(self.evaluate(85, signal={"id": "new", "summary": "soon"}), [])
        messages = self.evaluate(85, events=timeline(2), signal={"id": "2", "summary": "landed"})
        self.assertEqual(len(messages), 1)
        self.assertIn("✅", messages[0])

    def test_timeline_failure_cannot_downgrade_confirmed_signal(self):
        self.evaluate()
        self.evaluate(85, events=timeline(2), signal={"id": "2", "summary": "landed"})
        self.assertEqual(self.monitor._evaluate(forecast(85, {"id": "2", "summary": "landed"}), None, 100100), [])
        self.assertEqual(self.monitor.state["last_seen_signal_id"], "")

    def test_toggles_observe_without_alerting(self):
        self.monitor.config = dict(probability_warning=False, signal_notification=False, reset_notification=False)
        self.evaluate()
        self.assertEqual(self.evaluate(95, timeline(2), signal={"id": "new"}), [])
        self.assertEqual(self.monitor.state["last_seen_reset_id"], "2")

    async def test_persistent_subscriptions_and_restart_dedup(self):
        await self.monitor.subscribe("qq:GroupMessage:123", True)
        await self.monitor.set_signal_notification("qq:GroupMessage:123", False)
        self.evaluate(40)
        self.evaluate(77, timeline(2), signal={"id": "signal"})
        await self.monitor._save()
        restored = reset.ResetMonitor(self.path, {}, self.monitor.send, self.logs.append)
        restored._load()
        try:
            self.assertTrue(restored.state["subscriptions"]["qq:GroupMessage:123"])
            self.assertFalse(restored.signal_enabled("qq:GroupMessage:123"))
            self.assertEqual(restored._evaluate(forecast(77, {"id": "signal"}), reset.confirmed_events(timeline(2)), 100100), [])
        finally:
            await restored.close()

    async def test_signal_off_only_suppresses_standalone_not_crossings_or_reset(self):
        await self.monitor.subscribe("muted", True)
        await self.monitor.subscribe("enabled", True)
        await self.monitor.set_signal_notification("muted", False)
        self.evaluate(77)
        current = forecast(77, {"id": "new", "summary": "current information"})
        events = reset.confirmed_events(timeline())
        async def get(endpoint):
            return current if endpoint == "forecast" else events
        self.monitor.api.get = get
        await self.monitor.poll()
        self.assertEqual([origin for origin, _ in self.sent], ["enabled"])
        current = forecast(85, {"id": "newer", "summary": "updated information"})
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 3)
        self.assertEqual({origin for origin, _ in self.sent[1:]}, {"muted", "enabled"})
        self.assertTrue(all("updated information" in text for _, text in self.sent[1:]))
        events = reset.confirmed_events(timeline(2))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 5)
        self.assertTrue(all(text.startswith("✅") for _, text in self.sent[-2:]))

    async def test_session_setting_overrides_default_off(self):
        self.monitor.config = {"signal_notification": False}
        await self.monitor.subscribe("default", True)
        await self.monitor.subscribe("override", True)
        await self.monitor.set_signal_notification("override", True)
        self.evaluate(77)
        async def get(endpoint):
            return forecast(77, {"id": "new", "summary": "signal"}) if endpoint == "forecast" else reset.confirmed_events(timeline())
        self.monitor.api.get = get
        await self.monitor.poll()
        self.assertEqual([origin for origin, _ in self.sent], ["override"])

    async def test_old_state_migration_keeps_subscriptions_and_baseline(self):
        await self.monitor.subscribe("old", True)
        self.evaluate(77)
        await self.monitor._save()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.pop("signal_preferences")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        restored = reset.ResetMonitor(self.path, {}, self.monitor.send, self.logs.append)
        try:
            restored._load()
            self.assertTrue(restored.state["subscriptions"]["old"])
            self.assertEqual(restored.state["last_seen_reset_id"], "1")
            self.assertTrue(restored.signal_enabled("old"))
            self.assertEqual(restored._evaluate(forecast(77), reset.confirmed_events(timeline()), 100100), [])
        finally:
            await restored.close()

    async def test_disabling_signal_clears_pending_and_save_failure_rolls_back(self):
        self.monitor.state["pending"]["qq"] = [
            {"text": "⚠️ Codex 新官方信号 test", "created_at": time.time()},
            {"text": "Codex Reset 预测达到 83%", "created_at": time.time()},
        ]
        with patch.object(self.monitor, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(reset.ResetError):
                await self.monitor.set_signal_notification("qq", False)
        self.assertTrue(self.monitor.signal_enabled("qq"))
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 2)
        await self.monitor.set_signal_notification("qq", False)
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 1)
        self.assertTrue(self.monitor.state["pending"]["qq"][0]["text"].startswith("Codex Reset"))

    async def test_corrupt_state_is_not_overwritten(self):
        self.path.write_text('{"subscriptions": "invalid"}', encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(reset.ResetError):
            await self.monitor.start()
        self.assertIsNone(self.monitor.task)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_poll_failure_isolated_delivery_retry_and_off(self):
        await self.monitor.subscribe("bad", True)
        await self.monitor.subscribe("good", True)
        self.evaluate()
        fails = True
        async def send(origin, text):
            if origin == "bad" and fails:
                raise OSError("unavailable")
            self.sent.append((origin, text))
        self.monitor.send = send
        async def get(endpoint):
            return forecast(77) if endpoint == "forecast" else reset.confirmed_events(timeline(2))
        self.monitor.api.get = get
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)  # 最终确认覆盖同轮预测，只送达 good。
        self.assertEqual(len(self.monitor.state["pending"]["bad"]), 1)
        fails = False
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(text.endswith(reset.CREDIT) for _, text in self.sent))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        await self.monitor.subscribe("good", False)
        self.assertFalse(self.monitor.state["subscriptions"]["good"])

    async def test_endpoint_failure_does_not_block_other_event(self):
        self.evaluate()
        await self.monitor.subscribe("good", True)
        async def get(endpoint):
            if endpoint == "forecast":
                raise reset.ResetError("offline")
            return reset.confirmed_events(timeline(2))
        self.monitor.api.get = get
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.logs)

    async def test_save_failure_never_claims_subscription_success(self):
        with patch.object(self.monitor, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(reset.ResetError):
                await self.monitor.subscribe("group", True)
        self.assertNotIn("group", self.monitor.state["subscriptions"])

    async def test_task_shutdown_closes_session(self):
        entered = asyncio.Event()
        async def poll():
            entered.set()
            await asyncio.sleep(100)
        self.monitor.poll = poll
        await self.monitor.start()
        await entered.wait()
        await self.monitor.close()
        self.assertTrue(self.monitor.task.done())
        self.assertTrue(self.monitor.api.closed)

    async def test_shutdown_waits_for_subscription_write(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.monitor._write
        def write(data):
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
            original(data)
        with patch.object(self.monitor, "_write", side_effect=write):
            subscribing = asyncio.create_task(self.monitor.subscribe("qq", True))
            await entered.wait()
            closing = asyncio.create_task(self.monitor.close())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(closing.done())
            finally:
                release.set()
                await asyncio.gather(subscribing, closing)
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["subscriptions"]["qq"])


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_keeps_rate_gate_without_key_error(self):
        api = reset.ResetAPI()
        api.next_request["forecast"] = time.monotonic() + 60
        try:
            with self.assertRaises(reset.ResetError):
                await api.get("forecast")
        finally:
            await api.close()

    async def test_http_cache_retry_after_and_errors(self):
        from aiohttp import web
        hits = []
        mode = "ok"
        async def handler(request):
            hits.append((request.path, request.headers.get("User-Agent")))
            if mode == "429":
                return web.Response(status=429, headers={"Retry-After": "600"})
            if mode == "date":
                from email.utils import formatdate
                return web.Response(status=429, headers={"Retry-After": formatdate(time.time() + 600, usegmt=True)})
            if mode == "500":
                return web.Response(status=500)
            if mode == "json":
                return web.Response(text="not-json")
            if mode == "missing":
                return web.json_response({})
            if mode == "redirect":
                raise web.HTTPFound("/api/forecast")
            if mode == "large":
                return web.Response(body=b"x" * (2 * 1024 * 1024 + 1))
            return web.json_response({"probabilities": {"rounded_24h": 50, "rounded_48h": 75}, "confidence": "low", "last_reset_at": None, "age_days": None, "updated_at": "2026-10-01T00:00:00Z", "official_signal": None})
        app = web.Application()
        app.router.add_get("/api/forecast", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        api = reset.ResetAPI()
        try:
            with patch.object(reset, "SOURCE", f"http://127.0.0.1:{port}/"):
                values = await asyncio.gather(*(api.get("forecast") for _ in range(10)))
                self.assertTrue(all(value.p24 == 50 for value in values))
                self.assertEqual(len(hits), 1)
                self.assertIn("astrbot-plugin-account-quota/1.4.1", hits[0][1])
                for current in ("429", "date", "500", "json", "missing", "redirect", "large"):
                    mode = current
                    api.next_request["forecast"] = 0
                    with self.assertRaises(reset.ResetError):
                        await api.get("forecast")
                    before = len(hits)
                    with self.assertRaises(reset.ResetError):
                        await api.get("forecast")
                    self.assertEqual(len(hits), before)
                    if current in ("429", "date"):
                        self.assertGreater(api.next_request["forecast"] - time.monotonic(), 590)
        finally:
            await api.close()
            await runner.cleanup()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_default_insertion_preserves_and_migrates_seconds(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        # 框架按 schema 注入新默认值并移除未声明字段，旧字段必须仍在 schema。
        old = {"poll_interval": 1800}
        normalized = {key: old.get(key, value["default"]) for key, value in schema.items()}
        class Config(dict):
            async def save_config_async(self, updates):
                self.update(updates)
                self.saved = dict(self)
        config = Config(normalized)
        plugin = Plugin(Context(), config)
        try:
            await plugin._migrate_poll_config()
            self.assertEqual(config["poll_interval_minutes"], 30)
            self.assertEqual(config.saved["poll_interval"], 0)
            config["poll_interval_minutes"] = 60
            await plugin._migrate_poll_config()
            self.assertEqual(config["poll_interval_minutes"], 60)  # 后续设置 60 不会被旧值覆盖。
        finally:
            await plugin.terminate()

    async def test_signal_command_group_permissions_and_session_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            plugin.reset_monitor = reset.ResetMonitor(Path(tmp) / "state.json", {}, None, lambda text: None)
            for kind, role, astr_admin, allowed in [("FriendMessage", "member", False, True), ("GroupMessage", "member", False, False), ("GroupMessage", "admin", False, True), ("GroupMessage", "owner", False, True), ("GroupMessage", "member", True, True)]:
                event = Event(message="codex-reset signal off", admin=astr_admin)
                event.get_message_type = lambda: kind
                event.get_platform_name = lambda: "aiocqhttp"
                event.message_obj = types.SimpleNamespace(raw_message={"sender": {"role": role}})
                event.unified_msg_origin = f"qq:{kind}:{role}:{astr_admin}"
                await plugin.reset_command(event, "signal", "off")
                self.assertEqual(plugin.reset_monitor.signal_enabled(event.unified_msg_origin), not allowed)
                self.assertTrue(plugin.reset_monitor.signal_enabled("another-session"))
                self.assertTrue(event.stopped)
                self.assertTrue(event.sent[0].endswith(reset.CREDIT))
            await plugin.terminate()

    async def test_public_queries_and_history_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            monitor = reset.ResetMonitor(Path(tmp) / "state.json", {}, None, lambda text: None)
            plugin.reset_monitor = monitor
            async def get(endpoint):
                return forecast() if endpoint == "forecast" else reset.confirmed_events(timeline())
            monitor.api.get = get
            for args in (("", ""), ("history", ""), ("history", "10"), ("history", "21"), ("history", "bad"), ("watch", "status")):
                event = Event(message="codex-reset " + " ".join(args), admin=False)
                await plugin.reset_command(event, *args)
                self.assertEqual(len(event.sent), 1)
                self.assertNotIn("仅 AstrBot 管理员", event.sent[0])
                self.assertTrue(event.sent[0].endswith(reset.CREDIT))
                self.assertTrue(event.stopped)
            await plugin.terminate()

    async def test_watch_roles_and_current_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            plugin.reset_monitor = reset.ResetMonitor(Path(tmp) / "state.json", {}, None, lambda text: None)
            for kind, role, astr_admin, allowed in [("FriendMessage", "member", False, True), ("GroupMessage", "member", False, False), ("GroupMessage", "admin", False, True), ("GroupMessage", "owner", False, True), ("GroupMessage", "member", True, True)]:
                event = Event(message="codex-reset watch on", admin=astr_admin)
                event.get_message_type = lambda: kind
                event.get_platform_name = lambda: "aiocqhttp"
                event.message_obj = types.SimpleNamespace(raw_message={"sender": {"role": role}})
                event.unified_msg_origin = f"qq:{kind}:{role}:{astr_admin}"
                await plugin.reset_command(event, "watch", "on")
                self.assertEqual(plugin.reset_monitor.state["subscriptions"].get(event.unified_msg_origin, False), allowed)
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
