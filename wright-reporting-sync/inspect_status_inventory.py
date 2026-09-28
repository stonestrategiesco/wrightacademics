#!/usr/bin/env python3
"""Wright Academics -- full-history Teachworks status inventory. READ-ONLY,
DIAGNOSTIC ONLY.

Purpose: before defining "Attended" / "Missed" / "Excluded" for the new
Teachworks Reporting board, show the REAL lesson-level and participant-level
status values that exist across Wright Academics' full Teachworks lesson
history -- so that classification is decided from actual data, never assumed.

Uses ONLY audit.py's existing cache (output/_cache/lessons_raw.json etc. from
a prior audit.py/sync_monday.py/backfill_session_amounts.py run) if present --
makes NO new Teachworks API calls in that case. Falls back to a fresh full
pull only if no cache exists yet (pass --refresh-cache to force one).

Sanitized, same rule as audit.py's inspect_participants: only prints/writes
VALUES for participant-object keys that look like an id/type/category/role/
kind/status/attendance field -- never names, emails, phone numbers, etc.

Does NOT classify anything as Attended/Missed/Excluded. Does NOT touch
Monday. Does NOT compute the reporting-board aggregation -- all of that is a
separate, later step once the classification is agreed on.
"""

import sys
from collections import Counter

import audit


def main():
    refresh = "--refresh-cache" in sys.argv
    output_dir = audit.SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    config = audit.load_config()

    print("=== Wright Academics Teachworks Status Inventory (read-only, full history) ===")
    print("(Reuses your existing cache if present -- no new Teachworks calls in that case.)\n")

    students, students_pages, lessons, lessons_pages = audit.load_or_fetch_all(config, output_dir, refresh)
    print(f"\nUsing {len(lessons)} lesson record(s) across {lessons_pages} page(s) ({len(students)} students).\n")

    # --- Lesson-level status: every distinct value of the confirmed status field ---
    status_field = config["status_field"]
    lesson_status_counts = Counter(str(audit.get_nested(lesson, status_field)) for lesson in lessons)

    print(f"=== LESSON-LEVEL status (lesson['{status_field}']) -- {len(lessons)} lessons ===")
    for status_value, count in lesson_status_counts.most_common():
        print(f"  {status_value}: {count}")

    # --- Participant-level structure, over the FULL history (not a small sample) ---
    inspection = audit.inspect_participants(lessons, config["participants_field"])

    print(
        f"\n=== PARTICIPANT-LEVEL structure (sanitized -- no PII) -- "
        f"scanned all {inspection['sample_size']} lessons ==="
    )
    print(f"'{config['participants_field']}' present on {inspection['field_present_on_sample']} of {inspection['sample_size']} lessons.")
    print(f"Container type(s): {inspection['container_types_seen']}")
    print(f"Participant item type(s): {inspection['item_types_seen']}")
    if inspection["keys_seen_on_participant_objects"]:
        print("Keys on participant objects (values shown ONLY for id/type/category/role/kind/status/attendance-like keys,")
        print("capped at 10 distinct values per key -- check the JSON output if a field has more):")
        for k, info in inspection["keys_seen_on_participant_objects"].items():
            if info["safe_sample_values"]:
                print(f"  - {k} (type: {', '.join(info['type_seen'])}): {info['safe_sample_values']}")
            else:
                print(f"  - {k} (type: {', '.join(info['type_seen'])}): (value withheld -- not an id/type/status-like field)")
    else:
        print("No participant object keys found at all in this dataset.")

    summary = {
        "total_lessons_scanned": len(lessons),
        "lesson_status_field": status_field,
        "lesson_status_counts": dict(lesson_status_counts.most_common()),
        "participant_structure": inspection,
    }
    out_path = output_dir / "wright-teachworks-full-status-inventory.json"
    audit.write_json(out_path, summary)
    print(f"\nWrote: {out_path}")
    print(
        "\nThis is diagnostic only: nothing was classified as Attended/Missed/Excluded, "
        "nothing was aggregated, and nothing was written to Monday. Send back this console "
        "output (or the JSON file) so we can agree on the classification before any reporting "
        "code gets written."
    )


if __name__ == "__main__":
    main()
