"""
The briefing proves its relevance: every card shows *why it is for you*
(evidence chips from the data), the model's one sentence in your terms, and a
visual weight that matches its urgency — on the web page, in the terminal and
in the banner alike.
"""
from __future__ import annotations

from otto.core.notify import notification_lead
from otto.web.render import (
    for_you_line, headline, is_opportunity, item_reasons, render_body, render_item, render_page,
    urgency_level,
)

ASKED = {"kind": "asked", "label": "Asked of you", "tone": "you", "detail": "review the runner PR · due Fri"}
DIRECTIVE = {"kind": "directive", "label": "Your directive", "tone": "directive", "detail": "always review payments migration bugs"}
SEVERITY = {"kind": "severity", "label": "4 critical · 2 high", "tone": "alert", "detail": "severity stated in the message"}
OPP = {"kind": "opportunity", "label": "Opportunity · collaboration", "tone": "opportunity", "detail": "a new teammate offering to pair"}
OWN = {"kind": "own", "label": "Your message", "tone": "quiet", "detail": "you replied 40 min ago"}


def _item(**kw):
    base = {"id": "id1", "text": "Sam can you review the runner image PR by Friday? It blocks the release",
            "sender": "alice", "source_url": "https://acme.slack.com/archives/C1/p1", "urgency": "high",
            "urgency_score": 0.8, "time_display": "5m ago", "summary": "", "ai_analysis": "", "relevance": "",
            "action_items": [], "topics": [], "why": [], "for_you": ""}
    base.update(kw)
    return base


def _data(*items, channel="#eng"):
    return {"generated_at_human": "Today at 10:00 AM", "ai_powered": True,
            "sections": [{"source": "slack", "title": "Slack", "channels": [{"name": channel, "items": list(items)}]}]}


class TestCardEvidence:
    def test_chips_sit_under_the_title_with_their_evidence_as_tooltips(self):
        html = render_item(_item(why=[ASKED, DIRECTIVE, SEVERITY]))
        assert html.index('class="title"') < html.index('class="why"') < html.index('class="insight"')
        assert '<span class="wy wy-you" title="review the runner PR · due Fri">Asked of you</span>' in html
        assert 'class="wy wy-directive"' in html and 'class="wy wy-alert"' in html
        # …and the expanded card spells the same reasons out.
        assert "Why this is for you" in html and 'class="wy-k">Your directive</span>' in html
        assert 'class="wy-d">always review payments migration bugs</span>' in html

    def test_no_reasons_means_no_empty_chip_row(self):
        html = render_item(_item(why=[]))
        assert 'class="why"' not in html and "Why this is for you" not in html

    def test_malformed_reasons_are_ignored_and_escaped(self):
        html = render_item(_item(why=["junk", {"label": ""}, {"kind": "x", "label": "<b>bad</b>", "tone": "evil"}]))
        assert "<b>bad</b>" not in html and "&lt;b&gt;bad&lt;/b&gt;" in html
        assert 'wy-evil' not in html and 'wy-quiet' in html          # unknown tone falls back to quiet
        assert item_reasons({"why": None}) == []

    def test_for_you_sentence_beats_the_local_insight(self):
        item = _item(why=[ASKED], for_you="Alice is waiting on your review before Friday; it blocks the release.",
                     matched_directive="always review payments migration bugs")
        assert for_you_line(item) == "Alice is waiting on your review before Friday; it blocks the release."
        html = render_item(item)
        assert "Alice is waiting on your review" in html
        # Without the model's sentence the local insight steps in — minus what the chips already say.
        item["for_you"] = ""
        line = for_you_line(item)
        assert "From alice" not in line and "Relates to" not in line
        assert "Friday" in line                          # deadline awareness survives

    def test_urgency_shows_as_border_and_label(self):
        crit = render_item(_item(urgency="critical", urgency_score=0.95))
        assert 'class="card clickable urg-critical"' in crit and '<span class="lvl lvl-critical">Critical</span>' in crit
        high = render_item(_item(urgency="high", urgency_score=0.8))
        assert "urg-high" in high and ">High</span>" in high
        low = render_item(_item(urgency="low", urgency_score=0.2))
        assert "urg-" not in low and 'class="lvl' not in low
        assert urgency_level({"_urgency_score": 0.9}) == "critical" and urgency_level({"urgency": "medium"}) == ""


