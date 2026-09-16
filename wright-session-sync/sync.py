#!/usr/bin/env python3
"""
Wright Academics: Teachworks -> Monday.com Session Log sync.

Usage:
    python sync.py                 Normal scheduled run (rolling lookback window)
    python sync.py --dry-run       Fetch and report only; writes NOTHING
    python sync.py --full          Full historical reconciliation
    python sync.py --full --dry-run
    python sync.py --dump-sample   Print one raw Teachworks lesson JSON and exit
                                    (use this to verify field names in teachworks.py
                                    against the real API before trusting real output)

See README.md for full operating instructions.
"""

import argparse
import datetime
import json
import logging
import sys
from dataclasses import dataclass, field

import config
from monday_client import MondayClient
from teachworks import TeachworksClient

logger = logging.getLogger("wright_sync")

# A lesson date confirmed (via --diagnose-teachworks against the real API) to
# have at least one Attended record, used as a known-good fixture when
# isolating date-filter behavior.
KNOWN_GOOD_HISTORICAL_DATE = "2017-07-18"

# A 2026 date the prior Wright process reported 10 Attended sessions for,
# used to test whether "recent" dates behave differently from historical ones.
KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS = "2026-09-13"

# Safety cap for the read-only pagination diagnostic below.
MAX_DIAGNOSTIC_PAGES = 500


@dataclass
class SyncReport:
    mode: str
    start_date: str
    end_date: str
    runtime_seconds: float = 0.0
    lessons_fetched: int = 0
    attended_sessions_found: int = 0
    existing_ids_loaded: int = 0
    sessions_created: int = 0
    sessions_skipped: int = 0
    students_matched: int = 0
    missing_students: list = field(default_factory=list)
    connections_made: int = 0
    connection_errors: list = field(default_factory=list)
    creation_errors: list = field(default_factory=list)


def _build_item_name(session):
    student_name = session.get("student_name") or "Unknown Student"
    session_date = session.get("session_date") or ""
    return f"{student_name} - {session_date}".strip(" -")


def _build_column_values(session):
    session_date = session.get("session_date")
    student_id = session.get("student_id")
    return {
        config.COL_SESSION_DATE: {"date": session_date} if session_date else None,
        config.COL_STUDENT_NAME: session.get("student_name"),
        config.COL_TEACHWORKS_STUDENT_ID: str(student_id) if student_id is not None else None,
        config.COL_TUTOR: session.get("tutor"),
        config.COL_SERVICE: session.get("service"),
        config.COL_DURATION: session.get("duration_minutes"),
        config.COL_LOCATION: session.get("location"),
        config.COL_UNIQUE_ID: session["unique_key"],
        config.COL_AMOUNT: session.get("amount"),
    }


def run_sync(tw_client, monday_client, start_date, end_date, dry_run=False, mode="scheduled"):
    """Core sync logic. Takes already-constructed API clients so it can be
    unit tested with fakes/mocks instead of real HTTP calls."""
    import time as _time

    started_at = _time.time()
    report = SyncReport(mode=mode, start_date=start_date, end_date=end_date)

    logger.info("Loading existing Monday Session Log unique IDs (duplicate-prevention index)...")
    existing_ids = monday_client.get_existing_unique_ids(config.MONDAY_SESSIONS_BOARD_ID, config.COL_UNIQUE_ID)
    report.existing_ids_loaded = len(existing_ids)
    logger.info("Loaded %d existing unique IDs from Monday.", report.existing_ids_loaded)

    logger.info("Loading Monday Student lookup (Teachworks Student ID -> Monday Item ID)...")
    student_lookup = monday_client.get_student_lookup(config.MONDAY_STUDENTS_BOARD_ID, config.STUDENT_BOARD_COL_TEACHWORKS_ID)
    logger.info("Loaded %d Monday students with a Teachworks Student ID.", len(student_lookup))

    logger.info("Fetching Teachworks lessons for %s .. %s ...", start_date, end_date)
    lessons = tw_client.get_lessons(start_date, end_date)
    report.lessons_fetched = len(lessons)
    logger.info("Fetched %d Teachworks lessons.", report.lessons_fetched)

    sessions = tw_client.extract_attended_sessions(lessons)
    report.attended_sessions_found = len(sessions)
    logger.info("Found %d attended participant sessions.", report.attended_sessions_found)

    for session in sessions:
        unique_key = session["unique_key"]
        # Backward compatibility with the legacy Zapier sync, which stored
        # only the bare lesson_id in this column (no student component).
        # existing_ids already holds whatever raw strings are actually in
        # Monday's unique-ID column - legacy or composite - so checking both
        # forms against that same set requires no migration and no separate
        # lookup. New records always store the composite key; the legacy
        # form is only ever checked, never written.
        legacy_key = str(session["lesson_id"]) if session.get("lesson_id") is not None else None

        if unique_key in existing_ids or (legacy_key is not None and legacy_key in existing_ids):
            report.sessions_skipped += 1
            continue

        student_id_str = str(session["student_id"]) if session.get("student_id") is not None else None
        monday_student_item_id = student_lookup.get(student_id_str) if student_id_str else None

        if monday_student_item_id:
            report.students_matched += 1
        else:
            report.missing_students.append({
                "teachworks_student_id": session.get("student_id"),
                "student_name": session.get("student_name"),
                "lesson_id": session.get("lesson_id"),
            })
            logger.warning(
                "MISSING_STUDENT teachworks_student_id=%s student_name=%s lesson_id=%s",
                session.get("student_id"), session.get("student_name"), session.get("lesson_id"),
            )

        if dry_run:
            report.sessions_created += 1
            existing_ids.add(unique_key)
            continue

        try:
            item_id = monday_client.create_session_item(
                config.MONDAY_SESSIONS_BOARD_ID,
                config.MONDAY_SESSION_GROUP_ID,
                _build_item_name(session),
                _build_column_values(session),
            )
        except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
            logger.error(
                "CREATE_ERROR lesson_id=%s student_id=%s unique_key=%s error=%s",
                session.get("lesson_id"), session.get("student_id"), unique_key, exc,
            )
            report.creation_errors.append({"unique_key": unique_key, "error": str(exc)})
            continue

        # Immediately record the new key so this same run cannot create it twice.
        existing_ids.add(unique_key)
        report.sessions_created += 1

        if monday_student_item_id:
            try:
                monday_client.connect_student(
                    config.MONDAY_SESSIONS_BOARD_ID, item_id, config.COL_STUDENT_CONNECTION, monday_student_item_id
                )
                report.connections_made += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("CONNECTION_ERROR item_id=%s student_item_id=%s error=%s", item_id, monday_student_item_id, exc)
                report.connection_errors.append({"item_id": item_id, "error": str(exc)})

    report.runtime_seconds = _time.time() - started_at
    return report


def diagnose_teachworks(tw_client, start_date, end_date):
    """Read-only diagnostic: fires four /lessons request variants directly
    against Teachworks to isolate which query parameter causes a zero-result
    response. Prints only status codes, parameter names/values, record
    counts, and the first record's JSON — never headers or credentials.
    Makes zero Monday.com calls and zero writes of any kind."""
    variants = [
        ("1. page/per_page only (no status, no dates)", {"page": 1, "per_page": 10}),
        ("2. from_date/to_date together, no status", {"from_date": start_date, "to_date": end_date, "page": 1, "per_page": 10}),
        ("3. status=Attended only (no date filters)", {"status": "Attended", "page": 1, "per_page": 10}),
        ("4. current production query (status + from_date + to_date)", {
            "status": "Attended", "from_date": start_date, "to_date": end_date, "page": 1, "per_page": 10,
        }),
        ("5. from_date only (no to_date, no status)", {"from_date": start_date, "page": 1, "per_page": 10}),
        ("6. to_date only (no from_date, no status)", {"to_date": end_date, "page": 1, "per_page": 10}),
        (f"7. known-good single day {KNOWN_GOOD_HISTORICAL_DATE} (from_date=to_date, no status)", {
            "from_date": KNOWN_GOOD_HISTORICAL_DATE, "to_date": KNOWN_GOOD_HISTORICAL_DATE, "page": 1, "per_page": 10,
        }),
        (f"8. known-good single day {KNOWN_GOOD_HISTORICAL_DATE} + status=Attended", {
            "status": "Attended", "from_date": KNOWN_GOOD_HISTORICAL_DATE, "to_date": KNOWN_GOOD_HISTORICAL_DATE,
            "page": 1, "per_page": 10,
        }),
        (f"9. known-recent day {KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS} (expected 10 sessions) + status=Attended", {
            "status": "Attended",
            "from_date": KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS,
            "to_date": KNOWN_RECENT_DATE_WITH_EXPECTED_SESSIONS,
            "page": 1, "per_page": 100,
        }),
    ]

    print("=" * 70)
    print("TEACHWORKS DIAGNOSTIC MODE")
    print("Read-only. Zero Monday.com calls. Zero writes of any kind.")
    print(f"Date range used where applicable: {start_date} .. {end_date}")
    print("=" * 70)

    for label, params in variants:
        print(f"\n--- {label} ---")
        print(f"Request params: {params}")
        try:
            status_code, payload, records = tw_client.diagnostic_get("/lessons", params)
        except Exception as exc:  # noqa: BLE001 - one variant failing must not stop the others
            print(f"REQUEST ERROR: {exc}")
            continue

        print(f"HTTP status: {status_code}")
        if records is not None:
            print(f"Records returned: {len(records)}")
            if records:
                print("First record:")
                print(json.dumps(records[0], indent=2, default=str))
        else:
            print("Records returned: could not find a list of records in the response body.")
            if isinstance(payload, dict):
                print(f"Top-level response keys: {list(payload.keys())}")

    diagnose_teachworks_pagination(tw_client)

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - no Monday.com calls and no writes were made.")
    print("=" * 70)
    return 0


def diagnose_teachworks_pagination(tw_client, status="Attended", per_page=100, max_pages=MAX_DIAGNOSTIC_PAGES):
    """Read-only: walks every /lessons page for `status` with NO date filters,
    to see the full extent of what this credential can see. Does not print
    individual lessons — only aggregate counts and the earliest/latest
    `from_date` observed. Capped at `max_pages` for safety; clearly reports
    if that cap is hit. Zero Monday.com calls, zero writes."""
    print("\n" + "=" * 70)
    print(f"TEACHWORKS PAGINATION DIAGNOSTIC (status={status}, no date filters, cap={max_pages} pages)")
    print("Read-only. Does not print individual lessons.")
    print("=" * 70)

    total_lessons = 0
    earliest = None
    latest = None
    pages_fetched = 0
    cap_reached = False

    for page in range(1, max_pages + 1):
        try:
            status_code, payload, records = tw_client.diagnostic_get(
                "/lessons", {"status": status, "page": page, "per_page": per_page}
            )
        except Exception as exc:  # noqa: BLE001 - report and stop rather than crash the whole diagnostic
            print(f"REQUEST ERROR on page {page}: {exc}")
            break

        pages_fetched = page

        if status_code != 200:
            print(f"Stopping: page {page} returned HTTP {status_code}.")
            break
        if records is None:
            print(f"Stopping: page {page} response did not contain a recognizable record list.")
            break

        total_lessons += len(records)
        for record in records:
            record_date = record.get("from_date") if isinstance(record, dict) else None
            if record_date:
                if earliest is None or record_date < earliest:
                    earliest = record_date
                if latest is None or record_date > latest:
                    latest = record_date

        if len(records) < per_page:
            break
    else:
        cap_reached = True

    print(f"Total pages fetched: {pages_fetched}")
    print(f"Total lessons: {total_lessons}")
    print(f"Earliest from_date seen: {earliest}")
    print(f"Latest from_date seen: {latest}")
    if cap_reached:
        print(f"WARNING: safety cap of {max_pages} pages was reached. There is likely more data beyond this point.")
    print("=" * 70)


