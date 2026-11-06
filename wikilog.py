#!/usr/bin/env python3
"""Copy completed Home Assistant to-do items into tables on BookStack pages.

Each configured to-do list maps to one table on one wiki page. A run asks Home
Assistant for the list's completed items, and adds a row for each item it
hasn't seen before, dated by when the item was ticked off. Rows are only ever
added: clearing completed items in Home Assistant, or editing a row by hand on
the wiki, never removes anything.

Commands:
    wikilog.py baseline   Record every item that's already complete as seen,
                          without touching the wiki. Run once, at deploy.
      --before ISO-TIME   Only items completed before this time; anything
                          later is left for the next run to add.
    wikilog.py run        Add rows for newly completed items (the default).
    --dry-run             Print what would change; write nothing.
"""

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

NOTIFICATION_ID = "ha_todo_wikilog"
TIMEOUT = 20


class WikilogError(Exception):
    """A failure that should stop the run and be reported."""


def log(msg):
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- Configuration -----------------------------------------------------------

@dataclass(frozen=True)
class Target:
    entity_id: str
    page_id: int
    table_id: str


_TARGET_RE = re.compile(r"(todo\.[a-z0-9_]+)=(\d+)#([A-Za-z0-9_.%-]+)")


def parse_logs(spec):
    """Parse LOGS, e.g. 'todo.a=55#table-a,todo.b=55#table-b'."""
    targets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _TARGET_RE.fullmatch(part)
        if not m:
            raise WikilogError(
                f"Bad LOGS entry {part!r}: expected todo.<list>=<page id>#<table id>"
            )
        targets.append(Target(m[1], int(m[2]), m[3]))
    if not targets:
        raise WikilogError("LOGS is empty: nothing to do")
    return targets


# --- Turning a to-do item into a row ----------------------------------------

def display_date(completed_iso, tz):
    """'2026-07-23T14:54:31+00:00' -> '23 July 2026', in the given timezone."""
    dt = datetime.fromisoformat(completed_iso).astimezone(tz)
    return f"{dt.day} {dt:%B %Y}"


def parse_display_date(text):
    try:
        return datetime.strptime(" ".join(text.split()), "%d %B %Y").date()
    except ValueError:
        return None


_PAREN_SPLIT = re.compile(r"(\([^)]*\))")


def tidy_summary(summary):
    """Turn Title Case summaries ('Group Head Clean Cycle (cleaning tablet)')
    into sentence case to match the rest of the wiki. Anything else, and
    anything with an acronym in it, is left exactly as typed."""
    s = " ".join(summary.split())
    parts = _PAREN_SPLIT.split(s)
    outside = [p for i, p in enumerate(parts) if i % 2 == 0]
    words = [w for p in outside for w in p.split() if w[:1].isalpha()]
    if len(words) < 2 or not all(w[0].isupper() for w in words):
        return s
    if any(len(w) > 1 and w.isupper() for w in words):
        return s
    first = True
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
            continue
        tokens = []
        for tok in part.split(" "):
            if tok[:1].isalpha():
                tokens.append(tok if first else tok.lower())
                first = False
            else:
                tokens.append(tok)
        out.append(" ".join(tokens))
    return "".join(out)


def reference(description, pattern):
    """Pull a ticket number out of the item's description; failing that, use
    the description itself."""
    description = " ".join((description or "").split())
    if not description:
        return ""
    m = re.search(pattern, description) if pattern else None
    return m[0] if m else description


def row_cells(item, columns, tz, ref_pattern):
    cells = [display_date(item["completed"], tz), tidy_summary(item["summary"])]
    if columns >= 3:
        cells.append(reference(item.get("description"), ref_pattern))
    cells += [""] * (columns - len(cells))
    return cells


# --- Editing the table in the page's HTML -----------------------------------

_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def cell_text(cell_html):
    return " ".join(html.unescape(_TAG_RE.sub(" ", cell_html)).split())


def _norm(text):
    return " ".join(text.split()).casefold()


def find_table(page_html, table_id):
    """Return (start, end) of the <table> with this id."""
    m = re.search(
        r'<table\b[^>]*\bid="' + re.escape(table_id) + r'"[^>]*>', page_html, re.I
    )
    if not m:
        raise WikilogError(
            f'Table id="{table_id}" not found on the page. If the table was '
            "rebuilt in the editor, give it this id again (Source code view) or "
            "update LOGS."
        )
    end = page_html.lower().find("</table>", m.end())
    if end == -1:
        raise WikilogError(f'Table id="{table_id}" is never closed')
    return m.start(), end + len("</table>")


