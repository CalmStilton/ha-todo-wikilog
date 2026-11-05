import json
from zoneinfo import ZoneInfo

import pytest

import wikilog as w

LONDON = ZoneInfo("Europe/London")
REF = r"\bR\d{6,}\b"

# Trimmed from the live Sage Oracle page (BookStack page 55).
PAGE = """<p id="bkmrk-intro">Intro &amp; stuff</p>
<h2 id="bkmrk-maintenance-log">Maintenance log</h2>
<table id="bkmrk-maintenance-log-table">
<thead>
<tr><th>Date</th><th>What was done</th></tr>
</thead>
<tbody>
<tr><td>23 July 2026</td><td>Group head clean</td></tr>
<tr><td>2 August 2026</td><td>Group head backflush</td></tr>
<tr><td>5 November 2026</td><td>Group head clean cycle (cleaning tablet/powder)</td></tr>
</tbody>
</table>
<h2 id="bkmrk-repairs-log">Faults and repairs</h2>
<table id="bkmrk-repairs-log-table">
<thead>
<tr><th>Resolved</th><th>Issue</th><th>Sage ticket</th></tr>
</thead>
<tbody>
<tr><td>26 August 2026</td><td>Washer split on the milk steamer</td><td>R105002559</td></tr>
</tbody>
</table>
<p>Outro</p>"""


def item(uid, summary, completed, description=None):
    i = {"uid": uid, "summary": summary, "status": "completed", "completed": completed}
    if description is not None:
        i["description"] = description
    return i


# --- config ---

def test_parse_logs():
    t = w.parse_logs(" todo.a_b=55#bkmrk-x , todo.c=7#y,")
    assert t == [w.Target("todo.a_b", 55, "bkmrk-x"), w.Target("todo.c", 7, "y")]


@pytest.mark.parametrize("spec", ["", "todo.a=55", "light.a=55#x", "todo.a=x#y"])
def test_parse_logs_rejects(spec):
    with pytest.raises(w.WikilogError):
        w.parse_logs(spec)


# --- formatting ---

def test_display_date_uses_uk_time():
    # 23:30 UTC in summer is already the next day in London...
    assert w.display_date("2026-07-23T23:30:00+00:00", LONDON) == "24 July 2026"
    # ...but not in winter.
    assert w.display_date("2026-11-05T23:30:00+00:00", LONDON) == "5 November 2026"
    assert w.display_date("2026-11-05T18:54:04.251082+00:00", LONDON) == "5 November 2026"


@pytest.mark.parametrize("raw,expected", [
    ("Group Head Clean Cycle (cleaning tablet/powder)",
     "Group head clean cycle (cleaning tablet/powder)"),
    ("Water Filter Change", "Water filter change"),
    ("Descale", "Descale"),
    ("Cracked pf handle", "Cracked pf handle"),
    ("Cap doesn't come off the body", "Cap doesn't come off the body"),
    ("Replace PF Gasket", "Replace PF Gasket"),        # acronym: leave alone
    ("Call Sage (Ticket R1)", "Call sage (Ticket R1)"),  # parens untouched
    ("  Steam   Wand Clean ", "Steam wand clean"),
])
def test_tidy_summary(raw, expected):
    assert w.tidy_summary(raw) == expected


@pytest.mark.parametrize("desc,expected", [
    ("Sage ticket: R105002559", "R105002559"),
    ("Sage Ticket R105217488", "R105217488"),
    ("Spoke to Dave", "Spoke to Dave"),
    (None, ""),
    ("", ""),
])
def test_reference(desc, expected):
    assert w.reference(desc, REF) == expected


# --- table editing ---

def test_adds_row_in_date_order_and_leaves_rest_of_page_alone():
    new, added, dupes = w.add_items(
        PAGE, "bkmrk-maintenance-log-table",
        [item("u1", "Descale", "2026-09-16T12:06:44+00:00")], LONDON, REF)
    assert [a["uid"] for a in added] == ["u1"] and dupes == []
    assert ("<tr><td>2 August 2026</td><td>Group head backflush</td></tr>\n"
            "<tr><td>16 September 2026</td><td>Descale</td></tr>"
            "<tr><td>5 November 2026</td>") in new
    assert new.replace("<tr><td>16 September 2026</td><td>Descale</td></tr>", "") == PAGE


