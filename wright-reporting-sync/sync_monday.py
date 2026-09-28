#!/usr/bin/env python3
"""Wright Academics Teachworks -> Monday.com sync.

Teachworks is the source of truth; Monday.com is a reporting/operations
layer only. Three modes:

  inspect   Read-only. List the Monday board's real columns (id/title/type)
            so the correct column ids can be copied into .env. Never
            guesses ids.
  dry-run   Read-only. Compute the four Teachworks-derived metrics (reusing
            audit.py's already-validated fetch/cache/attendance logic),
            match each Teachworks student to an existing Monday item by
            Teachworks Student ID, and write a comparison report of what
            WOULD change. Makes no changes to Monday.
  update    WRITES to Monday. Only ever touches an item with EXACTLY one
            matching Teachworks Student ID -- a NOT FOUND or DUPLICATE id is
            always skipped, logged, never guessed at. Never creates a
            Monday item or a session record; Session Count is always
            replaced with the Teachworks lifetime count, never incremented.
            Zero-session safety: if a student currently has no attended
            Teachworks sessions, only Session Count is set (to 0) --
            First/Last Session Date and Tutor are left exactly as they are
            in Monday, never cleared. Use --limit N to test on a handful of
            real students before a full run (which asks for a typed
            confirmation). Writes a per-field CSV log either way.

dry-run and update share one function (diff_student_fields) to decide what
"changed" means, so a dry-run preview and a live update can never disagree.

Reuses audit.py (same folder) for all Teachworks auth, pagination, caching,
participant resolution, and attendance computation -- nothing here
re-derives that logic independently.
"""

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

import audit  # same-folder module: Teachworks auth/pagination/caching/attendance logic

MONDAY_FIELD_ENV_NAMES = {
    "api_token": "MONDAY_API_TOKEN",
    "board_id": "MONDAY_BOARD_ID",
    "column_teachworks_id": "MONDAY_COLUMN_TEACHWORKS_ID",
    "column_first_session_date": "MONDAY_COLUMN_FIRST_SESSION_DATE",
    "column_session_count": "MONDAY_COLUMN_SESSION_COUNT",
    "column_last_session_date": "MONDAY_COLUMN_LAST_SESSION_DATE",
    "column_tutor": "MONDAY_COLUMN_TUTOR",
    "column_session_data_last_synced": "MONDAY_COLUMN_SESSION_DATA_LAST_SYNCED",
}

INSPECT_REQUIRED = ["api_token", "board_id"]
DRY_RUN_REQUIRED = INSPECT_REQUIRED + [
    "column_teachworks_id", "column_first_session_date",
    "column_session_count", "column_last_session_date", "column_tutor",
]
UPDATE_REQUIRED = DRY_RUN_REQUIRED + ["column_session_data_last_synced"]

# Maps a diffable field to (the config key holding its Monday column id, its
# human-readable label for logs/reports).
FIELD_COLUMN_CONFIG_KEY = {
    "session_count": "column_session_count",
    "first_session_date": "column_first_session_date",
    "last_session_date": "column_last_session_date",
    "tutor": "column_tutor",
}
FIELD_LABELS = {
    "session_count": "Session Count",
    "first_session_date": "First Session Date",
    "last_session_date": "Last Session Date",
    "tutor": "Tutor",
}

BOARD_COLUMNS_QUERY = """
query ($boardId: [ID!]) {
  boards(ids: $boardId) {
    id
    name
    columns {
      id
      title
      type
    }
  }
}
"""

ITEMS_FIRST_PAGE_QUERY = """
query ($boardId: [ID!], $limit: Int!, $columnIds: [String!]) {
  boards(ids: $boardId) {
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        column_values(ids: $columnIds) {
          id
          text
          value
        }
      }
    }
  }
}
"""

ITEMS_NEXT_PAGE_QUERY = """
query ($cursor: String!, $limit: Int!, $columnIds: [String!]) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items {
      id
      name
      column_values(ids: $columnIds) {
        id
        text
        value
      }
    }
  }
}
"""