# Columns read for the dedup diagnostic: the production unique-ID column
# plus secondary fields (NEVER used for production deduplication decisions -
# the unique key, and the legacy bare-lesson-ID fallback, are the only
# production keys - see run_sync()).
_DEDUP_DIAGNOSTIC_COLUMNS = [
    config.COL_UNIQUE_ID,
    config.COL_SESSION_DATE,
    config.COL_TEACHWORKS_STUDENT_ID,
    config.COL_STUDENT_NAME,
    config.COL_TUTOR,
    config.COL_SERVICE,
]


def _classify_session(session, existing_unique_keys):
    """Compute the same composite/legacy keys run_sync() uses, and report
    which (if either) is already present in Monday's unique-ID column."""
    lesson_id = session.get("lesson_id")
    composite_key = session["unique_key"]
    legacy_key = str(lesson_id) if lesson_id is not None else None

    composite_exists = composite_key in existing_unique_keys
    legacy_exists = legacy_key is not None and legacy_key in existing_unique_keys

    return {
        "session": session,
        "composite_key": composite_key,
        "legacy_key": legacy_key,
        "composite_exists": composite_exists,
        "legacy_exists": legacy_exists,
        "matched": composite_exists or legacy_exists,
    }


def _find_likely_historical_matches(session, monday_items, limit=5):
    """Best-effort, READ-ONLY search for a historical Monday record that
    might represent this unmatched session under a different key format.
    Diagnostic only - NEVER used to decide production deduplication.

    Criteria (any one qualifies a candidate):
      A. exact session date + exact Teachworks Student ID
      B. exact session date + student name, when the Monday item's own
         Teachworks Student ID column is blank
      C. the Teachworks lesson ID appears in the Monday item's own name
    """
    session_date = session.get("session_date")
    student_id_str = str(session.get("student_id")) if session.get("student_id") is not None else None
    student_name = session.get("student_name")
    lesson_id_str = str(session.get("lesson_id")) if session.get("lesson_id") is not None else None

    matches = []
    for item in monday_items:
        cols = item["columns"]
        monday_student_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
        same_date = bool(session_date) and cols.get(config.COL_SESSION_DATE) == session_date

        criterion_a = same_date and student_id_str and monday_student_id == student_id_str
        criterion_b = (
            same_date and not monday_student_id
            and student_name and cols.get(config.COL_STUDENT_NAME) == student_name
        )
        criterion_c = bool(lesson_id_str) and lesson_id_str in (item.get("item_name") or "")

        if criterion_a or criterion_b or criterion_c:
            matches.append(item)
        if len(matches) >= limit:
            break
    return matches


def diagnose_dedup(tw_client, monday_client, start_date, end_date):
    """Read-only: for EVERY Teachworks participant session in range, reports
    whether it matches an existing Monday unique-ID value (composite or
    legacy bare-lesson-ID), exactly as run_sync() would decide. For sessions
    with no match, attempts a READ-ONLY, diagnostic-only search for a likely
    historical Monday record under a different key format. Never used to
    decide production deduplication, and makes ZERO Monday writes.

    Output is grouped into clearly separated, deterministically-ordered
    sections (per-session rows, then secondary investigation, then summary)
    rather than interleaved."""
    print("=" * 70)
    print("DEDUPLICATION DIAGNOSTIC (read-only, zero Monday writes)")
    print(f"Date range: {start_date} .. {end_date}")
    print("=" * 70)

    lessons = tw_client.get_lessons(start_date, end_date)
    sessions = tw_client.extract_attended_sessions(lessons)

    monday_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, _DEDUP_DIAGNOSTIC_COLUMNS)
    existing_unique_keys = {
        item["columns"].get(config.COL_UNIQUE_ID) for item in monday_items if item["columns"].get(config.COL_UNIQUE_ID)
    }

    rows = [_classify_session(session, existing_unique_keys) for session in sessions]
    # Deterministic order: by lesson ID then student ID, not fetch order.
    rows.sort(key=lambda r: (
        r["session"].get("lesson_id") if r["session"].get("lesson_id") is not None else -1,
        r["session"].get("student_id") if r["session"].get("student_id") is not None else -1,
    ))

    print("\n" + "-" * 70)
    print(f"PER-SESSION DIAGNOSTIC ROWS ({len(rows)} total, sorted by lesson ID then student ID)")
    print("-" * 70)
    for row in rows:
        session = row["session"]
        print(
            f"date={session.get('session_date')} lesson_id={session.get('lesson_id')} "
            f"student_id={session.get('student_id')} student_name={session.get('student_name')} "
            f"composite_key={row['composite_key']} composite_exists={row['composite_exists']} "
            f"legacy_exists={row['legacy_exists']} => {'MATCHED' if row['matched'] else 'UNMATCHED'}"
        )

    unmatched_rows = [r for r in rows if not r["matched"]]

    print("\n" + "-" * 70)
    print(f"SECONDARY INVESTIGATION for {len(unmatched_rows)} unmatched session(s) - DIAGNOSTIC ONLY")
    print("NEVER used for production deduplication.")
    print("-" * 70)

    unmatched_with_likely_match = 0
    for row in unmatched_rows:
        session = row["session"]
        print(
            f"\nUnmatched: lesson_id={session.get('lesson_id')} student_id={session.get('student_id')} "
            f"student_name={session.get('student_name')} date={session.get('session_date')} "
            f"expected_composite_key={row['composite_key']}"
        )

        likely_matches = _find_likely_historical_matches(session, monday_items)
        if not likely_matches:
            print("  No likely historical Monday record found.")
            continue

        unmatched_with_likely_match += 1
        for match in likely_matches:
            cols = match["columns"]
            print(f"  Monday item ID: {match['item_id']}")
            print(f"  Monday item name: {match.get('item_name') or '(blank)'}")
            print(f"  Monday session date: {cols.get(config.COL_SESSION_DATE) or '(blank)'}")
            print(f"  Monday Teachworks Student ID: {cols.get(config.COL_TEACHWORKS_STUDENT_ID) or '(blank)'}")
            print(f"  Monday {config.COL_UNIQUE_ID} value: {cols.get(config.COL_UNIQUE_ID) or '(blank)'}")
            print(f"  Tutor: {cols.get(config.COL_TUTOR) or '(blank)'}")
            print(f"  Service: {cols.get(config.COL_SERVICE) or '(blank)'}")

    composite_matches = sum(1 for r in rows if r["composite_exists"])
    legacy_only_matches = sum(1 for r in rows if r["legacy_exists"] and not r["composite_exists"])
    total_exact_matches = sum(1 for r in rows if r["matched"])
    unmatched_no_likely = len(unmatched_rows) - unmatched_with_likely_match

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Teachworks participant sessions: {len(rows)}")
    print(f"Composite-key matches: {composite_matches}")
    print(f"Legacy-key matches: {legacy_only_matches}")
    print(f"Total exact matches: {total_exact_matches}")
    print(f"Unmatched sessions: {len(unmatched_rows)}")
    print(f"Unmatched with likely date+student historical Monday record: {unmatched_with_likely_match}")
    print(f"Unmatched with no likely historical Monday record: {unmatched_no_likely}")

    unmatched_lesson_ids = sorted(
        {r["session"].get("lesson_id") for r in unmatched_rows if r["session"].get("lesson_id") is not None}
    )
    print(f"\nUnmatched Teachworks lesson IDs: {unmatched_lesson_ids}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


# Column titles we're trying to locate on the Students board before writing
# any student-metric code. Matched by exact title text against whatever the
# real API returns - never invented, never guessed from a screenshot.
_STUDENT_COLUMNS_OF_INTEREST = [
    "First Session",
    "Session Count",
    "Last session Date",
    "Tutor",
    "Session Data Last Updated",
    "Milestones",
    "Teachworks Student ID",
]


def diagnose_student_columns(monday_client):
    """Read-only: prints every column defined on the configured Students
    board (title, column ID, type), then flags which of the specific
    columns we're looking for were actually found. Makes ZERO Monday
    writes - this is schema discovery only, ahead of building any
    student-metric write code."""
    print("=" * 70)
    print("STUDENT BOARD COLUMN DIAGNOSTIC (read-only, zero Monday writes)")
    print(f"Board ID: {config.MONDAY_STUDENTS_BOARD_ID}")
    print("=" * 70)

    columns = monday_client.get_board_columns(config.MONDAY_STUDENTS_BOARD_ID)

    print(f"\n{len(columns)} column(s) found on this board:\n")
    title_width = max([len(c.get("title") or "") for c in columns] + [5])
    id_width = max([len(c.get("id") or "") for c in columns] + [9])
    header = f"{'TITLE':<{title_width}}  {'COLUMN ID':<{id_width}}  TYPE"
    print(header)
    print("-" * len(header))
    for col in columns:
        print(f"{(col.get('title') or ''):<{title_width}}  {(col.get('id') or ''):<{id_width}}  {col.get('type') or ''}")

    print("\n" + "-" * 70)
    print("Columns of interest for student-metric rollups (exact title match):")
    print("-" * 70)
    by_title = {(c.get("title") or "").strip().lower(): c for c in columns}
    for wanted in _STUDENT_COLUMNS_OF_INTEREST:
        match = by_title.get(wanted.strip().lower())
        if match:
            print(f"  FOUND     {wanted!r:<28} -> id={match['id']}  type={match['type']}")
        else:
            print(f"  NOT FOUND {wanted!r:<28} (no column with this exact title on the board)")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


# Columns read from the Session Log board to compute rollups. Deliberately
# NOT a fresh Teachworks crawl: every Session Log row already IS a
# Teachworks attended participant session, ingested by the locked,
# field-verified sync. Re-deriving rollups from years of one-day Teachworks
# queries would risk thousands of requests; this reads the board Monday
# already has, the same way get_existing_unique_ids/get_items do on every
# sync run today.
_SESSION_LOG_ROLLUP_SOURCE_COLUMNS = [
    config.COL_TEACHWORKS_STUDENT_ID,
    config.COL_SESSION_DATE,
    config.COL_TUTOR,
]

_STUDENT_ROLLUP_TARGET_COLUMNS = [
    config.STUDENT_BOARD_COL_TEACHWORKS_ID,
    config.STUDENT_COL_FIRST_SESSION_DATE,
    config.STUDENT_COL_SESSION_COUNT,
    config.STUDENT_COL_LAST_SESSION_DATE,
    config.STUDENT_COL_TUTOR,
    config.STUDENT_COL_SESSION_DATA_LAST_SYNCED,
]


def _aggregate_session_log_by_student(session_log_items):
    """Group Session Log rows by Teachworks Student ID and compute the
    lifetime rollup fields for each. Rows with no Teachworks Student ID or
    no session date are skipped (can't be attributed to a student or
    ordered). "Latest tutor" is the tutor on the row with the latest
    session date, breaking ties by item ID for determinism."""
    rows_by_student = {}
    for item in session_log_items:
        cols = item["columns"]
        tw_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
        session_date = cols.get(config.COL_SESSION_DATE)
        if not tw_id or not session_date:
            continue
        tutor = cols.get(config.COL_TUTOR)
        rows_by_student.setdefault(tw_id, []).append((session_date, item["item_id"], tutor))

    rollups = {}
    for tw_id, rows in rows_by_student.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        rollups[tw_id] = {
            "teachworks_student_id": tw_id,
            "first_session_date": rows[0][0],
            "last_session_date": rows[-1][0],
            "session_count": len(rows),
            "latest_tutor": rows[-1][2],
        }
    return rollups


def diagnose_student_rollups(monday_client, today=None):
    """Stage 1, read-only: calculates lifetime student rollups from the
    Monday Session Log board (see the module note above for why this - not
    a live Teachworks crawl), matches to Monday Students strictly by
    Teachworks Student ID (never by name), and reports CURRENT -> CALCULATED
    for each of the five target fields plus a MATCH / WOULD UPDATE / MISSING
    MONDAY STUDENT verdict per student. Makes ZERO Monday writes and issues
    ZERO Teachworks requests."""
    today = today or datetime.date.today().isoformat()

    print("=" * 70)
    print("STUDENT ROLLUP DIAGNOSTIC (read-only, zero Monday writes)")
    print("Calculated from the Monday Session Log board - see sync.py for why.")
    print("=" * 70)

    session_log_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, _SESSION_LOG_ROLLUP_SOURCE_COLUMNS)
    rollups = _aggregate_session_log_by_student(session_log_items)

    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, _STUDENT_ROLLUP_TARGET_COLUMNS)
    students_by_tw_id = {
        item["columns"].get(config.STUDENT_BOARD_COL_TEACHWORKS_ID): item
        for item in student_items
        if item["columns"].get(config.STUDENT_BOARD_COL_TEACHWORKS_ID)
    }

    missing = 0
    would_update = 0
    already_correct = 0

    for tw_id in sorted(rollups.keys()):
        rollup = rollups[tw_id]
        print(f"\nTeachworks Student ID: {tw_id}")
        print(
            f"  Calculated: first_session_date={rollup['first_session_date']} "
            f"session_count={rollup['session_count']} "
            f"last_session_date={rollup['last_session_date']} "
            f"latest_tutor={rollup['latest_tutor']}"
        )

        student_item = students_by_tw_id.get(tw_id)
        if not student_item:
            missing += 1
            print("  => MISSING MONDAY STUDENT (no Student item with this Teachworks Student ID; will not be created)")
            continue

        cols = student_item["columns"]
        current_count_raw = cols.get(config.STUDENT_COL_SESSION_COUNT) or ""
        try:
            current_count = int(current_count_raw)
        except ValueError:
            current_count = current_count_raw or "(blank)"

        # These four drive the MATCH / WOULD UPDATE verdict. "Session Data
        # Last Synced" is printed too (required below) but is a bookkeeping
        # timestamp that's expected to change on every real run, so it does
        # NOT by itself count as something needing correction.
        substantive = [
            ("First Session Date", cols.get(config.STUDENT_COL_FIRST_SESSION_DATE) or "(blank)", rollup["first_session_date"]),
            ("Session Count", current_count, rollup["session_count"]),
            ("Last Session Date", cols.get(config.STUDENT_COL_LAST_SESSION_DATE) or "(blank)", rollup["last_session_date"]),
            ("Tutor", cols.get(config.STUDENT_COL_TUTOR) or "(blank)", rollup["latest_tutor"]),
        ]
        for label, current, calculated in substantive:
            marker = "  <- WOULD CHANGE" if str(current) != str(calculated) else ""
            print(f"    {label}: {current} -> {calculated}{marker}")

        sync_date_current = cols.get(config.STUDENT_COL_SESSION_DATA_LAST_SYNCED) or "(blank)"
        print(f"    Session Data Last Synced: {sync_date_current} -> {today}")

        changed = any(str(current) != str(calculated) for _, current, calculated in substantive)
        if changed:
            would_update += 1
            print(f"  => WOULD UPDATE (Monday item {student_item['item_id']})")
        else:
            already_correct += 1
            print(f"  => MATCH (Monday item {student_item['item_id']}, already correct)")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Teachworks students calculated: {len(rollups)}")
    print(f"Monday students matched: {len(rollups) - missing}")
    print(f"Missing Monday students: {missing}")
    print(f"Students already correct: {already_correct}")
    print(f"Students that would change: {would_update}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made. Zero Teachworks requests were made.")
    print("=" * 70)
    return 0