def test_newest_row_goes_at_the_bottom():
    new, _, _ = w.add_items(
        PAGE, "bkmrk-maintenance-log-table",
        [item("u1", "Descale", "2026-11-06T10:00:00+00:00")], LONDON, REF)
    assert "cleaning tablet/powder)</td></tr>\n<tr><td>6 November 2026</td><td>Descale</td></tr></tbody>" in new


def test_three_column_table_gets_ticket():
    new, added, _ = w.add_items(
        PAGE, "bkmrk-repairs-log-table",
        [item("u2", "Cracked pf handle", "2026-09-21T21:07:02+00:00",
              "Sage Ticket R105217488")], LONDON, REF)
    assert "<tr><td>21 September 2026</td><td>Cracked pf handle</td><td>R105217488</td></tr>" in new
    # The other table is untouched.
    assert new.count("<tr>") == PAGE.count("<tr>") + 1


def test_row_already_on_page_is_not_added_twice():
    new, added, dupes = w.add_items(
        PAGE, "bkmrk-maintenance-log-table",
        [item("u3", "Group Head Clean", "2026-07-23T14:54:31+00:00")], LONDON, REF)
    assert new == PAGE and added == [] and [d["uid"] for d in dupes] == ["u3"]


def test_html_in_summary_is_escaped():
    new, _, _ = w.add_items(
        PAGE, "bkmrk-maintenance-log-table",
        [item("u4", "Clean <script> & polish", "2026-11-06T10:00:00+00:00")], LONDON, REF)
    assert "<td>Clean &lt;script&gt; &amp; polish</td>" in new


def test_missing_table_is_an_error():
    with pytest.raises(w.WikilogError, match="not found"):
        w.add_items(PAGE, "nope", [item("u", "x", "2026-01-01T00:00:00+00:00")], LONDON, REF)


def test_table_rebuilt_by_editor_without_thead():
    page = ('<table id="t" style="border-collapse: collapse;"><colgroup><col></colgroup>'
            '<tbody><tr><th>Date</th><th>What</th></tr>'
            '<tr><td style="x">1 July 2026</td><td><strong>A</strong></td></tr></tbody></table>')
    new, added, _ = w.add_items(page, "t", [item("u", "B", "2026-06-01T09:00:00+00:00")], LONDON, REF)
    assert len(added) == 1
    assert "<tr><th>Date</th><th>What</th></tr><tr><td>1 June 2026</td><td>B</td></tr><tr><td style" in new


def test_empty_table():
    page = '<table id="t"><thead><tr><th>Date</th><th>What</th></tr></thead><tbody></tbody></table>'
    new, _, _ = w.add_items(page, "t", [item("u", "B", "2026-06-01T09:00:00+00:00")], LONDON, REF)
    assert "<tbody><tr><td>1 June 2026</td><td>B</td></tr></tbody>" in new


# --- the run ---

class FakeHA:
    def __init__(self, lists):
        self.lists = lists
        self.notes = []

    def completed_items(self, entity_id):
        return list(self.lists.get(entity_id, []))

    def notify(self, message):
        self.notes.append(message)


class FakeWiki:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.puts = 0

    def get_html(self, page_id):
        return self.pages[page_id]

    def put_html(self, page_id, content):
        self.puts += 1
        self.pages[page_id] = content


TARGETS = [w.Target("todo.m", 55, "bkmrk-maintenance-log-table"),
           w.Target("todo.i", 55, "bkmrk-repairs-log-table")]


def test_run_refuses_without_baseline(tmp_path):
    with pytest.raises(w.WikilogError, match="baseline"):
        w.run(TARGETS, FakeHA({}), FakeWiki({55: PAGE}), str(tmp_path / "s.json"), LONDON, REF)


def test_baseline_then_only_new_items_are_added(tmp_path):
    state = str(tmp_path / "s.json")
    old = item("old", "Engagement ring", "2026-09-21T21:07:04+00:00")
    ha = FakeHA({"todo.i": [old]})
    w.baseline(TARGETS, ha, state)
    assert json.load(open(state))["seen"] == {"old": old["completed"]}

    wiki = FakeWiki({55: PAGE})
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF) == 0
    assert wiki.puts == 0  # nothing new: don't even touch the page

    ha.lists["todo.m"] = [item("new", "Descale", "2026-11-05T20:00:00+00:00")]
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF) == 1
    assert wiki.puts == 1
    assert "<td>Descale</td>" in wiki.pages[55]
    assert "Engagement ring" not in wiki.pages[55]

    # Next run: already seen, so no change even if the row is edited by hand.
    wiki.pages[55] = wiki.pages[55].replace("5 November 2026</td><td>Descale",
                                            "4 November 2026</td><td>Descale")
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF) == 0
    assert wiki.pages[55].count("Descale") == 1


