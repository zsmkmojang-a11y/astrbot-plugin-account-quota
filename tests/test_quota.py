import asyncio
import importlib
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_account_quota import quota


class FormatAndIntentTests(unittest.TestCase):
    def test_intents(self):
        for text, expected in [
            ("帮我查一下额度", "all"), ("请帮我查一下余额？", "all"),
            ("查一下 Codex 额度", "codex"), ("DeepSeek 还剩多少钱", "deepseek"),
            ("DeepSeek余额还有多少", "deepseek"), ("查看codex和deepseek的额度", "all"),
            ("额度怎么计算", None), ("不要帮我查一下额度", None),
            ("帮我查一下额度，然后写诗", None), ("他说帮我查一下额度", None),
            ("帮我查一下银行余额", None), ("查额度的插件怎么写", None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(quota.natural_target(text), expected)

    def test_trusted_endpoint(self):
        for url in ["https://api.deepseek.com", "https://api.deepseek.com/v1/", "https://api.deepseek.com:443"]:
            self.assertTrue(quota.is_official_deepseek(url))
        for url in ["http://api.deepseek.com", "https://api.deepseek.com.evil/v1", "https://api.deepseek.com@evil", "https://user@api.deepseek.com", "https://api.deepseek.com:444", "https://api.deepseek.com/v1?key=x", None]:
            self.assertFalse(quota.is_official_deepseek(url))

    def test_multiple_codex_buckets_and_missing_fields(self):
        data = {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 73, "windowDurationMins": 300, "resetsAt": 1800000000}, "secondary": {"usedPercent": 58, "windowDurationMins": 10080}},
            "other": {"primary": {"usedPercent": None, "windowDurationMins": 15}, "credits": {"balance": "0"}},
        }}
        rendered = quota.format_codex(data, now=1799990000)
        for text in ["5 小时", "剩余 27%", "7 天", "剩余 42%", "剩余 未知", "重置：未知", "额外 credits：0"]:
            self.assertIn(text, rendered)
        self.assertNotIn("剩余 100%", rendered)

    def test_codex_legacy_and_invalid_number(self):
        self.assertIn("剩余 0%", quota.format_codex({"rateLimits": {"primary": {"usedPercent": 105}}}))
        self.assertIn("未知", quota.format_codex({"rateLimits": {"primary": {"usedPercent": True, "resetsAt": float("inf")}}}))
        with self.assertRaises(quota.QueryError):
            quota.format_codex({"rateLimits": None})

    def test_deepseek_balance(self):
        rendered = quota.format_deepseek({"is_available": False, "balance_infos": [{"currency": "CNY", "total_balance": "0.001", "granted_balance": "0", "topped_up_balance": "0.001"}]})
        self.assertIn("可供 API 调用：否", rendered)
        self.assertIn("0.001 CNY", rendered)
        for data in [{}, {"balance_infos": [None]}, {"balance_infos": [{"currency": "CNY", "total_balance": "NaN"}]}]:
            with self.assertRaises(quota.QueryError):
                quota.format_deepseek(data)

    def test_deepseek_low_balance_boundary_and_currency(self):
        for currency, amount, expected in [("CNY", "9.999", True), ("CNY", "0", True), ("CNY", "10", False), ("CNY", "10.01", False), ("USD", "1", False)]:
            with self.subTest(currency=currency, amount=amount):
                data = {"balance_infos": [{"currency": currency, "total_balance": amount, "granted_balance": "0", "topped_up_balance": amount}]}
                self.assertEqual(quota.DEEPSEEK_LOW_BALANCE in quota.format_deepseek(data), expected)

    def test_deepseek_usd_is_opt_in_without_currency_conversion(self):
        cny = {"currency": "CNY", "total_balance": "9", "granted_balance": "0", "topped_up_balance": "9"}
        usd = {"currency": "USD", "total_balance": "1.234", "granted_balance": "0.234", "topped_up_balance": "1"}
        data = {"balance_infos": [cny, usd]}
        default = quota.format_deepseek(data)
        self.assertIn("9 CNY", default)
        self.assertNotIn("USD", default)
        enabled = quota.format_deepseek(data, show_usd=True)
        self.assertIn("9 CNY", enabled)
        self.assertIn("1.234 USD", enabled)
        self.assertIn(quota.DEEPSEEK_LOW_BALANCE, enabled)
        self.assertNotIn("USD", quota.format_deepseek({"balance_infos": [cny]}, show_usd=True))
        only_usd = quota.format_deepseek({"balance_infos": [usd]})
        self.assertIn("已按配置隐藏", only_usd)
        self.assertNotIn("1.234", only_usd)
        self.assertNotIn(quota.DEEPSEEK_LOW_BALANCE, only_usd)

    def test_weekly_remaining_and_reset_boundaries(self):
        now = 1800000000
        for minutes, used, remaining_seconds, expected in [
            (10080, 49, 172799, True), (10080, 50, 172799, False),
            (10080, 70, 172799, False), (10080, 49, 172800, False),
            (10080, 49, 0, False), (10080, 49, -1, False),
            (300, 10, 3600, False), (10080, None, 3600, False),
        ]:
            with self.subTest(minutes=minutes, used=used, seconds=remaining_seconds):
                window = {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": now + remaining_seconds}
                result = quota.format_codex({"rateLimitsByLimitId": {"one": {"secondary": window}, "two": {"secondary": window}}}, now=now)
                self.assertEqual(quota.CODEX_WEEKLY_RESET in result, expected)
                self.assertLessEqual(result.count(quota.CODEX_WEEKLY_RESET), 1)

    def test_reset_card_expiry_boundaries_and_status(self):
        now = 1800000000
        for seconds, status, expected in [(172799, "available", True), (172800, "available", False), (0, "available", False), (-1, "available", False), (3600, "redeemed", False), (None, "available", False)]:
            with self.subTest(seconds=seconds, status=status):
                card = {"status": status, "expiresAt": None if seconds is None else now + seconds}
                data = {"rateLimits": {"primary": {}}, "rateLimitResetCredits": {"availableCount": 2, "credits": [card, card]}}
                result = quota.format_codex(data, now=now)
                self.assertIn("可用重置卡：2 张", result)
                self.assertEqual(quota.CODEX_CARD_EXPIRY in result, expected)
                self.assertLessEqual(result.count(quota.CODEX_CARD_EXPIRY), 1)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_merges_concurrency_and_expires(self):
        cache = quota.QueryCache()
        count = 0
        async def load():
            nonlocal count
            count += 1
            await asyncio.sleep(0.02)
            return "result"
        values = await asyncio.gather(*[cache.get("same", load, 0) for _ in range(10)])
        self.assertEqual(count, 1)
        self.assertTrue(all(value.text == "result" for value in values))
        await cache.get("same", load, 10)
        await cache.get("same", load, 10)
        self.assertEqual(count, 2)
        await cache.get("other-account", load, 10)
        self.assertEqual(count, 3)
        await cache.close()

    async def test_cache_cancel_one_waiter_and_unload(self):
        cache = quota.QueryCache()
        started = asyncio.Event()
        cleaned = asyncio.Event()
        async def load():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.set()
        waiter = asyncio.create_task(cache.get("key", load, 30))
        await started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(cleaned.is_set())
        await cache.close()
        self.assertTrue(cleaned.is_set())

    async def test_cache_errors_and_size(self):
        cache = quota.QueryCache()
        async def fail():
            raise quota.QueryError("safe failure")
        result = await cache.get("failed", fail, 30)
        self.assertTrue(result.error)
        self.assertEqual(result.text, "safe failure")
        async def ok():
            return "ok"
        for i in range(40):
            await cache.get(str(i), ok, 30)
        self.assertEqual(len(cache._cache), 32)
        await cache.close()

    async def test_cached_report_uses_new_reply_text(self):
        cache = quota.QueryCache()
        count = 0
        async def load():
            nonlocal count
            count += 1
            return quota.deepseek_report({"balance_infos": [{"currency": "CNY", "total_balance": "1", "granted_balance": "0", "topped_up_balance": "1"}]})
        snapshot = await cache.get("account", load, 30)
        self.assertEqual(snapshot.reminders, ("deepseek_low_balance",))
        self.assertIn("第一次提示", quota.render_snapshot("DeepSeek", snapshot, {"deepseek_low_balance": "第一次提示"}))
        cached = await cache.get("account", load, 30)
        text = quota.render_snapshot("DeepSeek", cached, {"deepseek_low_balance": "新的提示"})
        self.assertIn("新的提示", text)
        self.assertNotIn("第一次提示", text)
        self.assertNotIn(quota.DEEPSEEK_LOW_BALANCE, text)
        self.assertEqual(count, 1)
        await cache.close()

    async def _codex(self, mode, timeout=3):
        original = asyncio.create_subprocess_exec
        processes = []
        async def spawn(*args, **kwargs):
            self.assertEqual(args[1:], ("app-server", "--listen", "stdio://"))
            proc = await original(sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode, **kwargs)
            processes.append(proc)
            return proc
        with patch.object(quota, "resolve_codex", return_value="fake"), patch.object(quota.asyncio, "create_subprocess_exec", side_effect=spawn):
            try:
                return await quota.fetch_codex("codex", "", timeout)
            finally:
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].returncode)

    async def test_codex_real_pipe_handshake_and_stderr(self):
        result = await self._codex("ok")
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 25)

    async def test_codex_failure_and_timeout_cleanup(self):
        for mode in ["error", "invalid", "eof", "hang"]:
            with self.subTest(mode=mode), self.assertRaises(quota.QueryError) as caught:
                await self._codex(mode, 0.1 if mode == "hang" else 3)
            self.assertNotIn("SECRET_TOKEN", str(caught.exception))

    async def test_codex_cancellation_cleanup(self):
        task = asyncio.create_task(self._codex("hang", 60))
        await asyncio.sleep(0.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_unload_during_codex_timeout_cleanup(self):
        original = asyncio.create_subprocess_exec
        cleanup_entered = asyncio.Event()
        processes = []
        async def spawn(*args, **kwargs):
            proc = await original(sys.executable, str(Path(__file__).with_name("fake_codex.py")), "hang", **kwargs)
            processes.append(proc)
            original_wait = proc.wait
            async def wait():
                cleanup_entered.set()
                return await original_wait()
            proc.wait = wait
            return proc
        cache = quota.QueryCache()
        async def load():
            return str(await quota.fetch_codex("codex", "", 0.1))
        with patch.object(quota, "resolve_codex", return_value="fake"), patch.object(quota.asyncio, "create_subprocess_exec", side_effect=spawn):
            waiter = asyncio.create_task(cache.get("codex", load, 30))
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 3)
                await cache.close()
                self.assertIsNotNone(processes[0].returncode)
            finally:
                if processes and processes[0].returncode is None:
                    processes[0].kill()
                    await processes[0].wait()
                await asyncio.gather(waiter, return_exceptions=True)

    async def test_deepseek_real_http(self):
        from aiohttp import web
        received = []
        async def handler(request):
            received.append(request.headers.get("Authorization"))
            mode = request.match_info["mode"]
            if mode == "ok":
                return web.json_response({"balance_infos": [{"currency": "USD", "total_balance": "1.25", "granted_balance": "0", "topped_up_balance": "1.25"}], "is_available": True})
            if mode == "redirect":
                raise web.HTTPFound("/ok")
            if mode == "invalid":
                return web.Response(text="private invalid reply")
            if mode == "large":
                return web.Response(body=b"x" * (quota.MAX_RESPONSE + 1))
            if mode == "slow":
                await asyncio.sleep(0.1)
                return web.json_response({})
            return web.Response(status=int(mode), text="SECRET_TOKEN")
        app = web.Application()
        app.router.add_get("/{mode}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        try:
            for mode in ["ok", "401", "429", "500", "redirect", "invalid", "large", "slow"]:
                with patch.object(quota, "DEEPSEEK_BALANCE_URL", f"http://127.0.0.1:{port}/{mode}"):
                    if mode == "ok":
                        result = await quota.fetch_deepseek("test-key", 2)
                        self.assertTrue(result["is_available"])
                    else:
                        with self.assertRaises(quota.QueryError) as caught:
                            await quota.fetch_deepseek("test-key", 0.02 if mode == "slow" else 2)
                        self.assertNotIn("SECRET_TOKEN", str(caught.exception))
            self.assertTrue(all(value == "Bearer test-key" for value in received))
            self.assertEqual(len(received), 8)  # 重定向未被跟随。
        finally:
            await runner.cleanup()


# 仅替换 AstrBot 接口；通信测试使用真实管道和本地 HTTP 服务。
def load_plugin():
    modules = {}
    for name in ["astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star", "astrbot.api.message_components"]:
        modules[name] = types.ModuleType(name)
    def decorator(*args, **kwargs):
        return lambda function: function
    def attach_filter(predicate):
        def decorate(function):
            function._test_filters = getattr(function, "_test_filters", []) + [predicate]
            return function
        return decorate
    def regex(pattern, **kwargs):
        return attach_filter(lambda event: bool(re.search(pattern, event.get_message_str().strip())))
    class CustomFilter:
        def __init__(self, raise_error=True):
            self.raise_error = raise_error
    def custom_filter(filter_type, **kwargs):
        instance = filter_type()
        return attach_filter(lambda event: instance.filter(event, {}))
    modules["astrbot.api.event"].filter = types.SimpleNamespace(command=decorator, regex=regex, custom_filter=custom_filter, CustomFilter=CustomFilter)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api"].logger = types.SimpleNamespace(warning=lambda text: None)
    class Plain:
        def __init__(self, text):
            self.text = text
    class Image:
        def __init__(self, file):
            self.file = file
        @staticmethod
        def fromURL(url):
            return Image(url)
        @staticmethod
        def fromFileSystem(path):
            return Image(str(Path(path).resolve()))
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].Image = Image
    class Star:
        def __init__(self, context):
            self.context = context
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_account_quota.main").AccountQuotaPlugin