def _sessions_after_date(session_log_items, baseline_date):
    """Group Session Log rows with a session date strictly after
    `baseline_date` by Teachworks Student ID: count, the latest date among
    them, and the tutor at that latest date (deterministic date+item_id
    tiebreak, same rule as _aggregate_session_log_by_student)."""
    rows_by_student = {}
    for item in session_log_items:
        cols = item["columns"]
        tw_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
        session_date = cols.get(config.COL_SESSION_DATE)
        if not tw_id or not session_date or session_date <= baseline_date:
            continue
        tutor = cols.get(config.COL_TUTOR)
        rows_by_student.setdefault(tw_id, []).append((session_date, item["item_id"], tutor))

    deltas = {}
    for tw_id, rows in rows_by_student.items():
        rows.sort(key=lambda row: (row[0], row[1]))
        deltas[tw_id] = {"count": len(rows), "max_date": rows[-1][0], "tutor_at_max_date": rows[-1][2]}
    return deltas


def diagnose_student_rollup_delta(monday_client, baseline_date):
    """Read-only validation of a baseline+delta rollup architecture, as an
    alternative to Stage 1's full-recompute-from-Session-Log approach
    (--diagnose-student-rollups). Restricts to Monday Students whose
    CURRENT Session Data Last Synced value equals `baseline_date` exactly,
    counts Session Log records strictly after that date, and shows what a
    baseline+delta approach would propose next to the student's current
    stored values. This is a comparison report only: it does not write
    anything, does not touch run_sync() or the Session Log pipeline, and
    does not decide which architecture gets built."""
    print("=" * 70)
    print("STUDENT ROLLUP BASELINE+DELTA VALIDATION (read-only, zero Monday writes)")
    print(f"Baseline date: {baseline_date}")
    print("=" * 70)

    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, _STUDENT_ROLLUP_TARGET_COLUMNS)
    baseline_students = [
        item for item in student_items
        if item["columns"].get(config.STUDENT_COL_SESSION_DATA_LAST_SYNCED) == baseline_date
    ]

    print(f"\n{len(baseline_students)} student(s) with Session Data Last Synced == {baseline_date}.")

    if baseline_students:
        session_log_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, _SESSION_LOG_ROLLUP_SOURCE_COLUMNS)
        deltas = _sessions_after_date(session_log_items, baseline_date)

        header = (
            f"Student | Current Session Count | Sessions after {baseline_date} | Proposed New Count | "
            "Current Last Session | New Last Session | Current Tutor | New Tutor"
        )
        print(f"\n{header}")
        print("-" * len(header))

        for item in sorted(baseline_students, key=lambda i: i.get("item_name") or ""):
            cols = item["columns"]
            tw_id = cols.get(config.STUDENT_BOARD_COL_TEACHWORKS_ID)
            student_name = item.get("item_name") or "(unnamed)"

            current_count_raw = cols.get(config.STUDENT_COL_SESSION_COUNT) or ""
            try:
                current_count = int(current_count_raw)
            except ValueError:
                current_count = 0

            current_last_session = cols.get(config.STUDENT_COL_LAST_SESSION_DATE) or "(blank)"
            current_tutor = cols.get(config.STUDENT_COL_TUTOR) or "(blank)"

            delta = deltas.get(tw_id)
            sessions_after = delta["count"] if delta else 0
            proposed_new_count = current_count + sessions_after

            if delta and (current_last_session == "(blank)" or delta["max_date"] > current_last_session):
                new_last_session = delta["max_date"]
                new_tutor = delta["tutor_at_max_date"]
            else:
                new_last_session = current_last_session
                new_tutor = current_tutor

            print(
                f"{student_name} | {current_count} | {sessions_after} | {proposed_new_count} | "
                f"{current_last_session} | {new_last_session} | {current_tutor} | {new_tutor}"
            )

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


# --- Stage 2: production-ready Student rollup calculation --------------
#
# Runs AFTER the Session Log sync (run_sync(), unchanged) in the intended
# nightly architecture, but is a fully separate step: it never touches
# run_sync(), the dedup/date-retrieval/normalization logic, or the Session
# Log write path. It iterates every MONDAY STUDENT item (not Teachworks-
# derived sessions) - matching "do not update all 1,096 students every
# night" - and uses EACH student's own currently-stored
# Session Data Last Synced value as ITS OWN checkpoint. First Session Date
# is intentionally never read into the update payload here: it must never
# be recalculated or overwritten.
#
# Includes config.COL_UNIQUE_ID (unlike Stage 1's source columns) so
# duplicate Session Log rows sharing a unique key are only ever counted
# once, defending against any stray duplicate Monday items.
_STUDENT_ROLLUP_V2_SOURCE_COLUMNS = [
    config.COL_UNIQUE_ID,
    config.COL_TEACHWORKS_STUDENT_ID,
    config.COL_SESSION_DATE,
    config.COL_TUTOR,
]


def _group_session_log_rows_by_student(session_log_items):
    """Dedupe Session Log rows by their own unique-ID column (a repeated
    unique key is only ever counted once), then group by Teachworks
    Student ID. Returns {tw_id: [(session_date, item_id, tutor), ...]},
    each list sorted by (date, item_id) for deterministic "latest" lookups."""
    deduped = {}
    for item in session_log_items:
        unique_key = item["columns"].get(config.COL_UNIQUE_ID)
        dedup_key = unique_key or item["item_id"]
        if dedup_key not in deduped:
            deduped[dedup_key] = item

    rows_by_student = {}
    for item in deduped.values():
        cols = item["columns"]
        tw_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
        session_date = cols.get(config.COL_SESSION_DATE)
        if not tw_id or not session_date:
            continue
        tutor = cols.get(config.COL_TUTOR)
        rows_by_student.setdefault(tw_id, []).append((session_date, item["item_id"], tutor))

    for rows in rows_by_student.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    return rows_by_student


