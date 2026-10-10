"""codex-reset.com 的公开预测与事件监控；不读取用户账号。"""

import asyncio
import copy
import hashlib
import json
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from .public_status import BANKED_LABELS, ServiceStatus, banked_updates

SOURCE = "https://codex-reset.com/"
CREDIT = "Data: codex-reset.com"
USER_AGENT = "astrbot-plugin-account-quota/1.4.6 (+https://github.com/zsmkmojang-a11y/astrbot-plugin-account-quota)"


class ResetError(Exception):
    """可直接向用户显示的受控错误。"""


def credited(text: str) -> str:
    # QQ 纯文本同时保留可访问链接和文档要求的末尾署名。
    return f"{text}\n\n· 来源：{SOURCE}\n{CREDIT}"


def number(value, minimum=0, maximum=float("inf")) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResetError("Reset 接口字段不完整或格式异常，请稍后重试。")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ResetError("Reset 接口数值异常，请稍后重试。")
    return float(value)


def timestamp(value) -> float:
    if not isinstance(value, str):
        raise ResetError("Reset 接口时间字段异常，请稍后重试。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing timezone")
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ResetError("Reset 接口时间字段异常，请稍后重试。") from exc


def timezone_info(name: str):
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ResetError("Reset 时区配置无效或系统缺少该时区数据，请使用 Asia/Shanghai。") from exc


def display_time(value, zone: str) -> str:
    # zone 保留给预测接口使用；回复时间统一遵循 AstrBot 运行主机的时区。
    return datetime.fromtimestamp(timestamp(value)).strftime("%Y-%m-%d %H:%M:%S")


def safe_text(value, limit=800) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def signal_info(value) -> tuple[str, str]:
    if not value:
        return "", "无官方信号"
    if isinstance(value, str):
        content = safe_text(value)
        return hashlib.sha256(content.encode()).hexdigest(), content
    if isinstance(value, dict):
        if value.get("active") is False:
            return "", "无官方信号"
        content = next((safe_text(value.get(k)) for k in ("summary", "text", "quote") if safe_text(value.get(k))), "")
        event_id = value.get("id") or value.get("tweet_id")
        if isinstance(event_id, (str, int)) and not isinstance(event_id, bool) and str(event_id).strip():
            return str(event_id), content or "接口报告了活跃官方信号"
        # 只依赖事件时间、内容与 URL；忽略可能随请求变化的概率等字段。
        if content:
            basis = [value.get("announced_at") or value.get("at"), content, value.get("url")]
            return hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest(), content
    raise ResetError("Reset 接口官方信号字段异常，请稍后重试。")


@dataclass(frozen=True)
class Forecast:
    p24: float
    p48: float
    confidence: str
    last_reset_at: str | None
    age_days: float | None
    updated_at: str
    signal_id: str
    signal_text: str
    signal_probability: float | None = None

    @classmethod
    def parse(cls, data):
        try:
            probabilities = data["probabilities"]
            p24 = number(probabilities["rounded_24h"], 0, 100)
            p48 = number(probabilities["rounded_48h"], 0, 100)
            confidence = data["confidence"]
            if confidence not in ("low", "medium", "high"):
                raise ResetError("Reset 接口置信度字段异常，请稍后重试。")
            last = data["last_reset_at"]
            age = data["age_days"]
            if last is not None:
                timestamp(last)
            if age is not None:
                age = number(age)
            timestamp(data["updated_at"])
            signal_id, signal_text = signal_info(data["official_signal"])
            signal_probability = None
            if signal_id:
                # 来源站的 Tibo 标题概率独立于历史 24h/48h。此扩展字段
                # 未纳入稳定接口契约，缺失或异常时仍保留历史模型查询。
                try:
                    signal_probability = number(probabilities.get("signal_percent"), 0, 100)
                except (ResetError, OverflowError):
                    pass
            return cls(p24, p48, confidence, last, age, data["updated_at"], signal_id, signal_text, signal_probability)
        except (KeyError, TypeError) as exc:
            raise ResetError("Reset 接口字段不完整或格式异常，请稍后重试。") from exc

    def probability_text(self, include_signal: bool = True) -> str:
        lines = []
        if include_signal and self.signal_probability is not None:
            lines.append(f"· Tibo 信号概率：{self.signal_probability:g}%")
        lines.extend((f"· 未来 24 小时：{self.p24:g}%", f"· 未来 48 小时：{self.p48:g}%"))
        return "\n".join(lines)

    def render(self, zone: str) -> str:
        last = display_time(self.last_reset_at, zone) if self.last_reset_at else "未知"
        age = f"{self.age_days:g} 天" if self.age_days is not None else "未知"
        confidence = {"low": "低", "medium": "中", "high": "高"}[self.confidence]
        return (f"Codex 全局 Reset 预测\n"
                f"{self.probability_text()}\n"
                f"· 置信度：{confidence}\n· 距上次 Reset：{age}\n"
                f"· 上次 Reset：{last}\n· 官方信号：{self.signal_text}\n"
                f"· 更新时间：{display_time(self.updated_at, zone)}\n\n"
                "第三方预测与信号解读，尚未确认到账喵。")