# change_simple_column_value takes a plain string and lets Monday coerce it
# per the column's real type (works for text, numbers, and date columns via
# a "YYYY-MM-DD" string). This is used deliberately instead of
# change_column_value (which needs a type-specific JSON shape per column)
# because this script cannot verify your board's exact column types from
# this environment. The one real limitation: if Tutor turns out to be a
# status/dropdown column rather than free text, this mutation only succeeds
# when the text exactly matches an existing label -- if that's the case here,
# update mode will log a per-field error for Tutor rather than silently
# failing, and we can switch to a label-aware mutation if needed.
CHANGE_SIMPLE_COLUMN_VALUE_MUTATION = """
mutation ($itemId: ID!, $boardId: ID!, $columnId: String!, $value: String) {
  change_simple_column_value(item_id: $itemId, board_id: $boardId, column_id: $columnId, value: $value) {
    id
  }
}
"""


class MondayApiError(Exception):
    pass


def load_monday_config():
    # Reuses audit.py's .env loader/getters so both scripts read the SAME
    # .env file in this folder with the same blank-means-default semantics.
    audit.load_dotenv(dotenv_path=audit.SCRIPT_DIR / ".env")
    return {
        "api_url": audit._get("MONDAY_API_URL", "https://api.monday.com/v2"),
        "api_token": audit._get("MONDAY_API_TOKEN"),
        "auth_header": audit._get("MONDAY_AUTH_HEADER", "Authorization"),
        "auth_scheme": audit._get("MONDAY_AUTH_SCHEME", "{key}"),
        "api_version": audit._get("MONDAY_API_VERSION", ""),
        "board_id": audit._get("MONDAY_BOARD_ID"),
        "items_page_size": audit._get_int("MONDAY_ITEMS_PAGE_SIZE", 100),
        "column_teachworks_id": audit._get("MONDAY_COLUMN_TEACHWORKS_ID", ""),
        "column_first_session_date": audit._get("MONDAY_COLUMN_FIRST_SESSION_DATE", ""),
        "column_session_count": audit._get("MONDAY_COLUMN_SESSION_COUNT", ""),
        "column_last_session_date": audit._get("MONDAY_COLUMN_LAST_SESSION_DATE", ""),
        "column_tutor": audit._get("MONDAY_COLUMN_TUTOR", ""),
        "column_session_data_last_synced": audit._get("MONDAY_COLUMN_SESSION_DATA_LAST_SYNCED", ""),
    }


def require_monday_fields(cfg, names, purpose):
    missing = [MONDAY_FIELD_ENV_NAMES[n] for n in names if not cfg[n]]
    if missing:
        print(f"Missing required Monday configuration for {purpose}:")
        for name in missing:
            print(f"  - {name}")
        print(f"\nFill these in the .env file at {audit.SCRIPT_DIR / '.env'} and re-run.")
        if "column_teachworks_id" in [n for n in names if not cfg[n]] or any(
            n.startswith("column_") for n in names if not cfg[n]
        ):
            print("Run `python sync_monday.py --mode inspect` first to see the real column ids.")
        sys.exit(1)


def monday_headers(cfg):
    value = cfg["auth_scheme"].format(key=cfg["api_token"]) if cfg["api_token"] else cfg["auth_scheme"]
    headers = {cfg["auth_header"]: value, "Content-Type": "application/json"}
    if cfg["api_version"]:
        headers["API-Version"] = cfg["api_version"]
    return headers


