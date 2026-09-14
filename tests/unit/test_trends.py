"""Trends and predictions (``otto.intelligence.trends``) — pure arithmetic over remembered messages."""
from __future__ import annotations

import time

from otto.intelligence.knowledge import Message
from otto.intelligence.trends import (
    channel_shifts,
    describe_cadence,
    extract_metrics,
    humanize_duration,
    series_from,
    sparkline,
    template_of,
)

SCAN = (
    "Scanner scan complete — api\nmain\nDuration : {dur}s\nRaw leads: 34\nConfirmed: 0\n"
    "Severity : 0 critical, {high} high, 0 medium\nReport-eligible findings:\n[HIGH 8.0]\nSomething"
)


def _msg(text, ts, *, channel="#security-alerts", sender="scanner", bot=True, key=None):
    return Message(key=key or f"{sender}{ts}", source="slack", channel=channel, sender=sender,
                   is_bot=bot, ts=ts, text=text)


class TestTemplatesAndMetrics:
    def test_template_strips_numbers_urls_and_keeps_the_shape(self):
        a = template_of(SCAN.format(dur="380.2", high=2))
        b = template_of(SCAN.format(dur="12.9", high=0))
        assert a == b == "scanner scan complete — api"
        assert template_of("Deploy #4412 finished in 93s https://ci.example/run/4412") == "deploy # finished in #s"

    def test_metrics_labelled_and_counted(self):
        m = extract_metrics(SCAN.format(dur="380.2", high=2))
        assert m["duration (s)"] == 380.2 and m["raw leads"] == 34 and m["confirmed"] == 0
        assert m["critical"] == 0 and m["high"] == 2 and m["medium"] == 0
        assert "severity" not in m                     # the 0 belongs to "critical"

    def test_metrics_ignore_clock_times_and_versions(self):
        m = extract_metrics("Nightly build finished: 12 tests failed, 3 warnings. Deploy at 8:19 AM. Version v2.3.1")
        assert m["tests"] == 12 and m["warnings"] == 3
        assert "nightly build finished" not in m
        assert not any("8" in k for k in m)

    def test_sparkline_scales_to_range(self):
        assert sparkline([1, 2, 3, 2, 1, 0, 5]) == "▂▄▅▄▂▁█"
        assert sparkline([3, 3, 3]) == "▄▄▄" and sparkline([0, 0]) == "▁▁" and sparkline([]) == ""
        assert len(sparkline(list(range(40)))) == 12

    def test_humanize_duration(self):
        assert humanize_duration(30) == "30 s"
        assert humanize_duration(5400) == "1.5 h"
        assert humanize_duration(7200) == "2 h"
        assert humanize_duration(90000) == "25 h"
        assert humanize_duration(3 * 86400) == "3 d"

    def test_metrics_read_the_way_a_person_says_them(self):
        """A duration in seconds is shown as minutes; the unit leaves the name."""
        from otto.intelligence.trends import MetricTrend, format_metric, split_unit
        assert split_unit("duration (s)") == ("duration", "s") and split_unit("high") == ("high", "")
        assert format_metric(3250.8, "s") == "54 min" and format_metric(380.2, "s") == "6 min"
        assert format_metric(45, "s") == "45 s" and format_metric(0.25, "s") == "250 ms"
        assert format_metric(1500, "ms") == "2 s" and format_metric(12.5, "%") == "12.5%"
        assert format_metric(2, "") == "2" and format_metric(2.25, "k") == "2.2k"
        d = MetricTrend(name="duration (s)", values=[300, 320, 3250.8], last=3250.8, usual=310, direction="up", spark="▁▁█").to_dict()
        assert d["name"] == "duration" and d["last"] == "54 min" and d["usual"] == "5 min"
        counts = MetricTrend(name="high", values=[2, 2, 5], last=5, usual=2, direction="up", spark="▁▁█").to_dict()
        assert counts["last"] == "5" and counts["usual"] == "2"

    def test_describe_cadence(self):
        assert describe_cadence(1800, [0, 1800]) == "every ~30 min"
        assert describe_cadence(6 * 3600, [0, 6 * 3600]) == "every ~6 h"
        assert describe_cadence(86400, [time.time()]).startswith("daily ~")
        assert describe_cadence(7 * 86400, [time.time()]).startswith("weekly (")
        assert describe_cadence(3 * 86400, [0]) == "every ~3 days"