def test_items_on_two_lists_same_page_are_one_write(tmp_path):
    state = str(tmp_path / "s.json")
    w.save_state(state, {})
    ha = FakeHA({"todo.m": [item("a", "Descale", "2026-11-06T10:00:00+00:00")],
                 "todo.i": [item("b", "Leak", "2026-11-06T11:00:00+00:00", "R123456789")]})
    wiki = FakeWiki({55: PAGE})
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF) == 2
    assert wiki.puts == 1


def test_failed_write_does_not_mark_seen(tmp_path):
    state = str(tmp_path / "s.json")
    w.save_state(state, {})

    class Broken(FakeWiki):
        def put_html(self, page_id, content):
            raise RuntimeError("boom")

    ha = FakeHA({"todo.m": [item("a", "Descale", "2026-11-06T10:00:00+00:00")]})
    with pytest.raises(RuntimeError):
        w.run(TARGETS, ha, Broken({55: PAGE}), state, LONDON, REF)
    assert w.load_state(state) == {}


def test_dry_run_writes_nothing(tmp_path):
    state = str(tmp_path / "s.json")
    ha = FakeHA({"todo.m": [item("a", "Descale", "2026-11-06T10:00:00+00:00")]})
    wiki = FakeWiki({55: PAGE})
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF, dry_run=True) == 1
    assert wiki.puts == 0 and not (tmp_path / "s.json").exists()


def test_main_notifies_home_assistant_on_failure(monkeypatch, tmp_path):
    ha = FakeHA({})
    monkeypatch.setattr(w, "HomeAssistant", lambda url, token: ha)
    monkeypatch.setenv("HA_URL", "http://ha")
    monkeypatch.setenv("HA_TOKEN", "t")
    monkeypatch.setenv("LOGS", "todo.m=55#x")
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("BOOKSTACK_URL", "http://wiki")
    monkeypatch.setenv("BOOKSTACK_TOKEN_ID", "i")
    monkeypatch.setenv("BOOKSTACK_TOKEN_SECRET", "s")
    assert w.main(["run"]) == 1
    assert ha.notes and "baseline" in ha.notes[0]


def test_baseline_before_leaves_later_items_for_the_run(tmp_path):
    from datetime import datetime
    state = str(tmp_path / "s.json")
    ha = FakeHA({"todo.m": [item("early", "Group Head Clean", "2026-07-23T14:54:31+00:00"),
                            item("late", "Descale", "2026-11-05T20:00:00+00:00")]})
    w.baseline(TARGETS, ha, state, before=datetime.fromisoformat("2026-11-05T19:16:00Z"))
    assert set(w.load_state(state)) == {"early"}
    wiki = FakeWiki({55: PAGE})
    assert w.run(TARGETS, ha, wiki, state, LONDON, REF) == 1
    assert "<td>5 November 2026</td><td>Descale</td>" in wiki.pages[55]


def test_before_needs_a_timezone():
    with pytest.raises(SystemExit):
        w.main(["baseline", "--before", "2026-11-05T19:16:00"])


def test_baseline_before_env_bootstraps_first_run(monkeypatch, tmp_path):
    ha = FakeHA({"todo.m": [item("early", "Group Head Clean", "2026-07-23T14:54:31+00:00"),
                            item("late", "Descale", "2026-11-05T20:00:00+00:00")]})
    wiki = FakeWiki({55: PAGE})
    monkeypatch.setattr(w, "HomeAssistant", lambda url, token: ha)
    monkeypatch.setattr(w, "BookStack", lambda *a: wiki)
    for k, v in {"HA_URL": "http://ha", "HA_TOKEN": "t", "BOOKSTACK_URL": "http://wiki",
                 "BOOKSTACK_TOKEN_ID": "i", "BOOKSTACK_TOKEN_SECRET": "s",
                 "LOGS": "todo.m=55#bkmrk-maintenance-log-table",
                 "STATE_FILE": str(tmp_path / "s.json"),
                 "BASELINE_BEFORE": "2026-11-05T19:16:00Z"}.items():
        monkeypatch.setenv(k, v)
    assert w.main(["run"]) == 0
    assert wiki.pages[55].count("Descale") == 1 and wiki.puts == 1
    assert w.main(["run"]) == 0 and wiki.puts == 1  # second run: nothing new