def monday_graphql(cfg, query, variables=None, max_retries=3):
    headers = monday_headers(cfg)
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(
                cfg["api_url"], headers=headers,
                json={"query": query, "variables": variables or {}}, timeout=30,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise MondayApiError(f"Network error calling Monday API: {exc}") from exc
            time.sleep(2 ** (attempt - 1))
            continue

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            time.sleep(2 ** (attempt - 1))
            continue

        try:
            body = resp.json()
        except ValueError as exc:
            raise MondayApiError(
                f"Monday API returned non-JSON (status {resp.status_code}). "
                f"Body starts with: {resp.text[:500]!r}"
            ) from exc

        if resp.status_code in (401, 403):
            raise MondayApiError(
                f"Authentication failed ({resp.status_code}) calling Monday API.\n"
                "Check MONDAY_API_TOKEN, MONDAY_AUTH_HEADER, MONDAY_AUTH_SCHEME in .env "
                "against Monday's Admin > API settings.\n"
                f"Body (first 500 chars): {json.dumps(body)[:500]}"
            )
        if body.get("errors"):
            raise MondayApiError(f"Monday GraphQL error(s): {json.dumps(body['errors'])[:2000]}")
        if resp.status_code != 200:
            raise MondayApiError(
                f"Unexpected response {resp.status_code} from Monday API.\n"
                f"Body (first 500 chars): {json.dumps(body)[:500]}"
            )
        return body.get("data") or {}


def run_inspect_mode(cfg, output_dir):
    require_monday_fields(cfg, INSPECT_REQUIRED, "board/column inspection")
    print("=== Monday.com Board/Column Inspection (read-only) ===")
    print(f"API URL: {cfg['api_url']}")
    print(f"Auth header: {cfg['auth_header']}: {audit.mask(monday_headers(cfg)[cfg['auth_header']], cfg['api_token'])}")
    print("=" * 60)
    print(f"REQUESTING BOARD ID: {cfg['board_id']}")
    print("=" * 60)
    print()

    try:
        data = monday_graphql(cfg, BOARD_COLUMNS_QUERY, {"boardId": [cfg["board_id"]]})
    except MondayApiError as exc:
        print(f"\nERROR calling Monday API:\n{exc}\n")
        sys.exit(1)

    boards = data.get("boards") or []
    if not boards:
        print(f"No board found for MONDAY_BOARD_ID={cfg['board_id']!r}. Double-check the board ID (from the board's URL).")
        sys.exit(1)

    board = boards[0]
    print("=" * 60)
    print(f"RETURNED BOARD: {board['name']!r}  (id={board['id']})")
    if str(board["id"]) != str(cfg["board_id"]):
        print(f"  WARNING: returned id {board['id']!r} does not match requested id {cfg['board_id']!r}!")
    print("=" * 60)
    print(
        f"\n>>> Confirm this is really the board you meant ({board['name']!r}) before trusting "
        "the column list below. A wrong id here (e.g. copied from the wrong browser tab) will "
        "return a real, valid-looking board that just isn't the one you intended. <<<\n"
    )
    print("Columns:")
    for col in board["columns"]:
        print(f"  id={col['id']!r}  title={col['title']!r}  type={col['type']}")

    audit.write_json(output_dir / "wright-monday-board-structure.json", board)
    print(f"\nWrote: {output_dir / 'wright-monday-board-structure.json'}")
    print(
        f"\nCopy the 'id' values above into .env for whichever columns on {board['name']!r} "
        "you need to configure."
    )


def fetch_all_monday_items(cfg, column_ids):
    limit = cfg["items_page_size"]
    print(f"Paging through ALL items on Monday board {cfg['board_id']} ...")

    data = monday_graphql(
        cfg, ITEMS_FIRST_PAGE_QUERY,
        {"boardId": [cfg["board_id"]], "limit": limit, "columnIds": column_ids},
    )
    boards = data.get("boards") or []
    if not boards:
        raise MondayApiError(f"No board found for MONDAY_BOARD_ID={cfg['board_id']!r}.")

    page = boards[0].get("items_page") or {}
    all_items = list(page.get("items") or [])
    print(f"  page 1: +{len(page.get('items') or [])} items (running total {len(all_items)})")
    cursor = page.get("cursor")

    page_num = 1
    while cursor:
        page_num += 1
        data = monday_graphql(cfg, ITEMS_NEXT_PAGE_QUERY, {"cursor": cursor, "limit": limit, "columnIds": column_ids})
        page = data.get("next_items_page") or {}
        items = page.get("items") or []
        all_items.extend(items)
        print(f"  page {page_num}: +{len(items)} items (running total {len(all_items)})")
        cursor = page.get("cursor")

    return all_items


def column_text(item, column_id):
    if not column_id:
        return ""
    for cv in item.get("column_values") or []:
        if cv.get("id") == column_id:
            return cv.get("text") or ""
    return ""


def build_teachworks_index(items, teachworks_column_id):
    index = defaultdict(list)
    for item in items:
        tw_id = column_text(item, teachworks_column_id).strip()
        if not tw_id:
            continue
        index[tw_id].append(item)
    return index


def _norm(value):
    return (value or "").strip()


def _counts_equal(current_text, new_count):
    current = _norm(current_text)
    if current == str(new_count):
        return True
    try:
        return float(current) == float(new_count)
    except (ValueError, TypeError):
        return False


def diff_student_fields(current, row):
    """The SINGLE source of truth for "what would actually change" -- used by
    both dry-run (to report it) and update mode (to write exactly this and
    nothing else), so the two can never disagree.

    current: dict with raw Monday text values for keys "session_count",
      "first_session_date", "last_session_date", "tutor".
    row: one row from audit.compute_attendance_rows().

    Returns {field_key: (current_value, new_value)} for fields that actually
    differ. Session Count is always eligible to compare/update, INCLUDING
    down to 0 -- Teachworks reporting zero attended sessions is a real,
    writable fact. First/Last Session Date and Tutor are different: per the
    zero-session safety rule, they are only ever considered "changed" when
    Teachworks currently HAS a value for them (i.e. the student has at least
    one attended session). If Teachworks has no attended-session value for
    one of those three fields, the existing Monday value is left alone
    entirely -- never compared, never overwritten with blank.
    """
    new_count = row["Lifetime Attended Sessions"]
    new_first = row["First Attended Session Date"]
    new_last = row["Most Recent Attended Session Date"]
    new_tutor = row["Most Recent Tutor Name"]

    diffs = {}
    if not _counts_equal(current["session_count"], new_count):
        diffs["session_count"] = (current["session_count"], str(new_count))
    if new_first and _norm(current["first_session_date"]) != _norm(new_first):
        diffs["first_session_date"] = (current["first_session_date"], new_first)
    if new_last and _norm(current["last_session_date"]) != _norm(new_last):
        diffs["last_session_date"] = (current["last_session_date"], new_last)
    if new_tutor and _norm(current["tutor"]) != _norm(new_tutor):
        diffs["tutor"] = (current["tutor"], new_tutor)
    return diffs


def read_current_values(item, monday_cfg):
    return {
        "session_count": column_text(item, monday_cfg["column_session_count"]),
        "first_session_date": column_text(item, monday_cfg["column_first_session_date"]),
        "last_session_date": column_text(item, monday_cfg["column_last_session_date"]),
        "tutor": column_text(item, monday_cfg["column_tutor"]),
    }


def run_dry_run_mode(monday_cfg, tw_config, output_dir, refresh_teachworks_cache):
    require_monday_fields(monday_cfg, DRY_RUN_REQUIRED, "the dry run")

    print("=== Wright Academics Teachworks -> Monday.com Sync -- DRY RUN (no writes) ===\n")

    # --- Reuse audit.py's already-validated Teachworks logic end to end ---
    participant_strategy = audit.run_participant_preflight(tw_config, output_dir)
    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(
        tw_config, output_dir, refresh_teachworks_cache
    )
    rows, field_resolution, diagnostics = audit.compute_attendance_rows(
        tw_config, students, lessons, participant_strategy
    )
    print(f"Computed attendance for {len(rows)} Teachworks students.\n")

    # --- Pull Monday's current state for the columns we care about ---
    column_ids = [
        monday_cfg["column_teachworks_id"],
        monday_cfg["column_first_session_date"],
        monday_cfg["column_session_count"],
        monday_cfg["column_last_session_date"],
        monday_cfg["column_tutor"],
    ]
    try:
        items = fetch_all_monday_items(monday_cfg, column_ids)
    except MondayApiError as exc:
        print(f"\nERROR fetching Monday items:\n{exc}\n")
        sys.exit(1)
    print(f"  -> total Monday items fetched: {len(items)}\n")

    index = build_teachworks_index(items, monday_cfg["column_teachworks_id"])

    report_rows = []
    counts = Counter()
    not_found_ids = []
    duplicate_ids = []

    for row in rows:
        tw_id = row["Teachworks Student ID"]
        new_first = row["First Attended Session Date"]
        new_count = row["Lifetime Attended Sessions"]
        new_last = row["Most Recent Attended Session Date"]
        new_tutor = row["Most Recent Tutor Name"]
        matches = index.get(tw_id, [])

        base = {
            "Teachworks Student ID": tw_id,
            "Student Name": row["Student Name"],
            "New First Session Date": new_first,
            "New Session Count": new_count,
            "New Last Session Date": new_last,
            "New Tutor": new_tutor,
        }

        if not matches:
            counts["NOT_FOUND"] += 1
            not_found_ids.append(tw_id)
            report_rows.append({
                **base,
                "Monday Item ID": "",
                "Current Monday First Session Date": "",
                "Current Monday Session Count": "",
                "Current Monday Last Session Date": "",
                "Current Monday Tutor": "",
                "Fields Changed": "",
                "Action": "NOT FOUND",
            })
            continue

        if len(matches) > 1:
            counts["DUPLICATE"] += 1
            item_ids = [m["id"] for m in matches]
            duplicate_ids.append({"teachworks_id": tw_id, "monday_item_ids": item_ids})
            report_rows.append({
                **base,
                "Monday Item ID": ", ".join(item_ids),
                "Current Monday First Session Date": "",
                "Current Monday Session Count": "",
                "Current Monday Last Session Date": "",
                "Current Monday Tutor": "",
                "Fields Changed": "",
                "Action": "DUPLICATE",
            })
            continue

        item = matches[0]
        current = read_current_values(item, monday_cfg)
        # This is the EXACT same function update mode uses to decide what to
        # write -- the dry-run report and a live update can never disagree.
        diffs = diff_student_fields(current, row)
        action = "UPDATE" if diffs else "NO CHANGE"
        counts[action.replace(" ", "_")] += 1

        report_rows.append({
            **base,
            "Monday Item ID": item["id"],
            "Current Monday First Session Date": current["first_session_date"],
            "Current Monday Session Count": current["session_count"],
            "Current Monday Last Session Date": current["last_session_date"],
            "Current Monday Tutor": current["tutor"],
            "Fields Changed": ", ".join(FIELD_LABELS[k] for k in diffs) if diffs else "none",
            "Action": action,
        })

    fieldnames = [
        "Teachworks Student ID", "Student Name", "Monday Item ID",
        "Current Monday First Session Date", "New First Session Date",
        "Current Monday Session Count", "New Session Count",
        "Current Monday Last Session Date", "New Last Session Date",
        "Current Monday Tutor", "New Tutor", "Fields Changed", "Action",
    ]
    csv_path = output_dir / "wright-monday-dry-run.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_of_truth": "Teachworks",
        "matching_key": "Teachworks Student ID",
        "totals": {
            "teachworks_students_evaluated": len(rows),
            "monday_items_fetched": len(items),
            "update": counts.get("UPDATE", 0),
            "no_change": counts.get("NO_CHANGE", 0),
            "not_found": counts.get("NOT_FOUND", 0),
            "duplicate": counts.get("DUPLICATE", 0),
        },
        "not_found_teachworks_ids": not_found_ids,
        "duplicate_teachworks_ids": duplicate_ids,
        "monday_column_ids_used": {
            "teachworks_id": monday_cfg["column_teachworks_id"],
            "first_session_date": monday_cfg["column_first_session_date"],
            "session_count": monday_cfg["column_session_count"],
            "last_session_date": monday_cfg["column_last_session_date"],
            "tutor": monday_cfg["column_tutor"],
        },
        "teachworks_field_resolution": field_resolution,
    }
    summary_path = output_dir / "wright-monday-dry-run-summary.json"
    audit.write_json(summary_path, summary)

    print("=== DRY RUN SUMMARY (no writes were made to Monday) ===")
    print(f"Teachworks students evaluated: {len(rows)}")
    print(f"Monday items fetched: {len(items)}")
    print(f"  UPDATE:    {counts.get('UPDATE', 0)}")
    print(f"  NO CHANGE: {counts.get('NO_CHANGE', 0)}")
    print(f"  NOT FOUND: {counts.get('NOT_FOUND', 0)}")
    print(f"  DUPLICATE: {counts.get('DUPLICATE', 0)}")
    if not_found_ids:
        print(f"\n{len(not_found_ids)} Teachworks Student ID(s) had no matching Monday item -- see {summary_path.name}")
    if duplicate_ids:
        print(f"{len(duplicate_ids)} Teachworks Student ID(s) matched MULTIPLE Monday items -- see {summary_path.name}")

    print(f"\nWrote:\n  {csv_path}\n  {summary_path}")
    print(
        "\nNo writes were made to Monday.com. Review the CSV, then try a small test with:\n"
        "  python sync_monday.py --mode update --limit 5"
    )