def compute_student_rollup_updates(monday_client, today=None):
    """Calculates, for every Monday Student item, what a baseline+delta
    rollup update would be. Read-only: calls monday_client.get_items() only,
    never any write method.

    Per student: checkpoint = that student's OWN current Session Data Last
    Synced value. A blank checkpoint is treated as "no prior checkpoint" -
    every existing session for that student counts as new (plain string
    comparison: "" sorts before any real date). New sessions = Session Log
    rows with session_date strictly greater than the checkpoint.

    Every successfully MATCHED student receives Session Data Last Synced =
    `today` (the run date), whether or not it has new sessions - this is
    what "checkpoint-only" updates are for, so the checkpoint always
    reflects "as of when this last ran" even for students with nothing new.

    If new sessions exist: count, last session date, and tutor all update
    (a "rollup update"). If no new sessions exist: count, last session,
    tutor, and First Session Date are all left exactly as they are - ONLY
    the checkpoint moves (a "checkpoint-only" update).

    A Student item with a blank Teachworks Student ID cannot be matched at
    all; it's reported as unmatched, receives NO checkpoint and NO write of
    any kind, and is never created.

    Returns a list of per-student result dicts."""
    today = today or datetime.date.today().isoformat()

    session_log_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, _STUDENT_ROLLUP_V2_SOURCE_COLUMNS)
    rows_by_student = _group_session_log_rows_by_student(session_log_items)

    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, _STUDENT_ROLLUP_TARGET_COLUMNS)

    results = []
    for item in student_items:
        cols = item["columns"]
        tw_id = cols.get(config.STUDENT_BOARD_COL_TEACHWORKS_ID)
        student_name = item.get("item_name") or "(unnamed)"

        if not tw_id:
            results.append({
                "monday_item_id": item["item_id"],
                "student_name": student_name,
                "teachworks_student_id": None,
                "matched": False,
                "update_kind": None,
            })
            continue

        current_count_raw = cols.get(config.STUDENT_COL_SESSION_COUNT) or ""
        try:
            current_count = int(current_count_raw)
        except ValueError:
            current_count = 0

        current_last_session = cols.get(config.STUDENT_COL_LAST_SESSION_DATE) or ""
        current_tutor = cols.get(config.STUDENT_COL_TUTOR) or ""
        checkpoint = cols.get(config.STUDENT_COL_SESSION_DATA_LAST_SYNCED) or ""

        rows = rows_by_student.get(tw_id, [])
        new_rows = [row for row in rows if row[0] > checkpoint]
        has_new_sessions = len(new_rows) > 0

        if has_new_sessions:
            proposed_last_session = new_rows[-1][0]
            proposed_tutor = new_rows[-1][2]
        else:
            proposed_last_session = current_last_session
            proposed_tutor = current_tutor

        # Every matched student gets the checkpoint advanced to the run
        # date, regardless of whether it has new sessions.
        new_checkpoint = today

        results.append({
            "monday_item_id": item["item_id"],
            "student_name": student_name,
            "teachworks_student_id": tw_id,
            "matched": True,
            "current_count": current_count,
            "new_session_count": len(new_rows),
            "proposed_count": current_count + len(new_rows),
            "current_last_session": current_last_session or "(blank)",
            "proposed_last_session": proposed_last_session or "(blank)",
            "current_tutor": current_tutor or "(blank)",
            "proposed_tutor": proposed_tutor or "(blank)",
            "checkpoint_used": checkpoint or "(none - all sessions treated as new)",
            "new_checkpoint": new_checkpoint,
            "has_new_sessions": has_new_sessions,
            "update_kind": "rollup" if has_new_sessions else "checkpoint_only",
        })
    return results


def _real_or_none(value):
    """compute_student_rollup_updates() substitutes '(blank)' for display
    purposes; translate that sentinel back to None (omitted from the
    Monday write) rather than ever writing the literal string '(blank)'."""
    return None if value in (None, "(blank)") else value


def _student_write_column_values(result):
    """Build the Monday column_values for ONE student's write, from a
    compute_student_rollup_updates() result dict. NEVER includes
    STUDENT_COL_FIRST_SESSION_DATE - First Session Date is never written.

    update_kind == "rollup": Session Count, Last Session Date, Tutor, and
    Session Data Last Synced.
    update_kind == "checkpoint_only": ONLY Session Data Last Synced."""
    checkpoint_value = {"date": result["new_checkpoint"]}

    if result["update_kind"] == "rollup":
        last_session = _real_or_none(result["proposed_last_session"])
        return {
            config.STUDENT_COL_SESSION_COUNT: result["proposed_count"],
            config.STUDENT_COL_LAST_SESSION_DATE: {"date": last_session} if last_session else None,
            config.STUDENT_COL_TUTOR: _real_or_none(result["proposed_tutor"]),
            config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: checkpoint_value,
        }
    return {config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: checkpoint_value}


def apply_student_rollup_updates(monday_client, today=None):
    """Stage 3: performs the REAL Monday writes for the rollup calculated by
    compute_student_rollup_updates() - reused as-is, no second calculation
    path. Unmatched students (blank Teachworks Student ID) receive no
    write at all.

    Each student's fields (including the checkpoint) are written in ONE
    change_multiple_column_values mutation via
    MondayClient.update_student_columns(), so a failed write leaves EVERY
    field for that student - the checkpoint included - completely
    unchanged: the checkpoint only ever advances when the write actually
    succeeds. One student's failure is logged and does NOT stop the
    remaining students from being processed.

    Returns a dict of {results, written_rollup, written_checkpoint_only,
    skipped_unmatched, failed}."""
    today = today or datetime.date.today().isoformat()
    results = compute_student_rollup_updates(monday_client, today=today)

    written_rollup = []
    written_checkpoint_only = []
    skipped_unmatched = []
    failed = []

    for r in results:
        if not r["matched"]:
            skipped_unmatched.append(r)
            continue

        column_values = _student_write_column_values(r)
        try:
            monday_client.update_student_columns(config.MONDAY_STUDENTS_BOARD_ID, r["monday_item_id"], column_values)
        except Exception as exc:  # noqa: BLE001 - one bad student must not stop the run
            logger.error(
                "STUDENT_ROLLUP_WRITE_ERROR monday_item_id=%s teachworks_student_id=%s error=%s",
                r["monday_item_id"], r["teachworks_student_id"], exc,
            )
            failed.append({**r, "error": str(exc)})
            continue

        if r["update_kind"] == "rollup":
            written_rollup.append(r)
        else:
            written_checkpoint_only.append(r)

    return {
        "results": results,
        "written_rollup": written_rollup,
        "written_checkpoint_only": written_checkpoint_only,
        "skipped_unmatched": skipped_unmatched,
        "failed": failed,
    }


def run_student_rollup_apply(monday_client, today=None):
    """Stage 3, CLI-facing: applies REAL Monday writes for student rollups
    and reports the outcome. Only reachable via --student-rollups --apply
    (enforced in main(), not here)."""
    today = today or datetime.date.today().isoformat()

    print("=" * 70)
    print("STUDENT ROLLUP - APPLYING WRITES (Stage 3, REAL Monday writes)")
    print(f"Run date / new checkpoint for successfully-updated students: {today}")
    print("=" * 70)

    outcome = apply_student_rollup_updates(monday_client, today=today)

    if outcome["written_rollup"]:
        print(f"\nRollup updates written: {len(outcome['written_rollup'])}")
        for r in sorted(outcome["written_rollup"], key=lambda r: r["student_name"]):
            print(
                f"  {r['student_name']} (item {r['monday_item_id']}): count -> {r['proposed_count']}, "
                f"last session -> {r['proposed_last_session']}, tutor -> {r['proposed_tutor']}"
            )

    print(f"\nCheckpoint-only updates written: {len(outcome['written_checkpoint_only'])}")

    if outcome["skipped_unmatched"]:
        print(f"\nSkipped (cannot be matched, no write): {len(outcome['skipped_unmatched'])}")
        for r in sorted(outcome["skipped_unmatched"], key=lambda r: r["student_name"]):
            print(f"  Monday item {r['monday_item_id']} ({r['student_name']}): no Teachworks Student ID")

    if outcome["failed"]:
        print(f"\nFAILED writes (checkpoint NOT advanced - will be retried next run): {len(outcome['failed'])}")
        for r in sorted(outcome["failed"], key=lambda r: r["student_name"]):
            print(f"  {r['student_name']} (item {r['monday_item_id']}): {r['error']}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Students evaluated: {len(outcome['results'])}")
    print(f"Rollup updates written: {len(outcome['written_rollup'])}")
    print(f"Checkpoint-only updates written: {len(outcome['written_checkpoint_only'])}")
    print(f"Skipped (cannot be matched): {len(outcome['skipped_unmatched'])}")
    print(f"Failed writes: {len(outcome['failed'])}")

    print("\n" + "=" * 70)
    print("STUDENT ROLLUP WRITE COMPLETE.")
    print("=" * 70)
    return 1 if outcome["failed"] else 0


def diagnose_checkpoint_migration_readiness(monday_client, sample_size=5):
    """Read-only: gathers the two facts needed before considering a move
    from a date-only rollup checkpoint to a datetime one:

    A. The raw settings_str for the Session Data Last Synced column
       (STUDENT_COL_SESSION_DATA_LAST_SYNCED), to inspect whether time is
       enabled/supported on this actual column.
    B. A small sample of real Session Log items with their session_date,
       created_at, and Teachworks Student ID, to see the exact format/
       timezone Monday returns for created_at.

    Uses two new, isolated MondayClient methods
    (get_column_settings / get_sample_items_with_created_at) - neither is
    called by get_items()/_iter_board_items() or any production path.
    Makes ZERO Monday writes. Does not change run_sync(), the Session Log
    creation pipeline, dedup, or any rollup calculation."""
    print("=" * 70)
    print("CHECKPOINT MIGRATION READINESS DIAGNOSTIC (read-only, zero Monday writes)")
    print("=" * 70)

    print("\n--- A: Session Data Last Synced column settings ---")
    column = monday_client.get_column_settings(config.MONDAY_STUDENTS_BOARD_ID, config.STUDENT_COL_SESSION_DATA_LAST_SYNCED)
    print(f"Column ID: {column.get('id')}")
    print(f"Title: {column.get('title')}")
    print(f"Type: {column.get('type')}")
    print(f"Raw settings_str: {column.get('settings_str')}")

    print(f"\n--- B: Sample of {sample_size} Session Log item(s) with created_at ---")
    sample_columns = [config.COL_SESSION_DATE, config.COL_TEACHWORKS_STUDENT_ID]
    sample = monday_client.get_sample_items_with_created_at(config.MONDAY_SESSIONS_BOARD_ID, sample_columns, limit=sample_size)
    if not sample:
        print("No Session Log items found.")
    for item in sample:
        cols = item["columns"]
        print(
            f"  item_id={item['item_id']} "
            f"session_date={cols.get(config.COL_SESSION_DATE) or '(blank)'} "
            f"created_at={item.get('created_at')} "
            f"teachworks_student_id={cols.get(config.COL_TEACHWORKS_STUDENT_ID) or '(blank)'}"
        )

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


# Exact titles of the two new columns the baseline+SET migration needs.
# Neither exists yet - config.STUDENT_COL_HISTORICAL_BASELINE and
# config.COL_PRE_BASELINE stay None until a human creates these columns in
# Monday and this diagnostic reports their real IDs.
_BASELINE_MIGRATION_COLUMNS_OF_INTEREST = [
    ("Historical Session Baseline", "Students", "Numbers"),
    ("Pre-Baseline", "Session Log", "Checkbox"),
]