def _body_span(table_html):
    """(start, end) of the region holding data rows, i.e. inside <tbody>."""
    lower = table_html.lower()
    m = re.search(r"<tbody\b[^>]*>", table_html, re.I)
    if m:
        end = lower.find("</tbody>", m.end())
        if end == -1:
            raise WikilogError("Table has an unclosed <tbody>")
        return m.end(), end
    # No tbody: rows sit directly in the table.
    return table_html.index(">") + 1, lower.rfind("</table>")


def table_shape(table_html):
    """Column count, and the data rows as (start, end, cells) relative to
    table_html."""
    body_start, body_end = _body_span(table_html)
    rows = []
    header_cols = None
    thead = re.search(r"<thead\b.*?</thead>", table_html, re.S | re.I)
    if thead:
        header_row = _ROW_RE.search(thead[0])
        if header_row:
            header_cols = len(_CELL_RE.findall(header_row[0]))
    for m in _ROW_RE.finditer(table_html, body_start, body_end):
        is_header = re.search(r"<th\b", m[0], re.I) and not re.search(r"<td\b", m[0], re.I)
        cells = [cell_text(c) for c in _CELL_RE.findall(m[0])]
        if is_header:
            header_cols = header_cols or len(cells)
            continue
        rows.append((m.start(), m.end(), cells))
    columns = header_cols or (len(rows[0][2]) if rows else 2)
    return columns, rows, body_end


def render_row(cells):
    return "<tr>" + "".join(f"<td>{html.escape(c, quote=False)}</td>" for c in cells) + "</tr>"


def is_duplicate(cells, rows):
    key = (_norm(cells[0]), _norm(cells[1]))
    return any(len(r) >= 2 and (_norm(r[0]), _norm(r[1])) == key for _, _, r in rows)


def insert_row(table_html, cells):
    """Insert a row in date order (oldest first). Rows whose date can't be
    read are left where they are and don't affect placement."""
    _, rows, body_end = table_shape(table_html)
    new_date = parse_display_date(cells[0])
    pos = body_end
    if new_date:
        for start, _, r in rows:
            d = parse_display_date(r[0]) if r else None
            if d and d > new_date:
                pos = start
                break
    return table_html[:pos] + render_row(cells) + table_html[pos:]


def add_items(page_html, table_id, items, tz, ref_pattern):
    """Add rows for items to one table. Returns (new_html, added, duplicates)
    where added and duplicates are lists of items."""
    start, end = find_table(page_html, table_id)
    table = page_html[start:end]
    added, dupes = [], []
    for item in sorted(items, key=lambda i: i["completed"]):
        columns, rows, _ = table_shape(table)
        cells = row_cells(item, columns, tz, ref_pattern)
        if is_duplicate(cells, rows):
            dupes.append(item)
            continue
        table = insert_row(table, cells)
        added.append(item)
    return page_html[:start] + table + page_html[end:], added, dupes


# --- State --------------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise WikilogError(f"Can't read state file {path}: {exc}") from exc
    return data.get("seen", {})


def save_state(path, seen):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": seen}, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --- Clients ------------------------------------------------------------------

def _base(url):
    url = url.strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