def set_monday_column_value(cfg, item_id, column_id, value):
    monday_graphql(cfg, CHANGE_SIMPLE_COLUMN_VALUE_MUTATION, {
        "itemId": item_id, "boardId": cfg["board_id"], "columnId": column_id, "value": str(value),
    })


def run_update_mode(monday_cfg, tw_config, output_dir, refresh_teachworks_cache, limit):
    require_monday_fields(monday_cfg, UPDATE_REQUIRED, "update mode")

    print("=== Wright Academics Teachworks -> Monday.com Sync -- UPDATE MODE ===")
    if limit is not None:
        print(f"--limit {limit}: only the first {limit} eligible (matched, non-duplicate) student(s) will be written.\n")
    else:
        print("!!! THIS WILL WRITE TO MONDAY.COM for every matched, non-duplicate Teachworks student. !!!\n")
        confirm = input("Type YES to continue, anything else to abort: ").strip()
        if confirm != "YES":
            print("Aborted -- nothing was written to Monday.")
            sys.exit(0)
        print()

    # --- Same computation as dry-run, on purpose: identical inputs, identical diff logic ---
    participant_strategy = audit.run_participant_preflight(tw_config, output_dir)
    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(
        tw_config, output_dir, refresh_teachworks_cache
    )
    rows, field_resolution, diagnostics = audit.compute_attendance_rows(
        tw_config, students, lessons, participant_strategy
    )
    print(f"Computed attendance for {len(rows)} Teachworks students.\n")

    column_ids = [
        monday_cfg["column_teachworks_id"],
        monday_cfg["column_first_session_date"],
        monday_cfg["column_session_count"],
        monday_cfg["column_last_session_date"],
        monday_cfg["column_tutor"],
        monday_cfg["column_session_data_last_synced"],
    ]
    try:
        items = fetch_all_monday_items(monday_cfg, column_ids)
    except MondayApiError as exc:
        print(f"\nERROR fetching Monday items:\n{exc}\n")
        sys.exit(1)
    print(f"  -> total Monday items fetched: {len(items)}\n")

    index = build_teachworks_index(items, monday_cfg["column_teachworks_id"])

    # Eligible = exactly one Monday match. NOT FOUND and DUPLICATE are never
    # written to, no matter what --limit is set to.
    eligible = []
    skipped_not_found = 0
    skipped_duplicate = 0
    for row in rows:
        matches = index.get(row["Teachworks Student ID"], [])
        if not matches:
            skipped_not_found += 1
        elif len(matches) > 1:
            skipped_duplicate += 1
        else:
            eligible.append((row, matches[0]))

    print(
        f"Eligible for update: {len(eligible)} "
        f"(skipped {skipped_not_found} NOT FOUND, {skipped_duplicate} DUPLICATE -- never written to)"
    )
    if limit is not None:
        eligible = eligible[:limit]
        print(f"Processing {len(eligible)} student(s) due to --limit {limit}.\n")
    else:
        print()

    today_str = datetime.now(timezone.utc).date().isoformat()
    log_rows = []
    students_updated = 0
    students_no_change = 0
    students_with_errors = 0
    field_error_count = 0

    for row, item in eligible:
        item_id = item["id"]
        tw_id = row["Teachworks Student ID"]
        name = row["Student Name"]
        current = read_current_values(item, monday_cfg)
        current_synced = column_text(item, monday_cfg["column_session_data_last_synced"])
        # The one and only place that decides what changes -- identical to dry-run.
        diffs = diff_student_fields(current, row)

        if not diffs:
            students_no_change += 1
            print(f"  [{tw_id}] {name}: no field changes needed")

        had_error = False
        for field_key, (old_val, new_val) in diffs.items():
            column_id = monday_cfg[FIELD_COLUMN_CONFIG_KEY[field_key]]
            label = FIELD_LABELS[field_key]
            try:
                set_monday_column_value(monday_cfg, item_id, column_id, new_val)
                status = "success"
                print(f"  [{tw_id}] {name}: {label} {old_val!r} -> {new_val!r}: OK")
            except MondayApiError as exc:
                status = "error"
                had_error = True
                field_error_count += 1
                print(f"  [{tw_id}] {name}: {label} {old_val!r} -> {new_val!r}: ERROR: {exc}")
            log_rows.append({
                "Monday Item ID": item_id, "Student Name": name, "Teachworks Student ID": tw_id,
                "Field": label, "Old Value": old_val, "New Value": new_val, "Status": status,
            })

        if diffs and not had_error:
            students_updated += 1
        if had_error:
            students_with_errors += 1

        # Only stamp "last synced" if everything for this student succeeded --
        # a partial failure should not be reported as a clean sync.
        if not had_error:
            try:
                set_monday_column_value(
                    monday_cfg, item_id, monday_cfg["column_session_data_last_synced"], today_str
                )
                log_rows.append({
                    "Monday Item ID": item_id, "Student Name": name, "Teachworks Student ID": tw_id,
                    "Field": "Session Data Last Synced", "Old Value": current_synced,
                    "New Value": today_str, "Status": "success",
                })
            except MondayApiError as exc:
                field_error_count += 1
                students_with_errors += 1
                print(f"  [{tw_id}] {name}: Session Data Last Synced -> {today_str!r}: ERROR: {exc}")
                log_rows.append({
                    "Monday Item ID": item_id, "Student Name": name, "Teachworks Student ID": tw_id,
                    "Field": "Session Data Last Synced", "Old Value": current_synced,
                    "New Value": today_str, "Status": "error",
                })
        else:
            print(f"  [{tw_id}] {name}: skipping Session Data Last Synced stamp (a field above errored)")

    log_path = output_dir / "wright-monday-update-log.csv"
    log_fieldnames = [
        "Monday Item ID", "Student Name", "Teachworks Student ID",
        "Field", "Old Value", "New Value", "Status",
    ]
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=log_fieldnames)
        writer.writeheader()
        writer.writerows(log_rows)

    print("\n=== UPDATE SUMMARY ===")
    print(f"Students processed: {len(eligible)}")
    print(f"  Updated (>=1 field changed, no errors): {students_updated}")
    print(f"  No change needed:                       {students_no_change}")
    print(f"  Had at least one error:                 {students_with_errors}")
    print(f"  Total field-level mutation errors:       {field_error_count}")
    print(f"Skipped (never written to): {skipped_not_found} NOT FOUND, {skipped_duplicate} DUPLICATE")
    print(f"\nWrote update log: {log_path}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Wright Academics Teachworks -> Monday.com sync. 'inspect' and 'dry-run' are "
            "read-only. 'update' WRITES to Monday for matched, non-duplicate students only."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["inspect", "dry-run", "update"],
        required=True,
        help=(
            "'inspect': list the Monday board's real columns (id/title/type) so you can fill "
            "in .env -- never guesses column ids. "
            "'dry-run': compute Teachworks metrics and compare against current Monday values; "
            "writes a report only, makes no changes to Monday. "
            "'update': WRITES the differing fields to Monday for every matched (exactly one "
            "Monday item), non-duplicate Teachworks student -- use --limit to test on a few first."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "update mode only: write to at most this many eligible (matched, non-duplicate) "
            "students, for safe testing (e.g. --limit 5) before a full run. Omit for a full run "
            "(which asks for a typed confirmation first)."
        ),
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Directory for output files (default: ./output, shared with audit.py).",
    )
    parser.add_argument(
        "--board-id", default=None,
        help=(
            "Override MONDAY_BOARD_ID for this run only, without editing .env -- mainly useful "
            "for `--mode inspect` against a different board (e.g. the Session Log board) than "
            "the one configured for the student sync."
        ),
    )
    parser.add_argument(
        "--refresh-teachworks-cache", action="store_true",
        help="Ignore audit.py's cached Teachworks students/lessons pull and re-fetch from Teachworks.",
    )
    args = parser.parse_args()

    if args.limit is not None and args.mode != "update":
        print("--limit only applies to --mode update.")
        sys.exit(2)
    if args.limit is not None and args.limit < 1:
        print("--limit must be a positive integer.")
        sys.exit(2)

    output_dir = Path(args.output_dir) if args.output_dir else audit.SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    monday_cfg = load_monday_config()
    if args.board_id:
        monday_cfg["board_id"] = args.board_id

    if args.mode == "inspect":
        run_inspect_mode(monday_cfg, output_dir)
    elif args.mode == "dry-run":
        tw_config = audit.load_config()
        run_dry_run_mode(monday_cfg, tw_config, output_dir, args.refresh_teachworks_cache)
    else:
        tw_config = audit.load_config()
        run_update_mode(monday_cfg, tw_config, output_dir, args.refresh_teachworks_cache, args.limit)


if __name__ == "__main__":
    main()
