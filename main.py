"""AstrBot 的 Codex 额度与 DeepSeek 官方余额查询插件。"""

import asyncio
import inspect
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from .quota import (
    NATURAL_PATTERN,
    DEFAULT_REMINDER_TEXTS,
    QueryCache,
    QueryError,
    Snapshot,
    credential_fingerprint,
    codex_report,
    deepseek_report,
    fetch_codex,
    fetch_deepseek,
    is_official_deepseek,
    natural_target,
    render_snapshot,
)


async def _maybe_await(value):
    return await value if inspect.isawaitable(value) else value


class AlreadyAwakeFilter(filter.CustomFilter):
    """在框架唤醒阶段过滤，避免 regex 本身把普通群消息唤醒。"""

    def filter(self, event, cfg):
        return bool(event.is_at_or_wake_command)


class AccountQuotaPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if hasattr(config, "get") else {}
        self.cache = QueryCache()
        self._closing = False

    def _text(self, name, default=""):
        value = self.config.get(name, default)
        return value.strip() if isinstance(value, str) else default

    def _seconds(self, name, default, minimum, maximum):
        value = self.config.get(name, default)
        if isinstance(value, bool):
            return default
        try:
            return min(max(int(value), minimum), maximum)
        except (ValueError, TypeError, OverflowError):
            return default

    async def _deepseek_key(self, event):
        """只从明确的 DeepSeek 官方提供商读取凭据，不按模型名猜测。"""
        provider_id = self._text("deepseek_provider_id")
        if provider_id:
            provider = await _maybe_await(self.context.get_provider_by_id(provider_id))
            if provider is None:
                raise QueryError("找不到配置的 DeepSeek 提供商，请检查 deepseek_provider_id。")
        else:
            getter = getattr(self.context, "get_using_provider_async", None)
            if getter is None:
                getter = self.context.get_using_provider
            provider = await _maybe_await(getter(event.unified_msg_origin))
            if not self._is_deepseek(provider):
                candidates = [p for p in await _maybe_await(self.context.get_all_providers()) if self._is_deepseek(p)]
                if len(candidates) != 1:
                    raise QueryError("请在插件配置中指定 DeepSeek 官方提供商 ID（deepseek_provider_id）。")
                provider = candidates[0]
        if not self._is_deepseek(provider):
            raise QueryError("所选提供商不是 DeepSeek 官方 API，请检查其 api_base。")
        current_key = getattr(provider, "get_current_key", None)
        key = await _maybe_await(current_key()) if current_key else None
        if not isinstance(key, str) or not key.strip():
            keys = await _maybe_await(provider.get_keys())
            valid_keys = [k.strip() for k in keys if isinstance(k, str) and k.strip()] if isinstance(keys, list) else []
            if len(set(valid_keys)) != 1:
                raise QueryError("无法确定 DeepSeek API Key，请为所选提供商配置一个有效 Key。")
            key = valid_keys[0]
        return key.strip()

    @staticmethod
    def _is_deepseek(provider):
        config = getattr(provider, "provider_config", None)
        return isinstance(config, dict) and is_official_deepseek(config.get("api_base", ""))

    async def _codex_snapshot(self):
        command = self._text("codex_path", "codex")
        home = self._text("codex_home")
        timeout = self._seconds("query_timeout_seconds", 20, 3, 120)

        async def load():
            return codex_report(await fetch_codex(command, home, timeout))

        return await self.cache.get("codex:" + credential_fingerprint(command + "\0" + home), load, self._seconds("cache_seconds", 30, 0, 300))

    async def _deepseek_snapshot(self, event):
        timeout = self._seconds("query_timeout_seconds", 20, 3, 120)
        show_usd = self.config.get("deepseek_show_usd", False) is True
        try:
            async with asyncio.timeout(timeout):
                key = await self._deepseek_key(event)
        except QueryError as exc:
            return Snapshot(str(exc), time.time(), True)
        except Exception:
            return Snapshot("无法读取 DeepSeek 提供商配置，请检查提供商 ID 与 AstrBot 版本。", time.time(), True)

        async def load():
            return deepseek_report(await fetch_deepseek(key, timeout), show_usd)

        cache_key = "deepseek:" + credential_fingerprint(key) + (":usd" if show_usd else ":cny")
        return await self.cache.get(cache_key, load, self._seconds("cache_seconds", 30, 0, 300))

    async def _answer(self, event, target):
        event.set_extra("astrbot_plugin_account_quota.reminder", False)
        if self.config.get("admin_only", True) and not event.is_admin():
            return "仅 AstrBot 管理员可以查询账户额度和余额。"
        names, jobs = [], []
        if target in ("all", "codex"):
            names.append("Codex")
            jobs.append(self._codex_snapshot())
        if target in ("all", "deepseek"):
            names.append("DeepSeek")
            jobs.append(self._deepseek_snapshot(event))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        rendered = []
        reminder_texts = {
            name: self._text(name + "_reply", default)
            for name, default in DEFAULT_REMINDER_TEXTS.items()
        }
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                rendered.append(f"{name}\n查询失败，请检查插件配置及运行环境。")
            else:
                rendered.append(render_snapshot(name, result, reminder_texts))
                if not result.error and result.reminders:
                    event.set_extra("astrbot_plugin_account_quota.reminder", True)
        return "\n\n".join(rendered)

    def _reminder_image(self):
        if not self.config.get("reminder_image_enabled", True):
            return None
        configured = self._text("reminder_image_path")
        try:
            if configured.startswith(("http://", "https://")):
                return Image.fromURL(configured)
            path = Path(configured).expanduser() if configured else Path("assets/default_reminder.png")
            if not path.is_absolute():
                path = Path(__file__).resolve().parent / path
            if not path.is_file():
                logger.warning("账户额度查询：提醒图片不存在，已改为仅发送文字；请检查 reminder_image_path。")
                return None
            return Image.fromFileSystem(str(path.resolve()))
        except (OSError, ValueError):
            logger.warning("账户额度查询：提醒图片配置无效，已改为仅发送文字。")
            return None

    async def _respond(self, event, target):
        if self._closing or event.is_stopped() or event.get_extra("astrbot_plugin_account_quota.handled", False):
            return
        event.set_extra("astrbot_plugin_account_quota.handled", True)
        # 直接发送，再停止事件；查询成功或失败均不再调用 LLM。
        try:
            answer = await self._answer(event, target)
            if not self._closing:
                image = (
                    self._reminder_image()
                    if event.get_extra("astrbot_plugin_account_quota.reminder", False)
                    else None
                )
                result = event.chain_result([Plain(answer), image]) if image is not None else event.plain_result(answer)
                await event.send(result)
        finally:
            event.stop_event()

    @filter.command("额度", priority=100)
    async def quota_command(self, event: AstrMessageEvent):
        """查询 Codex 额度和 DeepSeek 余额。"""
        await self._respond(event, "all")

    @filter.command("codex额度", priority=100)
    async def codex_command(self, event: AstrMessageEvent):
        """查询本机 Codex 登录账号的周期额度。"""
        await self._respond(event, "codex")

    @filter.command("deepseek余额", priority=100)
    async def deepseek_command(self, event: AstrMessageEvent):
        """查询 DeepSeek 官方账号余额。"""
        await self._respond(event, "deepseek")

    @filter.regex(NATURAL_PATTERN, priority=100)
    @filter.custom_filter(AlreadyAwakeFilter, priority=100)
    async def natural_query(self, event: AstrMessageEvent):
        if not self.config.get("natural_language_enabled", True):
            return
        if not event.is_at_or_wake_command:
            return
        target = natural_target(event.get_message_str())
        if target is not None:
            await self._respond(event, target)

    async def terminate(self):
        self._closing = True
        await self.cache.close()
