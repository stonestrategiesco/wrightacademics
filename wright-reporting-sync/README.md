# Wright Reporting Sync

A small, scheduled background job that keeps the Wright Academics
**Teachworks Reporting** board on Monday.com (company-level monthly KPIs)
in sync with Teachworks — Teachworks is the source of truth; Monday is a
display-only reporting layer.

This is a separate, independent sibling of `wright-session-sync/` in this
repo. It reads Teachworks and writes to a **different** Monday board (see
below); it never reads or writes the Session Log or Students boards that
`wright-session-sync` owns, and it makes no Zapier calls.

---

## What it computes, per month

From Teachworks participant-level lesson data:

- **Sessions Attended / Sessions Missed / Total Sessions** (Attended + Missed)
- **Students Served** (distinct students among Attended + Missed)
- **Avg Sessions / Student** (Sessions Attended ÷ Students Served, 2 decimals)
- **Attended / Missed / Cancelled / Scheduled / Total Amount** — Decimal-safe
  sums of participant `amount` by the same status classification, quantized
  to cents only at the final output (never by summing already-rounded
  subtotals)

Classification (confirmed against real Wright Academics data, not assumed):
`Attended` → Sessions Attended, `Missed` → Sessions Missed, `Cancelled` /
`Scheduled` → excluded from Total Sessions/Total Amount but still counted
and reported, anything else/null → `Unclassified` (always reported
explicitly, never silently dropped). Every aggregation includes a
reconciliation check (session-count and, separately, financial) proving the
buckets add up to the full total considered — a mismatch aborts the run
rather than writing bad numbers.

**Payments is intentionally out of scope** — no code here reads or writes
a Payments column on any board.

---

## Files

```
wright-reporting-sync/
    audit.py                        Teachworks REST client: auth, pagination,
                                     caching, attendance/participant resolution
    sync_monday.py                  Generic Monday.com GraphQL transport
                                     (see "About sync_monday.py" below)
    sync_reporting.py               CLI entrypoint: dry-run/trend/update modes
    inspect_status_inventory.py     Read-only: full-history Teachworks lesson/
                                     participant status inventory
    inspect_payments_diagnostic.py  Read-only: participant amount/unit_price/
                                     invoice_id investigation
    requirements.txt / requirements-dev.txt
    .env.example
    tests/
```

### About `sync_monday.py`

This file is a generic, board-agnostic Monday.com GraphQL client (queries,
pagination, a generic column-value mutation). `sync_reporting.py` imports
it only for that generic transport (`monday_graphql`, `fetch_all_monday_items`,
`column_text`, `set_monday_column_value`, `MondayApiError`,
`BOARD_COLUMNS_QUERY`) — never for its own `--mode update`/`--mode dry-run`
CLI, which targets the **Students board** and duplicates what
`wright-session-sync`'s Stage 2/3 rollup already owns in production.

**Do not run `python sync_monday.py --mode update` or `--mode dry-run` in
this folder.** Its `--mode inspect` (read-only column/group discovery) is
fine and occasionally useful. All real Students-board writes belong to
`wright-session-sync`, not this folder.

---

## 1. Environment variables

See `.env.example` for the full list with comments. Required:

| Variable | Description |
|---|---|
| `TEACHWORKS_API_KEY` | Teachworks API key (Teachworks admin settings → API). |
| `TEACHWORKS_BASE_URL` | Default `https://api.teachworks.com/v1`. |
| `MONDAY_API_TOKEN` | Monday.com API v2 token (same account as `wright-session-sync`, different board). |

Every `MONDAY_REPORTING_*` column/board id already has a working default
baked into `sync_reporting.py`, confirmed against the live board — only
override them in `.env` if the board is ever restructured.

```bash
cd wright-reporting-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in TEACHWORKS_API_KEY and MONDAY_API_TOKEN
```

---

## 2. Commands

### Read-only dry run (single month)

```bash
python sync_reporting.py --mode dry-run --year 2026 --month 9
```

Computes the month's aggregation from Teachworks and previews the exact
Monday diff (if Monday credentials are configured) — makes zero writes.

### Read-only monthly trend (multiple months, one Teachworks pull)

```bash
python sync_reporting.py --mode dry-run --year 2026 --month-range 1-9
```

Teachworks-only — no Monday calls at all. Prints session KPIs and the five
Amount fields for every month in the range, each with its own
reconciliation check.

