"""
Commitments, asks, deadlines and events (``otto.intelligence.commitments``).

Pure-function tests; all dates are relative to a fixed reference so the
suite does not depend on the day it runs. ``ref`` is a Wednesday afternoon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from otto.intelligence import commitments as cm
from otto.intelligence.commitments import extract, find_due

# Wednesday 2026-09-09, 14:30 UTC. Local-time assertions use .astimezone().
REF = datetime(2026, 9, 9, 14, 30, tzinfo=timezone.utc)
LOCAL_REF = REF.astimezone()
TODAY = LOCAL_REF.date()


def _local(obj):
    """Local datetime of a Due or of a Commitment's due date."""
    assert obj is not None
    when = obj.when if hasattr(obj, "when") else obj.due
    assert when is not None, obj
    return when.astimezone()


def _day(offset: int):
    return TODAY + timedelta(days=offset)


class TestFindDue:
    @pytest.mark.parametrize("text, days_ahead, hour, minute", [
        ("send it by Friday", 2, 17, 0),
        ("by EOD", 0, 17, 0),
        ("tomorrow eod", 1, 17, 0),
        ("by EOD Friday", 2, 17, 0),
        ("Friday EOD", 2, 17, 0),
        ("tomorrow morning", 1, 9, 0),
        ("this afternoon", 0, 14, 0),
        ("tonight", 0, 20, 0),
        ("in 3 days", 3, 17, 0),
        ("end of week", 2, 17, 0),
        ("next week", 5, 9, 0),                 # Monday 09:00
        ("by next Tuesday", 6, 17, 0),
        ("on Friday at 4:30 pm", 2, 16, 30),
        ("tomorrow at 10am", 1, 10, 0),
        ("by noon tomorrow", 1, 12, 0),
        ("first thing tomorrow", 1, 9, 0),
    ])
    def test_relative_phrases(self, text, days_ahead, hour, minute):
        when = _local(find_due(text, ref=REF))
        assert when.date() == _day(days_ahead), text
        assert (when.hour, when.minute) == (hour, minute), text

    @pytest.mark.parametrize("text, month, day", [
        ("on Sep 14", 9, 14), ("Sept 14th, 2026", 9, 14), ("14 Sep", 9, 14),
        ("due 9/30", 9, 30), ("2026-10-01", 10, 1), ("deadline October 2", 10, 2),
    ])
    def test_absolute_dates(self, text, month, day):
        when = _local(find_due(text, ref=REF))
        assert (when.month, when.day) == (month, day)
        assert when.year == 2026

    def test_month_day_without_year_rolls_into_next_year_when_long_past(self):
        assert _local(find_due("renewal on Jan 5", ref=REF)).year == 2027

    def test_clock_time_alone_is_today_or_tomorrow(self):
        later = _local(find_due("at 11pm", ref=REF))
        assert later.date() in (TODAY, _day(1)) and later.hour == 23
        d = find_due("meet at 3pm", ref=REF)
        assert d is not None and d.precise

    def test_in_hours_and_minutes_are_precise(self):
        d = find_due("in 2 hours", ref=REF)
        assert d is not None and d.precise
        assert d.when == REF + timedelta(hours=2)
        assert find_due("in 45 minutes", ref=REF).when == REF + timedelta(minutes=45)

    @pytest.mark.parametrize("text", [
        "last Friday we shipped", "since Monday", "no dates here", "the 8.0 release", "version 2/3 of the plan",
    ])
    def test_history_and_non_dates_give_nothing(self, text):
        assert find_due(text, ref=REF) is None

    def test_slash_dates_must_be_plausible(self):
        assert find_due("ratio 13/45", ref=REF) is None

    def test_phrases_are_reported_for_cleaning(self):
        d = find_due("please send the numbers by EOD Friday", ref=REF)
        assert d.text == "EOD Friday" and d.phrases == ("EOD Friday",)
        d = find_due("tomorrow morning works", ref=REF)
        assert d.phrases == ("tomorrow morning",)

    def test_weekend_end_of_week_means_next_friday(self):
        saturday = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        when = _local(find_due("by end of week", ref=saturday))
        assert when.weekday() == 4 and when.date() > saturday.astimezone().date()

    def test_same_weekday_later_in_the_day_means_next_week(self):
        wednesday_evening = LOCAL_REF.replace(hour=20, minute=0)
        when = _local(find_due("by Wednesday", ref=wednesday_evening))
        assert when.date() == TODAY + timedelta(days=7)


def _extract(text, sender="alice", channel="#eng", names=("robin",), bot=False):
    return extract(text, sender=sender, ts=REF, channel=channel, self_names=names, is_bot=bot)


def _one(text, **kw):
    found = _extract(text, **kw)
    assert len(found) == 1, found
    return found[0]


class TestPromises:
    def test_dated_promise(self):
        c = _one("I'll send the deck by Friday.")
        assert c.kind == "promise" and c.who == "alice" and c.what == "send the deck"
        assert _local(c).date() == _day(2) and c.confidence >= 0.8 and not c.for_you

    def test_your_own_promise_is_for_you(self):
        c = _one("I'll take a look at the sandbox findings this afternoon.", sender="robin")
        assert c.kind == "promise" and c.for_you and c.what == "take a look at the sandbox findings"

    def test_soft_promise_without_date_has_low_confidence(self):
        c = _one("Let me check with legal and get back to you.")
        assert c.kind == "promise" and c.confidence < 0.5 and c.due is None

    def test_weak_undated_promises_are_skipped(self):
        assert _extract("I'll be there in spirit") == []
        assert _extract("I'll think about it") == []

    def test_we_will_counts(self):
        c = _one("We will ship v2 next week.")
        assert c.kind == "promise" and c.what == "ship v2"

    def test_out_of_office_is_an_event_not_a_promise(self):
        c = _one("I'll be out Thursday and Friday.", sender="dave")
        assert c.kind == "event" and c.what == "dave out Thursday and Friday" and _local(c).date() == _day(1)

    def test_questions_about_oneself_are_not_promises(self):
        assert _extract("Will I need to send the report by Friday?") == []

    def test_bots_do_not_promise(self):
        assert _extract("I'll retry the job in 5 minutes", sender="ci-bot", bot=True) == []