def diagnose_baseline_migration_columns(monday_client):
    """Stage 1, read-only: searches the Students board and the Session Log
    board for the two new columns the baseline+SET migration needs
    (Historical Session Baseline, Pre-Baseline) by exact title match, and
    reports their real column IDs if found. If either column is missing,
    prints the exact board/name/type a human needs to create manually in
    Monday first - never guesses an ID. Makes ZERO Monday writes."""
    print("=" * 70)
    print("BASELINE MIGRATION COLUMN DIAGNOSTIC (read-only, zero Monday writes)")
    print("=" * 70)

    student_columns = monday_client.get_board_columns(config.MONDAY_STUDENTS_BOARD_ID)
    session_columns = monday_client.get_board_columns(config.MONDAY_SESSIONS_BOARD_ID)
    columns_by_board = {"Students": student_columns, "Session Log": session_columns}

    all_found = True
    for title, board_name, suggested_type in _BASELINE_MIGRATION_COLUMNS_OF_INTEREST:
        columns = columns_by_board[board_name]
        by_title = {(c.get("title") or "").strip().lower(): c for c in columns}
        match = by_title.get(title.strip().lower())
        print(f"\nBoard: {board_name}")
        print(f"Column name: {title!r}")
        if match:
            print(f"  FOUND -> id={match['id']}  type={match['type']}")
        else:
            all_found = False
            print("  NOT FOUND. Create it manually in Monday first:")
            print(f"    Board: {board_name}")
            print(f"    Column name: {title}")
            print(f"    Column type: {suggested_type}")

    print("\n" + "=" * 70)
    if all_found:
        print("Both columns found. Fill their real IDs into config.py:")
        print("  STUDENT_COL_HISTORICAL_BASELINE, COL_PRE_BASELINE")
    else:
        print("One or more columns are missing. Create them in Monday, then re-run this diagnostic.")
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


