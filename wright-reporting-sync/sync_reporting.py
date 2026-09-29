#!/usr/bin/env python3
"""Wright Academics -- Teachworks Reporting board sync (company-level monthly
aggregation). Phase 1: --mode dry-run (Teachworks-only, no Monday calls, kept
working exactly as before). Phase 2 (new): --mode dry-run ALSO previews the
exact Monday changes (read-only Monday queries, still zero writes) when a
Monday config is available, and --mode update WRITES those fields to Monday
-- restricted to September 2026 only for Phase 2.

Reuses audit.py for all Teachworks auth/pagination/caching, and reuses
sync_monday.py's generic Monday GraphQL helpers (monday_graphql,
fetch_all_monday_items, column_text, set_monday_column_value,
MondayApiError) -- this file adds no new Monday transport code of its own.

--mode update (single month, September 2026 only for now) NEVER creates a
Monday item -- it matches the existing item by name (e.g. "September 2026")
and only ever touches Sessions Attended, Sessions Missed, Total Sessions,
Students Served, and Last Updated.

--mode update --month-range (multi-month historical backfill, e.g. "1-8")
additionally writes Avg Sessions / Student (= Sessions Attended / Students
Served, rounded to 2 decimals) and five Decimal-safe financial fields --
Attended/Missed/Cancelled/Scheduled/Total Amount, computed by
compute_monthly_financials() -- using column ids confirmed by the user from
the live board (MONDAY_REPORTING_COLUMN_* in .env; this file never guesses
one). Before touching Teachworks or any Monday item, it runs a read-only
board-schema preflight (verify_board_schema(), reusing sync_monday.py's
BOARD_COLUMNS_QUERY) that fetches the board's REAL column list and confirms
every configured column id actually exists with the expected title/type --
this catches a stale/wrong column id up front, since Monday's items_page
column_values(ids:...) was observed to silently tolerate an invalid id while
the write mutation itself throws InvalidColumnIdException. It then fetches
the Reporting board once, matches every requested month's item by exact
name, requires BOTH session-count AND financial reconciliation to pass for
every requested month before any write, previews every month's changes,
asks for ONE typed confirmation, then writes and reads each item back from
Monday to verify. It refuses a month that hasn't fully elapsed yet unless
--allow-incomplete-month is passed.

Payments is never referenced anywhere in this file, in either mode. A NOT
FOUND or DUPLICATE name match always aborts (the whole multi-month batch,
before any write, if it happens in --month-range mode) -- never guessed at.

This is meant to grow into the "one scheduled process" for the Reporting
board (dry-run/update today, full Phase 3 update across all months later)
rather than being a disposable one-off -- matching the approved
architecture (Teachworks source of truth, Monday display-only, no
Session-Log-style per-session mirror, no new application).

Classification (confirmed via inspect_status_inventory.py against the real
Wright Academics dataset -- NOT assumed):
  participant['status'] == 'Attended'  -> Sessions Attended
  participant['status'] == 'Missed'    -> Sessions Missed
  participant['status'] == 'Cancelled' -> Excluded
  participant['status'] == 'Scheduled' -> Excluded
  anything else / missing/null         -> Unclassified (reported explicitly,
                                           NEVER silently folded into another bucket)

Total Sessions (V1) = Sessions Attended + Sessions Missed. Cancelled and
Scheduled are excluded from Total Sessions entirely.
Students Served = distinct participant student_id among Attended + Missed
records for the month only.

Financial aggregation (Phase 1B, read-only so far -- no Monday writes exist
for these fields yet): compute_monthly_financials() sums participant-level
'amount' by the SAME classify_participant() status buckets, independently
of compute_monthly_aggregation() -- the already-verified session-count
function is never touched by this. All currency math is done with Decimal
across the raw participant amounts; values are only quantized to cents once,
at the very end, for display/output -- never by summing already-rounded
monthly/status subtotals -- so bucket subtotals always add up to the total
to the exact penny. Total Amount = Attended + Missed + Cancelled +
Scheduled amounts (Unclassified is reported separately, matching how
Unclassified is excluded from Total Sessions). The real Monday column ids
for these fields (Attended/Missed/Cancelled/Scheduled/Total Amount) have
been confirmed but are NOT wired to any write path in this file yet.
"""

import argparse
import calendar
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import audit
import sync_monday as sm  # reused for Monday GraphQL transport only (Phase 2)

PARTICIPANT_STATUS_FIELD = "status"  # confirmed via inspect_status_inventory.py
ATTENDED_VALUE = "Attended"
MISSED_VALUE = "Missed"
CANCELLED_VALUE = "Cancelled"
SCHEDULED_VALUE = "Scheduled"
UNCLASSIFIED_LABEL = "Unclassified"
KNOWN_STATUS_VALUES = {ATTENDED_VALUE, MISSED_VALUE, CANCELLED_VALUE, SCHEDULED_VALUE}