class TestAsks:
    def test_direct_ask_naming_you_is_for_you(self):
        c = _one("@robin could you sign off on the budget by Thursday?", sender="jane")
        assert c.kind == "ask" and c.for_you and c.who == "jane"
        assert c.what == "sign off on the budget" and _local(c).date() == _day(1)

    def test_ask_in_a_dm_is_for_you(self):
        c = _one("could you send me the numbers by EOD?", channel="@alice")
        assert c.kind == "ask" and c.for_you and c.what == "send me the numbers"

    def test_ask_in_a_channel_not_naming_you_is_not_for_you(self):
        c = _one("Can you review the PR by tomorrow?")
        assert c.kind == "ask" and not c.for_you and not c.open_call

    def test_open_call(self):
        c = _one("Can someone take the on-call swap next week?", sender="carol")
        assert c.kind == "ask" and c.open_call and not c.for_you and c.what == "take the on-call swap"

    def test_your_own_ask_means_you_are_waiting(self):
        c = _one("can you send me the numbers by tomorrow?", sender="robin")
        assert c.kind == "ask" and c.who == "robin" and not c.for_you

    def test_reminders_and_action_items(self):
        c = _one("Reminder: submit timesheets by EOD Friday.", sender="bob")
        assert c.kind == "ask" and c.what == "submit timesheets" and _local(c).date() == _day(2)
        c = _one("Action item: update the runbook", sender="bob")
        assert c.kind == "ask" and c.what == "update the runbook"

    def test_review_requests_keep_urls_intact(self):
        c = _one("PTAL https://example.com/pr/12", sender="hank")
        assert c.kind == "ask" and c.what == "https://example.com/pr/12"

    def test_rhetorical_can_you_is_ignored(self):
        assert _extract("can you believe it's Wednesday already", sender="frank") == []

    def test_please_join_is_the_event_not_an_ask(self):
        c = _one("Demo with the customer on Tuesday at 3pm, please join.", sender="erin")
        assert c.kind == "event" and c.what.startswith("Demo with the customer") and _local(c).hour == 15

    def test_bot_asks_only_count_when_aimed_at_you(self):
        assert _extract("please review the failing checks", sender="ci-bot", bot=True) == []
        c = _one("@robin please review the failing checks", sender="ci-bot", bot=True)
        assert c.kind == "ask" and c.for_you

    def test_a_promise_sentence_is_not_also_an_ask(self):
        found = _extract("I'll take a look at the sandbox findings this afternoon.", sender="robin")
        assert [c.kind for c in found] == ["promise"]

    def test_several_things_in_one_message(self):
        found = _extract("Hey can you review the PR by tomorrow? Also I'll send the deck Friday.")
        kinds = sorted(c.kind for c in found)
        assert kinds == ["ask", "promise"]


class TestDeadlinesAndEvents:
    def test_expiry_from_a_bot_is_a_deadline(self):
        c = _one("Certificate for api.example.com expires on Sep 20.", sender="scanner", bot=True)
        assert c.kind == "deadline" and c.confidence >= 0.6 and _local(c).month == 9 and _local(c).day == 20
        assert c.what == "Certificate for api.example.com expires"

    def test_event_with_time(self):
        c = _one("Team offsite kickoff Thursday at 9am", sender="pm")
        assert c.kind == "event" and _local(c).hour == 9 and _local(c).date() == _day(1)

    def test_dated_sentence_without_any_cue_is_ignored(self):
        assert _extract("Friday was wild", sender="bob") == []

    def test_deadline_naming_you_is_for_you(self):
        c = _one("@robin deadline for the SOC2 evidence is Sep 30", sender="pm")
        assert c.kind == "deadline" and c.for_you


class TestHelpers:
    def test_ids_are_stable_and_normalised(self):
        a = cm.Commitment("promise", "Alice", "Send the  deck!", None, "", 0.5, False)
        b = cm.Commitment("promise", "alice", "send the deck", None, "", 0.9, True)
        assert a.id == b.id and len(a.id) == 20

    def test_mentions_you_and_is_you(self):
        assert cm.mentions_you("hey @Robin can you", ["robin"])
        assert cm.mentions_you("robin: ping", ["robin"])
        assert not cm.mentions_you("robinson crusoe", ["robin"])
        assert not cm.mentions_you("anything", ["ab"])           # too short to be safe
        assert cm.is_you("You", []) and cm.is_you("Robin", ["robin"]) and not cm.is_you("bob", ["robin"])

    def test_looks_completed_needs_a_done_word_and_overlap(self):
        assert cm.looks_completed("done — sandbox findings reviewed", "take a look at the sandbox findings")
        assert not cm.looks_completed("sandbox findings look scary", "take a look at the sandbox findings")
        assert not cm.looks_completed("done!", "take a look at the sandbox findings")

    def test_clean_what(self):
        assert cm._clean_what("please send the numbers by EOD Friday", ["EOD Friday"]) == "send the numbers"
        assert cm._clean_what("Demo with the customer on Tuesday at 3pm , please join", ["Tuesday at 3pm"]) == \
            "Demo with the customer, please join"