class HomeAssistant:
    def __init__(self, url, token):
        self.base = _base(url)
        self.headers = {"Authorization": f"Bearer {token}"}

    def completed_items(self, entity_id):
        resp = requests.post(
            f"{self.base}/api/services/todo/get_items?return_response",
            headers=self.headers,
            json={"entity_id": entity_id, "status": "completed"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json()["service_response"][entity_id]["items"]
        dated = [i for i in items if i.get("completed")]
        if len(dated) < len(items):
            log(f"WARNING: {entity_id}: {len(items) - len(dated)} completed item(s) "
                "have no completion time and were skipped")
        return dated

    def notify(self, message):
        requests.post(
            f"{self.base}/api/services/persistent_notification/create",
            headers=self.headers,
            json={"title": "Wiki maintenance log", "message": message,
                  "notification_id": NOTIFICATION_ID},
            timeout=TIMEOUT,
        ).raise_for_status()


def _check(resp, what):
    """Raise with BookStack's own explanation, which says whether the problem
    is API access as a whole or permission on this particular page."""
    if resp.ok:
        return
    try:
        detail = resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = resp.text[:200].strip() or resp.reason
    raise WikilogError(f"BookStack refused to {what} (HTTP {resp.status_code}): {detail}")


class BookStack:
    def __init__(self, url, token_id, token_secret):
        self.base = _base(url)
        self.headers = {"Authorization": f"Token {token_id}:{token_secret}"}

    def get_html(self, page_id):
        resp = requests.get(f"{self.base}/api/pages/{page_id}",
                            headers=self.headers, timeout=TIMEOUT)
        _check(resp, f"read page {page_id}")
        content = resp.json().get("html") or ""
        if not content:
            raise WikilogError(f"Page {page_id} came back with no HTML")
        return content

    def put_html(self, page_id, content):
        resp = requests.put(f"{self.base}/api/pages/{page_id}", headers=self.headers,
                            json={"html": content}, timeout=TIMEOUT)
        _check(resp, f"update page {page_id}")


# --- The run ------------------------------------------------------------------

def baseline(targets, ha, state_path, dry_run=False, before=None):
    """Mark completed items as already logged. With `before` (an aware
    datetime), only items completed before then: the rest are left for the
    next run to add."""
    seen = load_state(state_path) or {}
    count = 0
    for t in targets:
        for item in ha.completed_items(t.entity_id):
            if before and datetime.fromisoformat(item["completed"]) >= before:
                continue
            if item["uid"] not in seen:
                seen[item["uid"]] = item["completed"]
                count += 1
                log(f"baseline {t.entity_id}: {item['completed']} {item['summary']}")
    if not dry_run:
        save_state(state_path, seen)
    log(f"Baseline recorded {count} item(s) as already logged"
        + (" (dry run, nothing saved)" if dry_run else ""))


def run(targets, ha, wiki, state_path, tz, ref_pattern, dry_run=False):
    seen = load_state(state_path)
    if seen is None:
        if not dry_run:
            raise WikilogError(
                f"No state file at {state_path}. Run 'baseline' once first so "
                "items already on the wiki aren't added twice."
            )
        seen = {}

    by_page = {}
    for t in targets:
        by_page.setdefault(t.page_id, []).append(t)

    total = 0
    for page_id, page_targets in by_page.items():
        pending = {t: [i for i in ha.completed_items(t.entity_id) if i["uid"] not in seen]
                   for t in page_targets}
        if not any(pending.values()):
            continue
        page_html = wiki.get_html(page_id)
        newly_seen = {}
        changed = False
        for t, items in pending.items():
            if not items:
                continue
            page_html, added, dupes = add_items(page_html, t.table_id, items, tz, ref_pattern)
            for item in added:
                log(f"add {t.entity_id} -> page {page_id}: "
                    f"{display_date(item['completed'], tz)} {item['summary']}")
            for item in dupes:
                log(f"already on page {page_id}: {item['summary']}")
            newly_seen.update({i["uid"]: i["completed"] for i in added + dupes})
            changed = changed or bool(added)
            total += len(added)
        if dry_run:
            continue
        if changed:
            wiki.put_html(page_id, page_html)
        # Only after the page is safely written.
        seen.update(newly_seen)
        save_state(state_path, seen)

    log(f"Added {total} row(s)" + (" (dry run, nothing written)" if dry_run else ""))
    return total


def _aware_datetime(text):
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("include a timezone, e.g. 2026-11-05T19:16:00Z")
    return dt


def _env(name, default=None, required=True):
    value = os.environ.get(name, "").strip() or default
    if required and not value:
        raise WikilogError(f"{name} is not set")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "baseline"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--before", type=_aware_datetime, metavar="ISO-TIME",
                        help="baseline only: just items completed before this time, "
                             "e.g. 2026-11-05T19:16:00Z")
    args = parser.parse_args(argv)

    ha = None
    try:
        ha = HomeAssistant(_env("HA_URL"), _env("HA_TOKEN"))
        targets = parse_logs(_env("LOGS"))
        state_path = _env("STATE_FILE", "/state/seen.json")
        if args.command == "baseline":
            baseline(targets, ha, state_path, args.dry_run, args.before)
            return 0
        auto_before = _env("BASELINE_BEFORE", required=False)
        if auto_before and load_state(state_path) is None:
            log(f"No state yet: baselining items completed before {auto_before}")
            baseline(targets, ha, state_path, args.dry_run, _aware_datetime(auto_before))
        wiki = BookStack(_env("BOOKSTACK_URL"), _env("BOOKSTACK_TOKEN_ID"),
                         _env("BOOKSTACK_TOKEN_SECRET"))
        tz = ZoneInfo(_env("DISPLAY_TZ", "Europe/London"))
        ref_pattern = _env("REF_PATTERN", r"\bR\d{6,}\b", required=False)
        run(targets, ha, wiki, state_path, tz, ref_pattern, args.dry_run)
        return 0
    except Exception as exc:  # report every failure, not just the expected ones
        msg = f"{type(exc).__name__}: {exc}" if not isinstance(exc, WikilogError) else str(exc)
        log(f"ERROR: {msg}")
        if ha and not args.dry_run and os.environ.get("HA_NOTIFY_ON_ERROR", "true").lower() != "false":
            try:
                ha.notify(f"The wiki log update failed: {msg}")
            except requests.RequestException as nexc:
                log(f"WARNING: couldn't notify Home Assistant: {nexc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