@dataclass(frozen=True)
class ResetEvent:
    id: str
    announced_at: str
    summary: str
    url: str

    def render(self, zone: str) -> str:
        return f"{display_time(self.announced_at, zone)}\n· {self.summary}\n· 详情：{self.url}"


def confirmed_events(data) -> list[ResetEvent]:
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ResetError("Reset 历史接口字段异常，请稍后重试。")
    events = {}
    for item in data["events"]:
        if not isinstance(item, dict):
            raise ResetError("Reset 历史接口事件格式异常，请稍后重试。")
        if item.get("group") != "reset" or item.get("announcement_state") != "announced":
            continue
        banked_state = item.get("banked_state")
        if (isinstance(banked_state, str) and banked_state in BANKED_LABELS) or item.get("banked_grant") is True:
            continue
        try:
            event_id = item["id"]
            if isinstance(event_id, bool) or not isinstance(event_id, (str, int)) or not str(event_id).strip():
                raise ResetError("Reset 历史接口事件 ID 异常，请稍后重试。")
            timestamp(item["announced_at"])
            summary, url = safe_text(item["summary"]), safe_text(item["url"])
            if not summary or not url.startswith("https://"):
                raise ResetError("Reset 历史接口事件字段异常，请稍后重试。")
            events[str(event_id)] = ResetEvent(str(event_id), item["announced_at"], summary, url)
        except (KeyError, TypeError) as exc:
            raise ResetError("Reset 历史接口事件字段不完整，请稍后重试。") from exc
    return sorted(events.values(), key=lambda event: (timestamp(event.announced_at), event.id), reverse=True)


class TimelineEvents(list):
    """保留正式 Reset 列表接口，同时携带独立的重置卡动态。"""

    def __init__(self, events, banked):
        super().__init__(events)
        self.banked = banked


def parse_timeline(data):
    events = confirmed_events(data)
    return TimelineEvents(events, banked_updates(data, timestamp, safe_text, ResetError))