def last_day_of_month(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def month_is_incomplete(year, month):
    """True if the month has not fully elapsed as of today (UTC) -- the same
    rule check_data_freshness uses for its second warning, factored out so
    the multi-month update can enforce it as a hard guard rather than just a
    printed note."""
    today = datetime.now(timezone.utc).date()
    return today <= last_day_of_month(year, month)


def read_cache_fetched_at(output_dir):
    """Best-effort read of audit.py's cache metadata, purely to warn if the
    data underlying this run predates the period being reported on. Returns
    None if no cache metadata is found (e.g. this run just did a fresh fetch
    and audit.py hasn't written meta yet, or output_dir differs)."""
    meta_path = output_dir / "_cache" / "cache_meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        return datetime.fromisoformat(meta["fetched_at"])
    except (KeyError, ValueError, OSError, json.JSONDecodeError):
        return None


def check_data_freshness(output_dir, year, month):
    """Two DISTINCT warnings, since only one of them is fixable by re-fetching:
      1. The cache is older than necessary (fixable: --refresh-teachworks-cache).
      2. The requested month hasn't finished yet as of today (NOT fixable by
         re-fetching -- inherent to reporting on an in-progress month).
    Neither of these blocks the run (Phase 1 dry-run is explicitly for
    validating the aggregation LOGIC, which is fine to do against partial or
    stale data) -- they just make sure the output can never be mistaken for
    a final, authoritative count.
    """
    warnings = []
    today = datetime.now(timezone.utc).date()
    month_end = last_day_of_month(year, month)

    fetched_at = read_cache_fetched_at(output_dir)
    if fetched_at is not None:
        fetched_date = fetched_at.date()
        if fetched_date < min(month_end, today):
            warnings.append(
                f"DATA IS FROM A CACHE FETCHED {fetched_date.isoformat()} -- that is BEFORE "
                f"{year}-{month:02d} was complete. These totals reflect only whatever lessons "
                f"existed in Teachworks as of {fetched_date.isoformat()}, NOT the full month. "
                "This run is for validating the AGGREGATION LOGIC only -- do not treat these "
                "numbers as the authoritative count. Re-run with --refresh-teachworks-cache "
                "for a real validation/write."
            )

    if month_is_incomplete(year, month):
        warnings.append(
            f"{year}-{month:02d} has not finished yet as of today ({today.isoformat()}) -- even "
            "a fully fresh fetch right now would only capture a PARTIAL month. This is inherent "
            "to reporting on the current month, not fixable by refreshing the cache."
        )

    return warnings, fetched_at.isoformat() if fetched_at else None


def classify_participant(participant):
    status = participant.get(PARTICIPANT_STATUS_FIELD)
    if status in KNOWN_STATUS_VALUES:
        return status
    return UNCLASSIFIED_LABEL


def compute_monthly_aggregation(config, lessons, year, month):
    lesson_date_field = audit.detect_field(lessons, audit.LESSON_DATE_CANDIDATES, override=config["lesson_date_field"])
    if not lesson_date_field:
        raise SystemExit("Could not detect a lesson date field on this dataset -- cannot filter by month.")

    bucket_counts = Counter()
    served_students = set()
    unclassified_examples = []
    included_dates = []
    total_lessons_in_month = 0
    duplicate_participant_entries = 0
    classified_but_missing_student_id = 0

    for lesson in lessons:
        dt = audit.parse_date_value(audit.get_nested(lesson, lesson_date_field))
        if dt is None or dt.year != year or dt.month != month:
            continue
        total_lessons_in_month += 1
        included_dates.append(dt)

        lesson_id = lesson.get("id")
        participants = lesson.get("participants") or []
        if not isinstance(participants, list):
            participants = [participants]

        # Diagnostic only: flag (not dedupe) a student appearing more than
        # once as a participant on the same lesson -- every entry still
        # counts below so the reconciliation total is never silently altered.
        student_id_counts_this_lesson = Counter(
            p.get("student_id") for p in participants if isinstance(p, dict)
        )
        for count in student_id_counts_this_lesson.values():
            if count > 1:
                duplicate_participant_entries += count - 1

        for participant in participants:
            if not isinstance(participant, dict):
                continue
            student_id = participant.get("student_id")
            bucket = classify_participant(participant)
            bucket_counts[bucket] += 1

            if bucket in (ATTENDED_VALUE, MISSED_VALUE):
                if student_id is not None:
                    served_students.add(student_id)
                else:
                    classified_but_missing_student_id += 1

            if bucket == UNCLASSIFIED_LABEL and len(unclassified_examples) < 20:
                unclassified_examples.append({
                    "lesson_id": lesson_id,
                    "student_id": student_id,
                    "raw_status": participant.get(PARTICIPANT_STATUS_FIELD),
                })

    total_participant_records = sum(bucket_counts.values())
    reconciliation_sum = (
        bucket_counts[ATTENDED_VALUE] + bucket_counts[MISSED_VALUE]
        + bucket_counts[CANCELLED_VALUE] + bucket_counts[SCHEDULED_VALUE]
        + bucket_counts[UNCLASSIFIED_LABEL]
    )
    total_sessions = bucket_counts[ATTENDED_VALUE] + bucket_counts[MISSED_VALUE]

    return {
        "year": year,
        "month": month,
        "lesson_date_field_used": lesson_date_field,
        "total_lessons_in_month": total_lessons_in_month,
        "date_range_included": {
            "min": min(included_dates).isoformat() if included_dates else None,
            "max": max(included_dates).isoformat() if included_dates else None,
        },
        "sessions_attended": bucket_counts[ATTENDED_VALUE],
        "sessions_missed": bucket_counts[MISSED_VALUE],
        "total_sessions": total_sessions,
        "students_served": len(served_students),
        "cancelled_count": bucket_counts[CANCELLED_VALUE],
        "scheduled_count": bucket_counts[SCHEDULED_VALUE],
        "unclassified_count": bucket_counts[UNCLASSIFIED_LABEL],
        "total_participant_records_considered": total_participant_records,
        "reconciliation_sum": reconciliation_sum,
        "reconciliation_ok": reconciliation_sum == total_participant_records,
        "duplicate_participant_entries": duplicate_participant_entries,
        "classified_but_missing_student_id": classified_but_missing_student_id,
        "unclassified_examples": unclassified_examples,
    }


def compute_reporting_result(config, output_dir, year, month, refresh_teachworks_cache):
    """Single source of truth for the Teachworks-side aggregation. Used
    IDENTICALLY by --mode dry-run (to preview it) and --mode update (to
    decide what to write), so the two can never disagree about the numbers."""
    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(
        config, output_dir, refresh_teachworks_cache
    )
    print(f"\nUsing {len(lessons)} total lesson record(s) across {lessons_pages} page(s) of full history;")
    print("filtering to the requested month locally (no server-side date query used).\n")

    freshness_warnings, cache_fetched_at = check_data_freshness(output_dir, year, month)
    if freshness_warnings:
        print("!" * 70)
        for w in freshness_warnings:
            print(f"!!! {w}\n")
        print("!" * 70 + "\n")

    result = compute_monthly_aggregation(config, lessons, year, month)
    result["cache_fetched_at"] = cache_fetched_at
    result["freshness_warnings"] = freshness_warnings
    result["is_authoritative"] = not freshness_warnings
    return result


def print_aggregation(result, year, month):
    print(f"=== {year}-{month:02d} AGGREGATION (participant-level status is authoritative) ===")
    dr = result["date_range_included"]
    print(f"Date range actually included: {dr['min']} to {dr['max']}")
    print(f"Lessons in month: {result['total_lessons_in_month']}\n")
    print(f"Sessions Attended:                {result['sessions_attended']}")
    print(f"Sessions Missed:                  {result['sessions_missed']}")
    print(f"Total Sessions (Attended+Missed): {result['total_sessions']}")
    print(f"Students Served:                  {result['students_served']}")
    print(f"Cancelled (excluded):             {result['cancelled_count']}")
    print(f"Scheduled (excluded):             {result['scheduled_count']}")
    print(f"Unclassified:                     {result['unclassified_count']}")

    if result["duplicate_participant_entries"]:
        print(
            f"\nNOTE: {result['duplicate_participant_entries']} duplicate (lesson, student) "
            "participant entries found on the same lesson -- included as-is in the counts "
            "above, NOT deduped, so the reconciliation total stays exact."
        )
    if result["classified_but_missing_student_id"]:
        print(
            f"NOTE: {result['classified_but_missing_student_id']} Attended/Missed record(s) had "
            "no student_id -- counted in Sessions Attended/Missed but NOT in Students Served."
        )

    print(f"\nTotal participant records considered: {result['total_participant_records_considered']}")
    print(
        f"Reconciliation (Attended+Missed+Cancelled+Scheduled+Unclassified == total considered): "
        f"{result['reconciliation_sum']} == {result['total_participant_records_considered']} -> "
        f"{'OK' if result['reconciliation_ok'] else 'MISMATCH -- INVESTIGATE'}"
    )

    if result["unclassified_examples"]:
        print("\nSample unclassified records (up to 20):")
        for ex in result["unclassified_examples"]:
            print(f"  lesson_id={ex['lesson_id']} student_id={ex['student_id']} raw_status={ex['raw_status']!r}")


# =====================================================================
# Phase 1B: financial aggregation (read-only -- no Monday write path exists
# for these fields yet). Computed independently of compute_monthly_aggregation
# so that already-verified session-count logic is never touched here.
# =====================================================================

CENTS = Decimal("0.01")


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _quantize(d):
    return str(d.quantize(CENTS, rounding=ROUND_HALF_UP))


def compute_monthly_financials(config, lessons, year, month):
    """Decimal-safe sum of participant['amount'] by the SAME
    classify_participant() status buckets used for session counts, computed
    independently (own month-filter loop) so compute_monthly_aggregation is
    never touched. Every underlying amount is accumulated as Decimal; values
    are only quantized to cents once, at the end, for the returned/displayed
    totals -- never by adding together already-rounded subtotals -- so the
    per-status amounts always add up to the total to the exact penny."""
    lesson_date_field = audit.detect_field(lessons, audit.LESSON_DATE_CANDIDATES, override=config["lesson_date_field"])
    if not lesson_date_field:
        raise SystemExit("Could not detect a lesson date field on this dataset -- cannot filter by month.")

    bucket_sums = defaultdict(lambda: Decimal("0"))
    null_or_non_numeric_count = 0
    total_participant_records = 0

    for lesson in lessons:
        dt = audit.parse_date_value(audit.get_nested(lesson, lesson_date_field))
        if dt is None or dt.year != year or dt.month != month:
            continue
        participants = lesson.get("participants") or []
        if not isinstance(participants, list):
            participants = [participants]
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            total_participant_records += 1
            bucket = classify_participant(participant)
            amount = _to_decimal(participant.get("amount"))
            if amount is None:
                null_or_non_numeric_count += 1
                continue
            bucket_sums[bucket] += amount

    attended_amount = bucket_sums[ATTENDED_VALUE]
    missed_amount = bucket_sums[MISSED_VALUE]
    cancelled_amount = bucket_sums[CANCELLED_VALUE]
    scheduled_amount = bucket_sums[SCHEDULED_VALUE]
    unclassified_amount = bucket_sums[UNCLASSIFIED_LABEL]
    total_amount = attended_amount + missed_amount + cancelled_amount + scheduled_amount

    # Every participant record lands in exactly one of these 5 buckets (by
    # classify_participant's construction), so this sum and the raw sum of
    # every bucket are the same computation by definition -- this is a
    # correctness PROOF (would only fail if a bucket were ever added without
    # updating this reconciliation), the same role the session-count
    # reconciliation already plays above.
    reconciliation_sum = attended_amount + missed_amount + cancelled_amount + scheduled_amount + unclassified_amount
    full_participant_amount_total = sum(bucket_sums.values(), Decimal("0"))

    return {
        "year": year, "month": month,
        "attended_amount": _quantize(attended_amount),
        "missed_amount": _quantize(missed_amount),
        "cancelled_amount": _quantize(cancelled_amount),
        "scheduled_amount": _quantize(scheduled_amount),
        "unclassified_amount": _quantize(unclassified_amount),
        "total_amount": _quantize(total_amount),
        "null_or_non_numeric_amount_count": null_or_non_numeric_count,
        "total_participant_records_considered": total_participant_records,
        "financial_reconciliation_sum": _quantize(reconciliation_sum),
        "financial_reconciliation_ok": reconciliation_sum == full_participant_amount_total,
    }


def print_monthly_financials(fin, year, month):
    print(f"\n=== {year}-{month:02d} FINANCIALS (Decimal-safe; quantized to cents only for display) ===")
    print(f"Attended Amount:   ${fin['attended_amount']}")
    print(f"Missed Amount:     ${fin['missed_amount']}")
    print(f"Cancelled Amount:  ${fin['cancelled_amount']}")
    print(f"Scheduled Amount:  ${fin['scheduled_amount']}")
    print(f"Total Amount (Attended+Missed+Cancelled+Scheduled): ${fin['total_amount']}")
    print(f"Unclassified Amount: ${fin['unclassified_amount']}")
    print(
        f"null/non-numeric amount records: {fin['null_or_non_numeric_amount_count']} of "
        f"{fin['total_participant_records_considered']} participant record(s)"
    )
    print(
        "Financial reconciliation (Attended+Missed+Cancelled+Scheduled+Unclassified amounts == "
        f"full participant-amount total): ${fin['financial_reconciliation_sum']} -> "
        f"{'OK' if fin['financial_reconciliation_ok'] else 'MISMATCH -- INVESTIGATE'}"
    )


# =====================================================================
# Phase 2: Monday.com "Teachworks Reporting" board (read-only preview in
# dry-run, writes only in update mode). Reuses sync_monday.py's generic
# GraphQL transport (monday_graphql, fetch_all_monday_items, column_text,
# set_monday_column_value, MondayApiError) -- no new Monday transport code.
# =====================================================================

MONDAY_REPORTING_ENV_DEFAULTS = {
    # (env var name, default) -- defaults are the real values confirmed by
    # the user directly from the "Teachworks Reporting" board in Monday, so
    # this works out of the box; still overridable via .env if the board
    # ever changes.
    "board_id": ("MONDAY_REPORTING_BOARD_ID", "18432993218"),
    "column_sessions_attended": ("MONDAY_REPORTING_COLUMN_SESSIONS_ATTENDED", "numeric_mm7mrpe4"),
    "column_sessions_missed": ("MONDAY_REPORTING_COLUMN_SESSIONS_MISSED", "numeric_mm7mh4qd"),
    "column_total_sessions": ("MONDAY_REPORTING_COLUMN_TOTAL_SESSIONS", "numeric_mm7me68f"),
    "column_students_served": ("MONDAY_REPORTING_COLUMN_STUDENTS_SERVED", "numeric_mm7mhsf3"),
    "column_last_updated": ("MONDAY_REPORTING_COLUMN_LAST_UPDATED", "date_mm7m4799"),
    # Confirmed by the user directly from the live board -- used by the
    # multi-month update path only (the single-month September update never
    # references these).
    "column_avg_sessions_per_student": ("MONDAY_REPORTING_COLUMN_AVG_SESSIONS_PER_STUDENT", "numeric_mm7mg9ve"),
    "column_attended_amount": ("MONDAY_REPORTING_COLUMN_ATTENDED_AMOUNT", "numeric_mm7mymrr"),
    "column_missed_amount": ("MONDAY_REPORTING_COLUMN_MISSED_AMOUNT", "numeric_mm7m84fd"),
    "column_cancelled_amount": ("MONDAY_REPORTING_COLUMN_CANCELLED_AMOUNT", "numeric_mm7m2mg5"),
    "column_scheduled_amount": ("MONDAY_REPORTING_COLUMN_SCHEDULED_AMOUNT", "numeric_mm7m7y3x"),
    "column_total_amount": ("MONDAY_REPORTING_COLUMN_TOTAL_AMOUNT", "numeric_mm7mymtn"),
}

# Columns only the multi-month update path (--mode update --month-range)
# ever reads or writes -- never the single-month September update, and
# Payments is never in this list or referenced anywhere in this file.
MULTI_MONTH_ONLY_COLUMNS = [
    "column_avg_sessions_per_student", "column_attended_amount", "column_missed_amount",
    "column_cancelled_amount", "column_scheduled_amount", "column_total_amount",
]

REPORTING_FIELD_COLUMN_KEY = {
    "sessions_attended": "column_sessions_attended",
    "sessions_missed": "column_sessions_missed",
    "total_sessions": "column_total_sessions",
    "students_served": "column_students_served",
    "avg_sessions_per_student": "column_avg_sessions_per_student",
    "attended_amount": "column_attended_amount",
    "missed_amount": "column_missed_amount",
    "cancelled_amount": "column_cancelled_amount",
    "scheduled_amount": "column_scheduled_amount",
    "total_amount": "column_total_amount",
}
REPORTING_FIELD_LABELS = {
    "sessions_attended": "Sessions Attended",
    "sessions_missed": "Sessions Missed",
    "total_sessions": "Total Sessions",
    "students_served": "Students Served",
    "avg_sessions_per_student": "Avg Sessions / Student",
    "attended_amount": "Attended Amount",
    "missed_amount": "Missed Amount",
    "cancelled_amount": "Cancelled Amount",
    "scheduled_amount": "Scheduled Amount",
    "total_amount": "Total Amount",
}


def load_reporting_monday_config():
    # Reuses the SAME Monday API token/.env as sync_monday.py -- one Monday
    # account, one .env, no separate credential setup for this board.
    audit.load_dotenv(dotenv_path=audit.SCRIPT_DIR / ".env")
    cfg = {
        "api_url": audit._get("MONDAY_API_URL", "https://api.monday.com/v2"),
        "api_token": audit._get("MONDAY_API_TOKEN"),
        "auth_header": audit._get("MONDAY_AUTH_HEADER", "Authorization"),
        "auth_scheme": audit._get("MONDAY_AUTH_SCHEME", "{key}"),
        "api_version": audit._get("MONDAY_API_VERSION", ""),
        "items_page_size": audit._get_int("MONDAY_ITEMS_PAGE_SIZE", 100),
    }
    for key, (env_name, default) in MONDAY_REPORTING_ENV_DEFAULTS.items():
        cfg[key] = audit._get(env_name, default)
    return cfg


def month_item_name(year, month):
    """Canonical Reporting-board item name for a month, e.g. "09 - September 2026".
    The zero-padded prefix makes the board and its dashboards sort chronologically.
    Every item this script creates uses this name."""
    return f"{month:02d} - {calendar.month_name[month]} {year}"


def legacy_month_item_name(year, month):
    """The name items had before the numbered format, e.g. "September 2026".
    Only ever used to RECOGNISE an existing item - never to create one."""
    return f"{calendar.month_name[month]} {year}"


def month_item_names(year, month):
    """Every name that identifies this month's item: canonical first, then legacy."""
    return (month_item_name(year, month), legacy_month_item_name(year, month))


def reporting_column_ids(monday_cfg):
    ids = [
        monday_cfg["column_sessions_attended"], monday_cfg["column_sessions_missed"],
        monday_cfg["column_total_sessions"], monday_cfg["column_students_served"],
        monday_cfg["column_last_updated"],
    ]
    for key in MULTI_MONTH_ONLY_COLUMNS:
        if monday_cfg.get(key):
            ids.append(monday_cfg[key])
    return ids


def read_current_reporting_values(item, cfg):
    values = {
        "sessions_attended": sm.column_text(item, cfg["column_sessions_attended"]),
        "sessions_missed": sm.column_text(item, cfg["column_sessions_missed"]),
        "total_sessions": sm.column_text(item, cfg["column_total_sessions"]),
        "students_served": sm.column_text(item, cfg["column_students_served"]),
        "last_updated": sm.column_text(item, cfg["column_last_updated"]),
    }
    for field_key, column_key in (
        ("avg_sessions_per_student", "column_avg_sessions_per_student"),
        ("attended_amount", "column_attended_amount"),
        ("missed_amount", "column_missed_amount"),
        ("cancelled_amount", "column_cancelled_amount"),
        ("scheduled_amount", "column_scheduled_amount"),
        ("total_amount", "column_total_amount"),
    ):
        if cfg.get(column_key):
            values[field_key] = sm.column_text(item, cfg[column_key])
    return values


def reporting_new_values(result, include_avg=False, fin=None):
    """Builds the {field: new_value} dict for a month's result. include_avg
    and fin are both False/None for the existing single-month --mode update
    (September 2026, unchanged behavior). The multi-month --mode update
    --month-range path passes include_avg=True and the matching
    compute_monthly_financials() result for fin, adding Avg Sessions /
    Student and the five (already Decimal-quantized) financial fields.
    Payments is never referenced here."""
    values = {
        "sessions_attended": result["sessions_attended"],
        "sessions_missed": result["sessions_missed"],
        "total_sessions": result["total_sessions"],
        "students_served": result["students_served"],
    }
    if include_avg:
        served = result["students_served"]
        avg = round(result["sessions_attended"] / served, 2) if served else 0.0
        values["avg_sessions_per_student"] = avg
    if fin is not None:
        values["attended_amount"] = fin["attended_amount"]
        values["missed_amount"] = fin["missed_amount"]
        values["cancelled_amount"] = fin["cancelled_amount"]
        values["scheduled_amount"] = fin["scheduled_amount"]
        values["total_amount"] = fin["total_amount"]
    return values


def _norm(value):
    return (value or "").strip()


def _numeric_equal(current_text, new_value):
    current = _norm(current_text)
    if current == str(new_value):
        return True
    try:
        return float(current) == float(new_value)
    except (ValueError, TypeError):
        return False


def diff_reporting_fields(current, new_values):
    """The SINGLE source of truth for what a Reporting-board update would
    change -- used identically by the dry-run/preview paths and both update
    modes (single-month and multi-month), so none of them can ever disagree.
    Only ever compares whatever keys are present in new_values (Sessions
    Attended/Missed, Total Sessions, Students Served, and -- for the
    multi-month path only -- Avg Sessions / Student). Payments is never
    referenced anywhere in this file."""
    diffs = {}
    for key, new_val in new_values.items():
        if not _numeric_equal(current.get(key), new_val):
            diffs[key] = (current.get(key), str(new_val))
    return diffs


def fetch_reporting_items(monday_cfg):
    return sm.fetch_all_monday_items(monday_cfg, reporting_column_ids(monday_cfg))


def find_matches_by_name(items, target_name):
    return [i for i in items if (i.get("name") or "").strip() == target_name]


def find_month_matches(items, year, month):
    """Every item that is this month's item under EITHER accepted name
    ("09 - September 2026" or legacy "September 2026"). More than one match -
    including one of each form - is a DUPLICATE the callers refuse to guess
    about; this function never chooses between them."""
    names = month_item_names(year, month)
    return [i for i in items if (i.get("name") or "").strip() in names]


def describe_matches(matches):
    return ", ".join(f"id={m['id']} {(m.get('name') or '').strip()!r}" for m in matches)


def find_reporting_item(monday_cfg, year, month):
    items = fetch_reporting_items(monday_cfg)
    matches = find_month_matches(items, year, month)
    return matches, len(items)


ITEM_BY_ID_QUERY = """
query ($itemIds: [ID!], $columnIds: [String!]) {
  items (ids: $itemIds) {
    id
    name
    column_values(ids: $columnIds) {
      id
      text
      value
    }
  }
}
"""


def fetch_item_by_id(monday_cfg, item_id):
    """Read-only verification helper: re-reads a single item straight from
    Monday by id (not from any local cache of the earlier fetch), used after
    a write to prove the value actually landed."""
    data = sm.monday_graphql(monday_cfg, ITEM_BY_ID_QUERY, {
        "itemIds": [item_id], "columnIds": reporting_column_ids(monday_cfg),
    })
    items = data.get("items") or []
    return items[0] if items else None


# Expected (title, type) for every column id the multi-month update can ever
# write to. type is Monday's raw GraphQL column type string (e.g. "numbers",
# "date"). Checked against the LIVE board schema, not against items --
# items_page's column_values(ids:...) was observed to silently tolerate a
# stale/invalid column id (returning nothing for it) while the
# change_simple_column_value mutation validates the id and throws
# InvalidColumnIdException. A preflight against items can miss exactly the
# bug that hit live: it must query the board's real column list instead.
EXPECTED_REPORTING_COLUMNS = {
    "column_sessions_attended": ("Sessions Attended", "numbers"),
    "column_sessions_missed": ("Sessions Missed", "numbers"),
    "column_total_sessions": ("Total Sessions", "numbers"),
    "column_students_served": ("Students Served", "numbers"),
    "column_last_updated": ("Last Updated", "date"),
    "column_avg_sessions_per_student": ("Avg Sessions / Student", "numbers"),
    "column_attended_amount": ("Attended Amount", "numbers"),
    "column_missed_amount": ("Missed Amount", "numbers"),
    "column_cancelled_amount": ("Cancelled Amount", "numbers"),
    "column_scheduled_amount": ("Scheduled Amount", "numbers"),
    "column_total_amount": ("Total Amount", "numbers"),
}


def verify_board_schema(monday_cfg, required_column_keys):
    """Read-only preflight: fetches the board's REAL column list from Monday
    (sm.BOARD_COLUMNS_QUERY -- the same query `sync_monday.py --mode
    inspect` uses) and confirms every column id this run will write to
    actually exists on the board AND matches its expected title/type.
    Returns a list of problem strings (empty = all OK); never raises, so the
    caller decides how to report and whether to abort."""
    try:
        data = sm.monday_graphql(monday_cfg, sm.BOARD_COLUMNS_QUERY, {"boardId": [monday_cfg["board_id"]]})
    except sm.MondayApiError as exc:
        return [f"Could not fetch board schema from Monday: {exc}"]

    boards = data.get("boards") or []
    if not boards:
        return [f"No board found for board id {monday_cfg['board_id']!r}."]
    real_columns_by_id = {c["id"]: c for c in boards[0]["columns"]}

    problems = []
    for config_key in required_column_keys:
        expected_title, expected_type = EXPECTED_REPORTING_COLUMNS[config_key]
        configured_id = monday_cfg.get(config_key)
        if not configured_id:
            problems.append(f"{expected_title}: no column id configured (config key {config_key}).")
            continue
        real_col = real_columns_by_id.get(configured_id)
        if real_col is None:
            problems.append(
                f"{expected_title}: configured column id {configured_id!r} does not exist on board "
                f"{monday_cfg['board_id']} -- Monday would reject any write to it."
            )
            continue
        if real_col["title"] != expected_title:
            problems.append(
                f"{expected_title}: configured column id {configured_id!r} exists but is titled "
                f"{real_col['title']!r} on the live board, not {expected_title!r} -- likely the wrong id."
            )
        if real_col["type"] != expected_type:
            problems.append(
                f"{expected_title}: configured column id {configured_id!r} exists but has type "
                f"{real_col['type']!r}, not the expected {expected_type!r}."
            )
    return problems


# =====================================================================
# Current-month auto-create (--current-month unattended production mode
# ONLY -- never the historical --month-range backfill path, which always
# passes auto_create_missing=False and can never create anything). Creates
# AT MOST one item, only when the exact current-month item does not exist
# yet, only inside an EXISTING year group -- year groups are never created.
# =====================================================================

BOARD_GROUPS_QUERY = """
query ($boardId: [ID!]) {
  boards(ids: $boardId) {
    groups {
      id
      title
    }
  }
}
"""

CREATE_ITEM_MUTATION = """
mutation ($boardId: ID!, $groupId: String!, $itemName: String!) {
  create_item(board_id: $boardId, group_id: $groupId, item_name: $itemName) {
    id
  }
}
"""


def year_group_title(year):
    # Matches the live board's year-group naming convention. Confirm the
    # REAL group titles first with `python sync_reporting.py --list-groups`
    # (read-only) rather than assuming "2026" is right -- override via
    # MONDAY_REPORTING_YEAR_GROUP_TITLE_TEMPLATE in .env (e.g. "FY{year}")
    # if the board's groups are named differently.
    template = audit._get("MONDAY_REPORTING_YEAR_GROUP_TITLE_TEMPLATE", "{year}")
    return template.format(year=year)


def fetch_board_groups(monday_cfg):
    data = sm.monday_graphql(monday_cfg, BOARD_GROUPS_QUERY, {"boardId": [monday_cfg["board_id"]]})
    boards = data.get("boards") or []
    return boards[0].get("groups") or [] if boards else []


def find_year_group(monday_cfg, year):
    """Read-only: finds the EXISTING group matching this year's expected
    title. Returns (group_id, expected_title) or (None, expected_title) if
    no such group exists. NEVER creates a group -- a missing year group is
    always the caller's cue to abort, not to guess or create one."""
    expected_title = year_group_title(year)
    groups = fetch_board_groups(monday_cfg)
    matches = [g for g in groups if g.get("title") == expected_title]
    return (matches[0]["id"], expected_title) if matches else (None, expected_title)


def create_month_item(monday_cfg, group_id, item_name):
    data = sm.monday_graphql(monday_cfg, CREATE_ITEM_MUTATION, {
        "boardId": monday_cfg["board_id"], "groupId": group_id, "itemName": item_name,
    })
    created = data.get("create_item")
    if not created or not created.get("id"):
        raise sm.MondayApiError(f"create_item returned no item id for {item_name!r}")
    return created["id"]


def blank_reporting_values():
    """The 'current' values for a not-yet-created item -- every field reads
    as blank, so diff_reporting_fields shows every computed value as
    something that needs to be set, using the EXACT SAME diff/preview/write
    logic as an existing item. No separate code path for a freshly created
    item beyond the creation call itself."""
    values = {key: "" for key in REPORTING_FIELD_COLUMN_KEY}
    values["last_updated"] = ""
    return values


def preview_monday_changes(monday_cfg, result, year, month):
    """Read-only: finds the Monday item named for this month and reports
    exactly what would change. Only makes read Monday API calls -- never
    writes. Returns (status, item_or_none, diffs_or_none); status is one of
    MATCHED / NOT_FOUND / DUPLICATE / SKIPPED / ERROR."""
    target_name = month_item_name(year, month)

    if not monday_cfg["api_token"]:
        print(
            "\nMonday preview SKIPPED: MONDAY_API_TOKEN is not set in .env -- "
            "showing Teachworks-only numbers above. Set it (same .env as "
            "sync_monday.py) to preview the exact Monday changes."
        )
        return "SKIPPED", None, None

    names = month_item_names(year, month)
    print(f"\n=== Monday.com preview: item named {names[0]!r} (or legacy {names[1]!r}) on board {monday_cfg['board_id']} ===")
    try:
        matches, total_items = find_reporting_item(monday_cfg, year, month)
    except sm.MondayApiError as exc:
        print(f"ERROR calling Monday API: {exc}")
        return "ERROR", None, None
    print(f"(scanned {total_items} item(s) on the board)")

    if not matches:
        print(
            f"NOT FOUND: no existing item named {names[0]!r} or {names[1]!r} on this board. This script never "
            "creates items -- create it in Monday first, then re-run."
        )
        return "NOT_FOUND", None, None
    if len(matches) > 1:
        print(f"DUPLICATE: {len(matches)} items for {target_name!r} found ({describe_matches(matches)}) -- ambiguous, will not write.")
        return "DUPLICATE", None, None

    item = matches[0]
    current = read_current_reporting_values(item, monday_cfg)
    diffs = diff_reporting_fields(current, reporting_new_values(result, include_avg=False))
    today_str = datetime.now(timezone.utc).date().isoformat()

    print(f"Matched Monday item id={item['id']!r} name={item['name']!r}")
    if not diffs:
        print("No field changes needed for Sessions Attended/Missed/Total Sessions/Students Served.")
    else:
        print("Would change:")
        for key, (old, new) in diffs.items():
            print(f"  {REPORTING_FIELD_LABELS[key]}: {old!r} -> {new!r}")
    print(f"  Last Updated: {current['last_updated']!r} -> {today_str!r} (always stamped on a successful sync)")
    print("Avg Sessions / Student and Payments are never touched by this script.")

    return "MATCHED", item, diffs


def run_dry_run(config, output_dir, year, month, refresh_teachworks_cache, monday_cfg=None):
    print(f"=== Wright Academics Teachworks Reporting -- DRY RUN ({year}-{month:02d}) ===")
    print(
        "Teachworks aggregation below is always read-only. The Monday preview (if configured) "
        "is ALSO read-only -- no writes happen in dry-run mode, ever.\n"
    )

    result = compute_reporting_result(config, output_dir, year, month, refresh_teachworks_cache)
    print_aggregation(result, year, month)

    result["phase_1b_notes"] = (
        "Participant records also carry 'unit_price', 'amount', and 'invoice_id'. Not used for "
        "any computation in this file. Worth investigating whether these let Payments be derived "
        "without per-lesson detail API calls, once these session counts are validated."
    )

    if monday_cfg is not None:
        status, item, diffs = preview_monday_changes(monday_cfg, result, year, month)
        result["monday_preview"] = {
            "status": status,
            "board_id": monday_cfg["board_id"],
            "item_name_expected": month_item_name(year, month),
            "item_id": item["id"] if item else None,
            "diffs": {k: {"current": v[0], "new": v[1]} for k, v in (diffs or {}).items()},
        }
    else:
        result["monday_preview"] = {"status": "SKIPPED", "board_id": None, "item_name_expected": None, "item_id": None, "diffs": {}}

    out_path = output_dir / f"wright-teachworks-reporting-dry-run-{year}-{month:02d}.json"
    audit.write_json(out_path, result)
    print(f"\nWrote: {out_path}")

    if monday_cfg is None:
        print("\nNo Monday API calls were made in this run. Nothing was written anywhere except this local report.")
    elif result["monday_preview"]["status"] == "MATCHED":
        print(f"\nNo writes were made to Monday. Review the preview above, then run:")
        print(f"  python sync_reporting.py --mode update --year {year} --month {month}")
    else:
        print("\nNo writes were made to Monday (see Monday preview status above).")

    if result["freshness_warnings"]:
        print("\n" + "!" * 70)
        print("!!! REMINDER: this run is NOT authoritative -- see the warnings above.")
        print("!!! Do not use these numbers for a Monday write until re-validated.")
        print("!" * 70)
    else:
        print(f"\nData freshness: OK -- cache fetched {result['cache_fetched_at']}, {year}-{month:02d} is complete.")


def run_update(config, monday_cfg, output_dir, year, month, refresh_teachworks_cache):
    print(f"=== Wright Academics Teachworks Reporting -- UPDATE ({year}-{month:02d}) ===")

    if (year, month) != (2026, 9):
        print(
            f"\nPhase 2 restricts writes to September 2026 ONLY. Refusing to write {year}-{month:02d}. "
            "Nothing was changed."
        )
        raise SystemExit(1)

    if not monday_cfg["api_token"]:
        print("\nMONDAY_API_TOKEN is not set in .env -- cannot write to Monday. Nothing was changed.")
        raise SystemExit(1)

    result = compute_reporting_result(config, output_dir, year, month, refresh_teachworks_cache)
    print_aggregation(result, year, month)

    if result["freshness_warnings"]:
        print("\n" + "!" * 70)
        for w in result["freshness_warnings"]:
            print(f"!!! {w}\n")
        print("!" * 70)

    target_name = month_item_name(year, month)
    status, item, diffs = preview_monday_changes(monday_cfg, result, year, month)

    if status in ("NOT_FOUND", "DUPLICATE", "SKIPPED", "ERROR"):
        print(f"\nAborting -- nothing was written to Monday (status: {status}).")
        raise SystemExit(1)

    print(f"\n!!! THIS WILL WRITE TO the {target_name!r} item on Monday board {monday_cfg['board_id']}. !!!")
    confirm = input("Type YES to continue, anything else to abort: ").strip()
    if confirm != "YES":
        print("Aborted -- nothing was written to Monday.")
        raise SystemExit(0)

    today_str = datetime.now(timezone.utc).date().isoformat()
    written = {}
    errors = []
    for key, (old_val, new_val) in diffs.items():
        column_id = monday_cfg[REPORTING_FIELD_COLUMN_KEY[key]]
        label = REPORTING_FIELD_LABELS[key]
        try:
            sm.set_monday_column_value(monday_cfg, item["id"], column_id, new_val)
            print(f"  {label}: {old_val!r} -> {new_val!r}: OK")
            written[key] = new_val
        except sm.MondayApiError as exc:
            print(f"  {label}: {old_val!r} -> {new_val!r}: ERROR: {exc}")
            errors.append({"field": label, "error": str(exc)})

    if not diffs:
        print("  (no Sessions Attended/Missed/Total Sessions/Students Served fields needed a change)")

    if not errors:
        try:
            sm.set_monday_column_value(monday_cfg, item["id"], monday_cfg["column_last_updated"], today_str)
            print(f"  Last Updated: -> {today_str!r}: OK")
        except sm.MondayApiError as exc:
            print(f"  Last Updated: -> {today_str!r}: ERROR: {exc}")
            errors.append({"field": "Last Updated", "error": str(exc)})
    else:
        print("  Skipping Last Updated stamp -- at least one field above failed.")

    log = {
        "item_id": item["id"], "item_name": item["name"], "board_id": monday_cfg["board_id"],
        "year": year, "month": month, "fields_written": written,
        "last_updated_stamped": today_str if not errors else None,
        "errors": errors, "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    log_path = output_dir / f"wright-teachworks-reporting-update-{year}-{month:02d}.json"
    audit.write_json(log_path, log)
    print(f"\nWrote update log: {log_path}")

    if errors:
        print(f"\n{len(errors)} field(s) failed to write -- see log above / {log_path.name}.")
        raise SystemExit(1)
    print("\nUpdate complete.")


def run_trend_dry_run(config, output_dir, year, start_month, end_month, refresh_teachworks_cache):
    """Teachworks-only validation across several months in ONE Teachworks pull
    (fetch once, filter locally per month) -- reuses compute_monthly_aggregation
    AND compute_monthly_financials, the exact same classification logic
    already validated for September (session-count logic is untouched; the
    financial numbers are a separate, independently-computed addition).
    Makes NO Monday API calls at all; --mode update remains restricted to a
    single month (September 2026) for session fields, and the multi-month
    update path never touches any financial column."""
    print(f"=== Wright Academics Teachworks Reporting -- MONTHLY TREND ({year}-{start_month:02d} to {year}-{end_month:02d}) ===")
    print("Teachworks-only, read-only. No Monday API calls are made in this mode.\n")

    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(config, output_dir, refresh_teachworks_cache)
    print(f"\nUsing {len(lessons)} total lesson record(s) across {lessons_pages} page(s) of full history;")
    print("filtering to each month locally from this single Teachworks pull.\n")

    freshness_warnings, cache_fetched_at = check_data_freshness(output_dir, year, end_month)
    if freshness_warnings:
        print("!" * 70)
        for w in freshness_warnings:
            print(f"!!! {w}\n")
        print("!" * 70 + "\n")

    rows = []
    for month in range(start_month, end_month + 1):
        result = compute_monthly_aggregation(config, lessons, year, month)
        fin = compute_monthly_financials(config, lessons, year, month)
        served = result["students_served"]
        avg = (result["sessions_attended"] / served) if served else None
        rows.append({
            "year": year, "month": month, "month_label": month_item_name(year, month),
            "sessions_attended": result["sessions_attended"],
            "sessions_missed": result["sessions_missed"],
            "total_sessions": result["total_sessions"],
            "students_served": served,
            "avg_attended_sessions_per_student": round(avg, 2) if avg is not None else None,
            "cancelled_count": result["cancelled_count"],
            "scheduled_count": result["scheduled_count"],
            "unclassified_count": result["unclassified_count"],
            "total_participant_records_considered": result["total_participant_records_considered"],
            "reconciliation_ok": result["reconciliation_ok"],
            "date_range_included": result["date_range_included"],
            "attended_amount": fin["attended_amount"],
            "missed_amount": fin["missed_amount"],
            "cancelled_amount": fin["cancelled_amount"],
            "scheduled_amount": fin["scheduled_amount"],
            "unclassified_amount": fin["unclassified_amount"],
            "total_amount": fin["total_amount"],
            "null_or_non_numeric_amount_count": fin["null_or_non_numeric_amount_count"],
            "financial_reconciliation_ok": fin["financial_reconciliation_ok"],
        })

    header = f"{'Month':<16}{'Attended':>10}{'Missed':>8}{'Total':>8}{'Students':>10}{'Avg/Student':>13}{'Recon':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        avg_str = f"{r['avg_attended_sessions_per_student']:.2f}" if r["avg_attended_sessions_per_student"] is not None else "n/a"
        recon_str = "OK" if r["reconciliation_ok"] else "MISMATCH"
        print(
            f"{r['month_label']:<16}{r['sessions_attended']:>10}{r['sessions_missed']:>8}"
            f"{r['total_sessions']:>8}{r['students_served']:>10}{avg_str:>13}{recon_str:>10}"
        )
        fin_recon_str = "OK" if r["financial_reconciliation_ok"] else "MISMATCH"
        print(
            f"    Amounts: Attended=${r['attended_amount']} Missed=${r['missed_amount']} "
            f"Cancelled=${r['cancelled_amount']} Scheduled=${r['scheduled_amount']} "
            f"Total=${r['total_amount']} Unclassified=${r['unclassified_amount']} "
            f"(null/non-numeric amount: {r['null_or_non_numeric_amount_count']}, FinRecon={fin_recon_str})"
        )

    mismatches = [r for r in rows if not r["reconciliation_ok"]]
    if mismatches:
        print(f"\nWARNING: {len(mismatches)} month(s) failed session-count reconciliation -- investigate before trusting them.")
    fin_mismatches = [r for r in rows if not r["financial_reconciliation_ok"]]
    if fin_mismatches:
        print(f"\nWARNING: {len(fin_mismatches)} month(s) failed financial reconciliation -- investigate before trusting them.")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_fetched_at": cache_fetched_at,
        "freshness_warnings": freshness_warnings,
        "months": rows,
    }
    out_path = output_dir / f"wright-teachworks-reporting-trend-{year}-{start_month:02d}-to-{end_month:02d}.json"
    audit.write_json(out_path, out)
    print(f"\nWrote: {out_path}")
    print("\nNo Monday API calls were made in this run. Nothing was written anywhere except this local report.")

    if freshness_warnings:
        print("\n" + "!" * 70)
        print("!!! REMINDER: see freshness warnings above -- an incomplete/in-progress month's row is NOT authoritative.")
        print("!" * 70)


def run_multi_update(config, monday_cfg, output_dir, year, start_month, end_month, refresh_teachworks_cache, allow_incomplete_month=False, skip_confirmation=False, auto_create_missing=False):
    """Multi-month historical backfill -- e.g. January-August 2026 -- and ALSO
    the engine behind --current-month (a 1-month range). Reuses the SAME
    Monday transport (sm.monday_graphql/fetch_all_monday_items/
    set_monday_column_value) and the SAME diff_reporting_fields function the
    single-month --mode update already uses; this is not a second write
    path, just a second caller of the existing one. Aborts the ENTIRE batch
    before any write if any requested month's item is NOT FOUND or
    DUPLICATE (subject to auto_create_missing below), if any month fails
    session-count OR financial reconciliation, if a requested month hasn't
    fully elapsed yet (unless allow_incomplete_month), OR if the board-schema
    preflight finds any configured column id missing or mismatched on the
    live board. Writes Sessions Attended/Missed, Total Sessions, Students
    Served, Avg Sessions / Student, Attended/Missed/Cancelled/Scheduled/
    Total Amount, and Last Updated. Never touches Payments.

    auto_create_missing=True (ONLY ever passed for --current-month, never
    for a --month-range historical backfill) allows creating AT MOST ONE
    item, only for a NOT_FOUND month, only when the update range is exactly
    one month, only inside an EXISTING year group (see find_year_group --
    a missing year group still aborts; groups are never auto-created), and
    only after the read-only detection/preview phase -- the actual
    create_item call happens in the same place, and behind the same
    confirmation gate, as every other write. After creating, the board is
    re-fetched and EXACTLY one matching item is re-verified before any
    field is written, so a race or an unexpected duplicate aborts that
    month rather than proceeding on an assumption. A DUPLICATE (more than
    one existing match) still always aborts -- auto-create only ever fires
    on a true NOT_FOUND (zero matches).

    skip_confirmation=True (only ever set by --yes) skips the interactive
    "Type YES" prompt for unattended/scheduled runs -- reads no stdin. Every
    other safeguard above still applies identically; the full preview is
    still printed either way, just not gated on a keypress."""
    print(f"=== Wright Academics Teachworks Reporting -- MULTI-MONTH UPDATE ({year}-{start_month:02d} to {year}-{end_month:02d}) ===")

    if not monday_cfg["api_token"]:
        print("\nMONDAY_API_TOKEN is not set in .env -- cannot write to Monday. Nothing was changed.")
        raise SystemExit(1)

    missing_columns = [c for c in MULTI_MONTH_ONLY_COLUMNS if not monday_cfg.get(c)]
    if missing_columns:
        env_names = [MONDAY_REPORTING_ENV_DEFAULTS[c][0] for c in missing_columns]
        print(
            "\nThe following required column id(s) are not set in .env: " + ", ".join(env_names) + ". "
            "This multi-month update writes Avg Sessions / Student and the five financial fields, "
            "and refuses to guess any column id. Run\n"
            f"  python sync_monday.py --mode inspect --board-id {monday_cfg['board_id']}\n"
            "copy the missing id(s) into .env, then re-run. Nothing was changed."
        )
        raise SystemExit(1)

    # Pure-local check (no Monday call) BEFORE the schema preflight below, so
    # an incomplete-month range is refused without ever touching Monday --
    # preserves the existing "zero Monday calls" guarantee for this guard.
    incomplete_months = [m for m in range(start_month, end_month + 1) if month_is_incomplete(year, m)]
    if incomplete_months and not allow_incomplete_month:
        labels = ", ".join(month_item_name(year, m) for m in incomplete_months)
        print(
            f"\nRefusing to write: {labels} has/have not finished yet as of today "
            f"({datetime.now(timezone.utc).date().isoformat()}). A multi-month historical update "
            "must not silently include a partial/in-progress month. Re-run with "
            "--allow-incomplete-month if you specifically intend to write a partial month. "
            "Nothing was changed."
        )
        raise SystemExit(1)

    # --- Board-schema preflight: confirm every configured column id ACTUALLY
    # EXISTS on the live board with the expected title/type, before touching
    # Teachworks or Monday items at all. This is what the September/January-
    # August live run was missing: fetch_reporting_items' column_values(ids:
    # [...]) silently tolerated the stale Total Amount id, so the batch
    # proceeded all the way to the write mutation before Monday rejected it.
    print(f"Board-schema preflight: checking configured column ids against the live board {monday_cfg['board_id']} ...")
    required_column_keys = list(REPORTING_FIELD_COLUMN_KEY.values()) + ["column_last_updated"]
    schema_problems = verify_board_schema(monday_cfg, required_column_keys)
    if schema_problems:
        print("\nAborting -- board-schema preflight failed (read-only check against the live board):")
        for p in schema_problems:
            print(f"  - {p}")
        print(
            f"\nNothing was written to Monday. Run `python sync_monday.py --mode inspect --board-id "
            f"{monday_cfg['board_id']}` to get the exact current column id(s), fix .env, then re-run."
        )
        raise SystemExit(1)
    print(f"Board-schema preflight OK -- all {len(required_column_keys)} configured column id(s) exist with the expected title/type.\n")

    # --- Compute every requested month from ONE Teachworks pull ---
    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(config, output_dir, refresh_teachworks_cache)
    print(f"\nUsing {len(lessons)} total lesson record(s) across {lessons_pages} page(s) of full history;")
    print("filtering to each month locally from this single Teachworks pull.\n")

    month_results = {}
    month_financials = {}
    for month in range(start_month, end_month + 1):
        result = compute_monthly_aggregation(config, lessons, year, month)
        fin = compute_monthly_financials(config, lessons, year, month)
        month_results[month] = result
        month_financials[month] = fin
        print_aggregation(result, year, month)
        print_monthly_financials(fin, year, month)
        print()

    reconciliation_failures = [m for m, r in month_results.items() if not r["reconciliation_ok"]]
    financial_reconciliation_failures = [m for m, f in month_financials.items() if not f["financial_reconciliation_ok"]]
    if reconciliation_failures or financial_reconciliation_failures:
        if reconciliation_failures:
            labels = ", ".join(month_item_name(year, m) for m in reconciliation_failures)
            print(f"Aborting -- session-count reconciliation failed for: {labels}.")
        if financial_reconciliation_failures:
            labels = ", ".join(month_item_name(year, m) for m in financial_reconciliation_failures)
            print(f"Aborting -- financial reconciliation failed for: {labels}.")
        print("Nothing was written to Monday.")
        raise SystemExit(1)

    # --- Fetch the board ONCE, match every requested month by exact name ---
    print(f"=== Matching Monday items on board {monday_cfg['board_id']} ===")
    try:
        items = fetch_reporting_items(monday_cfg)
    except sm.MondayApiError as exc:
        print(f"ERROR calling Monday API: {exc}\nNothing was written to Monday.")
        raise SystemExit(1)
    print(f"(scanned {len(items)} item(s) on the board)\n")

    matched = {}
    to_create = {}  # month -> (group_id, group_title) -- creation itself is deferred past confirmation
    problems = []
    for month in range(start_month, end_month + 1):
        target_name = month_item_name(year, month)
        matches = find_month_matches(items, year, month)
        if len(matches) == 1:
            matched[month] = matches[0]
        elif len(matches) > 1:
            problems.append(f"{target_name}: DUPLICATE -- {len(matches)} items found ({describe_matches(matches)}).")
        elif auto_create_missing:
            if (end_month - start_month + 1) != 1:
                problems.append(f"{target_name}: NOT FOUND, and auto-create is only supported for a single-month update.")
                continue
            print(f"{target_name}: NOT FOUND -- checking for an existing year group to create it in (read-only) ...")
            group_id, expected_group_title = find_year_group(monday_cfg, year)
            if group_id is None:
                problems.append(
                    f"{target_name}: NOT FOUND, and cannot auto-create -- no existing group titled "
                    f"{expected_group_title!r} on board {monday_cfg['board_id']}. This script never "
                    "creates year groups; create the group manually first, or confirm the real group "
                    "title with `python sync_reporting.py --list-groups`."
                )
                continue
            print(f"  Found year group {expected_group_title!r} (id={group_id}) -- will create {target_name!r} in it after confirmation.")
            to_create[month] = (group_id, expected_group_title)
        else:
            problems.append(f"{target_name}: NOT FOUND -- no existing item named {target_name!r} or "
                            f"{legacy_month_item_name(year, month)!r}. This script never creates items.")

    if problems:
        print("\nAborting the ENTIRE multi-month update -- nothing was written to Monday:")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)

    # --- Preview every month's diff BEFORE asking for the one confirmation ---
    # to_create months use blank_reporting_values() as "current" so the same
    # diff_reporting_fields logic shows every field as something to be set --
    # no separate preview code path for a freshly created item.
    print("=== Preview of ALL proposed Monday changes (no writes yet) ===")
    all_diffs = {}
    all_current = {}
    today_str = datetime.now(timezone.utc).date().isoformat()
    for month in range(start_month, end_month + 1):
        target_name = month_item_name(year, month)
        if month in to_create:
            current = blank_reporting_values()
            _, group_title = to_create[month]
            header = f"\n{target_name} (WILL CREATE new item in group {group_title!r}):"
        else:
            item = matched[month]
            current = read_current_reporting_values(item, monday_cfg)
            header = f"\n{target_name} (item id={item['id']}):"
        all_current[month] = current
        new_values = reporting_new_values(month_results[month], include_avg=True, fin=month_financials[month])
        diffs = diff_reporting_fields(current, new_values)
        all_diffs[month] = diffs

        print(header)
        if not diffs:
            print("  No field changes needed.")
        else:
            for key, (old, new) in diffs.items():
                print(f"  {REPORTING_FIELD_LABELS[key]}: {old!r} -> {new!r}")
        print(f"  Last Updated: {current['last_updated']!r} -> {today_str!r} (always stamped on a successful sync)")

    months_with_changes = sum(1 for d in all_diffs.values() if d)
    print(f"\n{months_with_changes} of {end_month - start_month + 1} month(s) have at least one field change.")
    if to_create:
        created_labels = ", ".join(month_item_name(year, m) for m in to_create)
        print(f"{len(to_create)} month(s) will be CREATED as a new item before being written: {created_labels}.")
    print("Avg Sessions / Student = Sessions Attended / Students Served, rounded to 2 decimals.")
    print("Total Amount = Attended + Missed + Cancelled + Scheduled Amount (Decimal-safe, quantized to cents).")
    print("Payments is never touched. Students board, Session Log board, Zapier, and Railway are never touched.")

    month_labels = ", ".join(month_item_name(year, m) for m in range(start_month, end_month + 1))
    print(f"\n!!! THIS WILL WRITE TO {end_month - start_month + 1} item(s) on Monday board {monday_cfg['board_id']}: {month_labels}. !!!")
    if skip_confirmation:
        print("--yes passed: skipping interactive confirmation for this unattended/scheduled run (reads no stdin).")
    else:
        confirm = input("Type YES to continue, anything else to abort: ").strip()
        if confirm != "YES":
            print("Aborted -- nothing was written to Monday.")
            raise SystemExit(0)

    # --- Write, then read each item back from Monday to verify ---
    write_results = []
    any_failure = False
    for month in range(start_month, end_month + 1):
        target_name = month_item_name(year, month)
        diffs = all_diffs[month]

        if month in to_create:
            group_id, group_title = to_create[month]
            print(f"\n--- Creating {target_name} in group {group_title!r} ---")
            try:
                new_item_id = create_month_item(monday_cfg, group_id, target_name)
            except sm.MondayApiError as exc:
                print(f"  ERROR creating item: {exc}")
                write_results.append({
                    "month": month, "month_label": target_name, "item_id": None,
                    "before": all_current[month], "fields_written": {}, "last_updated_written": None,
                    "after": None, "verify_status": "SKIPPED (item creation failed)",
                    "errors": [{"field": "item creation", "error": str(exc)}], "success": False,
                })
                any_failure = True
                continue
            print(f"  Created item id={new_item_id!r}. Re-fetching the board to verify exactly one item exists ...")
            try:
                refreshed_items = fetch_reporting_items(monday_cfg)
            except sm.MondayApiError as exc:
                print(f"  ERROR re-fetching board after creation: {exc}")
                write_results.append({
                    "month": month, "month_label": target_name, "item_id": new_item_id,
                    "before": all_current[month], "fields_written": {}, "last_updated_written": None,
                    "after": None, "verify_status": "SKIPPED (post-create re-fetch failed)",
                    "errors": [{"field": "post-create re-fetch", "error": str(exc)}], "success": False,
                })
                any_failure = True
                continue
            refreshed_matches = find_month_matches(refreshed_items, year, month)
            if len(refreshed_matches) != 1:
                msg = f"created an item but re-fetch found {len(refreshed_matches)} item(s) named this, not exactly 1"
                print(f"  ERROR: {msg} -- aborting this month rather than guessing which one is correct.")
                write_results.append({
                    "month": month, "month_label": target_name, "item_id": new_item_id,
                    "before": all_current[month], "fields_written": {}, "last_updated_written": None,
                    "after": None, "verify_status": f"COULD NOT VERIFY -- {msg}",
                    "errors": [{"field": "post-create verification", "error": msg}], "success": False,
                })
                any_failure = True
                continue
            item = refreshed_matches[0]
            print(f"  Verified exactly 1 item named {target_name!r} on the board (id={item['id']}).")
        else:
            item = matched[month]

        print(f"\n--- Writing {target_name} (item id={item['id']}) ---")

        month_errors = []
        fields_written = {}
        for key, (old_val, new_val) in diffs.items():
            column_id = monday_cfg[REPORTING_FIELD_COLUMN_KEY[key]]
            label = REPORTING_FIELD_LABELS[key]
            try:
                sm.set_monday_column_value(monday_cfg, item["id"], column_id, new_val)
                print(f"  {label}: {old_val!r} -> {new_val!r}: OK")
                fields_written[key] = new_val
            except sm.MondayApiError as exc:
                print(f"  {label}: {old_val!r} -> {new_val!r}: ERROR: {exc}")
                month_errors.append({"field": label, "error": str(exc)})

        if not diffs:
            print("  (no KPI fields needed a change)")

        last_updated_written = None
        if not month_errors:
            try:
                sm.set_monday_column_value(monday_cfg, item["id"], monday_cfg["column_last_updated"], today_str)
                print(f"  Last Updated: -> {today_str!r}: OK")
                last_updated_written = today_str
            except sm.MondayApiError as exc:
                print(f"  Last Updated: -> {today_str!r}: ERROR: {exc}")
                month_errors.append({"field": "Last Updated", "error": str(exc)})
        else:
            print("  Skipping Last Updated stamp -- at least one field above failed.")

        # --- Verify: read this exact item back from Monday (not from cache) ---
        after_values = None
        if month_errors:
            verify_status = "SKIPPED (write errors)"
        else:
            try:
                fresh_item = fetch_item_by_id(monday_cfg, item["id"])
                if fresh_item is None:
                    verify_status = "COULD NOT VERIFY -- item not found on read-back"
                else:
                    after_values = read_current_reporting_values(fresh_item, monday_cfg)
                    field_mismatches = [
                        key for key, new_val in fields_written.items()
                        if not _numeric_equal(after_values.get(key), new_val)
                    ]
                    if last_updated_written and _norm(after_values.get("last_updated")) != _norm(last_updated_written):
                        field_mismatches.append("last_updated")
                    verify_status = "OK" if not field_mismatches else f"MISMATCH on: {', '.join(field_mismatches)}"
            except sm.MondayApiError as exc:
                verify_status = f"COULD NOT VERIFY -- {exc}"

        success = not month_errors and verify_status == "OK"
        any_failure = any_failure or not success
        print(f"  Verify (read back from Monday): {verify_status}")
        print(f"  Result: {'SUCCESS' if success else 'FAILED'}")

        write_results.append({
            "month": month, "month_label": target_name, "item_id": item["id"],
            "before": all_current[month], "fields_written": fields_written,
            "last_updated_written": last_updated_written, "after": after_values,
            "verify_status": verify_status, "errors": month_errors, "success": success,
        })

    print("\n=== MULTI-MONTH UPDATE SUMMARY ===")
    for r in write_results:
        print(f"  {r['month_label']:<16} {'SUCCESS' if r['success'] else 'FAILED':<8} verify={r['verify_status']}")

    log = {
        "year": year, "start_month": start_month, "end_month": end_month,
        "board_id": monday_cfg["board_id"], "synced_at": datetime.now(timezone.utc).isoformat(),
        "months": write_results,
    }
    log_path = output_dir / f"wright-teachworks-reporting-multi-update-{year}-{start_month:02d}-to-{end_month:02d}.json"
    audit.write_json(log_path, log)
    print(f"\nWrote update log: {log_path}")

    if any_failure:
        failed = [r["month_label"] for r in write_results if not r["success"]]
        print(f"\n{len(failed)} of {len(write_results)} month(s) FAILED: {', '.join(failed)}. See log above / {log_path.name}.")
        raise SystemExit(1)
    print(f"\nAll {len(write_results)} month(s) updated and verified successfully.")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Wright Academics Teachworks Reporting board sync. 'dry-run' is always read-only "
            "(Teachworks aggregation + a Monday preview, no writes; add --month-range for a "
            "multi-month Teachworks-only trend table). 'update' WRITES to the matching Monday "
            "item -- Phase 2 restricts this to September 2026 only."
        )
    )
    parser.add_argument(
        "--mode", choices=["dry-run", "update"], default="dry-run",
        help=(
            "'dry-run': compute the Teachworks aggregation and preview the exact Monday changes "
            "(read-only, makes no writes). 'update': WRITES Sessions Attended/Missed, Total "
            "Sessions, Students Served, and Last Updated to the existing Monday item matching "
            "this month's name -- never creates an item, restricted to September 2026 for Phase 2 "
            "unless combined with --month-range for a multi-month historical backfill."
        ),
    )
    parser.add_argument("--year", type=int, default=2026, help="Calendar year to aggregate (default 2026).")
    parser.add_argument("--month", type=int, default=9, help="Calendar month to aggregate, 1-12 (default 9 = September).")
    parser.add_argument(
        "--month-range", default=None,
        help=(
            "e.g. '1-8'. With --mode dry-run: a Teachworks-only monthly trend table "
            "(Attended/Missed/Total/Students Served/Avg per Student), no Monday calls. With "
            "--mode update: a multi-month historical backfill that also writes Avg Sessions / "
            "Student -- matches each month's existing Monday item by name, previews every "
            "month first, asks for one typed confirmation, then writes and verifies each."
        ),
    )
    parser.add_argument(
        "--allow-incomplete-month", action="store_true",
        help=(
            "Multi-month update only (--mode update --month-range): allow writing a month that "
            "has not fully elapsed yet. Without this, such a month causes the whole batch to be "
            "refused before any write. Implied automatically by --current-month."
        ),
    )
    parser.add_argument(
        "--current-month", action="store_true",
        help=(
            "Resolve --year/--month-range to today's calendar month at runtime, updating exactly "
            "that one month. Intended for an unattended/scheduled run (e.g. a daily Railway cron "
            "job) so the command line never needs editing as months roll over. Automatically "
            "implies --allow-incomplete-month, since the current month is always in progress by "
            "definition -- that's the point: KPIs and Scheduled/Attended/Missed/Cancelled Amount "
            "are meant to keep refreshing as Teachworks statuses change through the month. Not "
            "valid combined with --month-range (pick one)."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "Multi-month/--current-month update only: skip the interactive 'Type YES' "
            "confirmation prompt for unattended execution (reads no stdin). Every other "
            "safeguard (schema preflight, reconciliation, exact name matching, NOT_FOUND/"
            "DUPLICATE abort, read-back verification) still applies identically."
        ),
    )
    parser.add_argument(
        "--list-groups", action="store_true",
        help=(
            "Read-only: print the Reporting board's real group id(s)/title(s) from Monday and exit. "
            "Use this to confirm the exact year-group naming convention (e.g. is it '2026' or "
            "something else?) before relying on --current-month's auto-create, which matches a "
            "group by exact title and never creates one. Makes one read-only Monday call, no writes."
        ),
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--refresh-teachworks-cache", action="store_true",
        help="Ignore audit.py's cached Teachworks students/lessons pull and re-fetch from Teachworks.",
    )
    args = parser.parse_args()

    if args.list_groups:
        monday_cfg = load_reporting_monday_config()
        print(f"=== Groups on Monday board {monday_cfg['board_id']} (read-only) ===")
        try:
            groups = fetch_board_groups(monday_cfg)
        except sm.MondayApiError as exc:
            print(f"ERROR calling Monday API: {exc}")
            raise SystemExit(1)
        if not groups:
            print("No groups found on this board.")
        for g in groups:
            print(f"  id={g['id']!r}  title={g['title']!r}")
        print(
            "\nThe current MONDAY_REPORTING_YEAR_GROUP_TITLE_TEMPLATE resolves this year to: "
            f"{year_group_title(datetime.now(timezone.utc).date().year)!r} -- confirm a group above "
            "matches exactly, or set that env var to the real convention."
        )
        return

    if args.current_month and args.month_range:
        print("--current-month cannot be combined with --month-range -- pick one.")
        raise SystemExit(2)

    if args.current_month:
        today = datetime.now(timezone.utc).date()
        args.year = today.year
        args.month_range = f"{today.month}-{today.month}"
        args.allow_incomplete_month = True

    output_dir = Path(args.output_dir) if args.output_dir else audit.SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    config = audit.load_config()

    if args.month_range:
        try:
            start_s, end_s = args.month_range.split("-")
            start_month, end_month = int(start_s), int(end_s)
        except ValueError:
            print("--month-range must look like '1-9'.")
            raise SystemExit(2)
        if not (1 <= start_month <= end_month <= 12):
            print("--month-range must satisfy 1 <= start <= end <= 12.")
            raise SystemExit(2)
        if args.current_month and start_month != end_month:
            print("--current-month must resolve to exactly one month.")
            raise SystemExit(2)

        if args.mode == "dry-run":
            run_trend_dry_run(config, output_dir, args.year, start_month, end_month, args.refresh_teachworks_cache)
        else:
            monday_cfg = load_reporting_monday_config()
            run_multi_update(
                config, monday_cfg, output_dir, args.year, start_month, end_month,
                args.refresh_teachworks_cache, allow_incomplete_month=args.allow_incomplete_month,
                skip_confirmation=args.yes, auto_create_missing=args.current_month,
            )
        return

    if not (1 <= args.month <= 12):
        print("--month must be between 1 and 12.")
        raise SystemExit(2)

    monday_cfg = load_reporting_monday_config()

    if args.mode == "dry-run":
        run_dry_run(config, output_dir, args.year, args.month, args.refresh_teachworks_cache, monday_cfg=monday_cfg)
    else:
        run_update(config, monday_cfg, output_dir, args.year, args.month, args.refresh_teachworks_cache)


if __name__ == "__main__":
    main()
