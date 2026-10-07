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

SOURCE = "https://codex-reset.com/"
CREDIT = "Data: codex-reset.com"
USER_AGENT = "astrbot-plugin-account-quota/1.4.1 (+https://github.com/zsmkmojang-a11y/astrbot-plugin-account-quota)"


class ResetError(Exception):
    """可直接向用户显示的受控错误。"""


def credited(text: str) -> str:
    # QQ 纯文本同时保留可访问链接和文档要求的末尾署名。
    return f"{text}\n来源：{SOURCE}\n{CREDIT}"


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
    return datetime.fromtimestamp(timestamp(value), timezone_info(zone)).strftime("%Y-%m-%d %H:%M:%S") + f" ({zone})"


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
            return cls(p24, p48, confidence, last, age, data["updated_at"], signal_id, signal_text)
        except (KeyError, TypeError) as exc:
            raise ResetError("Reset 接口字段不完整或格式异常，请稍后重试。") from exc

    def render(self, zone: str) -> str:
        last = display_time(self.last_reset_at, zone) if self.last_reset_at else "未知"
        age = f"{self.age_days:g} 天" if self.age_days is not None else "未知"
        confidence = {"low": "低", "medium": "中", "high": "高"}[self.confidence]
        return (f"Codex 全局 Reset 预测（第三方预测，不代表已到账）\n"
                f"未来 24 小时：{self.p24:g}%\n未来 48 小时：{self.p48:g}%\n"
                f"置信度：{confidence} ({self.confidence})\n距上次 Reset：{age}\n"
                f"上次 Reset：{last}\n官方信号（来源站判断）：{self.signal_text}\n"
                f"更新时间：{display_time(self.updated_at, zone)}")


@dataclass(frozen=True)
class ResetEvent:
    id: str
    announced_at: str
    summary: str
    url: str

    def render(self, zone: str) -> str:
        return f"{display_time(self.announced_at, zone)}\n{self.summary}\n{self.url}"


def confirmed_events(data) -> list[ResetEvent]:
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ResetError("Reset 历史接口字段异常，请稍后重试。")
    events = {}
    for item in data["events"]:
        if not isinstance(item, dict):
            raise ResetError("Reset 历史接口事件格式异常，请稍后重试。")
        if item.get("group") != "reset" or item.get("announcement_state") != "announced":
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


