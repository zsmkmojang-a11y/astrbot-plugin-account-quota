"""额度查询、消息匹配和缓存；不依赖 AstrBot，可独立测试。"""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


SHANGHAI = timezone(timedelta(hours=8))
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
MAX_RESPONSE = 1024 * 1024
TWO_DAYS = 2 * 24 * 60 * 60
DEEPSEEK_LOW_BALANCE = "该充蓝色大肥鱼了喵，要吃不起白饭了。"
CODEX_WEEKLY_RESET = "要重置了，快蹬喵"
CODEX_CARD_EXPIRY = "重置卡要过期了，快蹬喵"
DEFAULT_REMINDER_TEXTS = {
    "deepseek_low_balance": DEEPSEEK_LOW_BALANCE,
    "codex_weekly_reset": CODEX_WEEKLY_RESET,
    "codex_card_expiry": CODEX_CARD_EXPIRY,
}
_REQUEST = r"(?:(?:请|麻烦你?)(?:\s*)?)?(?:(?:帮我|给我|替我)\s*)?(?:查一下|查下|查询一下|查询|查看一下|查看|看看|查)"
_PLATFORM = r"(?:codex|deepseek)"
_SUBJECT = rf"(?:{_PLATFORM}(?:\s*(?:和|与|、|及|\+)\s*{_PLATFORM})?\s*(?:的\s*)?)?"
_NOUN = r"(?:剩余额度|剩余余额|额度|余额)"
NATURAL_PATTERN = (
    rf"(?i)^\s*(?:{_REQUEST}\s*{_SUBJECT}{_NOUN}"
    rf"|{_PLATFORM}\s*(?:的\s*)?(?:{_NOUN}\s*)?(?:还剩多少(?:钱)?|还有多少(?:钱)?|剩多少(?:钱)?))"
    r"\s*[？?！!。~～]*\s*$"
)


class QueryError(Exception):
    """仅包含可安全展示的固定错误信息。"""


@dataclass(frozen=True)
class QuotaReport:
    text: str
    reminders: tuple[str, ...] = ()


def render_report(report: QuotaReport, reminder_texts: dict | None = None) -> str:
    texts = DEFAULT_REMINDER_TEXTS if reminder_texts is None else reminder_texts
    lines = [report.text]
    for reminder in report.reminders:
        text = texts.get(reminder, DEFAULT_REMINDER_TEXTS.get(reminder, ""))
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def natural_target(text: str) -> str | None:
    if not isinstance(text, str) or len(text) > 150:
        return None
    if not re.fullmatch(NATURAL_PATTERN, text):
        return None
    platforms = set(re.findall(_PLATFORM, text.lower()))
    return next(iter(platforms)) if len(platforms) == 1 else "all"


def is_official_deepseek(base_url: str) -> bool:
    try:
        url = urlsplit(base_url.strip())
        return (
            url.scheme == "https"
            and url.hostname == "api.deepseek.com"
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path.rstrip("/") in ("", "/v1", "/beta")
        )
    except (AttributeError, ValueError):
        return False


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _time_text(value) -> str:
    value = _number(value)
    if value is None:
        return "未知"
    try:
        return datetime.fromtimestamp(value, SHANGHAI).strftime("%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "未知"


def _duration(minutes) -> str:
    minutes = _number(minutes)
    if minutes is None or minutes <= 0:
        return "周期未知"
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g} 天"
    if minutes % 60 == 0:
        return f"{minutes / 60:g} 小时"
    return f"{minutes:g} 分钟"