class TestTitles:
    def test_long_summaries_are_cut_before_an_elaboration_not_mid_word(self):
        from otto.web.render import item_title
        long_text = "Scanner complete — sandbox main " + "details " * 30
        summary = ("A security scan of the 'sandbox' project's 'main' branch has completed, identifying four critical and "
                   "two high severity vulnerabilities, including Remote Code Execution and Insecure Deserialization.")
        title = item_title({"text": long_text, "summary": summary, "action_items": ["Review the full report notebook."]})
        assert title == ("A security scan of the 'sandbox' project's 'main' branch has completed, identifying four critical "
                         "and two high severity vulnerabilities")
        # a comma-separated list is not a clause: keep the ellipsis
        listy = "A scan found 11 high severity findings including " + ", ".join(f"issue {i}" for i in range(30))
        t2 = item_title({"text": long_text, "summary": listy, "action_items": []})
        assert t2.endswith("…") and len(t2) <= 140


class TestPageStructure:
    def test_header_tallies_the_evidence(self):
        data = _data(
            _item(id="a", why=[ASKED, DIRECTIVE]),
            _item(id="b", urgency="medium", urgency_score=0.5, why=[{"kind": "reply", "label": "New reply in your thread", "tone": "you"}]),
        )
        parts = render_body(data)
        assert parts["why"] == "1 asks you for something · 1 new reply in a thread of yours · 1 touches a directive of yours"
        page = render_page(data)
        assert '<span class="s2" id="why">1 asks you for something' in page      # inline after the headline
        assert "$('#why')" in page                      # in-place refresh swaps it too

    def test_opportunities_are_not_lost_in_the_fold(self):
        opp = _item(id="o", urgency="low", urgency_score=0.2, text="hi all, joining the platform team, happy to pair on infra",
                    why=[OPP], opportunity_score=0.6)
        quiet = _item(id="q", urgency="low", urgency_score=0.1, text="test notification, integration is working", why=[])
        parts = render_body(_data(_item(id="a", why=[ASKED]), opp, quiet))
        main = parts["main"]
        assert main.index("Needs you") < main.index("Worth a look") < main.index("Also noticed · 1")
        assert 'class="wy wy-opportunity"' in main
        assert is_opportunity(opp) and not is_opportunity(quiet)
        assert is_opportunity({"opportunity_score": "0.7"}) and not is_opportunity({"opportunity_score": "n/a"})
        # The model may call your own message an opportunity; it still is not a find *for you*.
        assert not is_opportunity({"opportunity_score": 0.9, "why": [OWN]})

    def test_headline_counts_what_is_worth_a_look(self):
        items = [dict(_item(id="a", why=[ASKED]), _urgency_score=0.8),
                 dict(_item(id="o", why=[OPP], opportunity_score=0.6), _urgency_score=0.2)]
        assert headline(items) == "1 item needing your attention · 1 worth a look"
        assert headline([items[1]]) == "Nothing urgent · 1 worth a look"

    def test_folded_rows_say_why_they_are_folded(self):
        mine = _item(id="m", urgency="low", urgency_score=0.2, text="build failed twice this morning, could be upstream", sender="Sam", why=[OWN])
        parts = render_body(_data(_item(id="a", why=[ASKED]), mine))
        assert '<span class="lp-why">Your message</span>' in parts["main"]


