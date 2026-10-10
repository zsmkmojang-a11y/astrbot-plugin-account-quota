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

from test_quota import Context, Event, Plugin
from test_reset_monitor import forecast, timeline
from astrbot_plugin_account_quota import alarm_monitor as alarm
from astrbot_plugin_account_quota import reset_monitor as reset

NOW = 1800000000.0
DAY = 86400


def account_data(weekly=NOW + 3 * DAY, expiry=NOW + 3 * DAY):
    return {"rateLimitsByLimitId": {"codex": {"primary": {"windowDurationMins": 300, "resetsAt": NOW + 100, "usedPercent": 50},
           "secondary": {"windowDurationMins": 10080, "resetsAt": weekly, "usedPercent": 30}}},
           "rateLimitResetCredits": {"availableCount": 1, "credits": [{"status": "available", "expiresAt": expiry}]}}


class ScheduleTests(unittest.TestCase):
    def test_weekly_only_and_available_cards_grouped(self):
        data = account_data()
        data["rateLimitResetCredits"]["credits"] += [{"status": "available", "expiresAt": NOW + 3 * DAY}, {"status": "used", "expiresAt": NOW + DAY}, {"status": "available", "expiresAt": NOW - 1}]
        weekly, cards = alarm.schedule_from_quota(data, NOW)
        self.assertEqual(list(weekly), ["codex:secondary"])
        self.assertEqual(next(iter(cards.values()))["count"], 2)

    def test_missing_fields_are_not_inferred_or_zero(self):
        self.assertEqual(alarm.schedule_from_quota({}, NOW), (None, None))
        data = account_data()
        data["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] = None
        data["rateLimitResetCredits"]["credits"][0]["expiresAt"] = True
        self.assertEqual(alarm.schedule_from_quota(data, NOW), (None, None))
        data["rateLimitResetCredits"] = {"availableCount": 0}
        self.assertEqual(alarm.schedule_from_quota(data, NOW)[1], {})

    def test_different_card_expiry_seconds_never_merge(self):
        data = account_data()
        data["rateLimitResetCredits"]["credits"].append({"status": "available", "expiresAt": NOW + 3 * DAY + 3600})
        _, cards = alarm.schedule_from_quota(data, NOW)
        self.assertEqual(len(cards), 2)
        self.assertEqual(sorted(entry["at"] for entry in cards.values()), [NOW + 3 * DAY, NOW + 3 * DAY + 3600])


class AlarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "alarm.json"
        self.sent, self.logs, self.refreshes = [], [], []
        async def send(origin, text):
            self.sent.append((origin, text))
        async def refresh():
            self.refreshes.append(True)
        self.monitor = alarm.AlarmMonitor(self.path, "account", send, refresh, self.logs.append)

    async def asyncTearDown(self):
        await self.monitor.close()
        self.temp.cleanup()

    async def test_48h_24h_and_natural_refresh_once(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        await self.monitor.tick(NOW)
        self.assertEqual(self.sent, [])
        await self.monitor.tick(NOW + DAY)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("48 小时" in text and "喵" in text for _, text in self.sent))
        await self.monitor.tick(NOW + DAY + 1)
        self.assertEqual(len(self.sent), 2)
        await self.monitor.tick(NOW + 2 * DAY)
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(all("24 小时" in text for _, text in self.sent[-2:]))
        await self.monitor.tick(NOW + 3 * DAY)
        self.assertEqual(len(self.sent), 5)
        self.assertIn("已经到自然刷新时间", self.sent[-1][1])
        self.assertEqual(len(self.refreshes), 1)
        await self.monitor.tick(NOW + 3 * DAY + 1)
        self.assertEqual(len(self.sent), 5)

    async def test_first_record_under24h_only_current_stage(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(NOW + 12 * 3600, NOW + 12 * 3600), NOW)
        self.assertEqual(len(self.sent), 2)  # 记忆时立即发送，不等待轮询。
        await self.monitor.tick(NOW)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("24 小时" in text and "48 小时" not in text for _, text in self.sent))

    async def test_default_poll_and_custom_reminder_windows(self):
        self.assertEqual(self.monitor.interval, 30 * 60)
        custom = alarm.AlarmMonitor(self.path, "account", self.monitor.send, self.monitor.refresh, self.logs.append,
                                    {"codex_alarm_poll_minutes": 7, "codex_alarm_reminder_hours_1": 6, "codex_alarm_reminder_hours_2": 12})
        try:
            await custom.subscribe("qq", True)
            await custom.remember(account_data(NOW + 20 * 3600, NOW + 20 * 3600), NOW)
            self.assertEqual(self.sent, [])
            self.assertIn("7 分钟", custom.status("qq"))
            await custom.tick(NOW + 8 * 3600)
            self.assertTrue(all("12 小时" in text for _, text in self.sent))
            await custom.tick(NOW + 14 * 3600)
            await custom.tick(NOW + 14 * 3600 + 1)
            self.assertEqual(len(self.sent), 4)
            self.assertTrue(all("6 小时" in text for _, text in self.sent[-2:]))
        finally:
            await custom.close()

    async def test_identical_configured_windows_send_once(self):
        custom = alarm.AlarmMonitor(self.path, "account", self.monitor.send, self.monitor.refresh, self.logs.append,
                                    {"codex_alarm_reminder_hours_1": 12, "codex_alarm_reminder_hours_2": 12})
        try:
            await custom.subscribe("qq", True)
            await custom.remember(account_data(NOW + 10 * 3600, NOW + 10 * 3600), NOW)
            await custom.tick(NOW + 6 * 3600)
            self.assertEqual(len(self.sent), 2)
        finally:
            await custom.close()

    async def test_restart_after_refresh_reads_current_quota_without_old_notification(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        # 模拟刷新到点时消息未送达，随后停机；恢复时应静默重新查询。
        key, entry = next(iter(self.monitor.state["weekly"].items()))
        self.monitor.state["pending"]["qq"] = [self.monitor._event("weekly", key, entry, "due", NOW + 3 * DAY)]
        await self.monitor._save()
        restored = alarm.AlarmMonitor(self.path, "account", self.monitor.send, None, self.logs.append)
        async def refresh():
            self.refreshes.append(True)
            await restored.remember(account_data(NOW + 10 * DAY, NOW + 11 * DAY), NOW + 3 * DAY + 10)
        restored.refresh = refresh
        try:
            restored._load()
            await restored.tick(NOW + 3 * DAY + 10)
            self.assertEqual(self.sent, [])
            self.assertEqual(len(self.refreshes), 1)
            self.assertEqual(restored.state["weekly"][key]["at"], NOW + 10 * DAY)
            self.assertFalse(restored.state["pending"]["qq"])
        finally:
            await restored.close()

    async def test_query_past_old_refresh_does_not_replay_notification(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        await self.monitor.remember(account_data(NOW + 10 * DAY, NOW + 11 * DAY), NOW + 3 * DAY + 1)
        self.assertEqual(self.sent, [])

    async def test_armed_deadline_tolerates_short_delay_but_not_stale_catchup(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        await self.monitor.tick(NOW + 3 * DAY + 2, scheduled_due={"codex:secondary": NOW + 3 * DAY})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("已经到自然刷新时间", self.sent[0][1])
        await self.monitor.tick(NOW + 3 * DAY + 3, scheduled_due={"codex:secondary": NOW + 3 * DAY})
        self.assertEqual(len(self.sent), 1)

    async def test_background_deadline_wakes_before_poll_interval(self):
        refreshed = asyncio.Event()
        now = time.time()
        at = now + 1
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(at, now + 10 * DAY), now)
        self.sent.clear()  # 只检查实际定时器到点发送。
        async def refresh():
            await self.monitor.remember(account_data(at + 7 * DAY, now + 10 * DAY))
            refreshed.set()
        self.monitor.refresh = refresh
        await self.monitor.start()
        await asyncio.wait_for(refreshed.wait(), 3)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("已经到自然刷新时间", self.sent[0][1])

    async def test_failed_due_is_not_replayed_after_reconnect(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        original = self.monitor.send
        async def fail(origin, text):
            raise OSError("offline")
        self.monitor.send = fail
        await self.monitor.tick(NOW + 3 * DAY)
        self.assertTrue(self.monitor.state["pending"]["qq"])
        self.monitor.send = original
        await self.monitor.tick(NOW + 3 * DAY + 30)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.monitor.state["pending"]["qq"])

    async def test_work_crossing_deadline_still_notifies_and_reads_immediately(self):
        refreshed = asyncio.Event()
        now = time.time()
        await self.monitor.remember(account_data(now + 0.3, now + 10 * DAY), now)
        await self.monitor.subscribe("qq", True)
        original = self.monitor.send
        async def slow_send(origin, text):
            if "24 小时" in text:
                await asyncio.sleep(0.5)
            await original(origin, text)
        async def refresh():
            await self.monitor.remember(account_data(time.time() + 7 * DAY, time.time() + 10 * DAY))
            refreshed.set()
        self.monitor.send, self.monitor.refresh = slow_send, refresh
        await self.monitor.start()
        await asyncio.wait_for(refreshed.wait(), 3)
        self.assertEqual(sum("已经到自然刷新时间" in text for _, text in self.sent), 1)

    async def test_restart_keeps_dates_subscriptions_and_dedup(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(NOW + 2 * DAY, NOW + 2 * DAY), NOW)
        await self.monitor.tick(NOW)
        restored = alarm.AlarmMonitor(self.path, "account", self.monitor.send, self.monitor.refresh, self.logs.append)
        try:
            restored._load()
            restored.refresh_requested = False
            await restored.tick(NOW + 1)
            self.assertEqual(len(self.sent), 2)
            self.assertTrue(restored.state["subscriptions"]["qq"])
            self.assertEqual(restored.state["weekly"], self.monitor.state["weekly"])
        finally:
            await restored.close()

    async def test_refresh_updates_next_cycle_without_losing_due_notification(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(NOW + DAY), NOW)
        # 自然刷新已到，用户先查询到了新周期，旧周期的到时通知仍需发送。
        await self.monitor.remember(account_data(NOW + 8 * DAY), NOW + DAY)
        await self.monitor.tick(NOW + DAY)
        self.assertEqual(sum("已经到自然刷新时间" in text for _, text in self.sent), 1)
        self.assertEqual(self.monitor.state["weekly"]["codex:secondary"]["at"], NOW + 8 * DAY)

    async def test_early_global_reset_replaces_future_schedule_and_pending(self):
        await self.monitor.subscribe("qq", True)
        original = self.monitor.send
        async def fail(origin, text):
            raise OSError("offline")
        self.monitor.send = fail
        await self.monitor.remember(account_data(NOW + DAY, NOW + DAY), NOW)
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 2)
        await self.monitor.remember(account_data(NOW + 7 * DAY, NOW + 8 * DAY), NOW + 100)
        self.monitor.send = original
        await self.monitor.tick(NOW + 100)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.monitor.state["weekly"]["codex:secondary"]["at"], NOW + 7 * DAY)

    async def test_missing_timestamps_keep_known_dates_explicit_no_cards_clears(self):
        await self.monitor.remember(account_data(), NOW)
        old = copy.deepcopy(self.monitor.state)
        await self.monitor.remember({"rateLimitResetCredits": {"availableCount": 5}}, NOW + 1)
        self.assertEqual(self.monitor.state["weekly"], old["weekly"])
        self.assertEqual(self.monitor.state["cards"], old["cards"])
        await self.monitor.remember({"rateLimitResetCredits": {"availableCount": 5, "credits": []}}, NOW + 1)
        self.assertEqual(self.monitor.state["cards"], old["cards"])
        await self.monitor.remember({"rateLimitResetCredits": {"availableCount": 0}}, NOW + 2)
        self.assertEqual(self.monitor.state["cards"], {})

    async def test_partial_weekly_response_preserves_other_bucket(self):
        data = account_data()
        data["rateLimitsByLimitId"]["other"] = {"secondary": {"windowDurationMins": 10080, "resetsAt": NOW + 4 * DAY}}
        await self.monitor.remember(data, NOW)
        data["rateLimitsByLimitId"]["other"]["secondary"]["resetsAt"] = None
        data["rateLimitsByLimitId"]["codex"]["secondary"]["resetsAt"] = NOW + 8 * DAY
        await self.monitor.remember(data, NOW + 1)
        self.assertEqual(self.monitor.state["weekly"]["other:secondary"]["at"], NOW + 4 * DAY)
        self.assertEqual(self.monitor.state["weekly"]["codex:secondary"]["at"], NOW + 8 * DAY)

    async def test_multiple_subscriptions_and_delivery_failure_retry(self):
        await self.monitor.subscribe("bad", True)
        await self.monitor.subscribe("good", True)
        original = self.monitor.send
        fail = True
        async def send(origin, text):
            if origin == "bad" and fail:
                raise OSError("offline")
            await original(origin, text)
        self.monitor.send = send
        await self.monitor.remember(account_data(NOW + 2 * DAY, NOW + 2 * DAY), NOW)
        await self.monitor.tick(NOW)
        self.assertEqual(len(self.sent), 2)
        fail = False
        await self.monitor.tick(NOW + 1)
        self.assertEqual(len(self.sent), 4)
        self.assertTrue(self.logs)

    async def test_24h_supersedes_failed_48h_and_off_clears_pending(self):
        await self.monitor.subscribe("qq", True)
        original = self.monitor.send
        async def fail(origin, text):
            raise OSError("offline")
        self.monitor.send = fail
        await self.monitor.remember(account_data(NOW + 2 * DAY, NOW + 2 * DAY), NOW)
        await self.monitor.tick(NOW)
        self.assertEqual(len(self.monitor.state["pending"]["qq"]), 2)
        self.monitor.send = original
        await self.monitor.tick(NOW + DAY)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all("24 小时" in text for _, text in self.sent))
        await self.monitor.subscribe("qq", False)
        await self.monitor.tick(NOW + 2 * DAY)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("qq", self.monitor.state["pending"])

    async def test_account_path_change_keeps_subscription_but_clears_old_times(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(), NOW)
        restored = alarm.AlarmMonitor(self.path, "other-account", self.monitor.send, self.monitor.refresh, self.logs.append)
        try:
            restored._load()
            self.assertTrue(restored.state["subscriptions"]["qq"])
            self.assertEqual(restored.state["weekly"], {})
            self.assertEqual(restored.state["cards"], {})
        finally:
            await restored.close()

    async def test_save_failure_does_not_replace_good_memory(self):
        await self.monitor.remember(account_data(), NOW)
        previous = copy.deepcopy(self.monitor.state)
        with patch.object(self.monitor, "_write", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                await self.monitor.remember(account_data(NOW + 9 * DAY), NOW + 1)
        self.assertEqual(self.monitor.state, previous)

    async def test_corrupt_state_is_preserved(self):
        self.path.write_text('{"weekly":1}', encoding="utf-8")
        original = self.path.read_bytes()
        with self.assertRaises(alarm.AlarmError):
            await self.monitor.start()
        self.assertEqual(self.path.read_bytes(), original)

    async def test_unload_waits_for_active_state_write(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.monitor._write
        def write(data):
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
            original(data)
        with patch.object(self.monitor, "_write", side_effect=write):
            subscription = asyncio.create_task(self.monitor.subscribe("qq", True))
            await entered.wait()
            closing = asyncio.create_task(self.monitor.close())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(closing.done())
            finally:
                release.set()
                await asyncio.gather(subscription, closing)

    async def test_natural_refresh_reads_and_remembers_next_window(self):
        await self.monitor.subscribe("qq", True)
        await self.monitor.remember(account_data(NOW + DAY), NOW)
        async def refresh():
            await self.monitor.remember(account_data(NOW + 8 * DAY), NOW + DAY)
        self.monitor.refresh = refresh
        await self.monitor.tick(NOW + DAY)
        self.assertEqual(self.monitor.state["weekly"]["codex:secondary"]["at"], NOW + 8 * DAY)
        self.assertTrue(any("已经到自然刷新时间" in text for _, text in self.sent))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_disabled_skips_alarm_and_reset_reads_but_keeps_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context(), {"codex_query_enabled": False})
            plugin.alarm_monitor = alarm.AlarmMonitor(Path(tmp) / "alarm.json", "account", None, plugin._refresh_codex_alarm, lambda text: None)
            await plugin.alarm_monitor.remember(account_data(), NOW)
            with patch.dict(Plugin._codex_snapshot.__globals__, {"fetch_codex": unittest.mock.AsyncMock()}) as globals_, patch.object(plugin, "_codex_snapshot") as snapshot:
                event = Event(message="codex-alarm")
                await plugin.alarm_command(event)
                self.assertIn("自动读取暂停", event.sent[0])
                self.assertTrue(plugin.alarm_monitor.state["subscriptions"][event.unified_msg_origin])
                await plugin._refresh_codex_alarm()
                await plugin._on_confirmed_reset("reset-event")
                await plugin.alarm_monitor.tick(NOW + 3 * DAY + 1)
                snapshot.assert_not_awaited()
                globals_["fetch_codex"].assert_not_awaited()
                self.assertEqual(plugin.alarm_monitor.state["weekly"]["codex:secondary"]["at"], NOW + 3 * DAY)
            await plugin.terminate()

    async def test_public_reset_query_continues_with_personal_query_disabled(self):
        plugin = Plugin(Context(), {"codex_query_enabled": False})
        with tempfile.TemporaryDirectory() as tmp:
            monitor = reset.ResetMonitor(Path(tmp) / "state.json", {}, None, lambda text: None)
            plugin.reset_monitor = monitor
            async def get(endpoint):
                if endpoint == "forecast": return forecast()
                if endpoint == "timeline": return reset.parse_timeline(timeline())
                raise reset.ResetError("status unavailable")
            monitor.api.get = unittest.mock.AsyncMock(side_effect=get)
            try:
                event = Event(message="codex-reset")
                await plugin.reset_command(event)
                self.assertEqual({call.args[0] for call in monitor.api.get.await_args_list}, {"forecast", "timeline", "status-history"})
                self.assertIn("Data: codex-reset.com", event.sent[0])
                self.assertNotIn("查询已关闭", event.sent[0])
                self.assertNotIn("Codex 服务", event.sent[0])
            finally:
                await plugin.terminate()

    async def test_slow_old_query_cannot_overwrite_new_reset_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            plugin.alarm_monitor = alarm.AlarmMonitor(Path(tmp) / "alarm.json", "account", None, None, lambda text: None)
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def fetch(*args):
                calls.append(True)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                    return account_data(NOW + DAY)
                return account_data(NOW + 8 * DAY)
            with patch.dict(Plugin._codex_snapshot.__globals__, {"fetch_codex": fetch}):
                old = asyncio.create_task(plugin._codex_snapshot())
                await entered.wait()
                fresh = asyncio.create_task(plugin._refresh_codex_alarm())
                try:
                    await asyncio.sleep(0.01)
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                await asyncio.gather(old, fresh)
                self.assertEqual(plugin.alarm_monitor.state["weekly"]["codex:secondary"]["at"], NOW + 8 * DAY)
            await plugin.terminate()

    async def test_quota_query_records_times_and_refresh_bypasses_cached_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            plugin.alarm_monitor = alarm.AlarmMonitor(Path(tmp) / "alarm.json", "account", None, None, lambda text: None)
            globals_ = Plugin._codex_snapshot.__globals__
            data = account_data()
            async def fetch(*args):
                return copy.deepcopy(data)
            with patch.dict(globals_, {"fetch_codex": fetch}):
                first = await plugin._codex_snapshot()
                self.assertFalse(first.error)
                self.assertEqual(plugin.alarm_monitor.state["weekly"]["codex:secondary"]["at"], NOW + 3 * DAY)
                data = account_data(NOW + 8 * DAY)
                await plugin._refresh_codex_alarm()
                self.assertEqual(plugin.alarm_monitor.state["weekly"]["codex:secondary"]["at"], NOW + 8 * DAY)
            await plugin.terminate()

    async def test_command_group_role_scope_and_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Plugin(Context())
            plugin.alarm_monitor = alarm.AlarmMonitor(Path(tmp) / "alarm.json", "account", None, None, lambda text: None)
            async def refresh():
                await plugin.alarm_monitor.remember(account_data(), NOW)
            plugin._refresh_codex_alarm = refresh
            for role, allowed in [("member", False), ("admin", True), ("owner", True)]:
                event = Event(message="codex-alarm", admin=False)
                event.get_message_type = lambda: "GroupMessage"
                event.get_platform_name = lambda: "aiocqhttp"
                event.message_obj = types.SimpleNamespace(raw_message={"sender": {"role": role}})
                event.unified_msg_origin = f"qq:GroupMessage:{role}"
                await plugin.alarm_command(event)
                self.assertEqual(plugin.alarm_monitor.state["subscriptions"].get(event.unified_msg_origin, False), allowed)
                self.assertTrue(event.stopped)
                self.assertEqual(len(event.sent), 1)
            self.assertFalse(plugin.alarm_monitor.state["subscriptions"].get("unrelated", False))
            event = Event(message="codex-alarm off")
            event.unified_msg_origin = "qq:GroupMessage:admin"
            await plugin.alarm_command(event, "off")
            self.assertFalse(plugin.alarm_monitor.state["subscriptions"][event.unified_msg_origin])
            await plugin.terminate()

    async def test_reset_callback_independent_of_message_toggle_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            fails = True
            async def callback(event_id):
                seen.append(event_id)
                if fails:
                    raise OSError("offline")
            monitor = reset.ResetMonitor(Path(tmp) / "reset.json", {"reset_notification": False}, None, lambda text: None, callback)
            data = timeline(1)
            async def get(endpoint):
                return forecast() if endpoint == "forecast" else reset.confirmed_events(data)
            monitor.api.get = get
            await monitor.poll()
            self.assertEqual(seen, [])  # 历史基线不触发读取。
            data = timeline(2)
            await monitor.poll()
            self.assertEqual(seen, ["2"])
            self.assertEqual(monitor.state["pending_account_refresh_id"], "2")
            fails = False
            await monitor.poll()
            self.assertEqual(seen, ["2", "2"])
            self.assertEqual(monitor.state["pending_account_refresh_id"], "")
            await monitor.poll()
            self.assertEqual(seen, ["2", "2"])
            await monitor.close()


if __name__ == "__main__":
    unittest.main()
