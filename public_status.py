"""公开重置卡动态与 Codex 服务状态；不代表个人账号到账。"""

import time
from dataclasses import dataclass


BANKED_LABELS = {
    "announced": "已宣布发放", "arriving": "正在发放",
    "available": "来源报告已到账", "unknown": "发放动态有更新",
}
SERVICE_LABELS = {
    "operational": "正常", "degraded_performance": "性能下降",
    "partial_outage": "部分故障", "major_outage": "严重故障",
    "under_maintenance": "维护中",
}
AGGREGATE_LABELS = {**SERVICE_LABELS, "degraded": "服务异常"}


@dataclass(frozen=True)
class BankedUpdate:
    id: str
    state: str
    at: str
    summary: str
    url: str

    @property
    def key(self):
        return f"{self.id}:{self.state}"

    def render(self, display_time, zone):
        text = (f"🎟️ 重置卡{BANKED_LABELS[self.state]}啦喵\n"
                f"· 时间：{display_time(self.at, zone)}\n· {self.summary}")
        if self.url:
            text += f"\n· 详情：{self.url}"
        return text + "\n· 发卡不会直接刷新额度，个人到账与到期时间请查询实际账号喵。"


def banked_updates(data, timestamp, safe_text, time_error):
    """可选扩展字段异常只跳过该卡事件，正式 Reset 仍按原契约解析。"""
    updates = {}
    for item in data.get("events", []):
        if not isinstance(item, dict):
            continue
        state = item.get("banked_state")
        if not isinstance(state, str) or state not in BANKED_LABELS or item.get("scope") not in (None, "global"):
            continue
        event_id = item.get("id")
        summary = safe_text(item.get("summary"))
        at = item.get("announced_at")
        if isinstance(event_id, bool) or not isinstance(event_id, (str, int)) or not str(event_id).strip() or not summary:
            continue
        try:
            timestamp(at)
        except (time_error, ValueError, TypeError):
            continue
        url = safe_text(item.get("url"))
        if not url.startswith("https://"):
            url = ""
        update = BankedUpdate(str(event_id), state, at, summary, url)
        updates[update.key] = update
    return sorted(updates.values(), key=lambda item: (timestamp(item.at), item.key), reverse=True)


@dataclass(frozen=True)
class ServiceStatus:
    degraded: bool
    checked_at: str
    affected: tuple[str, ...]

    def is_fresh(self, timestamp):
        return -300 <= time.time() - timestamp(self.checked_at) <= 7200

    @classmethod
    def parse(cls, data, timestamp, safe_text):
        if not isinstance(data, dict) or data.get("stale") is not False:
            raise ValueError("服务状态过期或缺少有效性标记")
        current = data.get("current")
        if not isinstance(current, dict):
            raise ValueError("Codex 服务状态未知")
        checked_at = data.get("checked_at")
        checked = timestamp(checked_at)
        if not -300 <= time.time() - checked <= 7200:
            raise ValueError("Codex 服务状态检查时间已过期或异常")
        surfaces = data.get("surfaces", current.get("surfaces"))
        if not isinstance(surfaces, list) or not surfaces:
            raise ValueError("缺少 Codex 组件状态")
        affected = []
        for surface in surfaces:
            if not isinstance(surface, dict) or not isinstance(surface.get("status"), str) or surface.get("status") not in SERVICE_LABELS:
                raise ValueError("Codex 组件状态未知")
            status = surface["status"]
            if status != "operational":
                label = safe_text(surface.get("label"), 100) or safe_text(surface.get("id"), 100) or "Codex"
                affected.append(f"{label}：{SERVICE_LABELS[status]}")
        aggregate = current.get("codex")
        if not isinstance(aggregate, str) or aggregate not in AGGREGATE_LABELS:
            # 明确异常的组件仍足以确认故障；聚合未知时不能仅靠正常组件宣布恢复。
            if not affected:
                raise ValueError("Codex 服务状态未知")
        elif aggregate != "operational" and not affected:
            affected.append(f"Codex：{AGGREGATE_LABELS[aggregate]}")
        return cls(bool(affected), checked_at, tuple(affected))

    def render(self, display_time, zone):
        if not self.degraded:
            return ""
        return ("⚠️ Codex 服务出现异常了喵\n"
                + "\n".join(f"· {item}" for item in self.affected)
                + f"\n· 检查时间：{display_time(self.checked_at, zone)}"
                + "\n· 可能影响使用，服务异常不等于额度用完喵。"
                + "\n· 官方状态：https://status.openai.com/")
