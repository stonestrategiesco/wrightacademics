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
