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
3. Fetches Teachworks lessons in a date range (fully paginated).
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

## ⚠️ Before you trust real output: verify the Teachworks response mapping

The Teachworks **request** shape (base URL, `/lessons` endpoint, the
`Authorization: Token token=<key>` header, and the `status`/`from_date`/
`to_date`/`page`/`per_page` query params) is confirmed against Wright's
previously-working Zapier implementation — this is known-working, not a
guess.

What's still a **best-effort assumption**, isolated in `teachworks.py`, is
the shape of each lesson's *response* JSON — how participants are listed
and the exact field names for tutor/service/location/student:

- `TeachworksClient._is_attended()` — assumes an `attended: true/false` flag
  or a `status` string like `"attended"` on each participant.
- `TeachworksClient.normalize_participant()` — assumes field names like
  `tutor_name`, `service_name`, `location_name`, `student_name`, `price`,
  with fallbacks to a few nested alternatives.

**Before running anything against production data**, run:

```bash
python sync.py --dump-sample --lookback-days 30
```

This authenticates for real and prints one raw Teachworks lesson JSON
object, with no writes at all. Compare it against `normalize_participant()`
and `_is_attended()` in `teachworks.py` and adjust the field names if they
don't match. This is a small, contained fix if needed — everything
Teachworks-shape-dependent lives in that one file.

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

Read-only. Fires four `/lessons` request variants directly at Teachworks so
you can see exactly which query parameter is causing a zero-result (or
wrong-result) response, without needing to touch or even instantiate
Monday.com:

1. `page`/`per_page` only — no `status`, no dates.
2. `from_date`/`to_date` only — no `status`.
3. `status=Attended` only — no date filters.
4. The current production query — `status` + `from_date` + `to_date`.

For each variant it prints only the request parameter names/values, the
HTTP status code, how many records came back, and the first record's raw
JSON if any — never headers, never the API key/token.

## 4. Dry run (no writes — safe to run anytime)

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

## 5. Normal sync (writes to Monday)

```bash
python sync.py
```

Checks the last `LOOKBACK_DAYS` days (default 3) and creates any missing
Session Log items. Safe to run repeatedly — this is what the nightly
schedule runs.

## 6. Full reconciliation

```bash
python sync.py --full
```

Scans from `FULL_SYNC_START_DATE` through today and reconciles against
every existing Monday Session Log item. Still fully idempotent — it will
not duplicate anything already on the board. This is comparatively slow
and API-heavy; **do not schedule it nightly**. Run it manually when you
suspect the board has drifted from Teachworks (e.g. after the old Zapier
automation was still partially active, or after a long outage).

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

## 7. Deploying to Railway

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

## 8. Configuring the nightly schedule

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

## 9. Inspecting logs

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

## 10. Giving / revoking developer access later

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
