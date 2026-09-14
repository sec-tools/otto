"""
Trends and predictions from Otto's memory — local, data-driven, explainable.

Given the messages Otto has remembered, this module finds *series*: things
that keep happening (a scanner posting results, a bot reporting a nightly
job, someone sharing the same status update every morning). For each
series it works out

* the **cadence** ("daily ~08:20", "every ~6 h", "weekly (Mon)") from the
  median gap between occurrences and how regular the gaps are,
* the **next expected** occurrence and whether it is **late** — a nightly
  job that has not reported by the usual time is the kind of thing a
  person notices only when it is far too late,
* **metric trends**: numbers inside the messages ("2 high", "Duration:
  380s", "Raw leads: 34") tracked over time, with the last value compared
  to what is usual and rendered as a tiny sparkline.

Everything here is pure arithmetic over local data. Every statement Otto
makes from it carries its evidence (count, span, values), so the user can
judge it — no black boxes.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from otto.intelligence.knowledge import Message

MIN_SERIES_COUNT = 3
MAX_TEMPLATE_CHARS = 60
SPARK_CHARS = "▁▂▃▄▅▆▇█"

_URL_RE = re.compile(r"https?://\S+")
_NUM_RE = re.compile(r"\d[\d.,:]*")
_LABELLED_METRIC = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z /_\-]{1,28}?)\s*[:=]\s*(?P<value>-?\d+(?:[.,]\d+)?)\s*(?P<unit>%|ms|s|m|h|min|sec|k|K|M|x)?(?![\w:])"
)
_COUNTED_METRIC = re.compile(
    r"\b(?P<value>\d+)\s+(?P<name>critical|high|medium|low|errors?|warnings?|failures?|failed|passed|passing|tests?|"
    r"findings?|vulnerabilit(?:y|ies)|alerts?|incidents?|tickets?|prs?|pull requests?|issues?|leads?|open|closed|"
    r"users?|signups?|orders?|deploys?|commits?|messages?|files?|packages?|dependencies|hosts?|nodes?|pods?|jobs?)\b",
    re.IGNORECASE,
)
_COUNT_WORD_AFTER = re.compile(
    r"^\s*(?:critical|high|medium|low|errors?|warnings?|failures?|failed|passed|passing|tests?|findings?|"
    r"vulnerabilit(?:y|ies)|alerts?|incidents?|tickets?|prs?|pull requests?|issues?|leads?|open|closed|users?|"
    r"signups?|orders?|deploys?|commits?|messages?|files?|packages?|dependencies|hosts?|nodes?|pods?|jobs?)\b",
    re.IGNORECASE,
)
_PRIORITY_METRICS = ("critical", "high", "failures", "failed", "errors", "incidents", "alerts", "vulnerabilities",
                     "findings", "medium", "warnings", "open", "tickets", "issues")
_SEVERITY_WORDS = ("critical", "high", "medium", "low")


@dataclass
class MetricTrend:
    name: str
    values: list[float]
    last: float
    usual: float                 # median of the previous values
    direction: str               # "up" | "down" | "flat"
    spark: str

    def to_dict(self) -> dict:
        name, unit = split_unit(self.name)
        return {
            "name": name, "last": format_metric(self.last, unit), "usual": format_metric(self.usual, unit),
            "direction": self.direction, "spark": self.spark, "n": len(self.values),
        }


@dataclass
class Series:
    key: str
    channel: str
    sender: str
    label: str
    n: int
    first_ts: float
    last_ts: float
    cadence_s: float | None
    regularity: float
    cadence: str
    next_expected: float | None
    late_by_s: float | None
    metrics: list[MetricTrend] = field(default_factory=list)

    @property
    def periodic(self) -> bool:
        return self.cadence_s is not None and self.regularity >= 0.5

    @property
    def moving_metrics(self) -> list[MetricTrend]:
        return [m for m in self.metrics if m.direction != "flat"]


@dataclass
class ChannelShift:
    channel: str
    this_week: int
    last_week: int
    ratio: float
    direction: str               # "busier" | "quieter"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def template_of(text: str) -> str:
    """Shape of a message with the variable parts (numbers, URLs) removed."""
    first = (text or "").strip().split("\n", 1)[0]
    first = _URL_RE.sub("", first)
    first = _NUM_RE.sub("#", first.lower())
    first = re.sub(r"#+", "#", first)
    first = re.sub(r"[#\s.,:;/()\[\]-]+$", "", re.sub(r"\s+", " ", first)).strip()
    return first[:MAX_TEMPLATE_CHARS]


def sparkline(values: Sequence[float], width: int = 12) -> str:
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK_CHARS[0] * len(vals) if lo <= 0 else SPARK_CHARS[3] * len(vals)
    out = []
    for v in vals:
        idx = int(round((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[max(0, min(len(SPARK_CHARS) - 1, idx))])
    return "".join(out)


def humanize_duration(seconds: float) -> str:
    s = abs(float(seconds))
    if s < 90:
        return f"{int(round(s))} s"
    if s < 5400:
        return f"{int(round(s / 60))} min"
    if s < 172800:
        hours = s / 3600
        return f"{hours:.0f} h" if hours >= 10 or abs(hours - round(hours)) < 0.15 else f"{hours:.1f} h"
    days = s / 86400
    return f"{days:.0f} d" if abs(days - round(days)) < 0.15 or days >= 10 else f"{days:.1f} d"


def _fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}"


_UNIT_SUFFIX = re.compile(r"\s*\((%|ms|s|m|h|min|sec|k|K|M)\)\s*$")
_SECONDS_TO_S = {"s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "ms": 0.001}   # "m" is ambiguous — left as is


def split_unit(name: str) -> tuple[str, str]:
    """``"duration (s)"`` → ``("duration", "s")``; names without a unit come back unchanged."""
    m = _UNIT_SUFFIX.search(name or "")
    if not m:
        return (name or "").strip(), ""
    return (name or "")[:m.start()].strip(), m.group(1)


def format_metric(value: float, unit: str = "") -> str:
    """A number the way a person would say it: ``3250.8`` seconds is ``54 min``, ``12`` percent is ``12%``."""
    if unit in _SECONDS_TO_S:
        seconds = float(value) * _SECONDS_TO_S[unit]
        if abs(seconds) < 1:
            return f"{seconds * 1000:.0f} ms"
        return humanize_duration(seconds)
    if unit == "%":
        return f"{_fmt_num(float(value))}%"
    if unit in ("k", "K", "M"):
        return f"{_fmt_num(float(value))}{unit}"
    return _fmt_num(float(value))


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _mad(values: Sequence[float], center: float) -> float:
    return _median([abs(v - center) for v in values]) if values else 0.0


def _local_minutes(ts: float) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
    return d.hour * 60 + d.minute


def _typical_clock(timestamps: Sequence[float]) -> str:
    """The typical local time of day (median over the most common hour bucket) as ``HH:MM``."""
    mins = [_local_minutes(t) for t in timestamps]
    if not mins:
        return ""
    buckets: dict[int, list[int]] = {}
    for m in mins:
        buckets.setdefault(m // 60, []).append(m)
    hour, members = max(buckets.items(), key=lambda kv: (len(kv[1]), -kv[0]))
    med = int(_median(members))
    return f"{med // 60:02d}:{med % 60:02d}"


def describe_cadence(gap_s: float, timestamps: Sequence[float]) -> str:
    if gap_s < 5400:
        return f"every ~{max(1, int(round(gap_s / 60)))} min"
    if gap_s < 20 * 3600:
        return f"every ~{gap_s / 3600:.0f} h"
    if gap_s < 30 * 3600:
        clock = _typical_clock(timestamps)
        return f"daily ~{clock}" if clock else "daily"
    if 6 * 86400 <= gap_s <= 8 * 86400:
        last = datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).astimezone()
        return f"weekly ({last.strftime('%a')})"
    days = gap_s / 86400
    return f"every ~{days:.0f} days" if days >= 1.5 else "daily"


def extract_metrics(text: str) -> dict[str, float]:
    """Numbers with a name attached, e.g. ``{"high": 2, "duration (s)": 380.2}``."""
    out: dict[str, float] = {}
    cleaned = _URL_RE.sub("", text or "")
    for m in _LABELLED_METRIC.finditer(cleaned):
        name = re.sub(r"\s+", " ", m.group("name").strip().lower().strip(" -_/"))
        if not name or len(name) < 2 or name in ("at", "on", "in", "by", "to", "id", "ts", "pr", "v"):
            continue
        if re.search(r"\b(?:am|pm|utc|gmt)\b", cleaned[m.end():m.end() + 4].lower()):
            continue   # clock times are not metrics
        if _COUNT_WORD_AFTER.match(cleaned[m.end():m.end() + 24]):
            continue   # "Severity: 0 critical" — the number belongs to the counted word
        try:
            value = float(m.group("value").replace(",", ""))
        except ValueError:
            continue
        unit = (m.group("unit") or "").strip()
        if unit and unit not in ("x",):
            name = f"{name} ({unit})"
        out.setdefault(name, value)
    for m in _COUNTED_METRIC.finditer(cleaned):
        name = m.group("name").lower()
        name = {"vulnerability": "vulnerabilities", "failed": "failures"}.get(name, name)
        if name in _SEVERITY_WORDS or name.endswith("s") or name in ("open", "closed", "passing", "dependencies"):
            key = name
        else:
            key = name + "s"
        try:
            out.setdefault(key, float(m.group("value")))
        except ValueError:
            continue
    return out


def _direction(values: Sequence[float]) -> tuple[float, str]:
    """(usual, direction) comparing the last value with the median of the earlier ones."""
    if len(values) < 3:
        return (values[-1] if values else 0.0), "flat"
    prev = list(values[:-1])
    usual = _median(prev)
    spread = _mad(prev, usual)
    integers = all(abs(v - round(v)) < 1e-9 for v in values)
    threshold = max(1.0 if integers else 0.0, 1.5 * spread, 0.15 * abs(usual))
    last = values[-1]
    if last > usual + threshold:
        return usual, "up"
    if last < usual - threshold:
        return usual, "down"
    return usual, "flat"


def _pick_metrics(series_metrics: dict[str, list[float]], n: int) -> list[MetricTrend]:
    trends: list[MetricTrend] = []
    for name, values in series_metrics.items():
        if len(values) < max(MIN_SERIES_COUNT, int(0.6 * n)):
            continue
        if max(values) - min(values) < 1e-9:
            continue        # a constant carries no trend ("critical: 0" every time)
        usual, direction = _direction(values)
        trends.append(MetricTrend(name=name, values=values, last=values[-1], usual=usual,
                                  direction=direction, spark=sparkline(values)))

    def _rank(t: MetricTrend) -> tuple:
        pri = next((i for i, p in enumerate(_PRIORITY_METRICS) if p in t.name), len(_PRIORITY_METRICS))
        moving = 0 if t.direction != "flat" else 1
        var = -statistics.pvariance(t.values) if len(t.values) > 1 else 0.0
        return (moving, pri, var)

    trends.sort(key=_rank)
    return trends[:3]


# ---------------------------------------------------------------------------
# Series detection
# ---------------------------------------------------------------------------

def series_from(messages: Iterable[Message], *, now: float, min_count: int = MIN_SERIES_COUNT) -> list[Series]:
    """Group messages into recurring series and describe each one."""
    groups: dict[tuple[str, str, str], list[Message]] = {}
    for m in messages:
        template = template_of(m.text)
        if len(template) < 6:
            continue
        key = (m.channel.lower(), (m.sender or "").lower(), template)
        groups.setdefault(key, []).append(m)

    out: list[Series] = []
    for (channel, sender, template), msgs in groups.items():
        if len(msgs) < min_count:
            continue
        msgs.sort(key=lambda x: x.ts)
        stamps = sorted({round(x.ts) for x in msgs})
        if len(stamps) < min_count:
            continue
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        cadence_s = _median(gaps) if gaps else None
        regularity = 0.0
        if cadence_s and cadence_s > 0:
            regularity = max(0.0, 1.0 - _mad(gaps, cadence_s) / cadence_s)
        periodic = cadence_s is not None and regularity >= 0.5 and len(gaps) >= 2
        next_expected = stamps[-1] + cadence_s if periodic and cadence_s else None
        late_by = None
        if next_expected is not None and cadence_s:
            tolerance = max(0.35 * cadence_s, 600.0)
            if now > next_expected + tolerance:
                late_by = now - next_expected

        per_metric: dict[str, list[float]] = {}
        for x in msgs:
            for name, value in extract_metrics(x.text).items():
                per_metric.setdefault(name, []).append(value)
        metrics = _pick_metrics(per_metric, len(msgs))

        latest = msgs[-1]
        label = _URL_RE.sub("", latest.text.strip().split("\n", 1)[0]).strip()
        label = re.sub(r"\s+", " ", label)[:80] or template
        out.append(Series(
            key=f"{channel}|{sender}|{template}",
            channel=latest.channel, sender=latest.sender, label=label, n=len(msgs),
            first_ts=stamps[0], last_ts=stamps[-1], cadence_s=cadence_s if periodic else None,
            regularity=regularity,
            cadence=describe_cadence(cadence_s, stamps) if periodic and cadence_s else "irregular",
            next_expected=next_expected, late_by_s=late_by, metrics=metrics,
        ))

    def _rank(s: Series) -> tuple:
        return (
            0 if s.late_by_s else 1,
            0 if s.moving_metrics else 1,
            0 if s.periodic else 1,
            -s.n,
        )

    out.sort(key=_rank)
    return out


def channel_shifts(daily_counts_by_channel: dict[str, Sequence[int]], *, min_messages: int = 8) -> list[ChannelShift]:
    """Channels whose last 7 days differ a lot from the 7 before (needs two full weeks of counts)."""
    shifts: list[ChannelShift] = []
    for channel, counts in daily_counts_by_channel.items():
        if len(counts) < 14:
            continue
        last_week, this_week = sum(counts[-14:-7]), sum(counts[-7:])
        if max(last_week, this_week) < min_messages:
            continue
        if this_week >= 2 * max(last_week, 1) and this_week >= min_messages:
            shifts.append(ChannelShift(channel, this_week, last_week, this_week / max(last_week, 1), "busier"))
        elif last_week >= min_messages and this_week <= 0.4 * last_week:
            shifts.append(ChannelShift(channel, this_week, last_week, this_week / last_week, "quieter"))
    shifts.sort(key=lambda s: -abs(s.this_week - s.last_week))
    return shifts
