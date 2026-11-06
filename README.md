# ha-todo-wikilog

Copies completed Home Assistant to-do items into tables on BookStack wiki
pages, so there's a lasting "what was done when" record. Its first job is the
two tables on the
[Sage Oracle maintenance log](https://wiki.lukmor.com/books/appliances-warranties/page/sage-oracle-maintenance-log)
page (BookStack page 111), fed from the **Oracle Maintenance** and **Oracle Issues** lists.

Each run:

1. Asks Home Assistant for the completed items on every configured list
   (`todo.get_items`, which includes the time each item was ticked off).
2. Skips items it has logged before. It remembers them by Home Assistant's item
   ID in `/state/seen.json`.
3. Adds a row for each new item to that list's table, in date order, dated by
   when it was ticked off (UK time). A table with a third column gets the
   ticket number from the item's description (`R` plus 6+ digits), or the
   whole description if there's no ticket number.
4. Writes the page back once, and only if something was added. Everything
   outside the tables is passed through untouched.

Rows are only ever added. Clearing completed items in Home Assistant doesn't
remove them from the wiki, and editing a row by hand (say, fixing a date
because something was ticked off a day late) sticks, since that item is
already marked as logged.

If a run fails (wiki unreachable, table missing, and so on) it exits non-zero
and raises a persistent notification in Home Assistant, so a quietly broken
log gets noticed.

It's a one-shot container. Ofelia runs it hourly.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `HA_URL` | yes | | e.g. `http://192.168.50.10:8123` |
| `HA_TOKEN` | yes | | Home Assistant long-lived access token |
| `BOOKSTACK_URL` | yes | | e.g. `http://192.168.50.12:6875` |
| `BOOKSTACK_TOKEN_ID` | yes | | API token ID of the wiki service account |
| `BOOKSTACK_TOKEN_SECRET` | yes | | API token secret for the same account |
| `LOGS` | yes | (see compose file) | Comma-separated `todo.<list>=<page id>#<table id>` |
| `BASELINE_BEFORE` | | | On the very first run (no state file yet), treat items completed before this time as already on the wiki. ISO time with a timezone, e.g. `2026-11-05T19:16:00Z` |
| `STATE_FILE` | | `/state/seen.json` | Mount a volume at `/state` |
| `DISPLAY_TZ` | | `Europe/London` | Timezone for the dates written to the wiki |
| `REF_PATTERN` | | `\bR\d{6,}\b` | Regex for the ticket number in a 3-column table |
| `HA_NOTIFY_ON_ERROR` | | `true` | `false` to stop the failure notification |

### Table IDs

Tables are found by their HTML `id`, e.g. `bkmrk-maintenance-log-table`.
Editing the table's rows in the wiki editor is fine. If the table is deleted
and rebuilt, it loses the id and the next run fails with "Table id=... not
found". To fix it, open the page editor, use **Source code**, and put
`id="..."` back on the `<table>` tag.

To log another list, add a table with a Date column and a description column,
give it an id, and add `todo.<list>=<page id>#<table id>` to `LOGS`.

## Commands

    wikilog.py [run]              add new rows (the default)
    wikilog.py baseline [--before ISO-TIME]
                                  mark items as already logged, write nothing to the wiki
    --dry-run                     print what would happen; write nothing

`run` refuses to start without a state file unless `BASELINE_BEFORE` is set,
because otherwise it would add every completed item ever, including ones
already on the wiki. Rows already in the table with the same date and wording
are recognised and not added twice, which is a second safety net if the state
volume is ever lost.

## Deploying (Docker VM, 192.168.50.12)

### 1. Home Assistant token

In Home Assistant, open your profile (bottom left) → **Security** →
**Long-lived access tokens** → **Create token**. Name it `ha-todo-wikilog` and
copy it; it's only shown once.

### 2. Wiki service account

There's no API for creating users, so this is done by hand. BookStack grants
book permissions to roles, not to individual users, so the account gets its
own role:

1. **Settings → Roles → Create new role**: name it `Wiki automation`. Under
   **System permissions** tick **Access system API**, and leave every
   content permission (the "All books / chapters / pages" grid) unticked.
2. **Settings → Users → Add new user**: name `ha-todo-wikilog`, a throwaway
   email and password, role **Wiki automation** only.
3. On the **Appliances & Warranties** book, open **Permissions**, add an
   override for the **Wiki automation** role and tick **View** and
   **Update**. The log page inherits this from the book.
4. Open the user from the Users list → **API Tokens → Create token**. Copy the
   Token ID and Token Secret; the secret is only shown once.

If runs fail with HTTP 403, BookStack's message in the notification says
which part is missing. "does not have permission to make API calls" means
step 1's API tick. "permission to access the requested page" or a 403 on
update means step 3, or that the page itself has its own permissions that
override the book's. That happened on first deploy: page 111 was created by
another account and didn't pick up the book-level grant until the page's own
permissions (or its owner) were changed to include this account.

### 3. Portainer stack

In Portainer: **Stacks → Add stack**, name `ha-todo-wikilog`, paste
`docker-compose.yml`, and under **Environment variables** add `HA_URL`,
`HA_TOKEN`, `BOOKSTACK_URL`, `BOOKSTACK_TOKEN_ID` and `BOOKSTACK_TOKEN_SECRET`.
Deploy. The container runs once straight away, sets its baseline, adds any
items ticked off since 5 Nov 2026 19:16 UTC, and exits. Check its logs.

### 4. Ofelia schedule

Add to `/opt/ofelia/config.ini` on the Docker host, then `docker restart ofelia`
(see Thinky-Stack's `ofelia-job-update-guide.md`):

```ini
[job-run "ha-todo-wikilog"]
schedule = @hourly
container = ha-todo-wikilog
```

Runs with nothing new don't touch the wiki, so hourly costs nothing and
doesn't fill the page history with empty revisions.

## Development

    pip install -r requirements-dev.txt
    python -m pytest
