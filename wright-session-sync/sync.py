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

        if unique_key in existing_ids:
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
    monday_client = MondayClient(
        api_token=config.MONDAY_API_TOKEN,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        max_retries=config.MAX_RETRIES,
        retry_base_delay=config.RETRY_BASE_DELAY_SECONDS,
    )

    if args.dump_sample:
        logger.info("Fetching a sample of Teachworks lessons for %s .. %s to inspect raw JSON...", start_date, end_date)
        lessons = tw_client.get_lessons(start_date, end_date)
        if not lessons:
            print(f"No lessons found in {start_date} .. {end_date}. Try --full or a different range.")
            return 0
        print(json.dumps(lessons[0], indent=2, default=str))
        return 0

    mode = "FULL RECONCILIATION" if args.full else "SCHEDULED (rolling lookback)"
    if args.dry_run:
        mode = f"DRY RUN - {mode}"

    report = run_sync(tw_client, monday_client, start_date, end_date, dry_run=args.dry_run, mode=mode)
    print_report(report)

    return 1 if report.creation_errors else 0


if __name__ == "__main__":
    sys.exit(main())
