"""时间工具：统一以 UTC 存储，支持跨时区换算与本地化展示。"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def to_utc(value: datetime) -> datetime:
    """把带时区的时间换算为 UTC。"""
    if value.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return value.astimezone(UTC)


def parse_local(start_local: str, tz_name: str) -> datetime:
    """把不带时区的本地时间按指定时区换算为 UTC。"""
    try:
        zone = ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError(f"未知时区：{tz_name}") from exc
    try:
        naive = datetime.fromisoformat(start_local)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法本地时间：{start_local}") from exc
    if naive.tzinfo is not None:
        raise ValueError("本地时间不应携带时区，请使用 timezone 字段")
    return naive.replace(tzinfo=zone).astimezone(UTC)


def parse_utc(text: str) -> datetime:
    """解析存储用的 UTC 文本。"""
    return to_utc(datetime.fromisoformat(text))


def iso(value: datetime) -> str:
    """生成稳定的 UTC 存储文本（秒级精度，可按字典序比较）。"""
    return to_utc(value).replace(microsecond=0).isoformat()


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def render_local(value: datetime | str, tz_name: str) -> str:
    """把 UTC 时间渲染为指定时区的本地文本，用于可解释输出。"""
    if isinstance(value, str):
        value = parse_utc(value)
    return to_utc(value).astimezone(ZoneInfo(tz_name)).isoformat()
