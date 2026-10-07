"""记忆本机 Codex 的周刷新及重置卡时间，为已订阅会话发送闹钟。"""

import asyncio
import copy
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .quota import _number, _time_text
from .reset_monitor import bounded_int


class AlarmError(Exception):
    pass


def valid_epoch(value):
    value = _number(value)
    return float(value) if value is not None and value > 0 and _time_text(value) != "未知" else None


def schedule_from_quota(data: dict, now: float):
    """字段缺失不等于没有卡或没有周周期；用 None 保留最后有效记录。"""
    buckets = data.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": data["rateLimits"]} if isinstance(data.get("rateLimits"), dict) else None
    weekly = None
    if buckets is not None:
        candidates = {}
        for bucket_id, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            for field in ("primary", "secondary"):
                window = bucket.get(field)
                if not isinstance(window, dict) or window.get("windowDurationMins") != 10080:
                    continue
                at = valid_epoch(window.get("resetsAt"))
                if at is not None:
                    name = bucket.get("limitName") or str(bucket_id)
                    candidates[f"{bucket_id}:{field}"] = {"at": at, "label": str(name)[:100]}
        if candidates:
            weekly = candidates
    cards = None
    credits = data.get("rateLimitResetCredits")
    if isinstance(credits, dict):
        count = _number(credits.get("availableCount"))
        if count == 0:
            cards = {}
        elif isinstance(credits.get("credits"), list) and credits["credits"]:
            candidates, malformed = {}, False
            for card in credits["credits"]:
                if not isinstance(card, dict):
                    malformed = True
                    continue
                if card.get("status") != "available":
                    continue
                at = valid_epoch(card.get("expiresAt"))
                if at is None:
                    malformed = True
                    continue
                if at > now:
                    key = repr(at)
                    entry = candidates.setdefault(key, {"at": at, "count": 0})
                    entry["count"] += 1
            if not malformed and (candidates or count is None):
                cards = candidates
    return weekly, cards


