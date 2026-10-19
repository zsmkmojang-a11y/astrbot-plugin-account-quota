"""官方 agy CLI 的只读额度查询，不向模型发送提示词。"""

import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .quota import QueryError, QuotaReport, _time_text


def resolve_agy(command):
    command = os.path.expandvars(os.path.expanduser(command.strip().strip('"')))
    found = shutil.which(command)
    if not found and command.lower() in ("agy", "agy.exe") and os.name == "nt":
        candidates = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "agy/bin/agy.exe")
        candidates.append(Path.home() / "AppData/Local/agy/bin/agy.exe")
        found = next((str(path) for path in candidates if path.is_file()), None)
    if not found and Path(command).is_file():
        found = command
    if not found or Path(found).suffix.lower() in (".cmd", ".bat", ".ps1", ".js"):
        raise QueryError("找不到反重力原生 CLI，请安装 agy 并配置程序路径喵。")
    return str(Path(found).resolve())


async def _run(executable, args):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    proc = await asyncio.create_subprocess_exec(executable, *args, stdin=asyncio.subprocess.DEVNULL,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)
    async def read(stream, keep):
        data = bytearray()
        while chunk := await stream.read(16384):
            if keep:
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise QueryError("反重力响应过大，请检查 CLI 版本喵。")
        return bytes(data)
    readers = [asyncio.create_task(read(proc.stdout, True)), asyncio.create_task(read(proc.stderr, False))]
    try:
        output, _ = await asyncio.gather(*readers)
        code = await proc.wait()
        if code:
            raise QueryError("反重力查询失败，请先在运行 AstrBot 的系统用户下启动 agy 完成登录，再检查网络喵。")
        return output.decode("utf-8", errors="strict")
    finally:
        async def cleanup():
            if os.name != "nt":
                # 主进程已退出时，继承管道的后代仍可能留在进程组内。
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            if proc.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await killer.wait()
                if proc.returncode is None:
                    try: proc.kill()
                    except ProcessLookupError: pass
            await proc.wait()
            for task in readers: task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        task = asyncio.create_task(cleanup())
        cancelled = False
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled = True
        task.result()
        if cancelled: raise asyncio.CancelledError


def antigravity_report(data):
    try:
        command = data["command"]
        if command.get("name") not in ("usage", "quota", "/usage", "/quota"):
            raise ValueError("command")
        groups = command["data"]["groups"]
        if not isinstance(groups, list) or not groups:
            raise ValueError("groups")
        lines = ["反重力额度喵"]
        for group in groups:
            label = group.get("display_name") or group.get("name") or group.get("id") or "模型组"
            if not isinstance(label, str): raise ValueError("label")
            lines.append(f"· {label[:100]}")
            buckets = group.get("buckets")
            if not isinstance(buckets, list) or not buckets: raise ValueError("buckets")
            for bucket in buckets:
                fraction = bucket.get("remaining_fraction")
                if fraction is None:
                    remaining = "未知"
                elif isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                    raise ValueError("fraction")
                else: remaining = f"{fraction * 100:g}%"
                window = bucket.get("window") or "额度窗口"
                if not isinstance(window, str): raise ValueError("window")
                window = {"weekly": "周额度", "5h": "5 小时额度"}.get(window, window)
                reset = bucket.get("reset_time")
                if reset is None: at = "未知"
                else:
                    from datetime import datetime
                    parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                    if parsed.tzinfo is None: raise ValueError("timezone")
                    at = _time_text(parsed.timestamp())
                lines.append(f"  · {window[:80]}：剩余 {remaining}\n  · 刷新时间：{at}")
        return QuotaReport("\n".join(lines))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError) as exc:
        raise QueryError("反重力额度数据格式无法识别，请更新 CLI 或检查插件兼容性喵。") from exc


async def fetch_antigravity(command, timeout):
    executable = resolve_agy(command)
    try:
        async with asyncio.timeout(timeout):
            version = await _run(executable, ["--version"])
            match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
            if not match or tuple(map(int, match.groups())) < (1, 1, 11):
                raise QueryError("反重力 CLI 需要 1.1.11 或更新版本，请先升级喵。")
            return antigravity_report(json.loads(await _run(executable, ["--print", "/usage", "--output-format", "json"])))
    except TimeoutError as exc:
        raise QueryError("反重力额度查询超时，请检查登录和网络喵。") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise QueryError("反重力 CLI 启动失败或未返回有效 JSON，请检查安装和登录喵。") from exc