class TestOtherSurfaces:
    def test_banner_leads_with_the_strongest_reasons(self):
        assert notification_lead(_item(why=[ASKED, SEVERITY, DIRECTIVE])) == "Asked of you · 4 critical · 2 high"
        assert notification_lead(_item(why=[])) == ""
        assert notification_lead({"why": ["junk", {"label": "Ok"}]}) == "Ok"

    def test_banner_body_is_reason_then_sentence(self):
        from datetime import datetime, time as dtime, timezone
        from otto.core.notify import NotificationPolicy, Notifier
        n = Notifier(NotificationPolicy(quiet_hours_start=dtime(23, 0), quiet_hours_end=dtime(7, 0), max_per_hour=5),
                     fallback_osascript=False)
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        data = _data(_item(id="a", urgency_score=0.85, action_items=["Review the PR"], why=[ASKED, DIRECTIVE],
                           for_you="Alice needs your review before Friday."))
        assert n.process_briefing(data, noon) == 1
        (banner,) = n.pending()
        assert banner["body"] == "Asked of you · Your directive — Alice needs your review before Friday."
        # Without evidence or a sentence the banner falls back to the summary/text as before.
        plain = _data(_item(id="b", urgency_score=0.85, action_items=["Decide"], summary="Prod is down, need a decision on rollback"))
        assert n.process_briefing(plain, noon) == 1
        assert n.pending()[-1]["body"] == "Prod is down, need a decision on rollback"