class ResetAPI:
    """端点级串行化与 60 秒缓存，手动查询也遵守 Retry-After。"""

    def __init__(self, zone="Asia/Shanghai"):
        self.zone = zone
        self.session: aiohttp.ClientSession | None = None
        self.locks = {name: asyncio.Lock() for name in ("forecast", "timeline")}
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
                    return self.cache[endpoint]
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
                        raise ResetError(f"Reset 数据源暂不可用（HTTP {response.status}），请稍后重试。")
                    # 避免异常服务响应无限占用内存。
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        payload.extend(chunk)
                        if len(payload) > 2 * 1024 * 1024:
                            raise ResetError("Reset 数据源响应过大，请稍后重试。")
                    data = json.loads(payload)
                    value = Forecast.parse(data) if endpoint == "forecast" else confirmed_events(data)
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
        self.cooldown = bounded_int(config, "probability_cooldown", 21600, 0, 604800)
        self.thresholds = sorted(set(bounded_int(config, f"threshold_{i}", default, 1, 100) for i, default in enumerate((75, 83, 93), 1)))
        self.state = {
            "subscriptions": {}, "last_seen_reset_id": "", "last_seen_reset_at": 0, "seen_reset_ids": [],
            "last_seen_signal_id": "", "seen_signal_ids": [],
            "last_probability_level": 0, "last_probability_notify_time": 0,
            "probability_notify_times": {}, "timeline_initialized": False,
            "forecast_initialized": False, "pending": {}, "signal_preferences": {}, "pending_account_refresh_id": "",
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
            for key in ("last_seen_reset_at", "last_probability_level", "last_probability_notify_time"):
                number(data[key])
            if not all(isinstance(v, str) for v in data["seen_signal_ids"] + data["seen_reset_ids"]):
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
        return (f"当前会话 Reset 订阅：{'开启' if enabled else '关闭'}\n"
                f"轮询间隔：{self.interval / 60:g} 分钟\n24h / 48h 任一达到阈值：{' / '.join(map(str, self.thresholds))}%\n"
                f"同级重复提醒冷却：{self.cooldown / 3600:g} 小时\n"
                f"概率提醒：{'开' if self.config.get('probability_warning', True) else '关'}\n"
                f"新官方信号单独提醒：{'开' if self.signal_enabled(origin) else '关'}\n"
                f"确认 Reset 提醒：{'开' if self.config.get('reset_notification', True) else '关'}")

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
                    messages.append("✅ Codex Reset 已由来源站确认啦喵！\n" + latest.render(self.zone) + "\n可以开蹬了喵！\n个人账号是否到账，记得查看实际额度喵。")
        if forecast is not None:
            probability = max(forecast.p24, forecast.p48)
            level = max((v for v in self.thresholds if probability >= v), default=0)
            signal_id = forecast.signal_id
            # 已由 timeline 确认的同一事件不再降级成“未确认”信号。
            if signal_id in state["seen_reset_ids"] or signal_id == state["last_seen_reset_id"]:
                signal_id = ""
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
                        signal_message = "⚠️ Codex 新官方信号（来源站判断，尚未确认到账）\n" + forecast.signal_text + f"\n未来 24h：{forecast.p24:g}%；48h：{forecast.p48:g}%"
                previous = state["last_probability_level"]
                last_notify = state["probability_notify_times"].get(str(level))
                cooled = last_notify is None or now - last_notify >= self.cooldown
                if level > previous and cooled and self.config.get("probability_warning", True):
                    if probability >= 93:
                        wording = "🚨 Strong Watch！快看看官方信号里的 Reset 时间或日期，目前尚未确认到账喵。"
                    elif probability >= 83:
                        wording = ("⚠️ 官方很有可能已经说要 Reset 啦，记得核对下面的来源信号；目前尚未确认到账喵。"
                                   if signal_id else "⚠️ 已达到官方信号预警档位，不过接口暂时没有官方信号，目前尚未确认到账喵。")
                    else:
                        wording = "📈 距离上次重置已经有点久啦，未来很有可能会重置喵。"
                    signal_note = ("官方信号（来源站判断）：" + forecast.signal_text) if signal_id else "接口暂时没有活跃官方信号，这个档位只是概率预警，不代表官方已经表态或给出日期喵。"
                    messages.append(f"Codex Reset 预测达到 {level}% 提醒档位喵\n{wording}\n未来 24h：{forecast.p24:g}%；48h：{forecast.p48:g}%\n{signal_note}\n这是第三方预测，目前尚未确认 Reset，也不代表额度已到账喵。")
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
        results = await asyncio.gather(self.api.get("forecast"), self.api.get("timeline"), return_exceptions=True)
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
            if self.on_confirm and previous["timeline_initialized"] and self.state["last_seen_reset_id"] != previous["last_seen_reset_id"]:
                self.state["pending_account_refresh_id"] = self.state["last_seen_reset_id"]
            for text in messages:
                for origin, enabled in self.state["subscriptions"].items():
                    if enabled:
                        if text.startswith("⚠️ Codex 新官方信号") and not self.signal_enabled(origin):
                            continue
                        queue = self.state["pending"].setdefault(origin, [])
                        if text.startswith("✅"):
                            queue[:] = [item for item in queue if item["text"].startswith("✅")]
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
                while queue:
                    item = queue[0]
                    muted_signal = item["text"].startswith("⚠️ Codex 新官方信号") and not self.signal_enabled(origin)
                    if not self.state["subscriptions"].get(origin) or muted_signal or now - item["created_at"] > 86400:
                        queue.pop(0)
                        await self._save()
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