class ResetAPI:
    """端点级串行化与 60 秒缓存，手动查询也遵守 Retry-After。"""

    def __init__(self, zone="Asia/Shanghai"):
        self.zone = zone
        self.session: aiohttp.ClientSession | None = None
        self.locks = {name: asyncio.Lock() for name in ("forecast", "timeline", "status-history")}
        self.next_request: dict[str, float] = {}
        self.cache: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self.closed = False

    async def get(self, endpoint: str):
        async with self.locks[endpoint]:
            if self.closed:
                raise ResetError("Reset 监控已停止。")
            now = time.monotonic()
            if now < self.next_request.get(endpoint, 0):
                if endpoint in self.errors:
                    raise ResetError(self.errors[endpoint])
                if endpoint in self.cache:
                    value = self.cache[endpoint]
                    if isinstance(value, ServiceStatus) and not value.is_fresh(timestamp):
                        raise ResetError("Codex 服务状态缓存已过期，请稍后重试。")
                    return value
                raise ResetError("Reset 本轮请求未完成，请稍后重试。")
            self.next_request[endpoint] = now + 60
            if self.session is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": USER_AGENT})
            params = {"tz": self.zone} if endpoint == "forecast" else {"locale": "zh"}
            try:
                async with self.session.get(SOURCE + "api/" + endpoint, params=params, allow_redirects=False) as response:
                    if response.status == 429:
                        delay = 60.0
                        retry = response.headers.get("Retry-After", "")
                        try:
                            delay = max(delay, float(retry))
                        except ValueError:
                            try:
                                delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
                            except (TypeError, ValueError, OverflowError):
                                pass
                        if not math.isfinite(delay):
                            delay = 60
                        self.next_request[endpoint] = time.monotonic() + delay
                        raise ResetError("Reset 数据源请求受限，正在等待 Retry-After，请稍后重试。")
                    if response.status != 200:
                        raise ResetError(f"Reset 数据源暂不可用，HTTP {response.status}，请稍后重试。")
                    # 避免异常服务响应无限占用内存。
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        payload.extend(chunk)
                        if len(payload) > 2 * 1024 * 1024:
                            raise ResetError("Reset 数据源响应过大，请稍后重试。")
                    data = json.loads(payload)
                    if endpoint == "forecast":
                        value = Forecast.parse(data)
                    elif endpoint == "timeline":
                        value = parse_timeline(data)
                    else:
                        value = ServiceStatus.parse(data, timestamp, safe_text)
                self.cache[endpoint] = value
                self.errors.pop(endpoint, None)
                return value
            except ResetError as exc:
                self.errors[endpoint] = str(exc)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, OverflowError) as exc:
                error = "Reset 数据源网络超时或响应异常，请稍后重试。"
                self.errors[endpoint] = error
                raise ResetError(error) from exc

    async def close(self):
        self.closed = True
        # 等待在途命令结束后关闭 session，避免重载留下 HTTP 请求。
        for lock in self.locks.values():
            async with lock:
                pass
        if self.session is not None:
            await self.session.close()


