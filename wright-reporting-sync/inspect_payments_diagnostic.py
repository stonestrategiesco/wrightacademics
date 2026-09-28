#!/usr/bin/env python3
"""Wright Academics -- Teachworks Payments field diagnostic (Phase 1B
investigation). READ ONLY, DIAGNOSTIC ONLY.

Purpose: figure out what participant['amount'] actually represents --
lesson revenue (priced regardless of what happens to the lesson), invoiced
revenue (tied to invoice_id regardless of whether it was paid), or actual
cash collected -- before deciding what the Monday "Payments" column should
be fed with. This script does NOT assume an answer; it only surfaces
patterns (nulls, sums by attendance status, amount vs unit_price,
invoice_id cardinality) for a human to interpret.

Makes NO Monday API calls, writes nothing to Teachworks, and does not touch
the Students board or the old Session Log board. Reuses audit.py for all
Teachworks auth/pagination/caching (reuses your existing cache if present --
no new Teachworks calls in that case) and sync_reporting.py's month-filter/
classification helpers -- no new Teachworks transport code.
"""

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import audit
import sync_reporting as sr


def _is_present(value):
    return value is not None and value != ""


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect_september_participant_records(config, lessons, year, month):
    lesson_date_field = audit.detect_field(lessons, audit.LESSON_DATE_CANDIDATES, override=config["lesson_date_field"])
    if not lesson_date_field:
        raise SystemExit("Could not detect a lesson date field -- cannot filter by month.")

    records = []
    for lesson in lessons:
        dt = audit.parse_date_value(audit.get_nested(lesson, lesson_date_field))
        if dt is None or dt.year != year or dt.month != month:
            continue
        participants = lesson.get("participants") or []
        if not isinstance(participants, list):
            participants = [participants]
        for p in participants:
            if not isinstance(p, dict):
                continue
            records.append({
                "lesson_id": lesson.get("id"),
                "student_id": p.get("student_id"),
                "status": sr.classify_participant(p),
                "raw_status": p.get(sr.PARTICIPANT_STATUS_FIELD),
                "amount": p.get("amount"),
                "unit_price": p.get("unit_price"),
                "invoice_id": p.get("invoice_id"),
            })
    return records