def diagnose_baseline_migration(monday_client, sample_size=10):
    """Stage 2, read-only migration dry run for the baseline+SET
    architecture. Reports exactly what a real migration WOULD write on both
    the Students board (Historical Session Baseline) and the Session Log
    board (Pre-Baseline), without writing anything. Refuses to run (prints
    an error, returns 1) if the two new column IDs haven't been filled into
    config.py yet - never guesses them.

    This function does not decide or enforce the migration cutover boundary
    (which Session Log item IDs are frozen into the pre-baseline set) - it
    reports the board state as read in this single pass, which is exactly
    why that boundary must be established by a separate, explicit freeze
    step before any migration write code is built. See the accompanying
    investigation for that design."""
    if config.STUDENT_COL_HISTORICAL_BASELINE is None or config.COL_PRE_BASELINE is None:
        print("ERROR: --diagnose-baseline-migration requires both new column IDs to be configured first.")
        print("  config.STUDENT_COL_HISTORICAL_BASELINE and config.COL_PRE_BASELINE are still None.")
        print("  Run --diagnose-baseline-migration-columns, create the columns in Monday if needed,")
        print("  then fill their real IDs into config.py before running this migration dry run.")
        return 1

    print("=" * 70)
    print("BASELINE MIGRATION DRY RUN (read-only, zero Monday writes)")
    print("=" * 70)

    student_columns = [
        config.STUDENT_BOARD_COL_TEACHWORKS_ID,
        config.STUDENT_COL_SESSION_COUNT,
        config.STUDENT_COL_HISTORICAL_BASELINE,
    ]
    session_columns = [
        config.COL_UNIQUE_ID,
        config.COL_TEACHWORKS_STUDENT_ID,
        config.COL_PRE_BASELINE,
    ]
    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, student_columns)
    session_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, session_columns)

    # --- STUDENTS ---
    students_evaluated = len(student_items)
    with_count = 0
    without_count = 0
    already_baselined = 0
    would_receive_baseline = []
    for item in student_items:
        cols = item["columns"]
        count_raw = (cols.get(config.STUDENT_COL_SESSION_COUNT) or "").strip()
        try:
            count = int(count_raw)
            with_count += 1
        except ValueError:
            count = 0
            without_count += 1

        baseline_raw = (cols.get(config.STUDENT_COL_HISTORICAL_BASELINE) or "").strip()
        if baseline_raw:
            already_baselined += 1
        else:
            would_receive_baseline.append({
                "item_id": item["item_id"],
                "student_name": item.get("item_name") or "",
                "current_count": count_raw or "(blank)",
                "proposed_baseline": count,
            })

    print("\n--- STUDENTS ---")
    print(f"Students evaluated: {students_evaluated}")
    print(f"Students with current Session Count: {with_count}")
    print(f"Students with blank/null Session Count: {without_count}")
    print(f"Students whose Historical Session Baseline is already populated: {already_baselined}")
    print(f"Students that WOULD receive a baseline: {len(would_receive_baseline)}")

    if would_receive_baseline:
        print(f"\nSample (first {min(sample_size, len(would_receive_baseline))} of {len(would_receive_baseline)}):")
        print(f"{'Student':<30}  {'Current Session Count':<22}  Proposed Historical Baseline")
        for row in would_receive_baseline[:sample_size]:
            print(f"{row['student_name']:<30}  {str(row['current_count']):<22}  {row['proposed_baseline']}")

    # --- SESSION LOG ---
    rows_evaluated = len(session_items)
    already_pre_baseline = 0
    would_be_marked = []
    missing_student_id = 0
    missing_unique_key = 0
    seen_unique_keys = {}
    for item in session_items:
        cols = item["columns"]
        unique_key = (cols.get(config.COL_UNIQUE_ID) or "").strip()
        tw_student_id = (cols.get(config.COL_TEACHWORKS_STUDENT_ID) or "").strip()
        pre_baseline_raw = (cols.get(config.COL_PRE_BASELINE) or "").strip()

        if not tw_student_id:
            missing_student_id += 1
        if not unique_key:
            missing_unique_key += 1
        else:
            seen_unique_keys.setdefault(unique_key, []).append(item["item_id"])

        if pre_baseline_raw:
            already_pre_baseline += 1
        else:
            would_be_marked.append({
                "item_id": item["item_id"],
                "item_name": item.get("item_name") or "",
                "unique_key": unique_key or "(blank)",
                "teachworks_student_id": tw_student_id or "(blank)",
            })

    duplicate_unique_keys = {key: ids for key, ids in seen_unique_keys.items() if len(ids) > 1}

    print("\n--- SESSION LOG ---")
    print(f"Session Log rows evaluated: {rows_evaluated}")
    print(f"Rows already marked Pre-Baseline: {already_pre_baseline}")
    print(f"Rows that WOULD be marked Pre-Baseline: {len(would_be_marked)}")
    print(f"Duplicate unique identities detected: {len(duplicate_unique_keys)}")
    for key, ids in sorted(duplicate_unique_keys.items()):
        print(f"  unique_key={key}  item_ids={ids}")
    print(f"Rows missing Teachworks Student ID: {missing_student_id}")
    print(f"Rows missing unique_key: {missing_unique_key}")

    if would_be_marked:
        print(f"\nSample (first {min(sample_size, len(would_be_marked))} of {len(would_be_marked)}):")
        print(f"{'Item ID':<10}  {'Item Name':<30}  {'Unique Key':<20}  Teachworks Student ID")
        for row in would_be_marked[:sample_size]:
            print(f"{row['item_id']:<10}  {row['item_name']:<30}  {row['unique_key']:<20}  {row['teachworks_student_id']}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("This is NOT the migration cutover: see the accompanying freeze-boundary design")
    print("before any migration write code is built.")
    print("=" * 70)
    return 0


_SESSION_LOG_DUPLICATE_INVESTIGATION_COLUMNS = [
    config.COL_UNIQUE_ID,
    config.COL_TEACHWORKS_STUDENT_ID,
    config.COL_SESSION_DATE,
    config.COL_TUTOR,
]


def _unique_key_format_and_lesson_id(unique_key):
    """Classify a unique_key string as the composite format run_sync() writes
    today ('{lesson_id}_{student_id}') or the bare legacy format the old
    Zapier automation wrote, and pull out the shared lesson_id token either
    way. Blank keys return (None, None)."""
    if not unique_key:
        return None, None
    if "_" in unique_key:
        lesson_id, _, _student_id = unique_key.rpartition("_")
        return "composite", lesson_id
    return "legacy_bare", unique_key


def diagnose_session_log_duplicates(monday_client, sample_size=25):
    """Read-only investigation of duplicate Session Log unique identities,
    ahead of any baseline migration decision. Reports two DIFFERENT kinds
    of duplication, since they have different implications for the
    existing rollup dedup logic (_group_session_log_rows_by_student):

    1. EXACT duplicates: two or more Monday items share the literal same
       COL_UNIQUE_ID text (whatever its format). The current rollup grouping
       already collapses these to one, since it dedupes by exact string
       match before counting - but "more than one Monday item exists with
       identical content" is still worth a human's eyes.

    2. CROSS-FORMAT collisions: a bare legacy-format key (e.g. "89515688")
       and a composite key sharing the same leading lesson_id token (e.g.
       "89515688_2203327") appear as SEPARATE Monday items. These are NOT
       caught by the exact-duplicate check above (different literal
       strings) and are NOT collapsed by the current rollup dedup logic
       either - each would be counted as its own distinct session. Whether
       that's a real double-count depends on whether they represent the
       SAME participant (same Teachworks Student ID) or genuinely different
       participants in the same lesson; this diagnostic reports the
       Teachworks Student ID column directly from each item (independent of
       what's encoded in the key) so a human can judge that.

    Makes ZERO Monday writes. Does not change dedup logic, rollup
    calculation, or the Session Log sync pipeline in any way."""
    print("=" * 70)
    print("SESSION LOG DUPLICATE INVESTIGATION (read-only, zero Monday writes)")
    print("=" * 70)

    items = monday_client.get_items_with_created_at(
        config.MONDAY_SESSIONS_BOARD_ID, _SESSION_LOG_DUPLICATE_INVESTIGATION_COLUMNS
    )

    def _row(item):
        cols = item["columns"]
        return {
            "item_id": item["item_id"],
            "item_name": item.get("item_name") or "",
            "unique_key": cols.get(config.COL_UNIQUE_ID) or "",
            "teachworks_student_id": cols.get(config.COL_TEACHWORKS_STUDENT_ID) or "",
            "session_date": cols.get(config.COL_SESSION_DATE) or "",
            "tutor": cols.get(config.COL_TUTOR) or "",
            "created_at": item.get("created_at"),
        }

    rows = [_row(item) for item in items]

    # --- 1. Exact duplicate unique keys -----------------------------------
    by_unique_key = {}
    for row in rows:
        if row["unique_key"]:
            by_unique_key.setdefault(row["unique_key"], []).append(row)
    exact_duplicate_groups = {key: group for key, group in by_unique_key.items() if len(group) > 1}

    print(f"\nSession Log rows evaluated: {len(rows)}")
    print(f"Distinct non-blank unique keys: {len(by_unique_key)}")
    print(f"Exact duplicate unique-key groups: {len(exact_duplicate_groups)}")

    print("\n--- EXACT DUPLICATE UNIQUE KEYS ---")
    if not exact_duplicate_groups:
        print("None found.")
    sorted_dup_keys = sorted(exact_duplicate_groups)
    if len(sorted_dup_keys) > sample_size:
        print(f"(showing first {sample_size} of {len(sorted_dup_keys)} groups)")
    for key in sorted_dup_keys[:sample_size]:
        group = exact_duplicate_groups[key]
        key_format, lesson_id = _unique_key_format_and_lesson_id(key)
        print(f"\nunique_key={key!r}  format={key_format}  derived_lesson_id={lesson_id}  ({len(group)} rows)")
        print(f"  {'Item ID':<10}  {'Item Name':<28}  {'TW Student ID':<14}  {'Session Date':<13}  {'Tutor':<18}  Created At")
        for row in group:
            print(
                f"  {row['item_id']:<10}  {row['item_name']:<28}  {row['teachworks_student_id']:<14}  "
                f"{row['session_date']:<13}  {row['tutor']:<18}  {row['created_at']}"
            )
        distinct_student_ids = {row["teachworks_student_id"] for row in group}
        if len(distinct_student_ids) > 1:
            print(f"  NOTE: rows disagree on Teachworks Student ID ({sorted(distinct_student_ids)}) - investigate before assuming these are the same session.")

    # --- 2. Cross-format lesson_id collisions -----------------------------
    by_lesson_id = {}
    for row in rows:
        key_format, lesson_id = _unique_key_format_and_lesson_id(row["unique_key"])
        if lesson_id is None:
            continue
        by_lesson_id.setdefault(lesson_id, []).append((key_format, row))

    cross_format_groups = {}
    for lesson_id, entries in by_lesson_id.items():
        formats_present = {fmt for fmt, _row in entries}
        if "legacy_bare" in formats_present and "composite" in formats_present:
            cross_format_groups[lesson_id] = entries

    print(f"\n--- CROSS-FORMAT LESSON ID COLLISIONS (bare legacy key + composite key sharing a lesson_id) ---")
    print(f"Lesson IDs with both a legacy bare key and a composite key: {len(cross_format_groups)}")
    sorted_cross_format_lesson_ids = sorted(cross_format_groups)
    if len(sorted_cross_format_lesson_ids) > sample_size:
        print(f"(showing first {sample_size} of {len(sorted_cross_format_lesson_ids)})")
    same_student_by_lesson_id = {
        lesson_id: len({row["teachworks_student_id"] for _fmt, row in entries}) == 1
        for lesson_id, entries in cross_format_groups.items()
    }
    likely_true_duplicates = sum(1 for same_student in same_student_by_lesson_id.values() if same_student)
    for lesson_id in sorted_cross_format_lesson_ids[:sample_size]:
        entries = cross_format_groups[lesson_id]
        same_student = same_student_by_lesson_id[lesson_id]
        print(f"\nlesson_id={lesson_id}  ({len(entries)} rows, {'SAME' if same_student else 'DIFFERENT'} Teachworks Student ID across rows)")
        print(f"  {'Item ID':<10}  {'Format':<12}  {'Unique Key':<20}  {'TW Student ID':<14}  {'Session Date':<13}  {'Tutor':<18}  Created At")
        for fmt, row in entries:
            print(
                f"  {row['item_id']:<10}  {fmt:<12}  {row['unique_key']:<20}  {row['teachworks_student_id']:<14}  "
                f"{row['session_date']:<13}  {row['tutor']:<18}  {row['created_at']}"
            )
        if same_student:
            print("  LIKELY TRUE DUPLICATE: same lesson_id, same Teachworks Student ID, stored under two different key formats.")
        else:
            print("  LIKELY LEGITIMATE: same lesson, different participants - not a duplicate.")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Session Log rows evaluated: {len(rows)}")
    print(f"Exact duplicate unique-key groups: {len(exact_duplicate_groups)}")
    print(f"Cross-format lesson_id collisions: {len(cross_format_groups)}")
    print(f"  of which same-student (likely true duplicate): {likely_true_duplicates}")
    print(f"  of which different-student (likely legitimate multi-participant lesson): {len(cross_format_groups) - likely_true_duplicates}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("Nothing was deleted, archived, modified, or marked.")
    print("=" * 70)
    return 0


# --- Post-baseline canonical identity dedup -----------------------------
#
# NOT YET WIRED to any CLI command, write path, or the future --daily-sync.
# This is new, additive logic for the future baseline+SET nightly rollup
# calculation. _group_session_log_rows_by_student() above backs the
# CURRENT, still-live date-checkpoint --student-rollups command and is
# deliberately left untouched by everything below - a different function
# is needed because the post-baseline calculation requires a different
# notion of "duplicate": canonical (lesson_id, Teachworks Student ID)
# identity, not literal unique_key text, so a legacy bare key and a
# composite key for the same lesson+student collapse to one (see
# --diagnose-session-log-duplicates, which found 14 such collisions in
# production).
#
# Columns this will need once it IS wired up (not used by anything yet).
_POST_BASELINE_ROLLUP_SOURCE_COLUMNS = [
    config.COL_UNIQUE_ID,
    config.COL_TEACHWORKS_STUDENT_ID,
    config.COL_SESSION_DATE,
    config.COL_TUTOR,
    config.COL_PRE_BASELINE,
]


def _canonical_session_identity(item):
    """(lesson_id, teachworks_student_id) for one Session Log item.
    teachworks_student_id always comes from the item's own
    COL_TEACHWORKS_STUDENT_ID column (authoritative), never parsed out of
    the unique_key's suffix. lesson_id is derived from unique_key via
    _unique_key_format_and_lesson_id(), which already handles both the
    composite ('{lesson_id}_{student_id}') and legacy bare formats - so a
    bare key and a composite key for the same lesson+student produce the
    identical identity here. Falls back to (None, item_id) when either
    half is missing, so a row is never silently dropped - it just can't be
    collapsed with anything else."""
    cols = item["columns"]
    _key_format, lesson_id = _unique_key_format_and_lesson_id(cols.get(config.COL_UNIQUE_ID))
    tw_student_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
    if lesson_id and tw_student_id:
        return (lesson_id, tw_student_id)
    return (None, item["item_id"])


def _group_post_baseline_session_log_rows_by_student(session_log_items):
    """Like _group_session_log_rows_by_student(), but for the future
    baseline+SET architecture:

    1. Rows with Pre-Baseline=Yes are excluded FIRST, unconditionally -
       before any dedup happens. A Pre-Baseline row can never suppress,
       merge with, or contribute to a post-baseline count, regardless of
       whether it shares a canonical identity with a surviving row.
    2. The remaining (post-baseline) rows are deduped by canonical
       (lesson_id, Teachworks Student ID) identity - see
       _canonical_session_identity() - instead of literal unique_key text,
       so a legacy bare key and a composite key for the same lesson+student
       collapse to one.
    3. Survivors are grouped by Teachworks Student ID, same output shape as
       _group_session_log_rows_by_student(): {tw_id: [(session_date,
       item_id, tutor), ...]}, sorted by (date, item_id). A row with a
       blank Teachworks Student ID or blank session_date is skipped from
       grouping, matching that function's existing behavior.

    Does not call or modify _group_session_log_rows_by_student()."""
    post_baseline_items = [
        item for item in session_log_items
        if not (item["columns"].get(config.COL_PRE_BASELINE) or "").strip()
    ]

    deduped = {}
    for item in post_baseline_items:
        identity = _canonical_session_identity(item)
        if identity not in deduped:
            deduped[identity] = item

    rows_by_student = {}
    for item in deduped.values():
        cols = item["columns"]
        tw_id = cols.get(config.COL_TEACHWORKS_STUDENT_ID)
        session_date = cols.get(config.COL_SESSION_DATE)
        if not tw_id or not session_date:
            continue
        tutor = cols.get(config.COL_TUTOR)
        rows_by_student.setdefault(tw_id, []).append((session_date, item["item_id"], tutor))

    for rows in rows_by_student.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    return rows_by_student


def diagnose_baseline_migration_cutover(monday_client):
    """Read-only migration CUTOVER preview. Reads ONE frozen snapshot each
    of the Students board and the Session Log board (two get_items() calls,
    back-to-back, nothing written in between or after), and reports exactly
    what a real one-time migration would do against that snapshot - plus an
    in-memory simulation of the state immediately after it. Makes ZERO
    Monday writes.

    Refuses to run (prints an error, returns 1) if the two migration
    column IDs are not yet configured - same guard as
    diagnose_baseline_migration().

    Reuses, without modifying: _unique_key_format_and_lesson_id(),
    _canonical_session_identity(), and
    _group_post_baseline_session_log_rows_by_student(). Does not call or
    touch _group_session_log_rows_by_student() or
    compute_student_rollup_updates() - the separate, still-live
    date-checkpoint rollup path."""
    if config.STUDENT_COL_HISTORICAL_BASELINE is None or config.COL_PRE_BASELINE is None:
        print("ERROR: --diagnose-baseline-migration-cutover requires both new column IDs to be configured first.")
        print("  config.STUDENT_COL_HISTORICAL_BASELINE and config.COL_PRE_BASELINE are still None.")
        print("  Run --diagnose-baseline-migration-columns first if either is missing.")
        return 1

    print("=" * 70)
    print("BASELINE MIGRATION CUTOVER PREVIEW (read-only, zero Monday writes)")
    print("One frozen snapshot: Students + Session Log read back-to-back, no writes in between.")
    print("=" * 70)

    student_columns = [
        config.STUDENT_BOARD_COL_TEACHWORKS_ID,
        config.STUDENT_COL_SESSION_COUNT,
        config.STUDENT_COL_HISTORICAL_BASELINE,
    ]
    session_columns = [
        config.COL_UNIQUE_ID,
        config.COL_TEACHWORKS_STUDENT_ID,
        config.COL_SESSION_DATE,
        config.COL_TUTOR,
        config.COL_PRE_BASELINE,
    ]
    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, student_columns)
    session_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, session_columns)

    # --- STUDENTS ---
    students_evaluated = len(student_items)
    would_receive_baseline = []
    blank_or_zero_count = 0
    for item in student_items:
        cols = item["columns"]
        raw = (cols.get(config.STUDENT_COL_SESSION_COUNT) or "").strip()
        try:
            count = int(raw)
        except ValueError:
            count = 0
        if not raw or count == 0:
            blank_or_zero_count += 1

        baseline_raw = (cols.get(config.STUDENT_COL_HISTORICAL_BASELINE) or "").strip()
        if not baseline_raw:
            would_receive_baseline.append({
                "item_id": item["item_id"],
                "student_name": item.get("item_name") or "",
                "count": count,
            })

    baseline_sum = sum(row["count"] for row in would_receive_baseline)

    print("\n--- STUDENTS ---")
    print(f"Students evaluated: {students_evaluated}")
    print(f"Students that would receive Historical Session Baseline: {len(would_receive_baseline)}")
    print(f"Sum of current Session Count values that would be frozen into Historical Session Baseline: {baseline_sum}")
    print(f"Students with blank/zero Session Count: {blank_or_zero_count}")

    # --- SESSION LOG CUTOVER ---
    rows_evaluated = len(session_items)
    already_pre_baseline = 0
    would_be_marked = 0
    by_unique_key = {}
    identity_counts = {}
    missing_identity = 0
    for item in session_items:
        pre_baseline_raw = (item["columns"].get(config.COL_PRE_BASELINE) or "").strip()
        if pre_baseline_raw:
            already_pre_baseline += 1
        else:
            would_be_marked += 1

        unique_key = (item["columns"].get(config.COL_UNIQUE_ID) or "").strip()
        if unique_key:
            by_unique_key.setdefault(unique_key, []).append(item)

        identity = _canonical_session_identity(item)
        if identity[0] is None:
            missing_identity += 1
        identity_counts[identity] = identity_counts.get(identity, 0) + 1

    exact_duplicate_groups = {key: group for key, group in by_unique_key.items() if len(group) > 1}

    by_lesson_id_formats = {}
    for item in session_items:
        key_format, lesson_id = _unique_key_format_and_lesson_id(item["columns"].get(config.COL_UNIQUE_ID))
        if lesson_id is None:
            continue
        by_lesson_id_formats.setdefault(lesson_id, set()).add(key_format)
    cross_format_groups = {
        lesson_id: formats for lesson_id, formats in by_lesson_id_formats.items()
        if "legacy_bare" in formats and "composite" in formats
    }

    distinct_identities = len(identity_counts)
    duplicate_rows_collapsed = rows_evaluated - distinct_identities

    print("\n--- SESSION LOG CUTOVER ---")
    print(f"Total Session Log rows in the frozen snapshot: {rows_evaluated}")
    print(f"Rows that would be marked Pre-Baseline = Yes: {would_be_marked}")
    print(f"  (already marked Pre-Baseline = Yes: {already_pre_baseline})")
    print(f"Distinct canonical session identities represented: {distinct_identities}")
    print(f"Duplicate physical rows collapsed by canonical identity: {duplicate_rows_collapsed}")
    print(f"Exact-key duplicate groups: {len(exact_duplicate_groups)}")
    print(f"Cross-format bare/composite duplicate groups: {len(cross_format_groups)}")
    print(f"Rows missing canonical identity information: {missing_identity}")

    # --- POST-MIGRATION VALIDATION (in-memory simulation only, no writes) ---
    simulated_items = [
        {**item, "columns": {**item["columns"], config.COL_PRE_BASELINE: "v"}}
        for item in session_items
    ]
    post_migration_groups = _group_post_baseline_session_log_rows_by_student(simulated_items)
    post_migration_identity_count = sum(len(rows) for rows in post_migration_groups.values())

    print("\n--- POST-MIGRATION VALIDATION (in-memory simulation, zero Monday writes) ---")
    print("Simulated: every row in this frozen snapshot has Pre-Baseline = Yes.")
    print(f"Post-baseline session identities immediately after migration: {post_migration_identity_count}")
    if post_migration_identity_count == 0:
        print("CONFIRMED: no existing historical Session Log row can contribute again after cutover.")
        print("Therefore each student's proposed Session Count immediately after migration remains")
        print("exactly its frozen Historical Session Baseline (baseline + 0 post-baseline = baseline).")
    else:
        print("WARNING: expected 0 post-baseline identities immediately after a full-snapshot cutover.")
        print("This means the simulation or the frozen snapshot is inconsistent - investigate before migrating.")

    # --- PROPOSED MIGRATION WRITE COUNTS (not performed - preview only) ---
    student_baseline_writes = len(would_receive_baseline)
    session_log_pre_baseline_writes = would_be_marked
    total_writes = student_baseline_writes + session_log_pre_baseline_writes

    print("\n--- PROPOSED MIGRATION WRITE COUNTS (not performed - preview only) ---")
    print(f"Student baseline writes: {student_baseline_writes}")
    print(f"Session Log Pre-Baseline writes: {session_log_pre_baseline_writes}")
    print(f"Total Monday items that would be modified: {total_writes}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("=" * 70)
    return 0


# --- One-time baseline+SET migration APPLY -------------------------------
#
# Wholly separate from normal sync behavior: does not call or modify
# run_sync(), Teachworks retrieval, Session Log creation/dedup/connection
# logic, _group_session_log_rows_by_student(),
# compute_student_rollup_updates(), or
# _group_post_baseline_session_log_rows_by_student(). Reachable ONLY via
# --apply-baseline-migration.

def apply_baseline_migration(monday_client):
    """Computes the write set from ONE frozen Students + Session Log
    snapshot (two get_items() calls, back-to-back, no writes in between),
    using the exact same calculation demonstrated by the approved
    --diagnose-baseline-migration-cutover preview, then performs the real
    writes:

    - Each Student whose Historical Session Baseline is not already
      populated gets ONLY that column SET to their current Session Count
      (blank/null Session Count -> baseline 0). Session Count itself is
      never written.
    - Each Session Log item in the frozen snapshot that isn't already
      marked gets ONLY Pre-Baseline SET to Yes. No other Session Log
      column is written, and no row is deleted, archived, or merged -
      historical duplicates stay in place.

    An already-migrated student (Historical Session Baseline already
    populated) or an already-marked Session Log row is skipped entirely -
    never re-written - so this is safe to re-run.

    One write failure is logged and does NOT stop the remaining writes.
    Returns a dict of counts/failures for the CLI wrapper to report."""
    student_columns = [
        config.STUDENT_BOARD_COL_TEACHWORKS_ID,
        config.STUDENT_COL_SESSION_COUNT,
        config.STUDENT_COL_HISTORICAL_BASELINE,
    ]
    session_columns = [
        config.COL_UNIQUE_ID,
        config.COL_TEACHWORKS_STUDENT_ID,
        config.COL_SESSION_DATE,
        config.COL_TUTOR,
        config.COL_PRE_BASELINE,
    ]
    student_items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, student_columns)
    session_items = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, session_columns)

    students_to_baseline = []
    for item in student_items:
        cols = item["columns"]
        baseline_raw = (cols.get(config.STUDENT_COL_HISTORICAL_BASELINE) or "").strip()
        if baseline_raw:
            continue
        raw_count = (cols.get(config.STUDENT_COL_SESSION_COUNT) or "").strip()
        try:
            count = int(raw_count)
        except ValueError:
            count = 0
        students_to_baseline.append({"item_id": item["item_id"], "baseline": count})

    session_rows_to_mark = [
        item["item_id"] for item in session_items
        if not (item["columns"].get(config.COL_PRE_BASELINE) or "").strip()
    ]

    student_writes_succeeded = 0
    student_writes_failed = []
    for row in students_to_baseline:
        try:
            monday_client.update_student_columns(
                config.MONDAY_STUDENTS_BOARD_ID,
                row["item_id"],
                {config.STUDENT_COL_HISTORICAL_BASELINE: row["baseline"]},
            )
            student_writes_succeeded += 1
        except Exception as exc:  # noqa: BLE001 - one bad write must not stop the migration
            logger.error("BASELINE_WRITE_ERROR item_id=%s error=%s", row["item_id"], exc)
            student_writes_failed.append({"item_id": row["item_id"], "error": str(exc)})

    session_writes_succeeded = 0
    session_writes_failed = []
    for item_id in session_rows_to_mark:
        try:
            monday_client.update_student_columns(
                config.MONDAY_SESSIONS_BOARD_ID,
                item_id,
                {config.COL_PRE_BASELINE: {"checked": "true"}},
            )
            session_writes_succeeded += 1
        except Exception as exc:  # noqa: BLE001 - one bad write must not stop the migration
            logger.error("PRE_BASELINE_WRITE_ERROR item_id=%s error=%s", item_id, exc)
            session_writes_failed.append({"item_id": item_id, "error": str(exc)})

    return {
        "students_to_baseline": len(students_to_baseline),
        "student_writes_succeeded": student_writes_succeeded,
        "student_writes_failed": student_writes_failed,
        "session_rows_to_mark": len(session_rows_to_mark),
        "session_writes_succeeded": session_writes_succeeded,
        "session_writes_failed": session_writes_failed,
    }