### List the board's real groups (read-only)

```bash
python sync_reporting.py --list-groups
```

Prints every group's real `id`/`title` on the Reporting board, plus what
`MONDAY_REPORTING_YEAR_GROUP_TITLE_TEMPLATE` currently resolves the current
year to. Confirm a group matches exactly before relying on `--current-month`'s
auto-create. Makes exactly one read-only Monday call; no Teachworks call,
no writes.

### Historical multi-month update (writes, one-time backfill)

```bash
python sync_reporting.py --mode update --year 2026 --month-range 1-8
```

Board-schema preflight → session + financial reconciliation (both must pass
for every requested month) → matches each month's existing item by exact
name (never creates one in this mode) → previews every month's diff → one
typed `YES` confirmation → writes → reads each item back from Monday to
verify. Refuses any month that hasn't fully elapsed yet unless
`--allow-incomplete-month` is passed. Aborts the entire batch before any
write if any month is missing, duplicated, or fails reconciliation.

### Month item names

Each month is one item on the Reporting board, named
`{MM} - {Month} {YYYY}` — e.g. `09 - September 2026` — so the board and its
dashboards sort chronologically. That is the only name this script ever
creates. Lookups also accept the older `September 2026` form for the same
month, so existing items keep working without being renamed. If both forms
exist for one month, that month is treated as a duplicate and the run aborts
before writing anything; the script never picks one of them.

### Current-month update (the production/unattended path)

```bash
python sync_reporting.py --mode update --current-month --yes
```

This is the command the scheduled Railway job runs. Resolves today's
year/month at runtime (the command line never needs editing as months roll
over), and — **only in this mode** — will create the exact current-month
item if it's missing: it looks up the existing group matching the current
year (never creates a group), creates one item, re-fetches, and requires
exactly one match before continuing. A genuine duplicate still always
aborts. `--yes` skips the interactive confirmation (reads no stdin); every
other safeguard (schema preflight, reconciliation, exact name matching,
read-back verification) still applies identically.

`--current-month` always re-fetches Teachworks (it implies
`--refresh-teachworks-cache`), and the update refuses to write — exit code 1,
nothing written — unless the Teachworks data was fetched during that same
run. A cache from an earlier run, even earlier the same day, is never used
for the current month. Every run logs its source as
`Teachworks source: FRESH -- fetched by this run at …` or
`Teachworks source: CACHE -- loaded from …, fetched at …`. Historical and
manual runs (`--month`, `--month-range`, dry runs) may still use the cache in
`output/_cache` unless `--refresh-teachworks-cache` is passed.

---

## 3. Automated tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

All tests run against in-memory fakes/mocks — no network access and no real
credentials required. They cover (among ~90 assertions across 7 files):

- Decimal-safe financial aggregation (including float-rounding-trap cases)
  and the session/financial reconciliation checks
- The board-schema preflight catching a missing or mismatched column id
  before any write is attempted
- Multi-month update: missing/duplicate month abort, declined confirmation,
  current-month protection, a full write + read-back verify, a simulated
  write failure and a simulated read-back mismatch
- `--current-month`/`--yes`: correct year/month resolution, zero stdin
  reads, and every failure mode (missing item with/without a matching year
  group, duplicate item, write failure, post-create verification failure)
- The core anti-duplicate guarantee: rerunning after the current-month item
  already exists never calls `create_item` and never even looks up the
  year group
- `--list-groups` making exactly one read-only Monday call and nothing else

---

## 4. Deploying to Railway

This is a **separate Railway service** from `wright-session-sync`'s
(`wrightacademics-nightly`) — do not add this to that service or modify it.

1. In Railway: **New Project → Deploy from GitHub repo** (or **New Service**
   in the existing project), select this repo, and set the service's root
   directory to `wright-reporting-sync/`.
2. Under **Variables**, add `TEACHWORKS_API_KEY` and `MONDAY_API_TOKEN` (see
   `.env.example` for optional overrides).
3. Set the **Start Command** to a no-op — like `wright-session-sync`, this
   has no long-running process; it runs on a schedule.
4. Under **Settings → Cron Schedule**, set a daily schedule and set the
   **Cron Command** to:
   ```
   python sync_reporting.py --mode update --current-month --yes
   ```
5. Before enabling the schedule, trigger one manual run and confirm exit
   code 0 and a `"success": true` row in the JSON log for the current month.
