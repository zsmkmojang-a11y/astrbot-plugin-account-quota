"""Claude Code 登录与订阅额度读取；不创建模型对话。"""

import asyncio
import json
import math
import os
import re
import shutil
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

from .antigravity import _run
from .quota import QueryError, QuotaReport, _time_text

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def claude_survival_text(value, today=None):
    """按运行主机的日历日期计时；开始当天为0天，不代表存活检测。"""
    if not value:
        return ""
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            raise ValueError("date format")
        start = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        days = ((today or datetime.now().date()) - start).days
        if days < 0:
            return "· Claude 开始日期还没到，请检查配置喵。"
        return f"· 我的Claude存活了{days}天喵。"
    except (ValueError, TypeError):
        return "· Claude 开始日期无效，请填写 YYYY-MM-DD 喵。"


def resolve_claude(command):
    command = os.path.expandvars(os.path.expanduser(command.strip().strip('"')))
    found = shutil.which(command)
    if not found and command.lower() in ("claude", "claude.exe"):
        candidate = Path.home() / ".local/bin" / ("claude.exe" if os.name == "nt" else "claude")
        found = str(candidate) if candidate.is_file() else None
    if not found and Path(command).is_file(): found = command
    if not found or Path(found).suffix.lower() in (".cmd", ".bat", ".ps1", ".js"):
        raise QueryError("找不到 Claude 原生 CLI，请安装或配置 claude_path 喵。")
    return str(Path(found).resolve())


def read_token():
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    path = folder / ".credentials.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024: raise ValueError("size")
        oauth = json.loads(raw)["claudeAiOauth"]
        token = oauth["accessToken"]
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token): raise ValueError("token")
        expiry = oauth.get("expiresAt")
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and expiry <= time.time() * 1000:
            raise ValueError("expired")
        return token
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise QueryError("未找到有效的 Claude 订阅登录，请在 AstrBot 所在用户下运行 claude auth login 喵。") from exc


def claude_report(data):
    if not isinstance(data, dict): raise QueryError("Claude 额度响应格式异常喵。")
    labels = {"five_hour": "5 小时额度", "seven_day": "周额度", "seven_day_sonnet": "Sonnet 周额度", "seven_day_opus": "Opus 周额度"}
    lines = ["Claude 订阅额度喵"]
    try:
        for key, label in labels.items():
            window = data.get(key)
            if window is None: continue
            if not isinstance(window, dict): raise ValueError("window")
            used = window.get("utilization")
            if used is None: remaining = "未知"
            elif isinstance(used, bool) or not isinstance(used, (float, int)) or not math.isfinite(used) or not 0 <= used <= 100: raise ValueError("usage")
            else: remaining = f"{100-used:g}%"
            at = window.get("resets_at")
            if at is None: reset = "未知"
            else:
                parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
                if parsed.tzinfo is None: raise ValueError("timezone")
                reset = _time_text(parsed.timestamp())
            lines.extend((f"· {label}：剩余 {remaining}", f"· 刷新时间：{reset}"))
        if len(lines) == 1:
            raise QueryError("当前账号未返回 Claude 订阅额度，不代表额度为零；API 账单余额不适用此查询喵。")
        return QuotaReport("\n".join(lines))
    except (TypeError, ValueError, AttributeError, OverflowError, OSError) as exc:
        raise QueryError("Claude 额度数据格式无法识别，请稍后再试喵。") from exc


class ClaudeReader:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_request = 0

    async def fetch(self, command, timeout):
        async with self.lock:
            if time.monotonic() < self.next_request:
                raise QueryError("Claude 额度接口正在冷却，请稍后再查喵。")
            try:
                async with asyncio.timeout(timeout):
                    executable = resolve_claude(command)
                    try:
                        auth = json.loads(await _run(executable, ["auth", "status", "--json"]))
                    except (QueryError, ValueError, OSError) as exc:
                        raise QueryError("Claude 登录检查失败，请先运行 claude auth login 并检查安装喵。") from exc
                    if not isinstance(auth, dict) or auth.get("loggedIn") is not True or auth.get("authMethod") != "claude.ai":
                        raise QueryError("请先运行 claude auth login 登录 Claude 订阅账号；API Key 登录不支持订阅额度查询喵。")
                    token = await asyncio.to_thread(read_token)
                    self.next_request = time.monotonic() + 60
                    headers = {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "astrbot-plugin-account-quota/1.6.1"}
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                        async with session.get(USAGE_URL, headers=headers, allow_redirects=False) as response:
                            if response.status == 429:
                                delay = 300
                                retry = response.headers.get("Retry-After", "")
                                try: delay = max(delay, float(retry))
                                except ValueError:
                                    try: delay = max(delay, parsedate_to_datetime(retry).timestamp() - time.time())
                                    except (TypeError, ValueError, OverflowError): pass
                                if not math.isfinite(delay): delay = 300
                                self.next_request = time.monotonic() + min(delay, 86400)
                                raise QueryError("Claude 额度接口限流，已暂停重试，请稍后再查喵。")
                            if response.status in (401, 403):
                                raise QueryError("Claude 订阅登录已失效或权限不足，请运行 claude auth login 喵。")
                            if response.status != 200: raise QueryError("Claude 额度接口暂不可用，请稍后再试喵。")
                            raw = bytearray()
                            async for chunk in response.content.iter_chunked(16384):
                                raw.extend(chunk)
                                if len(raw) > 1024 * 1024: raise QueryError("Claude 额度响应过大喵。")
                            return claude_report(json.loads(raw))
            except TimeoutError as exc:
                raise QueryError("Claude 额度查询超时，请检查网络和登录喵。") from exc
            except (aiohttp.ClientError, ValueError, OSError) as exc:
                raise QueryError("Claude 额度查询失败，请检查网络和 CLI 配置喵。") from exc