def run_baseline_migration_apply(monday_client):
    """CLI-facing wrapper: performs the one-time baseline migration apply
    and prints the required summary. Only reachable via
    --apply-baseline-migration (enforced in main())."""
    if config.STUDENT_COL_HISTORICAL_BASELINE is None or config.COL_PRE_BASELINE is None:
        print("ERROR: --apply-baseline-migration requires both new column IDs to be configured first.")
        print("  config.STUDENT_COL_HISTORICAL_BASELINE and config.COL_PRE_BASELINE are still None.")
        print("  Run --diagnose-baseline-migration-columns first if either is missing.")
        return 1

    print("=" * 70)
    print("BASELINE MIGRATION APPLY (one-time, REAL Monday writes)")
    print("One frozen snapshot: Students + Session Log read back-to-back before any write.")
    print("=" * 70)

    outcome = apply_baseline_migration(monday_client)

    print(f"\nStudents to receive Historical Session Baseline (from this snapshot): {outcome['students_to_baseline']}")
    print(f"Session Log rows to mark Pre-Baseline = Yes (from this snapshot): {outcome['session_rows_to_mark']}")

    if outcome["student_writes_failed"]:
        print(f"\nFAILED student baseline writes: {len(outcome['student_writes_failed'])}")
        for f in outcome["student_writes_failed"]:
            print(f"  Monday item {f['item_id']}: {f['error']}")

    if outcome["session_writes_failed"]:
        print(f"\nFAILED Session Log Pre-Baseline writes: {len(outcome['session_writes_failed'])}")
        for f in outcome["session_writes_failed"]:
            print(f"  Monday item {f['item_id']}: {f['error']}")

    total_succeeded = outcome["student_writes_succeeded"] + outcome["session_writes_succeeded"]
    total_failed = len(outcome["student_writes_failed"]) + len(outcome["session_writes_failed"])

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Student baseline writes succeeded: {outcome['student_writes_succeeded']}")
    print(f"Student baseline writes failed: {len(outcome['student_writes_failed'])}")
    print(f"Session Log Pre-Baseline writes succeeded: {outcome['session_writes_succeeded']}")
    print(f"Session Log Pre-Baseline writes failed: {len(outcome['session_writes_failed'])}")
    print(f"Total successful writes: {total_succeeded}")
    print(f"Total failed writes: {total_failed}")

    print("\n" + "=" * 70)
    if total_failed:
        print(f"FINAL RESULT: FAILED - {total_failed} write(s) failed. See above.")
        print("Safe to re-run: already-migrated students and already-marked Session Log rows are skipped.")
    else:
        print("FINAL RESULT: SUCCESS - baseline migration applied with zero failures.")
    print("=" * 70)
    return 1 if total_failed else 0