Plugin = load_plugin()


class Event:
    def __init__(self, message="帮我查一下额度", wake=True, admin=True):
        self.message = message
        self.is_at_or_wake_command = wake
        self.admin = admin
        self.unified_msg_origin = "test:group:1"
        self.stopped = False
        self.sent = []
        self.extra = {}
    def is_admin(self): return self.admin
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def get_extra(self, name, default=None): return self.extra.get(name, default)
    def set_extra(self, name, value): self.extra[name] = value
    def get_message_str(self): return self.message
    def plain_result(self, text): return text
    def chain_result(self, components): return components
    async def send(self, text): self.sent.append(text)


class Provider:
    def __init__(self, base="https://api.deepseek.com/v1", key="test-key"):
        self.provider_config = {"api_base": base}
        self.key = key
    def get_current_key(self): return self.key
    def get_keys(self): return [self.key]


class Context:
    def __init__(self, current=None, providers=None):
        self.current = current
        self.providers = providers or []
    async def get_using_provider_async(self, umo): return self.current
    def get_all_providers(self): return self.providers
    def get_provider_by_id(self, provider_id): return self.current if provider_id == "selected" else None


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_usd_toggle_does_not_reuse_wrong_display_cache(self):
        plugin = Plugin(Context())
        data = {"balance_infos": [{"currency": "CNY", "total_balance": "20", "granted_balance": "0", "topped_up_balance": "20"}, {"currency": "USD", "total_balance": "2.5", "granted_balance": "0", "topped_up_balance": "2.5"}]}
        module_globals = Plugin._deepseek_snapshot.__globals__
        calls = []
        async def fetch(key, timeout):
            calls.append(key)
            return data
        with patch.object(plugin, "_deepseek_key", return_value="test-key"), patch.dict(module_globals, {"fetch_deepseek": fetch}):
            default = await plugin._deepseek_snapshot(Event())
            self.assertNotIn("USD", default.text)
            plugin.config["deepseek_show_usd"] = True
            enabled = await plugin._deepseek_snapshot(Event())
            self.assertIn("2.5 USD", enabled.text)
            plugin.config["deepseek_show_usd"] = False
            hidden = await plugin._deepseek_snapshot(Event())
            self.assertNotIn("USD", hidden.text)
            self.assertEqual(len(calls), 2)
        await plugin.terminate()

    @staticmethod
    def low_balance_snapshot():
        report = quota.deepseek_report({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "1.50", "granted_balance": "0", "topped_up_balance": "1.50"}]})
        return quota.Snapshot(report.text, time.time(), reminders=report.reminders)

    async def test_custom_low_balance_text_and_default_image(self):
        plugin = Plugin(Context(), {"deepseek_low_balance_reply": "余额告急，请充值！"})
        event = Event()
        with patch.object(plugin, "_deepseek_snapshot", return_value=self.low_balance_snapshot()):
            await plugin.deepseek_command(event)
            await plugin.deepseek_command(event)
        self.assertEqual(len(event.sent), 1)
        chain = event.sent[0]
        self.assertEqual(len(chain), 2)
        self.assertIn("余额告急，请充值！", chain[0].text)
        self.assertIn("可用余额：1.50 CNY", chain[0].text)
        self.assertNotIn(quota.DEEPSEEK_LOW_BALANCE, chain[0].text)
        image = Path(chain[1].file)
        self.assertTrue(image.is_absolute())
        self.assertEqual(image.name, "default_reminder.png")
        self.assertTrue(image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(event.stopped)

    async def test_codex_custom_texts_share_one_image(self):
        now = time.time()
        report = quota.codex_report({"rateLimits": {"secondary": {"windowDurationMins": 10080, "usedPercent": 20, "resetsAt": now + 3600}}, "rateLimitResetCredits": {"availableCount": 1, "credits": [{"status": "available", "expiresAt": now + 3600}]}}, now=now)
        snapshot = quota.Snapshot(report.text, now, reminders=report.reminders)
        plugin = Plugin(Context(), {"codex_weekly_reset_reply": "周额度快用呀", "codex_card_expiry_reply": "卡片快用了呀"})
        event = Event()
        with patch.object(plugin, "_codex_snapshot", return_value=snapshot):
            await plugin.codex_command(event)
        self.assertEqual(len(event.sent[0]), 2)
        text = event.sent[0][0].text
        self.assertIn("周额度快用呀", text)
        self.assertIn("卡片快用了呀", text)
        self.assertNotIn(quota.CODEX_WEEKLY_RESET, text)
        self.assertNotIn(quota.CODEX_CARD_EXPIRY, text)

    async def test_disabled_image_and_empty_reply(self):
        plugin = Plugin(Context(), {"reminder_image_enabled": False})
        event = Event()
        with patch.object(plugin, "_deepseek_snapshot", return_value=self.low_balance_snapshot()):
            await plugin.deepseek_command(event)
        self.assertIsInstance(event.sent[0], str)
        self.assertIn(quota.DEEPSEEK_LOW_BALANCE, event.sent[0])
        plugin.config = {"deepseek_low_balance_reply": ""}
        event = Event()
        with patch.object(plugin, "_deepseek_snapshot", return_value=self.low_balance_snapshot()):
            await plugin.deepseek_command(event)
        self.assertNotIn(quota.DEEPSEEK_LOW_BALANCE, event.sent[0][0].text)
        self.assertEqual(len(event.sent[0]), 2)

    async def test_custom_local_relative_and_url_images(self):
        plugin = Plugin(Context())
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "my picture.png"
            custom.write_bytes(Path(plugin._reminder_image().file).read_bytes())
            plugin.config["reminder_image_path"] = str(custom)
            self.assertEqual(plugin._reminder_image().file, str(custom.resolve()))
        plugin.config["reminder_image_path"] = "assets/default_reminder.png"
        self.assertTrue(Path(plugin._reminder_image().file).is_file())
        plugin.config["reminder_image_path"] = "https://example.com/custom.png"
        self.assertEqual(plugin._reminder_image().file, "https://example.com/custom.png")

    async def test_missing_image_preserves_text(self):
        plugin = Plugin(Context(), {"reminder_image_path": "assets/missing-file.png"})
        event = Event()
        with patch.object(plugin, "_deepseek_snapshot", return_value=self.low_balance_snapshot()):
            await plugin.deepseek_command(event)
        self.assertIsInstance(event.sent[0], str)
        self.assertIn(quota.DEEPSEEK_LOW_BALANCE, event.sent[0])
        self.assertTrue(event.stopped)

    async def test_no_image_on_normal_error_and_denial(self):
        plugin = Plugin(Context())
        for snapshot in [quota.Snapshot("正常余额：100 CNY", time.time()), quota.Snapshot(quota.DEEPSEEK_LOW_BALANCE, time.time(), True)]:
            event = Event()
            with patch.object(plugin, "_deepseek_snapshot", return_value=snapshot):
                await plugin.deepseek_command(event)
            self.assertIsInstance(event.sent[0], str)
        event = Event(admin=False)
        await plugin.deepseek_command(event)
        self.assertIsInstance(event.sent[0], str)
        self.assertIn("仅 AstrBot 管理员", event.sent[0])

    async def test_registration_does_not_wake_sleeping_message(self):
        # 官方 WakingCheckStage 会将通过全部注册过滤器的消息标记 is_wake。
        # 必须在过滤阶段阻止，而不是在 handler 里面返回。
        for wake in [False, True]:
            event = Event(wake=wake)
            event.is_wake = wake
            predicates = Plugin.natural_query._test_filters
            if all(predicate(event) for predicate in predicates):
                event.is_wake = True
            self.assertEqual(event.is_wake, wake)

    async def test_wake_permission_stop_and_no_duplicate(self):
        plugin = Plugin(Context())
        calls = []
        async def answer(event, target):
            calls.append(target)
            return "result"
        with patch.object(plugin, "_answer", side_effect=answer):
            sleeping = Event(wake=False)
            await plugin.natural_query(sleeping)
            self.assertEqual(sleeping.sent, [])
            event = Event()
            await plugin.natural_query(event)
            await plugin.quota_command(event)
            self.assertEqual(event.sent, ["result"])
            self.assertTrue(event.stopped)
            self.assertEqual(calls, ["all"])
        unauthorized = Event(admin=False)
        await plugin.quota_command(unauthorized)
        self.assertIn("仅 AstrBot 管理员", unauthorized.sent[0])
        self.assertTrue(unauthorized.stopped)
        await plugin.terminate()

    async def test_disable_natural_and_explanatory_questions(self):
        plugin = Plugin(Context(), {"natural_language_enabled": False})
        event = Event()
        await plugin.natural_query(event)
        self.assertFalse(event.stopped)
        plugin.config["natural_language_enabled"] = True
        event = Event("额度怎么计算")
        await plugin.natural_query(event)
        self.assertEqual(event.sent, [])
        self.assertFalse(event.stopped)

    async def test_select_credentials(self):
        official = Provider()
        other = Provider("https://relay.example/v1", "other-key")
        plugin = Plugin(Context(other, [official, other]))
        self.assertEqual(await plugin._deepseek_key(Event()), "test-key")
        plugin.context.providers.append(Provider(key="second-key"))
        with self.assertRaises(quota.QueryError):
            await plugin._deepseek_key(Event())
        plugin.config["deepseek_provider_id"] = "selected"
        with self.assertRaises(quota.QueryError):
            await plugin._deepseek_key(Event())
        plugin.context.current = official
        self.assertEqual(await plugin._deepseek_key(Event()), "test-key")

    async def test_independent_failure_and_concurrent_sources(self):
        plugin = Plugin(Context())
        started = set()
        async def codex():
            started.add("codex")
            await asyncio.sleep(0.01)
            self.assertEqual(started, {"codex", "deepseek"})
            raise quota.QueryError("safe failure")
        async def deepseek(event):
            started.add("deepseek")
            await asyncio.sleep(0.01)
            return quota.Snapshot("DeepSeek\n可用余额：1 CNY", time.time())
        with patch.object(plugin, "_codex_snapshot", side_effect=codex), patch.object(plugin, "_deepseek_snapshot", side_effect=deepseek):
            result = await plugin._answer(Event(), "all")
        self.assertIn("Codex\n查询失败", result)
        self.assertIn("可用余额：1 CNY", result)

    async def test_no_query_for_nonadmin(self):
        plugin = Plugin(Context())
        with patch.object(plugin, "_codex_snapshot", side_effect=AssertionError("must not run")), patch.object(plugin, "_deepseek_snapshot", side_effect=AssertionError("must not run")):
            result = await plugin._answer(Event(admin=False), "all")
        self.assertIn("仅 AstrBot 管理员", result)

    async def test_no_new_http_or_reply_after_unload(self):
        plugin = Plugin(Context())
        selecting = asyncio.Event()
        release = asyncio.Event()
        requests = []
        async def select_key(event):
            selecting.set()
            await release.wait()
            return "test-key"
        async def fetch(key, timeout):
            requests.append(key)
            return {}
        module_globals = Plugin._deepseek_snapshot.__globals__
        event = Event()
        with patch.object(plugin, "_deepseek_key", side_effect=select_key), patch.dict(module_globals, {"fetch_deepseek": fetch}):
            response = asyncio.create_task(plugin.deepseek_command(event))
            await selecting.wait()
            await plugin.terminate()
            release.set()
            await response
        self.assertEqual(requests, [])
        self.assertEqual(event.sent, [])
        self.assertTrue(event.stopped)
        self.assertEqual(len(plugin.cache._cache), 0)


if __name__ == "__main__":
    unittest.main()
