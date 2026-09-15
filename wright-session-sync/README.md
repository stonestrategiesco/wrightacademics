# Wright Session Sync

A small, scheduled background job that syncs **attended Teachworks lessons**
into the Wright Academics **Session Log** board on Monday.com.

It replaces a Zapier automation that produced duplicate Session Log records,
missed pages of results, and hit runtime limits. This tool is built to be
**idempotent**: running it twice (or a hundred times) over the same data
never creates a duplicate Session Log item.

This is intentionally **not** a web app. There is no frontend, dashboard,
login, or database. Wright Academics staff continue to work entirely inside
Teachworks and Monday.com. This tool only moves data between them, on a
schedule.

---

## How it works

1. Loads every existing "Unique Lesson/Student ID" value already on the
   Session Log board into memory (paginating the entire board — this is the
   full duplicate-prevention index).
2. Loads every Monday Student that has a Teachworks Student ID, into a
   `Teachworks Student ID -> Monday Item ID` lookup.
3. Fetches Teachworks lessons in a date range. Confirmed via production
   diagnostics that a single multi-day `from_date`/`to_date` request
   returns **zero** records even when matching data exists, so this issues
   one `from_date == to_date` request **per calendar date** in the range
   instead, each fully paginated independently, and combines the results.
4. For every **attended** participant on every lesson, builds the key
   `{teachworks_lesson_id}_{teachworks_student_id}`.
   - If that key already exists on the board → skip.
   - If not → create a new Session Log item, immediately record its key in
     memory (so the same run can never create it twice), and connect it to
     the matching Student item if one was found. If no Student is found,
     the Session Log item is still created, but a `MISSING_STUDENT` line is
     logged and included in the run's report — no Student is ever
     auto-created.

Normal scheduled runs only look back a few days (`LOOKBACK_DAYS`, default
3). Overlapping windows are intentional: since the sync is idempotent, it's
safe (and useful) to re-check the last few days every night in case
attendance was entered late or a previous run failed partway through.

## ⚠️ Teachworks field mapping: what's confirmed, what isn't

The Teachworks **request** shape (base URL, `/lessons` endpoint, the
`Authorization: Token token=<key>` header, one `from_date == to_date`
request per calendar date, and the `status`/`page`/`per_page` params) is
confirmed against Wright's previously-working Zapier implementation and
against live production diagnostics.

The **response** field mapping in `normalize_participant()` is now confirmed
against a real production lesson/participant (2026-09-13, lesson
`93279926`): `lesson_id` <- `lesson["id"]`, `session_date` <-
`lesson["from_date"]`, `tutor` <- `lesson["employee_name"]`, `service` <-
`lesson["service_name"]`, `location` <- `lesson["location_name"]`,
`student_id`/`student_name` <- `participant["student_id"]`/`["student_name"]`.
See `tests/test_teachworks.py::test_normalize_participant_matches_confirmed_2026_09_13_production_response`
for the exact fixture this was validated against.

**Still unconfirmed:** `duration_minutes` and `amount`. No real response
we've seen so far (only `from_date`/`from_time` on the lesson) has shown a
duration or price/amount field — their current lookups are unverified
guesses. Don't trust the Duration/Amount Monday columns until these are
confirmed the same way the other fields were, e.g. via:

```bash
python sync.py --dump-sample --lookback-days 3
```

which authenticates for real and prints one raw lesson JSON with no writes
at all.

---

## 1. Environment variables Wright needs to create

| Variable | Required | Description |
|---|---|---|
| `TEACHWORKS_API_KEY` | Yes | API key from the Teachworks account (Teachworks admin settings → API). |
| `TEACHWORKS_BASE_URL` | No (default shown) | `https://api.teachworks.com/v1` |
| `MONDAY_API_TOKEN` | Yes | A Monday.com API v2 token for a user/account that has access to both boards. Monday → Admin → API. |
| `LOOKBACK_DAYS` | No (default `3`) | How many days back a normal scheduled run checks. |
| `FULL_SYNC_START_DATE` | No (default `2020-01-01`) | Start date used by `--full`. Set this to whenever Wright's Teachworks data actually begins, to avoid scanning years of empty history. |
| `REQUEST_TIMEOUT_SECONDS` | No (default `30`) | HTTP timeout per request. |
| `MAX_RETRIES` | No (default `5`) | Retry attempts for transient (429/5xx/network) failures. |
| `RETRY_BASE_DELAY_SECONDS` | No (default `1`) | Base delay for exponential backoff between retries. |

Copy `.env.example` to `.env` and fill in the two required values for local
use. **Never commit `.env`** — it's already in `.gitignore`.

