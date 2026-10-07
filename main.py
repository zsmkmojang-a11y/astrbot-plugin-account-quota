"""AstrBot 的 Codex 额度与 DeepSeek 官方余额查询插件。"""

import asyncio
import inspect
import time
from collections.abc import Mapping
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .reset_monitor import ResetError, ResetMonitor, bounded_int, credited
from .alarm_monitor import AlarmError, AlarmMonitor

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
        self._codex_read_lock = asyncio.Lock()
        self._closing = False
        self.reset_monitor = None
        self.alarm_monitor = None
        self._reset_error = "Reset 监控尚未初始化，请稍后重试。"
        self._alarm_error = "额度刷新提醒还没初始化好，请稍后再试喵。"

    async def initialize(self):
        """框架加载后恢复订阅并启动独立的公共 Reset 监控。"""
        try:
            path = StarTools.get_data_dir("astrbot_plugin_account_quota") / "codex_alarm_state.json"
            account_key = credential_fingerprint(self._text("codex_path", "codex") + "\0" + self._text("codex_home"))
            alarm = AlarmMonitor(path, account_key, self._send_alarm, self._refresh_codex_alarm, logger.warning, self.config)
            await alarm.start()
            self.alarm_monitor = alarm
        except (AlarmError, OSError, RuntimeError, ValueError) as exc:
            self._alarm_error = str(exc) if isinstance(exc, AlarmError) else "额度刷新提醒初始化失败，请检查插件数据目录权限喵。"
            logger.warning(f"账户额度查询：{self._alarm_error}")
        try:
            await self._migrate_poll_config()
            path = StarTools.get_data_dir("astrbot_plugin_account_quota") / "reset_state.json"
            monitor = ResetMonitor(path, self.config, self._send_reset, logger.warning, self._on_confirmed_reset)
            await monitor.start()
            self.reset_monitor = monitor
        except (ResetError, OSError, RuntimeError, ValueError) as exc:
            self._reset_error = str(exc) if isinstance(exc, ResetError) else "Reset 监控初始化失败，请检查插件数据目录权限。"
            logger.warning(f"账户额度查询：{self._reset_error}")

    async def _migrate_poll_config(self):
        """保留旧 schema 字段直到框架加载后，再迁移轮询和冷却秒数。"""
        updates = {}
        seconds = bounded_int(self.config, "poll_interval", 0, 0, 86400)
        if seconds > 0:
            minutes = bounded_int(self.config, "poll_interval_minutes", 60, 1, 1440)
            # 升级时框架会注入默认 60；明确设置的其他分钟数优先。
            if minutes == 60:
                minutes = max(1, (seconds + 59) // 60)
            updates.update(poll_interval_minutes=minutes, poll_interval=0)
        seconds = bounded_int(self.config, "probability_cooldown", -1, -1, 604800)
        if seconds >= 0:
            minutes = bounded_int(self.config, "probability_cooldown_minutes", 360, 0, 10080)
            if minutes == 360:
                minutes = (seconds + 59) // 60
            # 旧值 0 表示不冷却，使用 -1 区分已迁移和有效的旧零值。
            updates.update(probability_cooldown_minutes=minutes, probability_cooldown=-1)
        if not updates:
            return
        save_async = getattr(self.config, "save_config_async", None)
        save_sync = getattr(self.config, "save_config", None)
        if callable(save_async):
            await _maybe_await(save_async(updates))
        elif callable(save_sync):
            await asyncio.to_thread(save_sync, updates)
        else:
            self.config.update(updates)

    async def _send_reset(self, origin, text):
        if self._closing:
            raise ResetError("插件正在停止。")
        sent = await self.context.send_message(origin, MessageChain().message(text))
        if sent is False:
            raise ResetError("目标消息平台不可用。")

    async def _send_alarm(self, origin, text):
        if self._closing:
            raise AlarmError("插件正在停止喵。")
        chain = MessageChain().message(text)
        image = self._reminder_image()
        if image is not None:
            chain.chain.append(image)
        if await self.context.send_message(origin, chain) is False:
            raise AlarmError("目标消息平台暂时不可用喵。")

    async def _refresh_codex_alarm(self):
        if not self._query_enabled("codex"):
            return
        # 独立缓存键避开普通查询的旧快照；仍合并并发且由 QueryCache 负责卸载取消。
        snapshot = await self._codex_snapshot(refresh=True)
        if snapshot.error:
            raise AlarmError(snapshot.text)

    async def _on_confirmed_reset(self, event_id):
        if not self._query_enabled("codex"):
            return
        if self.alarm_monitor is None:
            raise AlarmError(self._alarm_error)
        if not self._closing:
            await self._refresh_codex_alarm()

    @filter.command("codex-alarm", priority=100)
    async def alarm_command(self, event: AstrMessageEvent, action: str = "", extra: str = ""):
        """订阅当前会话的周自然刷新与重置卡到期提醒。"""
        if self._closing or event.is_stopped() or event.get_extra("astrbot_plugin_account_quota.handled", False):
            return
        event.set_extra("astrbot_plugin_account_quota.handled", True)
        try:
            tokens = event.get_message_str().split("codex-alarm", 1)[-1].split()
            if extra or len(tokens) > 1 or action not in ("", "on", "off", "status"):
                answer = "额度刷新提醒用法喵\n· 开启：/codex-alarm\n· 关闭：/codex-alarm off\n· 查看：/codex-alarm status"
            elif self.alarm_monitor is None:
                answer = self._alarm_error
            elif not self._can_manage_reset_watch(event):
                answer = "额度刷新订阅及记录仅限本群管理员、群主或 AstrBot 管理员查看和修改；私聊可自行订阅喵。"
            elif action == "status":
                answer = self.alarm_monitor.status(event.unified_msg_origin)
            else:
                enabled = action != "off"
                await self.alarm_monitor.subscribe(event.unified_msg_origin, enabled)
                answer = "已" + ("开启" if enabled else "关闭") + "本会话的额度刷新提醒喵。"
                if enabled and not self._query_enabled("codex"):
                    answer += "\nCodex 额度查询已关闭，自动读取暂停，提醒会依据已有日期记录喵。"
                elif enabled:
                    try:
                        await self._refresh_codex_alarm()
                    except (AlarmError, QueryError):
                        self.alarm_monitor.refresh_requested = True
                        answer += "\n订阅已保存，不过暂时没能读取最新额度，会保留已有时间并稍后重试喵。"
                answer += "\n" + self.alarm_monitor.status(event.unified_msg_origin)
            if not self._closing:
                await event.send(event.plain_result(answer))
        except AlarmError as exc:
            if not self._closing:
                await event.send(event.plain_result(str(exc)))
        except Exception as exc:
            logger.warning(f"Codex 额度刷新订阅失败：{type(exc).__name__}")
            if not self._closing:
                await event.send(event.plain_result("额度刷新订阅操作失败，请稍后再试喵。"))
        finally:
            event.stop_event()

    @staticmethod
    def _can_manage_reset_watch(event):
        if event.is_admin():
            return True
        message_type = event.get_message_type()
        kind = getattr(message_type, "value", message_type)
        if kind == "FriendMessage":
            return True
        # AstrBot 的 is_admin 是框架管理员；QQ 群角色来自 OneBot 事件元数据。
        if kind != "GroupMessage" or event.get_platform_name() != "aiocqhttp":
            return False
        raw = getattr(event.message_obj, "raw_message", None)
        sender = raw.get("sender") if isinstance(raw, Mapping) else getattr(raw, "sender", None)
        role = sender.get("role") if isinstance(sender, Mapping) else None
        return role in ("admin", "owner")

    async def _reset_answer(self, event, action, option, extra):
        monitor = self.reset_monitor
        if monitor is None:
            return self._reset_error
        # CommandFilter 会忽略多余实参，这里主动检查完整指令。
        tokens = event.get_message_str().split("codex-reset", 1)[-1].split()
        if extra or len(tokens) > 2:
            return "Reset 指令用法\n· 预测：/codex-reset\n· 历史：/codex-reset history 5，条数可选 1–20\n· 订阅：/codex-reset watch on|off|status\n· 信号提醒：/codex-reset signal on|off|status"
        if not action:
            return (await monitor.api.get("forecast")).render(monitor.zone)
        if action == "history":
            try:
                count = int(option) if option else 5
                if not 1 <= count <= 20:
                    raise ValueError
            except ValueError:
                return "历史条数须为 1–20 的整数，例如 /codex-reset history 10。"
            events = await monitor.api.get("timeline")
            if not events:
                return "当前接口未返回正式确认的 Reset 记录。"
            return "Codex Reset 历史 · 来源站已确认\n\n" + "\n\n".join(f"{i}. {item.render(monitor.zone)}" for i, item in enumerate(events[:count], 1))
        if action == "watch" and option in ("on", "off", "status"):
            if option == "status":
                return monitor.status(event.unified_msg_origin)
            if not self._can_manage_reset_watch(event):
                return "群订阅仅限当前 QQ 群管理员、群主或 AstrBot 管理员修改；私聊可自行订阅。"
            await monitor.subscribe(event.unified_msg_origin, option == "on")
            return "已" + ("开启" if option == "on" else "关闭") + "当前会话的 Codex Reset 订阅。\n" + monitor.status(event.unified_msg_origin)
        if action == "signal" and option in ("on", "off", "status"):
            if option == "status":
                return monitor.status(event.unified_msg_origin)
            if not self._can_manage_reset_watch(event):
                return "群信号提醒开关仅限当前 QQ 群管理员、群主或 AstrBot 管理员修改；私聊可自行设置。"
            await monitor.set_signal_notification(event.unified_msg_origin, option == "on")
            note = "关闭后仅随概率跨档预警显示当前信号状态；正式确认 Reset 的最终提醒继续遵循其开关。" if option == "off" else "新的官方信号满足预警阈值时可单独提醒。"
            return "已" + ("开启" if option == "on" else "关闭") + "当前会话的新官方信号单独提醒。\n" + note + "\n" + monitor.status(event.unified_msg_origin)
        return "Reset 指令用法\n· 预测：/codex-reset\n· 历史：/codex-reset history 5，条数可选 1–20\n· 订阅：/codex-reset watch on|off|status\n· 信号提醒：/codex-reset signal on|off|status"

    @filter.command("codex-reset", priority=100)
    async def reset_command(self, event: AstrMessageEvent, action: str = "", option: str = "", extra: str = ""):
        """公开 Reset 预测、历史以及当前会话订阅；不调用模型。"""
        if self._closing or event.is_stopped() or event.get_extra("astrbot_plugin_account_quota.handled", False):
            return
        event.set_extra("astrbot_plugin_account_quota.handled", True)
        try:
            try:
                answer = await self._reset_answer(event, action, option, extra)
            except ResetError as exc:
                answer = str(exc)
            except Exception as exc:
                logger.warning(f"Reset 指令失败：{type(exc).__name__}")
                answer = "Reset 查询失败，请稍后重试或检查插件日志。"
            if not self._closing:
                await event.send(event.plain_result(credited(answer)))
        finally:
            event.stop_event()

    def _text(self, name, default=""):
        value = self.config.get(name, default)
        return value.strip() if isinstance(value, str) else default

    def _query_enabled(self, source):
        return self.config.get(source + "_query_enabled", True) is not False

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
                    raise QueryError("请在插件配置中指定 DeepSeek 官方提供商 ID：deepseek_provider_id。")
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

    async def _codex_snapshot(self, refresh=False):
        if not self._query_enabled("codex"):
            return Snapshot("Codex 额度查询已关闭喵。", time.time(), True)
        command = self._text("codex_path", "codex")
        home = self._text("codex_home")
        timeout = self._seconds("query_timeout_seconds", 20, 3, 120)

        async def load():
            # 普通查询与强制刷新依次读取和记忆，旧的慢请求不能覆盖新周期。
            async with self._codex_read_lock:
                if not self._query_enabled("codex"):
                    raise QueryError("Codex 额度查询已关闭喵。")
                data = await fetch_codex(command, home, timeout)
                report = codex_report(data)
                if self.alarm_monitor is not None and not self._closing:
                    try:
                        await self.alarm_monitor.remember(data)
                    except Exception as exc:
                        logger.warning(f"Codex 刷新时间记忆失败，额度查询仍正常返回：{type(exc).__name__}")
                        if refresh:
                            raise QueryError("额度已经读到，但刷新时间没能保存，稍后会再试喵。") from exc
                return report

        key = "codex:" + credential_fingerprint(command + "\0" + home)
        if refresh:
            key += ":alarm-refresh"
        return await self.cache.get(key, load, 0 if refresh else self._seconds("cache_seconds", 30, 0, 300))

    async def _deepseek_snapshot(self, event):
        if not self._query_enabled("deepseek"):
            return Snapshot("DeepSeek 余额查询已关闭喵。", time.time(), True)
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
            if not self._query_enabled("deepseek"):
                raise QueryError("DeepSeek 余额查询已关闭喵。")
            return deepseek_report(await fetch_deepseek(key, timeout), show_usd)

        if not self._query_enabled("deepseek"):
            return Snapshot("DeepSeek 余额查询已关闭喵。", time.time(), True)
        cache_key = "deepseek:" + credential_fingerprint(key) + (":usd" if show_usd else ":cny")
        return await self.cache.get(cache_key, load, self._seconds("cache_seconds", 30, 0, 300))

    async def _answer(self, event, target):
        event.set_extra("astrbot_plugin_account_quota.reminder", False)
        if self.config.get("admin_only", True) and not event.is_admin():
            return "仅 AstrBot 管理员可以查询账户额度和余额。"
        names, jobs = [], []
        if target in ("all", "codex") and self._query_enabled("codex"):
            names.append("Codex")
            jobs.append(self._codex_snapshot())
        if target in ("all", "deepseek") and self._query_enabled("deepseek"):
            names.append("DeepSeek")
            jobs.append(self._deepseek_snapshot(event))
        if not jobs:
            if target == "codex":
                return "Codex 额度查询已关闭喵。"
            if target == "deepseek":
                return "DeepSeek 余额查询已关闭喵。"
            return "Codex 额度和 DeepSeek 余额查询都已关闭喵。"
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

    @filter.command("mytoken", priority=100)
    async def mytoken_command(self, event: AstrMessageEvent):
        """与 /额度 相同，查询 Codex 额度和 DeepSeek 余额。"""
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
        jobs = [self.cache.close()]
        if self.reset_monitor is not None:
            jobs.append(self.reset_monitor.close())
        if self.alarm_monitor is not None:
            jobs.append(self.alarm_monitor.close())
        await asyncio.gather(*jobs)