def codex_report(data: dict, now: float | None = None) -> QuotaReport:
    now = time.time() if now is None else now
    buckets = data.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = data.get("rateLimits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    if not buckets:
        raise QueryError("未获取到 Codex 额度；请确认 CLI 使用 ChatGPT 账号登录。")
    lines = ["Codex"]
    weekly_reminder = False
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limitName") or bucket_id
        lines.append(f"额度分组：{name}")
        if isinstance(bucket.get("planType"), str):
            lines.append(f"套餐：{bucket['planType']}")
        found = False
        for field, label in (("primary", "主要周期"), ("secondary", "次要周期")):
            window = bucket.get(field)
            if not isinstance(window, dict):
                continue
            found = True
            used = _number(window.get("usedPercent"))
            remaining = "未知" if used is None else f"{max(0, min(100, 100 - used)):g}%"
            lines.append(f"{_duration(window.get('windowDurationMins'))}（{label}）：剩余 {remaining}")
            resets = window.get("resetsAt")
            reset_text = _time_text(resets)
            delta = _number(resets)
            if (
                window.get("windowDurationMins") == 10080
                and used is not None
                and 0 <= used < 50
                and delta is not None
                and 0 < delta - now < TWO_DAYS
            ):
                weekly_reminder = True
            wait = ""
            if delta is not None and reset_text != "未知":
                seconds = max(0, math.ceil(delta - now))
                hours, remainder = divmod(seconds, 3600)
                mins = remainder // 60
                wait = f"（约 {hours} 小时 {mins} 分钟后）" if seconds else "（重置时间已到，请重新查询）"
            lines.append(f"重置：{reset_text}{wait}")
        if not found:
            lines.append("周期额度：未知")
        if bucket.get("rateLimitReachedType"):
            lines.append("状态：已触及使用限制")
        credits = bucket.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited") is True:
                lines.append("额外 credits：无限制")
            elif credits.get("balance") is not None:
                lines.append(f"额外 credits：{credits['balance']}")
    reminders = []
    if weekly_reminder:
        reminders.append("codex_weekly_reset")
    reset_cards = data.get("rateLimitResetCredits")
    if isinstance(reset_cards, dict):
        count = _number(reset_cards.get("availableCount"))
        if count is not None and count >= 0 and count % 1 == 0:
            lines.append(f"可用重置卡：{count:g} 张")
        cards = reset_cards.get("credits")
        expiries = []
        if isinstance(cards, list):
            for card in cards:
                if not isinstance(card, dict) or card.get("status") != "available":
                    continue
                expires = _number(card.get("expiresAt"))
                if expires is not None and expires > now:
                    expiries.append(expires)
        if expiries:
            earliest = min(expiries)
            lines.append(f"重置卡最近过期：{_time_text(earliest)}")
            if earliest - now < TWO_DAYS:
                reminders.append("codex_card_expiry")
    return QuotaReport("\n".join(lines), tuple(reminders))


def format_codex(data: dict, now: float | None = None) -> str:
    return render_report(codex_report(data, now))


def deepseek_report(data: dict, show_usd: bool = False) -> QuotaReport:
    infos = data.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise QueryError("DeepSeek 返回的余额数据不完整，请稍后重试。")
    lines = ["DeepSeek（账号余额）"]
    low_balance = False
    visible_balances = 0
    state = data.get("is_available")
    lines.append("可供 API 调用：" + ("是" if state is True else "否" if state is False else "未知"))
    for info in infos:
        if not isinstance(info, dict):
            raise QueryError("DeepSeek 返回的余额数据格式异常。")
        currency = info.get("currency")
        if currency not in ("CNY", "USD"):
            raise QueryError("DeepSeek 返回了无法识别的币种。")
        if currency == "USD" and not show_usd:
            continue
        visible_balances += 1
        lines.append(f"币种：{currency}")
        for field, label in (("total_balance", "可用余额"), ("topped_up_balance", "充值余额"), ("granted_balance", "赠金余额")):
            try:
                amount = Decimal(str(info[field]))
                if not amount.is_finite() or len(str(info[field])) > 40:
                    raise ValueError
            except (KeyError, InvalidOperation, ValueError):
                raise QueryError("DeepSeek 返回的余额数据格式异常。") from None
            lines.append(f"{label}：{amount:f} {currency}")
            if field == "total_balance" and currency == "CNY" and amount < Decimal("10"):
                low_balance = True
    reminders = ("deepseek_low_balance",) if low_balance else ()
    if not visible_balances:
        lines.append("接口仅返回 USD 余额，已按配置隐藏；可开启“显示 DeepSeek USD 余额”查看。")
    return QuotaReport("\n".join(lines), reminders)


def format_deepseek(data: dict, show_usd: bool = False) -> str:
    return render_report(deepseek_report(data, show_usd))


async def fetch_deepseek(key: str, timeout: float) -> dict:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True
        ) as session:
            async with session.get(
                DEEPSEEK_BALANCE_URL,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise QueryError("DeepSeek 鉴权失败，请检查所选提供商的 API Key。")
                if response.status == 429:
                    raise QueryError("DeepSeek 查询过于频繁，请稍后重试。")
                if response.status != 200:
                    raise QueryError(f"DeepSeek 余额查询失败（HTTP {response.status}）。")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise QueryError("DeepSeek 返回的数据过大。")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise QueryError("DeepSeek 返回的余额数据格式异常。")
                return data
    except QueryError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise QueryError("DeepSeek 余额查询超时，请检查网络后重试。") from None
    except (aiohttp.ClientError, ValueError, UnicodeError):
        raise QueryError("无法读取 DeepSeek 余额，请检查网络及 API Key。") from None


def resolve_codex(command: str) -> str:
    """直启原生二进制，避免 Windows npm/PowerShell 包装器的子进程残留。"""
    command = command.strip() or "codex"
    path = shutil.which(command)
    if path is None and Path(command).is_file():
        path = command
    if not path:
        raise QueryError("未找到 Codex CLI，请安装 CLI 或配置 codex_path。")
    binary = Path(path).resolve()
    if binary.suffix.lower() == ".exe":
        return str(binary)
    # npm 官方包的可选平台依赖与旧版 vendor 布局。
    package = binary.parent / "node_modules" / "@openai" / "codex"
    if binary.name == "codex.js":
        package = binary.parent.parent
    elif binary.parent.name == "bin" and binary.name == "codex":
        package = binary.parent.parent
    candidates = [
        *package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe"),
        *package.glob("node_modules/@openai/codex-*/vendor/*/codex/codex.exe"),
        *package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"),
        *package.glob("node_modules/@openai/codex-*/vendor/*/codex/codex"),
        *package.glob("vendor/*/bin/codex.exe"),
        *package.glob("vendor/*/codex/codex.exe"),
        *package.glob("vendor/*/bin/codex"),
        *package.glob("vendor/*/codex/codex"),
    ]
    if len(candidates) == 1:
        return str(candidates[0])
    if binary.suffix.lower() in (".cmd", ".bat", ".ps1", ".js") or candidates:
        raise QueryError("无法定位 Codex 原生程序，请将 codex_path 配置为 codex.exe 或原生 codex 的完整路径。")
    return str(binary)


async def fetch_codex(command: str, codex_home: str, timeout: float) -> dict:
    executable = resolve_codex(command)
    env = os.environ.copy()
    if codex_home.strip():
        home = Path(codex_home).expanduser()
        if not home.is_dir():
            raise QueryError("配置的 codex_home 目录不存在。")
        env["CODEX_HOME"] = str(home.resolve())
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    proc = None
    stderr_task = None

    async def discard_stderr():
        while await proc.stderr.read(16384):
            pass

    async def send(payload):
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    async def rpc(request_id, method, params=None):
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await send(payload)
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise QueryError("Codex 查询进程提前退出，请检查 CLI 登录状态与配置。")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise QueryError("Codex 返回的数据格式异常。")
            if message.get("id") != request_id or "method" in message:
                if "method" in message and "id" in message:
                    await send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
                continue
            if "error" in message:
                raise QueryError("Codex 接口查询失败，请确认 CLI 使用 ChatGPT 登录且可以联网。")
            result = message.get("result")
            if not isinstance(result, dict):
                raise QueryError("Codex 返回的数据格式异常。")
            return result

    try:
        async with asyncio.timeout(timeout):
            proc = await asyncio.create_subprocess_exec(
                executable, "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, limit=MAX_RESPONSE, **options,
            )
            stderr_task = asyncio.create_task(discard_stderr())
            await rpc(1, "initialize", {"clientInfo": {"name": "astrbot_account_quota", "title": "AstrBot Account Quota", "version": "1.2.0"}})
            await send({"method": "initialized", "params": {}})
            return await rpc(2, "account/rateLimits/read")
    except QueryError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        raise QueryError("Codex 额度查询超时，请检查登录状态、网络或增大查询超时。") from None
    except (OSError, ValueError, UnicodeError):
        raise QueryError("无法运行或读取 Codex CLI，请检查 codex_path 和运行环境。") from None
    finally:
        async def cleanup():
            drain_task = None
            try:
                if proc is not None:
                    # 管道仍有输出时持续排空，避免 wait 被管道背压阻塞。
                    async def discard_stdout():
                        while await proc.stdout.read(16384):
                            pass
                    drain_task = asyncio.create_task(discard_stdout())
                    if proc.stdin is not None:
                        proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.wait(), 2)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.wait()
            finally:
                tasks = [task for task in (drain_task, stderr_task) if task is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        # 卸载可能在超时后的清理期间再次取消查询。清理独立执行，
        # 等待回收完成后再传播取消，不能留下孤立的 CLI 进程。
        cleanup_task = asyncio.create_task(cleanup())
        interrupted = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                interrupted = True
        cleanup_task.result()
        if interrupted:
            raise asyncio.CancelledError


@dataclass(frozen=True)
class Snapshot:
    text: str
    queried_at: float
    error: bool = False
    reminders: tuple[str, ...] = ()


class QueryCache:
    """同一来源合并并发查询；缓存键不保存明文 Key，容量有界。"""

    def __init__(self):
        self._cache = OrderedDict()
        self._pending = {}
        self._closed = False

    async def get(self, key: str, loader, ttl: float) -> Snapshot:
        if self._closed:
            raise QueryError("插件正在卸载，请重新加载后查询。")
        entry = self._cache.get(key)
        if entry is not None and entry[0] > time.monotonic():
            self._cache.move_to_end(key)
            return entry[1]
        task = self._pending.get(key)
        if task is None:
            async def run():
                try:
                    try:
                        report = await loader()
                        if isinstance(report, QuotaReport):
                            result = Snapshot(report.text, time.time(), reminders=report.reminders)
                        else:
                            result = Snapshot(report, time.time())
                    except QueryError as exc:
                        result = Snapshot(str(exc), time.time(), True)
                    effective_ttl = min(ttl, 5) if result.error else ttl
                    self._cache[key] = (time.monotonic() + effective_ttl, result)
                    self._cache.move_to_end(key)
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                    return result
                finally:
                    self._pending.pop(key, None)
            task = asyncio.create_task(run())
            self._pending[key] = task
        return await asyncio.shield(task)

    async def close(self):
        self._closed = True
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()


def credential_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def render_snapshot(name: str, snapshot: Snapshot, reminder_texts: dict | None = None) -> str:
    text = (
        f"{name}\n{snapshot.text}" if snapshot.error
        else render_report(QuotaReport(snapshot.text, snapshot.reminders), reminder_texts)
    )
    return text + "\n数据时间：" + _time_text(snapshot.queried_at) + "（北京时间）"