Monday.com board IDs and column IDs are fixed in `config.py` per Wright's
board layout and should not need to change unless the board itself is
restructured.

---

## 2. Running locally

```bash
cd wright-session-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TEACHWORKS_API_KEY and MONDAY_API_TOKEN
```

## 3. Diagnosing a zero-results / wrong-filter Teachworks query

```bash
python sync.py --diagnose-teachworks --lookback-days 30
```

Read-only. Fires nine `/lessons` request variants directly at Teachworks so
you can see exactly which query parameter is causing a zero-result (or
wrong-result) response, then runs a full pagination walk — without needing
to touch or even instantiate Monday.com:

1. `page`/`per_page` only — no `status`, no dates.
2. `from_date`/`to_date` together — no `status`.
3. `status=Attended` only — no date filters.
4. The current production query — `status` + `from_date` + `to_date`.
5. `from_date` only — no `to_date`, no `status`.
6. `to_date` only — no `from_date`, no `status`.
7. A known-good single day (`KNOWN_GOOD_HISTORICAL_DATE` in `sync.py`,
   confirmed via diagnostics to have an Attended record) as
   `from_date=to_date`, no `status`.
8. That same known-good day, with `status=Attended` added.
9. A known-recent day (`KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS` in
   `sync.py`) the prior process reported 10 sessions for, with `status=Attended`.

For each variant it prints only the request parameter names/values, the
HTTP status code, how many records came back, and the first record's raw
JSON if any — never headers, never the API key/token.

It then runs a **pagination diagnostic**: walks every `/lessons` page for
`status=Attended` with no date filters at all (capped at `MAX_DIAGNOSTIC_PAGES`,
default 500, with a clear warning if the cap is hit), and reports total pages
fetched, total lessons seen, and the earliest/latest `from_date` across all
of them — without printing every individual lesson. This shows the full
extent of what this credential can actually see.

## 4. Investigating zero (or unexpected) duplicate-detection results

```bash
python sync.py --diagnose-dedup --lookback-days 2
```

Read-only — makes **zero Monday writes**, though unlike the two diagnostics
above it does read from Monday.com (it needs to see the existing unique-ID
column). Use this if a dry run or real sync reports far fewer duplicate
skips than you'd expect given how many Session Log records already exist.

It computes the unique key for every Teachworks participant session in the
range exactly as production does, compares it against every non-blank value
actually stored in Monday's `text_mm5h9n9g` column, and reports:

```
Teachworks participant sessions: <n>
Exact unique-key matches: <n>
No unique-key match: <n>
```

For the first 10 sessions with no exact match, it attempts a **read-only,
diagnostic-only** secondary comparison against existing Monday records using
session date + Teachworks Student ID (falling back to tutor/service), and
prints any likely match's Monday item ID, its stored unique-ID value, and
its session date — so you can see whether the old Zap stored a different
key format, stored no key at all, or whether these are genuinely new
sessions it never created. It also prints a sample of up to 10 non-blank
existing unique-ID values so you can see their actual format.

These secondary fields are **never** used to decide production
deduplication — only the exact unique-key match is. This diagnostic exists
purely to investigate, not to change, dedup behavior.

## 5. Dry run (no writes — safe to run anytime)

```bash
python sync.py --dry-run
```

This authenticates against both real APIs, fetches real data, and prints a
full report of what it *would* do — but creates and updates nothing. Use
this after any change, and before the very first real sync.

Example dry-run report:

```
======================================================================
WRIGHT ACADEMICS SESSION SYNC - RESULT
======================================================================
Sync mode:                        DRY RUN - SCHEDULED (rolling lookback)
Date range:                       2026-09-12 .. 2026-09-15
Runtime:                          2.1s
Teachworks lessons fetched:       41
Attended participant sessions:    57
Existing Monday IDs loaded:       1204
Sessions that WOULD be created:   6
Sessions skipped as duplicates:   51
Students matched:                 5
Missing students:                 1
Connections made:                 0
Connection errors:                0
Creation errors:                  0
----------------------------------------------------------------------
MISSING STUDENTS (Session Log item created, but NOT connected):
  - Teachworks Student ID 4821 (New Student Name), lesson 998231
======================================================================
RESULT: COMPLETED - all sessions synced, but some student connections need attention.
======================================================================
```

## 6. Normal sync (writes to Monday)

```bash
python sync.py
```

Checks the last `LOOKBACK_DAYS` days (default 3) and creates any missing
Session Log items. Safe to run repeatedly — this is what the nightly
schedule runs.

## 7. Full reconciliation

```bash
python sync.py --full
```