class TestPageIsThePanel:
    """The browser page is the menu bar panel laid out for a window: same header,
    same rows, same footer — and each fact once."""

    RADAR = {"todo": [], "waiting": [], "open_calls": [], "upcoming": [], "patterns": [],
             "attention": ["#ops is busier than usual: 27 messages this week vs 3 last week."],
             "memory": {"messages": 40, "channels": 2, "people": 3, "days": 4}}

    def test_header_row_and_footer_bar(self):
        page = render_page(_data(_item(id="a", why=[ASKED]), _item(id="b", urgency="medium", urgency_score=0.5)))
        assert '<span class="ring-n" id="ring-n">1</span>' in page           # the menu bar ring, with what needs you
        assert "<h1>Briefings</h1>" in page and 'id="live">Updated 10:00 AM<' in page
        assert '<span id="ft-count">2 items · 1 needs you</span>' in page
        assert 'data-action="clear" title="Dismiss everything on the briefing (one click; items stay in Otto\'s memory)">Clear<' in page
        # Nothing worth knowing → its button is hidden and the drawer's own Clear is greyed.
        assert 'id="wk-toggle" aria-expanded="false" aria-controls="wk" title="Heads-ups, what\'s likely next, your radar, and anything about Otto itself — none of it a notification." hidden>Worth knowing · 0<' in page
        assert 'data-action="clear-notes" title="Dismiss every note and radar row here (one click; nothing is deleted from Otto\'s memory)" disabled>Clear<' in page
        # the row is laid out like a panel row: accent bar, text, side column
        assert '<span class="bar" aria-hidden="true"></span>' in page and '<div class="side">' in page
        assert 'class="src">Slack</span><span class="ch">#eng</span><span class="who">alice</span>' in page
        assert "Refreshing…" in page                                            # ↻ and the footer share one handler

    def test_partial_carries_everything_the_page_swaps(self):
        parts = render_body(_data(_item(id="a", why=[ASKED])))
        assert set(parts) >= {"summary", "why", "meta", "digest", "problems", "main", "count", "important", "footer", "updated"}
        assert parts["important"] == "1" and parts["footer"] == "1 item · 1 needs you" and parts["updated"] == "Updated 10:00 AM"

    def test_heads_ups_are_not_repeated_by_the_radar(self):
        data = _data(_item(id="a", why=[ASKED]))
        data["radar"] = dict(self.RADAR)
        data["digest"] = {"source": "model", "items": 1, "digest": "One review is waiting on you.",
                          "connections": [], "predictions": [{"note": "Alice will ask again", "basis": "asked twice"}],
                          "heads_up": ["#ops is much busier than usual this week"]}
        parts = render_body(data)
        # The digest block keeps the sentence; ▲ and ◇ are not notifications and
        # go to the Worth knowing drawer — each once, each with an id to dismiss by.
        assert parts["digest"] == '<div class="dg"><p class="dg-text">One review is waiting on you.</p></div>'
        assert parts["wk"].count('class="rr note n-head"') == 1 and 'class="rr note n-pred"' in parts["wk"]
        assert 'title="asked twice"' in parts["wk"] and 'data-id="note:' in parts["wk"]
        assert "27 messages this week" not in parts["wk"]                      # the radar's raw note is not said twice
        assert 'class="group radar"' not in parts["wk"] and parts["wk_count"] == "2"   # no rows left → no radar block
        assert "27 messages" not in parts["main"] and "cert expires" not in parts["main"]
        data["radar"]["upcoming"] = [{"id": "radar:1", "kind": "deadline", "what": "cert expires", "channel": "#ops", "due_label": "Sep 20"}]
        parts = render_body(data)
        assert "cert expires" in parts["wk"] and "From memory · 40 messages" in parts["wk"] and parts["wk_count"] == "3"
        assert "cert expires" not in parts["main"]
        # No digest → the radar's own notes are the heads-ups.
        data.pop("digest")
        parts = render_body(data)
        assert "27 messages this week" in parts["wk"] and 'class="rr note n-quiet"' in parts["wk"]

    def test_worth_knowing_clears_on_its_own(self):
        from otto.web.render import digest_notes, note_id, worth_knowing_ids
        data = _data(_item(id="a", why=[ASKED]))
        data["radar"] = dict(self.RADAR)
        data["radar"]["upcoming"] = [{"id": "radar:1", "kind": "deadline", "what": "cert expires", "channel": "#ops", "due_label": "Sep 20"}]
        data["digest"] = {"source": "model", "items": 1, "digest": "x", "connections": [],
                          "predictions": [{"note": "Alice will ask again"}], "heads_up": ["#ops is much busier than usual this week"]}
        ids = worth_knowing_ids(data)
        assert ids == {note_id("#ops is much busier than usual this week"), note_id("Alice will ask again"), "radar:1"}
        assert note_id("  Alice   will ask AGAIN ") == note_id("Alice will ask again")           # whitespace/case-proof
        # Dismissing every id empties the drawer, leaves the items alone, and the payload agrees.
        parts = render_body(data, hidden=ids)
        assert parts["wk"] == '<p class="wk-empty">Nothing more to know right now.</p>' and parts["wk_count"] == "0"
        assert parts["count"] == "1" and digest_notes(data, ids) == []
        from otto.web.items import build_items
        payload = build_items(data, hidden=ids)
        assert payload["worth_knowing"]["count"] == 0 and payload["worth_knowing"]["notes"] == []
        assert payload["digest"]["heads_up"] == [] and payload["digest"]["predictions"] == []
        payload = build_items(data)
        assert payload["worth_knowing"]["count"] == 3 and [n["kind"] for n in payload["worth_knowing"]["notes"]] == ["heads_up", "prediction"]
        assert payload["worth_knowing"]["label"] == "Worth knowing"

    def test_connected_notes_flow_inline_with_numbered_jumps(self):
        data = _data(_item(id="a", why=[ASKED]), _item(id="b", urgency="medium", urgency_score=0.5))
        data["digest"] = {"source": "model", "items": 2, "digest": "", "predictions": [], "heads_up": [],
                          "connections": [{"ids": ["a", "b", "zzz"], "note": "Both are about the runner image"}]}
        dg = render_body(data)["digest"]
        assert ('<li class="n-conn" title="Connected"><span class="ng">⇄</span><span class="nt">Both are about the runner image '
                '<button type="button" class="dg-ref" data-goto="a" title="Show this item">1</button>'
                '<button type="button" class="dg-ref" data-goto="b" title="Show this item">2</button></span></li>') in dg
        assert "Connected</span>" not in dg                                    # glyphs, not uppercase keys

    def test_quiet_state_is_the_panels(self):
        data = {"generated_at_human": "Today at 10:00 AM", "sections": [], "radar": dict(self.RADAR),
                "source_status": [{"source": "slack", "ok": True, "error": "", "items": 0}],
                "digest": {"source": "local", "items": 0, "digest": "", "connections": [],
                           "predictions": [{"note": "The scan should finish by noon"}], "heads_up": ["Bob still owes the licence summary"]}}
        parts = render_body(data)
        assert parts["summary"] == "All clear — just keeping you up to date." and parts["footer"] == "Up to date"
        assert parts["digest"] == ""                                            # no sentence about nothing
        # What is still worth knowing waits in the drawer — the heads-up and the
        # prediction, once each, the radar's raw note not repeated.
        assert ('<div class="rsec wk-notes"><div class="rlabel">Notes <span class="rcount">2</span></div>'
                '<div class="rr note n-head" data-id="note:' in parts["wk"]
                and "Bob still owes the licence summary" in parts["wk"] and "The scan should finish by noon" in parts["wk"])
        assert "#ops is busier" not in parts["wk"] and parts["wk_count"] == "2"
        assert "Bob still owes" not in parts["main"] and "Slack · Connected" in parts["main"] and "empty-title" not in parts["main"]
        page = render_page(data)
        assert 'data-action="clear" title="Dismiss everything on the briefing (one click; items stay in Otto\'s memory)" disabled>' in page
        assert '>Worth knowing · 2</button>' in page and '<section class="wk" id="wk" aria-label="Worth knowing" hidden>' in page
        # A digest written about items since cleared is stale: nothing quiet is built from it,
        # and the radar's own note takes the heads-up's place.
        data["digest"]["items"] = 3
        parts = render_body(data)
        assert parts["digest"] == "" and "27 messages this week" in parts["wk"] and 'class="rr note n-quiet"' in parts["wk"]
        assert "Bob still owes" not in parts["wk"]

    def test_needs_a_look_rows_say_where_the_button_is(self):
        data = _data(_item(id="a", why=[ASKED]))
        data["source_status"] = [{"source": "slack", "ok": False, "error": "needs Accessibility permission (System Settings)", "items": 1}]
        data["screenshots"] = {"enabled": True, "granted": False}
        parts = render_body(data)
        assert parts["problems"].startswith('<div class="rsec problems"><div class="rlabel">About Otto <span class="rcount">2</span></div>'
                                            '<ul class="notes problems"><li class="prob"><span class="ng">⚠︎</span>')
        assert "Slack needs a macOS permission" in parts["problems"] and 'class="prob-h">Fix… in the menu bar</span>' in parts["problems"]
        assert "Thumbnails are off" in parts["problems"]
        page = render_page(data)
        assert "otto permissions" not in page                                  # the page never sends you to a terminal
        # About Otto is not a notification either: it sits at the top of the Worth
        # knowing drawer, counts toward its button, and is not what Clear clears.
        header, rest = page.split('<div class="w" id="main">', 1)
        assert 'id="problems"' not in header and "Thumbnails are off" not in header
        drawer = rest.split('<section class="wk"', 1)[1]
        assert drawer.index('<div id="problems">') < drawer.index('<div id="wk-body">') and "Thumbnails are off" in drawer
        assert parts["wk_count"] == "2" and parts["wk_clearable"] == "0" and parts["wk"] == ""   # nothing to clear, no "nothing more to know" either
        assert 'data-action="clear-notes" title="Dismiss every note and radar row here (one click; nothing is deleted from Otto\'s memory)" disabled>' in page
        assert '>Worth knowing · 2</button>' in page
        assert render_body(_data(_item(id="a")))["problems"] == ""

    def test_short_time(self):
        from otto.web.render import short_time
        assert short_time("Saturday, September 12 2026 · 10:49 AM") == "Updated 10:49 AM"
        assert short_time("Today at 10:00 AM") == "Updated 10:00 AM"
        assert short_time("") == "" and short_time("now") == "now"