def run_student_rollup_dry_run(monday_client, today=None):
    """Stage 2, combined dry run: runs the real rollup calculation
    (compute_student_rollup_updates) and reports it. Makes ZERO Monday
    writes - MondayClient.update_student_columns() exists but is never
    called from this path. Student writes are not enabled yet."""
    today = today or datetime.date.today().isoformat()

    print("=" * 70)
    print("STUDENT ROLLUP - COMBINED DRY RUN (Stage 2, zero Monday writes)")
    print(f"Run date (every matched student's checkpoint would advance to this date): {today}")
    print("=" * 70)

    results = compute_student_rollup_updates(monday_client, today=today)

    unmatched = [r for r in results if not r["matched"]]
    matched = [r for r in results if r["matched"]]
    rollup_updates = [r for r in matched if r["update_kind"] == "rollup"]
    checkpoint_only = [r for r in matched if r["update_kind"] == "checkpoint_only"]
    total_new_sessions = sum(r["new_session_count"] for r in rollup_updates)
    total_would_write = len(rollup_updates) + len(checkpoint_only)

    if unmatched:
        print(f"\n{len(unmatched)} Student item(s) could not be matched (blank Teachworks Student ID) - logged, skipped, NO checkpoint:")
        for r in sorted(unmatched, key=lambda r: r["student_name"]):
            print(f"  Monday item {r['monday_item_id']} ({r['student_name']}): no Teachworks Student ID - skipped")

    if rollup_updates:
        header = (
            "Student | Current Count | New Sessions | Proposed Count | "
            "Current Last Session | Proposed Last Session | Current Tutor | Proposed Tutor"
        )
        print(f"\n{header}")
        print("-" * len(header))
        for r in sorted(rollup_updates, key=lambda r: r["student_name"]):
            print(
                f"{r['student_name']} | {r['current_count']} | {r['new_session_count']} | {r['proposed_count']} | "
                f"{r['current_last_session']} | {r['proposed_last_session']} | {r['current_tutor']} | {r['proposed_tutor']}"
            )
    else:
        print("\nNo students have new sessions since their own checkpoint.")

    print(
        f"\n{len(checkpoint_only)} student(s) have zero new sessions and would receive a "
        f"checkpoint-only update (Session Data Last Synced -> {today}; Session Count, "
        "Last Session Date, Tutor, and First Session Date all left unchanged)."
    )

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Students evaluated: {len(results)}")
    print(f"Students with new sessions: {len(rollup_updates)}")
    print(f"Total new sessions: {total_new_sessions}")
    print(f"Students receiving rollup updates: {len(rollup_updates)}")
    print(f"Students receiving checkpoint-only updates: {len(checkpoint_only)}")
    print(f"Students skipped because they cannot be matched: {len(unmatched)}")
    print(f"Total Student items that WOULD be written: {total_would_write}")

    print("\n" + "=" * 70)
    print("DIAGNOSTIC COMPLETE - read-only. Zero Monday writes were made.")
    print("update_student_columns() exists but was NOT called. Student writes are not enabled.")
    print("=" * 70)
    return 0


def print_report(report):
    verb_created = "Sessions that WOULD be created" if report.mode.startswith("DRY RUN") else "Sessions created"
    lines = [
        "=" * 70,
        "WRIGHT ACADEMICS SESSION SYNC - RESULT",
        "=" * 70,
        f"Sync mode:                        {report.mode}",
        f"Date range:                       {report.start_date} .. {report.end_date}",
        f"Runtime:                          {report.runtime_seconds:.1f}s",
        f"Teachworks lessons fetched:       {report.lessons_fetched}",
        f"Attended participant sessions:    {report.attended_sessions_found}",
        f"Existing Monday IDs loaded:       {report.existing_ids_loaded}",
        f"{verb_created + ':':<35}{report.sessions_created}",
        f"Sessions skipped as duplicates:   {report.sessions_skipped}",
        f"Students matched:                 {report.students_matched}",
        f"Missing students:                 {len(report.missing_students)}",
        f"Connections made:                 {report.connections_made}",
        f"Connection errors:                {len(report.connection_errors)}",
        f"Creation errors:                  {len(report.creation_errors)}",
    ]

    if report.missing_students:
        lines.append("-" * 70)
        lines.append("MISSING STUDENTS (Session Log item created, but NOT connected):")
        for m in report.missing_students:
            lines.append(
                f"  - Teachworks Student ID {m['teachworks_student_id']} "
                f"({m['student_name']}), lesson {m['lesson_id']}"
            )

    if report.connection_errors:
        lines.append("-" * 70)
        lines.append("CONNECTION ERRORS:")
        for e in report.connection_errors:
            lines.append(f"  - item {e['item_id']}: {e['error']}")

    if report.creation_errors:
        lines.append("-" * 70)
        lines.append("CREATION ERRORS:")
        for e in report.creation_errors:
            lines.append(f"  - {e['unique_key']}: {e['error']}")

    lines.append("=" * 70)
    if report.creation_errors:
        lines.append("RESULT: COMPLETED WITH ERRORS - some sessions were NOT created. See above.")
    elif report.connection_errors or report.missing_students:
        lines.append("RESULT: COMPLETED - all sessions synced, but some student connections need attention.")
    else:
        lines.append("RESULT: SYNC OK - nothing needs attention.")
    lines.append("=" * 70)

    print("\n".join(lines))


def _date_range(args):
    today = datetime.date.today()
    if args.full:
        start = config.FULL_SYNC_START_DATE
        end = today.isoformat()
    else:
        lookback = args.lookback_days if args.lookback_days is not None else config.LOOKBACK_DAYS
        start = (today - datetime.timedelta(days=lookback)).isoformat()
        end = today.isoformat()
    return start, end


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync Teachworks attended sessions into Monday.com.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report only; create/update nothing.")
    parser.add_argument("--full", action="store_true", help="Full historical reconciliation instead of the rolling lookback window.")
    parser.add_argument("--lookback-days", type=int, default=None, help="Override LOOKBACK_DAYS for this run.")
    parser.add_argument("--dump-sample", action="store_true", help="Print one raw Teachworks lesson JSON and exit (for verifying field names).")
    parser.add_argument("--diagnose-teachworks", action="store_true", help="Read-only: test several /lessons query variants and print status/record counts. Makes zero Monday.com calls and zero writes.")
    parser.add_argument("--diagnose-dedup", action="store_true", help="Read-only: compare computed unique keys against Monday's stored unique-ID column to investigate duplicate-detection results. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-student-columns", action="store_true", help="Read-only: print every column (title/ID/type) on the configured Students board. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-student-rollups", action="store_true", help="Read-only: calculate lifetime student rollups from the Session Log board and compare against current Monday Student values. Reads Monday.com but makes zero writes and zero Teachworks requests.")
    parser.add_argument("--diagnose-student-rollup-delta", action="store_true", help="Read-only: validate a baseline+delta rollup approach for students whose current Session Data Last Synced equals --baseline-date. Reads Monday.com but makes zero writes.")
    parser.add_argument("--baseline-date", default=None, help="YYYY-MM-DD baseline date, required by --diagnose-student-rollup-delta.")
    parser.add_argument("--diagnose-checkpoint-migration", action="store_true", help="Read-only: inspect the Session Data Last Synced column's settings and a sample of Session Log items' created_at, ahead of any datetime-checkpoint migration. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-baseline-migration-columns", action="store_true", help="Read-only: search the Students and Session Log boards for the Historical Session Baseline / Pre-Baseline columns needed by the baseline+SET migration, and report their real IDs if found. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-baseline-migration", action="store_true", help="Read-only: full migration dry run for the baseline+SET architecture (Students Historical Session Baseline, Session Log Pre-Baseline). Requires config.STUDENT_COL_HISTORICAL_BASELINE / config.COL_PRE_BASELINE to be set first. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-session-log-duplicates", action="store_true", help="Read-only: investigate duplicate Session Log unique identities in detail (exact duplicates and cross-format bare/composite key collisions), with full row detail and created_at. Reads Monday.com but makes zero writes.")
    parser.add_argument("--diagnose-baseline-migration-cutover", action="store_true", help="Read-only: one frozen-snapshot migration cutover preview (Students + Session Log), including an in-memory post-migration simulation proving post-baseline identities start at zero. Requires config.STUDENT_COL_HISTORICAL_BASELINE / config.COL_PRE_BASELINE to be set first. Reads Monday.com but makes zero writes.")
    parser.add_argument("--apply-baseline-migration", action="store_true", help="ONE-TIME REAL WRITE: applies the baseline+SET migration from one frozen Students + Session Log snapshot - sets each unmigrated student's Historical Session Baseline to their current Session Count, and marks each unmarked Session Log row in the snapshot Pre-Baseline = Yes. Never touches Session Count or any other existing field. Safe to re-run (already-migrated items are skipped). Requires config.STUDENT_COL_HISTORICAL_BASELINE / config.COL_PRE_BASELINE to be set first.")
    parser.add_argument("--student-rollups", action="store_true", help="Calculate production-ready Student rollup updates (baseline+delta, per-student checkpoint). Combine with --dry-run to preview, or --apply to perform the real Monday writes.")
    parser.add_argument("--apply", action="store_true", help="Perform REAL Monday writes for --student-rollups. Writes never happen just because --dry-run is absent - --apply must be passed explicitly.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config.validate()

    start_date, end_date = _date_range(args)

    tw_client = TeachworksClient(
        api_key=config.TEACHWORKS_API_KEY,
        base_url=config.TEACHWORKS_BASE_URL,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        max_retries=config.MAX_RETRIES,
        retry_base_delay=config.RETRY_BASE_DELAY_SECONDS,
    )

    # Monday.com is intentionally not constructed above: --dump-sample and
    # --diagnose-teachworks never need it, and must not touch it at all.

    if args.dump_sample:
        logger.info("Fetching a sample of Teachworks lessons for %s .. %s to inspect raw JSON...", start_date, end_date)
        lessons = tw_client.get_lessons(start_date, end_date)
        if not lessons:
            print(f"No lessons found in {start_date} .. {end_date}. Try --full or a different range.")
            return 0
        print(json.dumps(lessons[0], indent=2, default=str))
        return 0

    if args.diagnose_teachworks:
        return diagnose_teachworks(tw_client, start_date, end_date)

    monday_client = MondayClient(
        api_token=config.MONDAY_API_TOKEN,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        max_retries=config.MAX_RETRIES,
        retry_base_delay=config.RETRY_BASE_DELAY_SECONDS,
    )

    if args.diagnose_student_columns:
        return diagnose_student_columns(monday_client)

    if args.diagnose_student_rollups:
        return diagnose_student_rollups(monday_client)

    if args.diagnose_student_rollup_delta:
        if not args.baseline_date:
            print("ERROR: --diagnose-student-rollup-delta requires --baseline-date YYYY-MM-DD")
            return 1
        return diagnose_student_rollup_delta(monday_client, args.baseline_date)

    if args.diagnose_checkpoint_migration:
        return diagnose_checkpoint_migration_readiness(monday_client)

    if args.diagnose_baseline_migration_columns:
        return diagnose_baseline_migration_columns(monday_client)

    if args.diagnose_baseline_migration:
        return diagnose_baseline_migration(monday_client)

    if args.diagnose_session_log_duplicates:
        return diagnose_session_log_duplicates(monday_client)

    if args.diagnose_baseline_migration_cutover:
        return diagnose_baseline_migration_cutover(monday_client)

    if args.apply_baseline_migration:
        return run_baseline_migration_apply(monday_client)

    if args.student_rollups:
        if args.apply and args.dry_run:
            print("ERROR: Pass either --dry-run or --apply for --student-rollups, not both.")
            return 1
        if args.apply:
            return run_student_rollup_apply(monday_client)
        if args.dry_run:
            return run_student_rollup_dry_run(monday_client)
        print("ERROR: --student-rollups requires either --dry-run (preview) or --apply (real writes).")
        return 1

    if args.diagnose_dedup:
        return diagnose_dedup(tw_client, monday_client, start_date, end_date)

    mode = "FULL RECONCILIATION" if args.full else "SCHEDULED (rolling lookback)"
    if args.dry_run:
        mode = f"DRY RUN - {mode}"

    report = run_sync(tw_client, monday_client, start_date, end_date, dry_run=args.dry_run, mode=mode)
    print_report(report)

    return 1 if report.creation_errors else 0


if __name__ == "__main__":
    sys.exit(main())