Scans from `FULL_SYNC_START_DATE` through today and reconciles against
every existing Monday Session Log item. Still fully idempotent — it will
not duplicate anything already on the board. This is comparatively slow
and API-heavy — since Teachworks requires one request per calendar date
(see "How it works" above), a multi-year `--full` run means one HTTP
request (or more, if a single day paginates) per day in that range, so a
default `FULL_SYNC_START_DATE` of `2020-01-01` means thousands of requests.
Set `FULL_SYNC_START_DATE` to the actual start of Wright's usable Teachworks
data before running this, and **do not schedule it nightly**. Run it
manually when you suspect the board has drifted from Teachworks (e.g. after
the old Zapier automation was still partially active, or after a long
outage).

Combine with `--dry-run` first to see the full scope before writing:

```bash
python sync.py --full --dry-run
```

---

## Automated tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

All tests run against in-memory fakes/mocks — no network access and no
real credentials required. They cover:

- Skipping an already-existing unique session ID
- Running the same sync twice produces zero duplicates
- A group lesson with two attended students produces two records
- The same lesson+student cannot be created twice in one run
- A newly created unique ID is immediately protected against re-creation
- A missing Monday Student never triggers Student creation, and is reported
- Teachworks pagination retrieves every page
- Monday `items_page` / `next_items_page` pagination retrieves every page
- `--dry-run` performs zero writes
- Transient (429/5xx/network) failures retry with backoff
- Permanent failures are raised clearly, without exhausting retries needlessly
- A failure creating one session does not stop the rest of the run

---

## 8. Deploying to Railway

1. Push this repository to GitHub (see below).
2. In Railway: **New Project → Deploy from GitHub repo**, select this repo.
3. Railway will detect Python. Set the **Start Command** to a no-op — this
   service has no long-running server process; it runs on a schedule (see
   below), so you do not need a persistent "Deploy" service running
   `python sync.py` in a loop.
4. Under **Variables**, add `TEACHWORKS_API_KEY`, `MONDAY_API_TOKEN`, and
   any optional overrides from the table above.
5. Under **Settings → Build**, Railway will run `pip install -r requirements.txt`
   automatically (Nixpacks detects `requirements.txt`).

## 9. Configuring the nightly schedule

Railway supports **Cron Schedules** on a service:

1. Open the service → **Settings → Cron Schedule**.
2. Set a schedule, e.g. `0 9 * * *` (9am UTC daily — pick a time a few
   hours after Wright's last lessons of the day, in UTC).
3. Set the **Cron Command** to:
   ```
   python sync.py
   ```
4. Leave the regular Start Command unset/idle — the cron job runs the
   command on schedule instead of keeping a process alive.

Do **not** put `--full` in the scheduled command — that's for manual,
occasional reconciliation only.

## 10. Inspecting logs

Every run prints a plain-text report (see the dry-run example above) plus
line-by-line logs for anything notable (`MISSING_STUDENT`, `CREATE_ERROR`,
`CONNECTION_ERROR`). In Railway: open the service → **Deployments** (or
**Cron** tab) → click the run → **View Logs**. The final `RESULT:` line
tells you at a glance whether the run needs attention:

- `RESULT: SYNC OK - nothing needs attention.`
- `RESULT: COMPLETED - all sessions synced, but some student connections need attention.`
- `RESULT: COMPLETED WITH ERRORS - some sessions were NOT created. See above.`

A non-developer can read that one line to know whether the night's sync was
clean.

## 11. Giving / revoking developer access later

This integration is just a GitHub repo plus a Railway project — both owned
by whichever GitHub/Railway account Wright Academics controls.

- **To give a developer access:** invite their GitHub account as a
  collaborator on the repo (GitHub → repo → Settings → Collaborators), and
  invite them to the Railway project (Railway → project → Settings →
  Members). Do not share the `.env` values directly — Railway variables can
  be viewed by anyone with project access, so access to the Railway project
  *is* access to the credentials.
- **To revoke access:** remove them from both the GitHub repo and the
  Railway project. If you suspect the API keys themselves were exposed,
  also **rotate** `TEACHWORKS_API_KEY` (Teachworks admin settings) and
  `MONDAY_API_TOKEN` (Monday.com → Admin → API → regenerate token), then
  update the Railway variables with the new values.

---

## Project structure

```
wright-session-sync/
    sync.py             CLI entrypoint + orchestration (run_sync)
    teachworks.py        Teachworks REST client + normalization
    monday_client.py     Monday.com GraphQL client
    config.py            Env vars, board/column ID constants, validation
    requirements.txt
    requirements-dev.txt
    .env.example
    .gitignore
    README.md
    tests/
```