def run_payments_diagnostic(config, output_dir, year, month, refresh_teachworks_cache):
    print(f"=== Wright Academics Teachworks Payments Field Diagnostic ({year}-{month:02d}) -- READ ONLY ===")
    print(
        "Investigates participant-level amount/unit_price/invoice_id only. Makes NO Monday API "
        "calls, writes nothing to Teachworks, and does not touch the Students board or the "
        "Session Log board.\n"
    )

    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(config, output_dir, refresh_teachworks_cache)
    freshness_warnings, cache_fetched_at = sr.check_data_freshness(output_dir, year, month)
    if freshness_warnings:
        print("!" * 70)
        for w in freshness_warnings:
            print(f"!!! {w}\n")
        print("!" * 70 + "\n")

    records = collect_september_participant_records(config, lessons, year, month)
    total = len(records)
    print(f"Participant records considered ({year}-{month:02d}): {total}\n")

    if total == 0:
        print("No participant records found for this month -- nothing to analyze.")
        return

    amount_values = [_to_number(r["amount"]) for r in records]
    unit_price_values = [_to_number(r["unit_price"]) for r in records]

    # --- Field presence ---------------------------------------------------
    amount_present = sum(1 for r in records if _is_present(r["amount"]))
    unit_price_present = sum(1 for r in records if _is_present(r["unit_price"]))
    invoice_present = sum(1 for r in records if _is_present(r["invoice_id"]))
    print("=== Field presence ===")
    print(f"amount present:      {amount_present}/{total} ({amount_present / total * 100:.1f}%)")
    print(f"unit_price present:  {unit_price_present}/{total} ({unit_price_present / total * 100:.1f}%)")
    print(f"invoice_id present:  {invoice_present}/{total} ({invoice_present / total * 100:.1f}%)")

    # --- Sums (nulls/non-numeric excluded, reported separately) ------------
    amount_numeric = [v for v in amount_values if v is not None]
    unit_price_numeric = [v for v in unit_price_values if v is not None]
    non_numeric_amount = sum(1 for r, v in zip(records, amount_values) if _is_present(r["amount"]) and v is None)

    print("\n=== Sums (numeric values only; nulls excluded from the sum) ===")
    print(f"sum(amount):     {sum(amount_numeric):.2f} across {len(amount_numeric)} numeric record(s)")
    print(f"sum(unit_price): {sum(unit_price_numeric):.2f} across {len(unit_price_numeric)} numeric record(s)")
    if non_numeric_amount:
        print(f"NOTE: {non_numeric_amount} record(s) had a non-null amount that could not be parsed as a number.")

    # --- THE key diagnostic: does amount depend on attendance status? ------
    print("\n=== amount, by participant status (does a Cancelled/Scheduled record still carry an amount?) ===")
    by_status_count = Counter()
    by_status_amount_sum = defaultdict(float)
    by_status_amount_present = Counter()
    for r, v in zip(records, amount_values):
        by_status_count[r["status"]] += 1
        by_status_amount_present[r["status"]] += 1 if _is_present(r["amount"]) else 0
        if v is not None:
            by_status_amount_sum[r["status"]] += v
    for status in sorted(by_status_count):
        n = by_status_count[status]
        present = by_status_amount_present[status]
        print(f"  {status:<14} n={n:<5} amount present={present:<5} sum(amount)={by_status_amount_sum[status]:.2f}")

    # --- amount vs unit_price relationship ----------------------------------
    print("\n=== amount vs unit_price (per record, where both are numeric) ===")
    both_numeric = [
        (r, a, u) for r, a, u in zip(records, amount_values, unit_price_values)
        if a is not None and u is not None
    ]
    equal_count = sum(1 for _, a, u in both_numeric if abs(a - u) < 0.005)
    different_count = len(both_numeric) - equal_count
    print(f"Both numeric: {len(both_numeric)} record(s)")
    print(f"  amount == unit_price: {equal_count}")
    print(f"  amount != unit_price: {different_count}")
    sample_diffs = []
    if different_count:
        diffs = sorted((a - u for _, a, u in both_numeric if abs(a - u) >= 0.005))
        print(f"  difference range where they differ: {diffs[0]:.2f} to {diffs[-1]:.2f}")
        sample_diffs = [
            {"lesson_id": r["lesson_id"], "student_id": r["student_id"], "status": r["status"], "amount": a, "unit_price": u}
            for r, a, u in both_numeric if abs(a - u) >= 0.005
        ][:10]
        print("  sample differing records (up to 10):")
        for s in sample_diffs:
            print(f"    {s}")

    # --- invoice_id cardinality: does one invoice cover multiple lessons? --
    print("\n=== invoice_id cardinality ===")
    invoice_counts = Counter(r["invoice_id"] for r in records if _is_present(r["invoice_id"]))
    unique_invoices = len(invoice_counts)
    print(f"Unique invoice_id values referenced: {unique_invoices}")
    records_per_invoice_min = records_per_invoice_max = None
    if unique_invoices:
        counts = list(invoice_counts.values())
        records_per_invoice_min, records_per_invoice_max = min(counts), max(counts)
        print(f"Participant records per invoice_id: min={records_per_invoice_min}, max={records_per_invoice_max}, avg={sum(counts) / len(counts):.2f}")
        multi = sum(1 for c in counts if c > 1)
        print(f"invoice_id values shared by more than one participant record: {multi} of {unique_invoices}")

    # --- Mismatches ----------------------------------------------------------
    amount_no_invoice = sum(1 for r in records if _is_present(r["amount"]) and not _is_present(r["invoice_id"]))
    invoice_no_amount = sum(1 for r in records if _is_present(r["invoice_id"]) and not _is_present(r["amount"]))
    print("\n=== Mismatches ===")
    print(f"amount present but invoice_id missing: {amount_no_invoice}")
    print(f"invoice_id present but amount missing: {invoice_no_amount}")

    # --- Raw sample for manual review (IDs only, no names/PII) -------------
    print("\n=== Sample raw records (up to 20, no student names/PII -- IDs only) ===")
    for r in records[:20]:
        print(f"  {r}")

    summary = {
        "year": year, "month": month,
        "cache_fetched_at": cache_fetched_at,
        "freshness_warnings": freshness_warnings,
        "total_participant_records": total,
        "field_presence": {
            "amount_present": amount_present,
            "unit_price_present": unit_price_present,
            "invoice_id_present": invoice_present,
        },
        "sums": {
            "sum_amount_numeric": sum(amount_numeric),
            "sum_unit_price_numeric": sum(unit_price_numeric),
            "amount_numeric_count": len(amount_numeric),
            "unit_price_numeric_count": len(unit_price_numeric),
            "amount_present_but_non_numeric": non_numeric_amount,
        },
        "by_status": {
            status: {
                "count": by_status_count[status],
                "amount_present": by_status_amount_present[status],
                "sum_amount": by_status_amount_sum[status],
            }
            for status in by_status_count
        },
        "amount_vs_unit_price": {
            "both_numeric_count": len(both_numeric),
            "equal_count": equal_count,
            "different_count": different_count,
            "sample_differing_records": sample_diffs,
        },
        "invoice_cardinality": {
            "unique_invoice_ids": unique_invoices,
            "records_per_invoice_min": records_per_invoice_min,
            "records_per_invoice_max": records_per_invoice_max,
        },
        "mismatches": {
            "amount_no_invoice": amount_no_invoice,
            "invoice_no_amount": invoice_no_amount,
        },
        "sample_records": records[:20],
    }
    out_path = output_dir / f"wright-teachworks-payments-diagnostic-{year}-{month:02d}.json"
    audit.write_json(out_path, summary)
    print(f"\nWrote: {out_path}")
    print("\nThis is a diagnostic only -- no Payments logic has been added to sync_reporting.py.")
    print("No Monday API calls were made. Nothing was written to Teachworks, the Students board, or the Session Log board.")


def main():
    parser = argparse.ArgumentParser(description="Wright Academics Teachworks Payments field diagnostic (read-only).")
    parser.add_argument("--year", type=int, default=2026, help="Calendar year (default 2026).")
    parser.add_argument("--month", type=int, default=9, help="Calendar month, 1-12 (default 9 = September).")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--refresh-teachworks-cache", action="store_true",
        help="Ignore audit.py's cached Teachworks students/lessons pull and re-fetch from Teachworks.",
    )
    args = parser.parse_args()

    if not (1 <= args.month <= 12):
        print("--month must be between 1 and 12.")
        raise SystemExit(2)

    output_dir = Path(args.output_dir) if args.output_dir else audit.SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    config = audit.load_config()
    run_payments_diagnostic(config, output_dir, args.year, args.month, args.refresh_teachworks_cache)


if __name__ == "__main__":
    main()