class TestSeries:
    def _daily(self, now, highs, *, start_days_ago=None):
        start_days_ago = start_days_ago if start_days_ago is not None else len(highs)
        base = now - start_days_ago * 86400
        return [_msg(SCAN.format(dur=f"{370 + i}.0", high=h), base + i * 86400 + (i % 2) * 60, key=f"s{i}")
                for i, h in enumerate(highs)]

    def test_regular_daily_series_predicts_the_next_one(self):
        now = time.time()
        msgs = self._daily(now, [2, 2, 0, 2, 2], start_days_ago=4.5)
        (s,) = series_from(msgs, now=now)
        assert s.n == 5 and s.periodic and s.cadence.startswith("daily")
        assert s.next_expected is not None and abs(s.next_expected - (msgs[-1].ts + 86400)) < 120
        assert s.late_by_s is None
        assert s.label == "Scanner scan complete — api"

    def test_late_series_is_flagged(self):
        now = time.time()
        msgs = self._daily(now, [2, 2, 2, 2], start_days_ago=6)     # last one ~3 days ago, cadence daily
        (s,) = series_from(msgs, now=now)
        assert s.late_by_s is not None and s.late_by_s > 86400

    def test_metric_trend_up_and_constant_metrics_dropped(self):
        now = time.time()
        msgs = self._daily(now, [2, 2, 0, 2, 2, 5], start_days_ago=5.5)
        (s,) = series_from(msgs, now=now)
        names = [m.name for m in s.metrics]
        assert names[0] == "high"                    # moving metric first
        high = s.metrics[0]
        assert high.direction == "up" and high.last == 5 and high.usual == 2
        assert high.spark == "▄▄▁▄▄█"
        assert "critical" not in names and "raw leads" not in names   # constants are noise

    def test_too_few_or_irregular_messages_are_not_a_pattern(self):
        now = time.time()
        assert series_from(self._daily(now, [2, 2]), now=now) == []
        irregular = [_msg("Deploy done", now - t, key=str(t)) for t in (100, 5000, 200000, 210000)]
        (s,) = series_from(irregular, now=now)
        assert not s.periodic and s.cadence == "irregular" and s.next_expected is None

    def test_series_are_split_by_channel_and_sender(self):
        now = time.time()
        a = self._daily(now, [1, 1, 1])
        b = [_msg(m.text, m.ts, channel="#other", key=m.key + "b") for m in a]
        assert len(series_from(a + b, now=now)) == 2

    def test_ranking_puts_late_and_moving_first(self):
        now = time.time()
        quiet = [_msg("Backup finished OK", now - i * 3600 - 100, sender="cron", key=f"q{i}") for i in range(5)]
        moving = self._daily(now, [1, 1, 1, 6], start_days_ago=3.5)
        late = [_msg("Digest ready", now - 3 * 86400 - i * 86400, sender="digest", key=f"l{i}") for i in range(4)]
        ranked = series_from(quiet + moving + late, now=now)
        assert ranked[0].label == "Digest ready"
        assert ranked[1].label.startswith("Scanner scan")


class TestChannelShifts:
    def test_busier_and_quieter(self):
        counts = {
            "#incidents": [1] * 7 + [4] * 7,
            "#random": [5] * 7 + [1] * 7,
            "#tiny": [0] * 7 + [1] * 7,
            "#short": [3] * 10,
        }
        shifts = {s.channel: s for s in channel_shifts(counts)}
        assert shifts["#incidents"].direction == "busier" and shifts["#incidents"].ratio == 4.0
        assert shifts["#random"].direction == "quieter"
        assert "#tiny" not in shifts and "#short" not in shifts