class AlarmMonitor:
    def __init__(self, path: Path, account_key: str, send: Callable[[str, str], Awaitable[None]],
                 refresh: Callable[[], Awaitable[None]], log: Callable[[str], None], config=None):
        self.path, self.account_key = path, account_key
        self.send, self.refresh, self.log = send, refresh, log
        self.state = {"subscriptions": {}, "account_key": account_key, "weekly": {}, "cards": {},
                      "updated_at": 0.0, "delivered": {}, "pending": {}}
        self.lock = asyncio.Lock()
        self.task = None
        self.closed = False
        self.last_refresh_attempt = 0.0
        self.refresh_requested = True
        config = config if hasattr(config, "get") else {}
        self.interval = bounded_int(config, "codex_alarm_poll_minutes", 30, 1, 1440) * 60
        self.reminder_hours = sorted(set(bounded_int(config, f"codex_alarm_reminder_hours_{i}", default, 1, 720) for i, default in enumerate((48, 24), 1)))
        self.wake = asyncio.Event()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not all(key in data for key in self.state):
                raise ValueError("fields")
            for key in ("subscriptions", "weekly", "cards", "delivered", "pending"):
                if not isinstance(data[key], dict):
                    raise ValueError("objects")
            if not isinstance(data["account_key"], str) or valid_epoch(data["updated_at"]) is None and data["updated_at"] != 0:
                raise ValueError("account or time")
            if not all(isinstance(k, str) and k and type(v) is bool for k, v in data["subscriptions"].items()):
                raise ValueError("subscriptions")
            for key in ("weekly", "cards"):
                for identity, entry in data[key].items():
                    if not isinstance(identity, str) or not isinstance(entry, dict) or valid_epoch(entry.get("at")) is None:
                        raise ValueError("schedule")
                    if key == "weekly" and not isinstance(entry.get("label"), str):
                        raise ValueError("label")
                    if key == "cards" and (type(entry.get("count")) is not int or entry["count"] <= 0):
                        raise ValueError("count")
            for entries in data["delivered"].values():
                if not isinstance(entries, dict) or not all(isinstance(k, str) and valid_epoch(v) is not None for k, v in entries.items()):
                    raise ValueError("delivery")
            for queue in data["pending"].values():
                if not isinstance(queue, list):
                    raise ValueError("queue")
                for item in queue:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
                        raise ValueError("item")
                    if item.get("kind") not in ("weekly", "cards") or not isinstance(item.get("key"), str) or valid_epoch(item.get("at")) is None or valid_epoch(item.get("created_at")) is None:
                        raise ValueError("item metadata")
                    stage = item.get("stage")
                    if stage != "due" and (not isinstance(stage, str) or not stage.endswith("h") or not stage[:-1].isdigit() or not 1 <= int(stage[:-1]) <= 720):
                        raise ValueError("stage")
            self.state = data
            # 磁盘上的到点消息来自上次运行，恢复时不补发，提前提醒仍可重试。
            for queue in self.state["pending"].values():
                queue[:] = [item for item in queue if item["stage"] != "due"]
            if data["account_key"] != self.account_key:
                # CLI 路径或用户目录改变，保留订阅但清除旧账户的日期与送达记录。
                self.state.update(account_key=self.account_key, weekly={}, cards={}, updated_at=0.0, delivered={}, pending={})
        except (OSError, ValueError, TypeError) as exc:
            raise AlarmError("额度刷新提醒的状态文件无法读取，请检查 codex_alarm_state.json 喵。") from exc

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    async def _save(self):
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
        self.task = asyncio.create_task(self._run(), name="account-quota-codex-alarm")

    async def subscribe(self, origin: str, enabled: bool):
        async with self.lock:
            if self.closed:
                raise AlarmError("额度刷新提醒正在停止喵。")
            previous = copy.deepcopy(self.state)
            self.state["subscriptions"][origin] = enabled
            if not enabled:
                self.state["pending"].pop(origin, None)
            try:
                await self._save()
            except OSError as exc:
                self.state = previous
                raise AlarmError("订阅保存失败，请检查插件数据目录的写入权限喵。") from exc
            self.wake.set()

    def status(self, origin: str) -> str:
        enabled = self.state["subscriptions"].get(origin, False)
        hours = "、".join(str(h) for h in reversed(self.reminder_hours))
        lines = ["额度刷新提醒设置喵", f"· 当前会话：{'开启' if enabled else '关闭'}", f"· 轮询间隔：{self.interval / 60:g} 分钟", f"· 提前提醒：{hours} 小时", "· 正常到达自然刷新时间时通知，错过的旧通知不补发喵。"]
        for entry in self.state["weekly"].values():
            lines.append(f"· {entry['label']} 周额度刷新：{_time_text(entry['at'])}")
        for entry in self.state["cards"].values():
            lines.append(f"· 重置卡到期：{_time_text(entry['at'])} · {entry['count']} 张")
        if not self.state["weekly"]:
            lines.append("· 还没有记住有效的周额度刷新时间喵。")
        if not self.state["cards"]:
            lines.append("· 目前没有记住有效的重置卡到期时间喵。")
        if self.state["updated_at"]:
            lines.append(f"· 记录时间：{_time_text(self.state['updated_at'])}")
        return "\n".join(lines)

    def _event(self, kind: str, key: str, entry: dict, stage: str, now: float) -> dict:
        at = entry["at"]
        identity = hashlib.sha256(f"{self.account_key}|{kind}|{key}|{at}|{stage}".encode()).hexdigest()
        if kind == "weekly":
            if stage == "due":
                text = f"🎉 周额度已经到自然刷新时间啦，可以去查看当前额度喵！\n· 额度：{entry['label']}\n· 刷新时间：{_time_text(at)}\n· 实际额度以 Codex 返回的结果为准喵。"
            else:
                text = f"⏰ 周额度会在 {stage[:-1]} 小时内自然刷新啦，剩余的额度记得用起来喵！\n· 额度：{entry['label']}\n· 刷新时间：{_time_text(at)}"
        else:
            text = f"🎟️ 重置卡会在 {stage[:-1]} 小时内到期，快去看看还能不能开蹬喵！\n· 数量：{entry['count']} 张\n· 到期时间：{_time_text(at)}\n· 依据上次查询记录，卡片是否仍可用请查看实际额度喵。"
        return {"id": identity, "kind": kind, "key": key, "at": at, "stage": stage, "text": text, "created_at": now}

    def _queue_due(self, now: float, scheduled_due: dict | None = None):
        for kind in ("weekly", "cards"):
            for key, entry in self.state[kind].items():
                remaining = entry["at"] - now
                if remaining <= 0:
                    # 只发送正常定时到点的通知；启动或查询发现已经错过则静默更新。
                    on_time = remaining == 0 or (scheduled_due or {}).get(key) == entry["at"] and remaining >= -60
                    if kind != "weekly" or not on_time:
                        continue
                    stage = "due"
                else:
                    hours = next((h for h in self.reminder_hours if remaining <= h * 3600), None)
                    if hours is None:
                        continue
                    stage = f"{hours}h"
                event = self._event(kind, key, entry, stage, now)
                for origin, enabled in self.state["subscriptions"].items():
                    if not enabled:
                        continue
                    delivered = self.state["delivered"].get(origin, {})
                    queue = self.state["pending"].setdefault(origin, [])
                    if event["id"] in delivered:
                        continue
                    queued = next((item for item in queue if item["id"] == event["id"]), None)
                    if queued is not None:
                        queued["text"] = event["text"]
                        continue
                    # 24h 覆盖待发送的 48h；刷新已到覆盖旧周期所有提前提醒。
                    queue[:] = [item for item in queue if not (item["kind"] == kind and item["key"] == key and item["at"] == entry["at"])]
                    queue.append(copy.deepcopy(event))

    def _pending_valid(self, item: dict, now: float, scheduled_due: dict | None = None) -> bool:
        if now - item["created_at"] > 86400:
            return False
        if item["stage"] == "due":
            on_time = now == item["at"] or (scheduled_due or {}).get(item["key"]) == item["at"]
            return on_time and item["at"] <= now <= item["at"] + 60
        entry = self.state[item["kind"]].get(item["key"])
        remaining = item["at"] - now
        hours = int(item["stage"][:-1])
        return hours in self.reminder_hours and entry is not None and entry["at"] == item["at"] and 0 < remaining <= hours * 3600

    async def remember(self, data: dict, now: float | None = None):
        now = time.time() if now is None else now
        weekly, cards = schedule_from_quota(data, now)
        async with self.lock:
            if self.closed:
                return
            previous = copy.deepcopy(self.state)
            # 新读到当前周期前，先移除已错过的旧通知；不补发历史自然刷新。
            self._queue_due(now)
            if weekly is not None:
                self.state["weekly"].update(weekly)
            if cards is not None:
                self.state["cards"] = cards
            self.state["updated_at"] = float(now)
            self._queue_due(now)
            for origin, queue in self.state["pending"].items():
                queue[:] = [item for item in queue if self._pending_valid(item, now)]
            try:
                await self._save()
            except OSError:
                self.state = previous
                raise
            self.refresh_requested = False
            self.wake.set()
        # 读取到窗口内日期时立即检查，不必再等下一轮 30 分钟。
        await self.tick(now, allow_refresh=False)

    async def tick(self, now: float | None = None, allow_refresh=True, scheduled_due: dict | None = None):
        now = time.time() if now is None else now
        async with self.lock:
            if self.closed:
                return
            previous = copy.deepcopy(self.state)
            self._queue_due(now, scheduled_due)
            # 送达记录仅保留最近一个月，过时日期本身不会再触发通知。
            for origin, delivered in self.state["delivered"].items():
                self.state["delivered"][origin] = {k: v for k, v in delivered.items() if now - v < 30 * 86400}
            try:
                if self.state != previous:
                    await self._save()
            except OSError:
                self.state = previous
                raise
            for origin, queue in list(self.state["pending"].items()):
                while queue:
                    item = queue[0]
                    if not self.state["subscriptions"].get(origin) or not self._pending_valid(item, now, scheduled_due):
                        queue.pop(0)
                        await self._save()
                        continue
                    try:
                        async with asyncio.timeout(15):
                            await self.send(origin, item["text"])
                    except Exception as exc:
                        self.log(f"Codex 额度刷新提醒发送失败，下一轮重试：{type(exc).__name__}")
                        break
                    self.state["delivered"].setdefault(origin, {})[item["id"]] = now
                    queue.pop(0)
                    await self._save()
            needs_refresh = any(self.state["subscriptions"].values()) and (
                self.refresh_requested or not self.state["updated_at"] or any(entry["at"] <= now for entry in self.state["weekly"].values())
            )
        # 自然刷新到时重新查询，失败在后续轮询重试，至少间隔 5 分钟。
        if allow_refresh and needs_refresh and now - self.last_refresh_attempt >= 300 and not self.closed:
            self.last_refresh_attempt = now
            try:
                await self.refresh()
            except Exception as exc:
                self.log(f"Codex 刷新时间重新读取失败，将稍后重试：{type(exc).__name__}")

    async def _run(self):
        scheduled_due = None
        while not self.closed:
            self.wake.clear()
            started_at = time.time()
            armed = {key: entry["at"] for key, entry in self.state["weekly"].items() if entry["at"] > started_at}
            try:
                await self.tick(scheduled_due=scheduled_due)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Codex 额度刷新提醒本轮失败，将继续检查：{type(exc).__name__}")
            scheduled_due = None
            now = time.time()
            crossed = {key: at for key, at in armed.items() if at <= now}
            if crossed:
                # 发消息或保存期间刚越过到点，不能先过滤成历史日期再睡一整轮。
                scheduled_due = crossed
                continue
            future = {key: entry["at"] for key, entry in self.state["weekly"].items() if entry["at"] > now}
            deadline = min(future.values(), default=now + self.interval)
            wait = min(self.interval, deadline - now)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                scheduled_due = {key: at for key, at in future.items() if at == deadline} if deadline <= now + self.interval else None

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.lock:
            pass