def bounded_int(config: Mapping, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = config.get(key, default)
        return default if isinstance(value, bool) else min(max(int(value), minimum), maximum)
    except (TypeError, ValueError, OverflowError):
        return default


class ResetMonitor:
    def __init__(self, path: Path, config: Mapping, send: Callable[[str, str], Awaitable[None]], log: Callable[[str], None], on_confirm: Callable[[str], Awaitable[None]] | None = None):
        self.path, self.config, self.send, self.log = path, config, send, log
        self.on_confirm = on_confirm
        zone = config.get("timezone", "Asia/Shanghai")
        self.zone = zone.strip() if isinstance(zone, str) else "Asia/Shanghai"
        timezone_info(self.zone)
        self.api = ResetAPI(self.zone)
        # 新配置使用分钟；旧安装包的秒数配置在未提供新字段时仍可读取。
        self.interval = (
            bounded_int(config, "poll_interval_minutes", 60, 1, 1440) * 60
            if "poll_interval_minutes" in config
            else bounded_int(config, "poll_interval", 3600, 60, 86400)
        )
        self.cooldown = (
            bounded_int(config, "probability_cooldown_minutes", 360, 0, 10080) * 60
            if "probability_cooldown_minutes" in config
            else bounded_int(config, "probability_cooldown", 21600, 0, 604800)
        )
        self.thresholds = sorted(set(bounded_int(config, f"threshold_{i}", default, 1, 100) for i, default in enumerate((75, 83, 93), 1)))
        self.state = {
            "subscriptions": {}, "last_seen_reset_id": "", "last_seen_reset_at": 0, "seen_reset_ids": [],
            "last_seen_signal_id": "", "seen_signal_ids": [],
            "last_probability_level": 0, "last_probability_notify_time": 0,
            "probability_notify_times": {}, "timeline_initialized": False,
            "forecast_initialized": False, "pending": {}, "signal_preferences": {}, "pending_account_refresh_id": "",
            "banked_initialized": False, "seen_banked_keys": [], "last_banked_at": 0,
            "service_initialized": False, "service_degraded": False, "service_checked_at": 0,
            "service_observed_at": 0,
        }
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.closed = False

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state object")
            # v1.3.0 没有会话信号偏好，保留既有订阅与游标并补充默认值。
            data.setdefault("signal_preferences", {})
            data.setdefault("pending_account_refresh_id", "")
            for key in ("banked_initialized", "seen_banked_keys", "last_banked_at",
                        "service_initialized", "service_degraded", "service_checked_at", "service_observed_at"):
                data.setdefault(key, copy.deepcopy(self.state[key]))
            for key in self.state:
                expected = type(self.state[key])
                matches = isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool) if expected is int else type(data.get(key)) is expected
                if key not in data or not matches:
                    raise ValueError("state fields")
            subscriptions = data["subscriptions"]
            if not all(isinstance(k, str) and k and type(v) is bool for k, v in subscriptions.items()):
                raise ValueError("subscriptions")
            if not all(isinstance(k, str) and k and type(v) is bool for k, v in data["signal_preferences"].items()):
                raise ValueError("signal preferences")
            if not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("created_at"), (int, float)) for item in v) for k, v in data["pending"].items()):
                raise ValueError("pending")
            for key in ("last_seen_reset_at", "last_probability_level", "last_probability_notify_time", "last_banked_at", "service_checked_at", "service_observed_at"):
                number(data[key])
            if not all(isinstance(v, str) for v in data["seen_signal_ids"] + data["seen_reset_ids"] + data["seen_banked_keys"]):
                raise ValueError("signals")
            for v in data["probability_notify_times"].values():
                number(v)
            self.state = data
        except (OSError, ValueError, TypeError, ResetError) as exc:
            # 禁止覆盖损坏状态；订阅及去重数据须由管理员恢复。
            raise ResetError("Reset 状态文件无法读取，监控已停用；请检查插件数据目录 reset_state.json。") from exc

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    async def _save(self):
        # shield + 等待线程退出，保证取消不会让后续写入与旧线程竞态。
        task = asyncio.create_task(asyncio.to_thread(self._write, copy.deepcopy(self.state)))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    async def start(self):
        await asyncio.to_thread(self._load)
        self.task = asyncio.create_task(self._run(), name="account-quota-reset-monitor")

    async def subscribe(self, origin: str, enabled: bool):
        async with self.lock:
            if self.closed:
                raise ResetError("Reset 监控已停止。")
            previous = copy.deepcopy(self.state)
            self.state["subscriptions"][origin] = enabled
            if not enabled:
                self.state["pending"].pop(origin, None)
            try:
                await self._save()
            except OSError as exc:
                self.state = previous
                raise ResetError("Reset 订阅保存失败，请检查插件数据目录的写入权限。") from exc

    def signal_enabled(self, origin: str) -> bool:
        return self.state["signal_preferences"].get(origin, bool(self.config.get("signal_notification", True)))

    async def set_signal_notification(self, origin: str, enabled: bool):
        async with self.lock:
            if self.closed:
                raise ResetError("Reset 监控已停止。")
            previous = copy.deepcopy(self.state)
            self.state["signal_preferences"][origin] = enabled
            if not enabled:
                queue = self.state["pending"].get(origin, [])
                queue[:] = [item for item in queue if not item["text"].startswith("⚠️ Codex 新官方信号")]
            try:
                await self._save()
            except OSError as exc:
                self.state = previous
                raise ResetError("Reset 信号提醒设置保存失败，请检查插件数据目录的写入权限。") from exc

    def status(self, origin: str) -> str:
        enabled = self.state["subscriptions"].get(origin, False)
        return (f"Reset 订阅设置\n· 当前会话：{'开启' if enabled else '关闭'}\n"
                f"· 轮询间隔：{self.interval / 60:g} 分钟\n· 预警线：{' / '.join(str(v) + '%' for v in self.thresholds)}\n"
                f"· 监测范围：Tibo 信号概率及未来 24 / 48 小时，任一达到预警线即提醒\n"
                f"· 同级提醒冷却：{self.cooldown / 60:g} 分钟\n"
                f"· 概率提醒：{'开' if self.config.get('probability_warning', True) else '关'}\n"
                f"· 新官方信号单独提醒：{'开' if self.signal_enabled(origin) else '关'}\n"
                f"· 确认 Reset 提醒：{'开' if self.config.get('reset_notification', True) else '关'}\n"
                f"· 重置卡发放动态：{'开' if self.config.get('banked_notification', True) else '关'}\n"
                f"· Codex 异常与恢复提醒：{'开' if self.config.get('service_notification', True) else '关'}")

    async def service_notice(self):
        """查询时只附有效的异常；网络失败、正常状态均不增加回复内容。"""
        try:
            status = await self.api.get("status-history")
            if isinstance(status, ServiceStatus) and status.is_fresh(timestamp):
                checked = timestamp(status.checked_at)
                async with self.lock:
                    if self.closed or checked < max(self.state["service_checked_at"], self.state["service_observed_at"]):
                        return ""
                    previous = self.state["service_observed_at"]
                    if checked > previous:
                        self.state["service_observed_at"] = checked
                        try:
                            await self._save()
                        except OSError as exc:
                            self.state["service_observed_at"] = previous
                            raise ResetError("Codex 服务检查时间保存失败，已暂时隐藏状态。") from exc
                    return status.render(display_time, self.zone)
        except ResetError as exc:
            self.log(f"Codex 服务状态查询失败：{exc}")
        return ""

    async def query_forecast(self):
        results = await asyncio.gather(self.api.get("forecast"), self.api.get("timeline"), self.service_notice(), return_exceptions=True)
        if isinstance(results[0], BaseException):
            raise results[0]
        sections = [results[0].render(self.zone)]
        banked = getattr(results[1], "banked", [])
        if banked:
            sections.append(banked[0].render(display_time, self.zone))
        if isinstance(results[2], str) and results[2]:
            sections.append(results[2])
        return "\n\n".join(sections)

    def _evaluate_public(self, events, service):
        messages = []
        state = self.state
        updates = getattr(events, "banked", None)
        if updates is not None:
            # 同轮只考察最新公开状态，旧事件回填不抢占当前发卡动态。
            latest = updates[0] if updates else None
            if updates and state["banked_initialized"]:
                if (latest.key not in state["seen_banked_keys"] and timestamp(latest.at) >= state["last_banked_at"]
                        and self.config.get("banked_notification", True)):
                    messages.append(latest.render(display_time, self.zone))
            state["banked_initialized"] = True
            keys = [item.key for item in updates[:200]]
            if latest and timestamp(latest.at) >= state["last_banked_at"]:
                state["last_banked_at"] = timestamp(latest.at)
                keys += state["seen_banked_keys"]
            else:
                keys = state["seen_banked_keys"] + keys
            state["seen_banked_keys"] = list(dict.fromkeys(keys))[:200]
        if isinstance(service, ServiceStatus) and service.is_fresh(timestamp):
            checked = timestamp(service.checked_at)
            if checked < state["service_observed_at"]:
                return messages
            state["service_observed_at"] = checked
            if not state["service_initialized"] or checked > state["service_checked_at"]:
                was_degraded = state["service_initialized"] and state["service_degraded"]
                if self.config.get("service_notification", True):
                    if service.degraded and not was_degraded:
                        messages.append(service.render(display_time, self.zone))
                    elif was_degraded and not service.degraded:
                        messages.append("🟢 Codex 服务已经恢复啦喵\n"
                                        f"· 检查时间：{display_time(service.checked_at, self.zone)}\n"
                                        "· 之前检测到的服务异常已解除，可以继续开蹬了喵。\n"
                                        "· 官方状态：https://status.openai.com/")
                state["service_initialized"] = True
                state["service_degraded"] = service.degraded
                state["service_checked_at"] = checked
        return messages

    def _evaluate(self, forecast: Forecast | None, events: list[ResetEvent] | None, now: float) -> list[str]:
        messages = []
        state = self.state
        if events is not None:
            for event in events:
                if event.id not in state["seen_reset_ids"]:
                    state["seen_reset_ids"] = (state["seen_reset_ids"] + [event.id])[-200:]
            latest = events[0] if events else None
            if not state["timeline_initialized"]:
                # 空列表不建立基线，避免恢复历史数据时误报。
                if latest:
                    state["timeline_initialized"] = True
                    state["last_seen_reset_id"] = latest.id
                    state["last_seen_reset_at"] = timestamp(latest.announced_at)
            elif latest and latest.id != state["last_seen_reset_id"] and timestamp(latest.announced_at) > state["last_seen_reset_at"]:
                state["last_seen_reset_id"] = latest.id
                state["last_seen_reset_at"] = timestamp(latest.announced_at)
                if self.config.get("reset_notification", True):
                    messages.append("✅ Codex Reset 已由来源站确认啦喵！\n" + latest.render(self.zone) + "\n\n可以开蹬了喵！\n· 个人账号是否到账，记得查看实际额度喵。")
        if forecast is not None:
            signal_id = forecast.signal_id
            # 已由 timeline 确认的同一事件不再降级成“未确认”信号。
            if signal_id in state["seen_reset_ids"] or signal_id == state["last_seen_reset_id"]:
                signal_id = ""
            probability = max(forecast.p24, forecast.p48,
                              forecast.signal_probability if signal_id and forecast.signal_probability is not None else 0)
            level = max((v for v in self.thresholds if probability >= v), default=0)
            probability_text = forecast.probability_text(include_signal=bool(signal_id))
            if not state["forecast_initialized"]:
                state["forecast_initialized"] = True
                if signal_id:
                    state["seen_signal_ids"].append(signal_id)
            else:
                signal_message = None
                if signal_id and signal_id not in state["seen_signal_ids"]:
                    state["seen_signal_ids"] = (state["seen_signal_ids"] + [signal_id])[-200:]
                    # 单独信号通知是否发送由各会话开关决定；跨档提醒始终附带信号状态。
                    if level and (self.config.get("signal_notification", True) or any(state["signal_preferences"].values())):
                        signal_message = "⚠️ Codex 新官方信号喵\n· " + forecast.signal_text + f"\n{probability_text}\n\n来源站的信号解读，尚未确认到账喵。"
                previous = state["last_probability_level"]
                last_notify = state["probability_notify_times"].get(str(level))
                cooled = last_notify is None or now - last_notify >= self.cooldown
                if level > previous and cooled and self.config.get("probability_warning", True):
                    if probability >= 93:
                        wording = "🚨 Strong Watch！快看看官方信号里的 Reset 时间或日期喵。"
                    elif probability >= 83:
                        wording = ("⚠️ 官方很有可能已经说要 Reset 啦，记得核对下面的来源信号喵。"
                                   if signal_id else "⚠️ 已达到官方信号预警档位，不过接口暂时没有官方信号喵。")
                    else:
                        wording = ("📈 Tibo 信号概率已经达到预警线，未来很有可能会重置喵。"
                                   if signal_id and forecast.signal_probability is not None and forecast.signal_probability >= level
                                   else "📈 距离上次重置已经有点久啦，未来很有可能会重置喵。")
                    signal_note = ("官方信号：" + forecast.signal_text) if signal_id else "当前无活跃官方信号，概率预警不代表官方已经表态或给出日期喵。"
                    messages.append(f"Codex Reset 预测达到 {level}% 提醒档位喵\n{wording}\n\n{probability_text}\n· {signal_note}\n\n第三方预测与信号解读，尚未确认到账喵。")
                    signal_message = None  # 同轮信号与跨档提醒合并，避免双重预警。
                    # 一次跃过多个档位后，冷却也覆盖被跨过的较低档位。
                    for threshold in self.thresholds:
                        if threshold <= level:
                            state["probability_notify_times"][str(threshold)] = now
                    state["last_probability_notify_time"] = now
                if signal_message:
                    messages.append(signal_message)
            state["last_seen_signal_id"] = signal_id
            state["last_probability_level"] = level
        # timeline 的最终确认覆盖同轮可能滞后的概率或信号。
        confirmations = [text for text in messages if text.startswith("✅")]
        return confirmations or messages

    async def poll(self):
        results = await asyncio.gather(self.api.get("forecast"), self.api.get("timeline"), self.api.get("status-history"), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self.log(f"Reset 轮询失败：{result}")
        forecast = results[0] if isinstance(results[0], Forecast) else None
        events = results[1] if isinstance(results[1], list) else None
        async with self.lock:
            if self.closed:
                return
            previous = copy.deepcopy(self.state)
            now = time.time()
            messages = self._evaluate(forecast, events, now)
            service = results[2] if isinstance(results[2], ServiceStatus) else None
            if service is not None and (
                not service.is_fresh(timestamp)
                or timestamp(service.checked_at) < max(self.state["service_checked_at"], self.state["service_observed_at"])
            ):
                service = None
            messages.extend(self._evaluate_public(events, service))
            if self.on_confirm and previous["timeline_initialized"] and self.state["last_seen_reset_id"] != previous["last_seen_reset_id"]:
                self.state["pending_account_refresh_id"] = self.state["last_seen_reset_id"]
            for text in messages:
                for origin, enabled in self.state["subscriptions"].items():
                    if enabled:
                        if text.startswith("⚠️ Codex 新官方信号") and not self.signal_enabled(origin):
                            continue
                        queue = self.state["pending"].setdefault(origin, [])
                        if text.startswith("✅"):
                            queue[:] = [item for item in queue if not item["text"].startswith(("Codex Reset 预测", "⚠️ Codex 新官方信号"))]
                        if text.startswith("🎟️"):
                            queue[:] = [item for item in queue if not item["text"].startswith("🎟️")]
                        if text.startswith(("⚠️ Codex 服务", "🟢 Codex 服务")):
                            queue[:] = [item for item in queue if not item["text"].startswith(("⚠️ Codex 服务", "🟢 Codex 服务"))]
                        queue.append({"text": credited(text), "created_at": now})
                        del queue[:-20]
            try:
                if self.state != previous:
                    await self._save()
            except OSError:
                self.state = previous
                raise
            # 分会话处理，失败会话不影响其他订阅；最多保留一天并每轮重试。
            for origin, queue in list(self.state["pending"].items()):
                remaining = len(queue)
                while queue and remaining:
                    remaining -= 1
                    item = queue[0]
                    muted_signal = item["text"].startswith("⚠️ Codex 新官方信号") and not self.signal_enabled(origin)
                    muted_public = (
                        item["text"].startswith("🎟️") and not self.config.get("banked_notification", True)
                        or item["text"].startswith(("⚠️ Codex 服务", "🟢 Codex 服务")) and not self.config.get("service_notification", True)
                    )
                    outdated_service = (
                        item["text"].startswith("⚠️ Codex 服务") and not self.state["service_degraded"]
                        or item["text"].startswith("🟢 Codex 服务") and self.state["service_degraded"]
                    )
                    if not self.state["subscriptions"].get(origin) or muted_signal or muted_public or outdated_service or now - item["created_at"] > 86400:
                        queue.pop(0)
                        await self._save()
                        continue
                    if service is None and item["text"].startswith(("⚠️ Codex 服务", "🟢 Codex 服务")):
                        # 状态源失效时暂缓服务通知，其他 Reset/发卡消息继续发送。
                        queue.append(queue.pop(0))
                        continue
                    try:
                        async with asyncio.timeout(15):
                            await self.send(origin, item["text"])
                    except Exception as exc:
                        self.log(f"Reset 推送失败，下一轮重试：{type(exc).__name__}")
                        break
                    queue.pop(0)
                    await self._save()
            refresh_id = self.state["pending_account_refresh_id"]
        # 与聊天推送开关独立；网络异常时保留任务，下轮继续重读本机时间。
        if refresh_id and self.on_confirm and not self.closed:
            try:
                await self.on_confirm(refresh_id)
            except Exception as exc:
                self.log(f"Reset 后本机额度时间读取失败，下轮重试：{type(exc).__name__}")
            else:
                async with self.lock:
                    if self.state["pending_account_refresh_id"] == refresh_id:
                        self.state["pending_account_refresh_id"] = ""
                        await self._save()

    async def _run(self):
        while not self.closed:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Reset 监控本轮失败，将继续轮询：{type(exc).__name__}")
            await asyncio.sleep(self.interval)

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # 命令触发的订阅写入不属于后台 task；重载前也必须等待其落盘。
        async with self.lock:
            pass
        await self.api.close()
