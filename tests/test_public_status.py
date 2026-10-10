import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_quota import Plugin, Context, Event, quota
from test_reset_monitor import forecast, timeline
from astrbot_plugin_account_quota import reset_monitor as reset
ServiceStatus = reset.ServiceStatus


def status(degraded=False, offset=0, stale=False):
    at = reset.datetime.fromtimestamp(time.time() + offset, reset.timezone.utc).isoformat()
    value = "partial_outage" if degraded else "operational"
    return {"stale": stale, "checked_at": at,
            "current": {"codex": value, "indicator": "major"},
            "surfaces": [{"id": "cli", "label": "Codex CLI", "status": value}]}


def banked(day=2, state="announced", event_id="card", url=None):
    data = timeline()
    data["events"].append({"id": event_id, "group": "credits", "banked_state": state,
                           "scope": "global", "announced_at": f"2026-10-{day:02d}T00:00:00Z",
                           "summary": "A banked reset update", "url": url})
    return data


class PublicFormatTests(unittest.TestCase):
    def test_service_ignores_global_indicator_and_normal_is_hidden(self):
        current = ServiceStatus.parse(status(), reset.timestamp, reset.safe_text)
        self.assertFalse(current.degraded)
        self.assertEqual(current.render(reset.display_time, "UTC"), "")

    def test_service_stale_unknown_and_missing_cannot_be_recovery(self):
        for mutation in (
            lambda d: d.update(stale=True), lambda d: d.pop("stale"),
            lambda d: d["current"].update(codex="unknown"),
            lambda d: d.update(surfaces=[]),
            lambda d: d["surfaces"][0].update(status="unknown"),
            lambda d: d.update(checked_at="2020-01-01T00:00:00Z"),
        ):
            data = status()
            mutation(data)
            with self.assertRaises(ValueError):
                ServiceStatus.parse(data, reset.timestamp, reset.safe_text)

    def test_degraded_aggregate_and_explicit_bad_component_are_detected(self):
        for aggregate in ("degraded", "unknown", None):
            data = status(True)
            data["current"]["codex"] = aggregate
            self.assertTrue(ServiceStatus.parse(data, reset.timestamp, reset.safe_text).degraded)

    def test_banked_observed_without_url_and_invalid_optional_rows(self):
        data = banked(state="available")
        data["events"] += [dict(data["events"][-1], id="bad", announced_at="bad"),
                            dict(data["events"][-1], id="private", scope="personal"),
                            dict(data["events"][-1], id="invalid", banked_state={})]
        events = reset.parse_timeline(data)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events.banked), 1)
        text = events.banked[0].render(reset.display_time, "UTC")
        self.assertIn("来源报告已到账", text)
        self.assertIn("不会直接刷新额度", text)
        self.assertNotIn("详情：", text)

    def test_card_flags_cannot_masquerade_as_regular_reset(self):
        data = timeline(3)
        data["events"][0]["banked_state"] = "available"
        self.assertEqual(reset.confirmed_events(data), [])
        for malformed in ({}, [], "", False, "unsupported"):
            data["events"][0]["banked_state"] = malformed
            self.assertEqual(len(reset.confirmed_events(data)), 1)


class PublicMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sent = []
        async def send(origin, text):
            self.sent.append((origin, text))
        self.monitor = reset.ResetMonitor(Path(self.temp.name) / "state.json", {}, send, lambda text: None)
        self.current, self.events, self.service = forecast(), reset.parse_timeline(banked()), None
        async def get(endpoint):
            if endpoint == "forecast": return self.current
            if endpoint == "timeline": return self.events
            if endpoint == "status-history":
                if isinstance(self.service, Exception): raise self.service
                return self.service
            raise AssertionError(endpoint)
        self.monitor.api.get = get
        await self.monitor.subscribe("qq", True)

    async def asyncTearDown(self):
        await self.monitor.close()
        self.temp.cleanup()

    def set_service(self, degraded, offset=0):
        self.service = ServiceStatus.parse(status(degraded, offset), reset.timestamp, reset.safe_text)

    async def test_banked_first_quiet_new_stage_dedup_restart_and_no_reset(self):
        await self.monitor.poll()
        self.assertEqual(self.sent, [])
        reset_id = self.monitor.state["last_seen_reset_id"]
        self.events = reset.parse_timeline(banked(state="arriving"))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("正在发放", self.sent[-1][1])
        self.events = reset.parse_timeline(banked(3, "available", "landed"))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.events = reset.parse_timeline(banked(2, "announced", "backfill"))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.monitor.state["last_seen_reset_id"], reset_id)
        restored = reset.ResetMonitor(self.monitor.path, {}, self.monitor.send, lambda text: None)
        try:
            restored._load()
            self.assertEqual(restored._evaluate_public(self.events, None), [])
        finally:
            await restored.close()
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)

    async def test_anomaly_recovery_only_after_detected_anomaly_no_repeats(self):
        self.set_service(False)
        await self.monitor.poll()
        self.assertEqual(self.sent, [])
        self.set_service(True, 1)
        await self.monitor.poll()
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("服务出现异常", self.sent[0][1])
        self.service = reset.ResetError("offline")
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.set_service(False, 2)
        await self.monitor.poll()
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("服务已经恢复", self.sent[-1][1])

    async def test_first_anomaly_alerts_but_old_snapshot_cannot_recover(self):
        self.set_service(True, 2)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.set_service(False, -1)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)

    async def test_public_state_save_failure_rolls_back_before_delivery(self):
        await self.monitor.poll()
        before = copy.deepcopy(self.monitor.state)
        self.events = reset.parse_timeline(banked(3, "available", "landed"))
        self.set_service(True)
        with patch.object(self.monitor, "_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): await self.monitor.poll()
        self.assertEqual(self.monitor.state, before)
        self.assertEqual(self.sent, [])
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)

    async def test_empty_banked_baseline_does_not_swallow_first_new_card(self):
        self.events = reset.parse_timeline(timeline())
        await self.monitor.poll()
        self.events = reset.parse_timeline(banked())
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("🎟️"))

    async def test_large_banked_baseline_does_not_repeat_latest(self):
        for same_time in (True, False):
            monitor = reset.ResetMonitor(self.monitor.path, {}, None, lambda text: None)
            try:
                rows = []
                for i in range(201):
                    data = banked(event_id=f"card-{i:03d}")
                    row = data["events"][-1]
                    if not same_time:
                        row["announced_at"] = reset.datetime.fromtimestamp(time.time() - i, reset.timezone.utc).isoformat()
                    rows.append(row)
                events = reset.parse_timeline({"events": rows})
                self.assertEqual(monitor._evaluate_public(events, None), [])
                self.assertEqual(monitor._evaluate_public(events, None), [])
                self.assertEqual(monitor._evaluate_public(events, None), [])
                self.assertEqual(len(monitor.state["seen_banked_keys"]), 200)
                self.assertIn(events.banked[0].key, monitor.state["seen_banked_keys"])
            finally:
                await monitor.close()

    async def test_query_watermark_prevents_old_snapshot_recovery(self):
        self.set_service(True)
        await self.monitor.poll()
        self.set_service(True, 10)
        self.assertIn("服务出现异常", await self.monitor.service_notice())
        self.set_service(False, 5)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.set_service(True, 10)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.set_service(False, 9)
        self.assertEqual(await self.monitor.service_notice(), "")
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.set_service(False, 11)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("服务已经恢复", self.sent[-1][1])

    async def test_status_expiring_during_cache_cannot_be_used(self):
        self.set_service(True)
        observed = reset.timestamp(self.service.checked_at)
        with patch.object(reset.time, "time", return_value=observed + 7201):
            self.assertEqual(await self.monitor.service_notice(), "")
            await self.monitor.poll()
        self.assertEqual(self.sent, [])

    async def test_confirmation_does_not_erase_card_or_service_messages(self):
        await self.monitor.poll()
        self.current = forecast(95)
        data = banked(3, "available", "newcard")
        data["events"][0] = timeline(3)["events"][0]
        self.events = reset.parse_timeline(data)
        self.set_service(True)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(any(text.startswith("✅") for _, text in self.sent))
        self.assertTrue(any(text.startswith("🎟️") for _, text in self.sent))
        self.assertTrue(any(text.startswith("⚠️ Codex 服务") for _, text in self.sent))

    async def test_failed_anomaly_is_superseded_by_recovery(self):
        async def fail(*args): raise OSError("offline")
        self.monitor.send = fail
        self.set_service(True)
        await self.monitor.poll()
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 1)
        self.monitor.send = lambda origin, text: self._send(origin, text)
        self.set_service(False, 1)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("服务已经恢复", self.sent[0][1])

    async def test_status_failure_defers_retry_without_blocking_reset(self):
        async def fail(*args): raise OSError("offline")
        self.monitor.send = fail
        self.set_service(True)
        await self.monitor.poll()
        self.monitor.send = self._send
        self.service = reset.ResetError("offline")
        self.events = reset.parse_timeline(timeline(3))
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][1].startswith("✅"))
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 1)
        self.set_service(True, 1)
        await self.monitor.poll()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("服务出现异常", self.sent[-1][1])

    async def _send(self, origin, text):
        self.sent.append((origin, text))

    async def test_old_state_migrates_and_config_toggles(self):
        await self.monitor.poll()
        data = copy.deepcopy(self.monitor.state)
        for key in ("banked_initialized", "seen_banked_keys", "last_banked_at", "service_initialized", "service_degraded", "service_checked_at", "service_observed_at"):
            data.pop(key)
        self.monitor.path.write_text(__import__('json').dumps(data), encoding="utf-8")
        self.monitor._load()
        self.assertFalse(self.monitor.state["service_initialized"])
        self.monitor.config = {"banked_notification": False, "service_notification": False}
        self.set_service(True)
        await self.monitor.poll()
        self.events = reset.parse_timeline(banked(3, "available", "newcard"))
        await self.monitor.poll()
        self.assertEqual(self.sent, [])

    async def test_query_only_appends_anomaly_and_card_source_failure_isolated(self):
        self.set_service(False)
        text = await self.monitor.query_forecast()
        self.assertNotIn("Codex 服务", text)
        self.assertIn("重置卡", text)
        self.set_service(True, 1)
        text = await self.monitor.query_forecast()
        self.assertIn("服务出现异常", text)
        self.service = reset.ResetError("offline")
        self.assertNotIn("Codex 服务", await self.monitor.query_forecast())
        plugin = Plugin(Context())
        plugin.reset_monitor = self.monitor
        try:
            self.set_service(True, 2)
            event = Event(message="codex-reset", admin=False)
            await plugin.reset_command(event)
            self.assertIn("服务出现异常", event.sent[0])
        finally:
            await plugin.terminate()

    async def test_quota_queries_only_include_anomaly_when_codex_is_requested(self):
        plugin = Plugin(Context(), {"deepseek_query_enabled": False})
        plugin.reset_monitor = self.monitor
        async def snapshot(): return quota.Snapshot("quota result", time.time())
        plugin._codex_snapshot = snapshot
        try:
            self.set_service(False)
            self.assertNotIn("Codex 服务", await plugin._answer(Event(), "codex"))
            self.set_service(True)
            for target in ("codex", "all"):
                self.assertIn("服务出现异常", await plugin._answer(Event(), target))
            self.assertNotIn("Codex 服务", await plugin._answer(Event(), "deepseek"))
            plugin.config["codex_query_enabled"] = False
            self.assertNotIn("Codex 服务", await plugin._answer(Event(), "codex"))
        finally:
            await plugin.terminate()


class PublicAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_timeline_status_cache_and_invalid_status(self):
        from aiohttp import web
        hits = []
        stale = False
        async def handler(request):
            hits.append(request.path)
            data = banked(state="available") if request.path.endswith("timeline") else status(True, stale=stale)
            return web.json_response(data)
        app = web.Application()
        app.router.add_get("/api/timeline", handler)
        app.router.add_get("/api/status-history", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        api = reset.ResetAPI()
        try:
            with patch.object(reset, "SOURCE", f"http://127.0.0.1:{port}/"):
                events = await api.get("timeline")
                self.assertEqual(len(events), 1)
                self.assertEqual(events.banked[0].state, "available")
                current = await api.get("status-history")
                self.assertTrue(current.degraded)
                self.assertIs(await api.get("status-history"), current)
                self.assertEqual(hits.count("/api/status-history"), 1)
                with patch.object(reset.time, "time", return_value=reset.timestamp(current.checked_at) + 7201):
                    with self.assertRaises(reset.ResetError):
                        await api.get("status-history")
                stale = True
                api.next_request["status-history"] = 0
                with self.assertRaises(reset.ResetError):
                    await api.get("status-history")
        finally:
            await api.close()
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
