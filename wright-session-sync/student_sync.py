"""
Students board roster sync: every Teachworks student should have exactly one
Students board item, matched by Teachworks Student ID.

For each Teachworks student (every status):
  * a Students item already has their Teachworks Student ID -> matched, nothing changes;
  * otherwise, if anything looks like it could already be them -> EXCEPTION for a
    person to review, nothing is created:
      - a Students item whose name matches (case, spacing, punctuation and word
        order ignored; one name containing all the other's words also counts),
        with or without a Teachworks Student ID;
      - another Teachworks student with a matching name;
      - no usable name;
  * otherwise -> created (only when STUDENT_SYNC_CREATE=true and a group is
    configured; until then it is reported as "would create").

A new item gets only its name, Teachworks Student ID and Historical Session
Baseline (its distinct Pre-Baseline Session Log sessions, so the nightly
rollup counts history correctly). No onboarding, contract, billing, family or
communication field is touched. Its Session Log rows are then connected to it.

Monday items with the same Teachworks Student ID twice are reported, never merged.
Re-running finds every created student by ID, so nothing is created twice.
"""

import logging
import re
from dataclasses import dataclass, field

import config

logger = logging.getLogger("wright_sync.students")


def name_tokens(name):
    """'Ash, James ' / 'james  ash' -> ('ash', 'james'). Order-insensitive."""
    return tuple(sorted(t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t))


def names_may_match(a, b):
    """Same words in any order, or one name's words all contained in the other's
    (a middle name or extra surname) - with at least two words on the shorter side."""
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= 2 and set(small) <= set(big)


def teachworks_name(student):
    first = (student.get("first_name") or "").strip()
    last = (student.get("last_name") or "").strip()
    return " ".join(p for p in (first, last) if p) or (student.get("name") or "").strip()


@dataclass
class StudentSyncReport:
    dry_run: bool = False
    create_enabled: bool = False
    teachworks_students: int = 0
    monday_students: int = 0
    matched: int = 0
    to_create: list = field(default_factory=list)     # planned (dry run / creation off) or created
    created: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)    # need a person to review
    sessions_connected: int = 0
    errors: list = field(default_factory=list)
    blocked: str = ""                                  # why nothing was created this run, if so

    @property
    def succeeded(self):
        return not self.errors


def sync_students(tw_client, monday_client, dry_run=False):
    report = StudentSyncReport(dry_run=dry_run, create_enabled=config.STUDENT_SYNC_CREATE)

    roster = [s for s in tw_client.get_all_students() if s.get("id") is not None]
    report.teachworks_students = len(roster)
    items = monday_client.get_items(config.MONDAY_STUDENTS_BOARD_ID, [config.STUDENT_BOARD_COL_TEACHWORKS_ID])
    report.monday_students = len(items)

    by_id = {}
    for item in items:
        tw_id = (item["columns"].get(config.STUDENT_BOARD_COL_TEACHWORKS_ID) or "").strip()
        if tw_id:
            by_id.setdefault(tw_id, []).append(item)
    for tw_id, dupes in sorted(by_id.items()):
        if len(dupes) > 1:
            report.exceptions.append({"teachworks_id": tw_id, "name": dupes[0].get("item_name"),
                                      "reason": f"{len(dupes)} Students items share this Teachworks Student ID: "
                                                + ", ".join(f"item {d['item_id']} {d.get('item_name')!r}" for d in dupes)})

    # Index names by word so each student is only compared with names sharing a word.
    monday_by_word, tw_by_word = {}, {}
    for item in items:
        tokens = name_tokens(item.get("item_name"))
        for word in set(tokens):
            monday_by_word.setdefault(word, []).append((tokens, item))
    for other in roster:
        tokens = name_tokens(teachworks_name(other))
        for word in set(tokens):
            tw_by_word.setdefault(word, []).append((tokens, other))

    def candidates(index, tokens, key):
        seen = {}
        for word in tokens:
            for entry in index.get(word, []):
                seen.setdefault(key(entry[1]), entry)
        return [seen[k] for k in sorted(seen)]

    for student in sorted(roster, key=lambda s: (teachworks_name(s).lower(), str(s["id"]))):
        tw_id, name = str(student["id"]), teachworks_name(student)
        if tw_id in by_id:
            report.matched += 1
            continue
        tokens = name_tokens(name)
        reasons = []
        if len(tokens) < 2:
            reasons.append("no usable first and last name in Teachworks")
        for other_tokens, item in candidates(monday_by_word, tokens, lambda i: str(i["item_id"])):
            if names_may_match(tokens, other_tokens):
                other_id = (item["columns"].get(config.STUDENT_BOARD_COL_TEACHWORKS_ID) or "").strip()
                reasons.append(f"Students item {item['item_id']} {item.get('item_name')!r} "
                               + (f"has Teachworks ID {other_id}" if other_id else "has no Teachworks ID"))
        for other_tokens, other in candidates(tw_by_word, tokens, lambda o: str(o["id"])):
            if str(other["id"]) != tw_id and names_may_match(tokens, other_tokens):
                reasons.append(f"Teachworks student {other['id']} {teachworks_name(other)!r} has a matching name")
        if reasons:
            report.exceptions.append({"teachworks_id": tw_id, "name": name, "reason": "; ".join(reasons)})
            continue
        report.to_create.append({"teachworks_id": tw_id, "name": name, "status": student.get("status")})

    if not report.to_create:
        return report

    # Baseline for each new student = their distinct Pre-Baseline Session Log sessions.
    from sync import _canonical_session_identity  # local import: sync imports this module
    session_columns = [config.COL_UNIQUE_ID, config.COL_TEACHWORKS_STUDENT_ID,
                       config.COL_STUDENT_CONNECTION, config.COL_PRE_BASELINE]
    session_rows = monday_client.get_items(config.MONDAY_SESSIONS_BOARD_ID, session_columns)
    rows_by_student = {}
    for row in session_rows:
        rows_by_student.setdefault((row["columns"].get(config.COL_TEACHWORKS_STUDENT_ID) or "").strip(), []).append(row)
    for plan in report.to_create:
        rows = rows_by_student.get(plan["teachworks_id"], [])
        plan["baseline"] = len({_canonical_session_identity(r) for r in rows
                                if (r["columns"].get(config.COL_PRE_BASELINE) or "").strip()})
        plan["unconnected_rows"] = [r for r in rows if not (r["columns"].get(config.COL_STUDENT_CONNECTION) or "").strip()]

    if len(report.to_create) > config.STUDENT_SYNC_MAX_CREATES:
        report.blocked = (f"{len(report.to_create)} students to create is more than STUDENT_SYNC_MAX_CREATES "
                          f"({config.STUDENT_SYNC_MAX_CREATES}); nothing created - review the list, then raise the limit")
    elif not config.STUDENT_SYNC_CREATE:
        report.blocked = "student creation is off (STUDENT_SYNC_CREATE is not true); nothing created"
    elif not config.MONDAY_STUDENTS_NEW_GROUP_ID:
        report.blocked = "MONDAY_STUDENTS_NEW_GROUP_ID is not set; nothing created"
    elif dry_run:
        report.blocked = "dry run; nothing created"
    if report.blocked:
        if len(report.to_create) > config.STUDENT_SYNC_MAX_CREATES and not dry_run and config.STUDENT_SYNC_CREATE:
            report.errors.append({"teachworks_id": None, "error": report.blocked})
        return report

    for plan in report.to_create:
        try:
            item_id = monday_client.create_student_item(
                config.MONDAY_STUDENTS_BOARD_ID, config.MONDAY_STUDENTS_NEW_GROUP_ID, plan["name"],
                {config.STUDENT_BOARD_COL_TEACHWORKS_ID: plan["teachworks_id"],
                 config.STUDENT_COL_HISTORICAL_BASELINE: plan["baseline"]})
        except Exception as exc:  # noqa: BLE001 - one bad student must not stop the run
            logger.error("STUDENT_CREATE_ERROR teachworks_id=%s error=%s", plan["teachworks_id"], exc)
            report.errors.append({"teachworks_id": plan["teachworks_id"], "error": str(exc)})
            continue
        plan["item_id"] = item_id
        report.created.append(plan)
        logger.info("STUDENT_CREATED item_id=%s teachworks_id=%s name=%s", item_id, plan["teachworks_id"], plan["name"])
        for row in plan["unconnected_rows"]:
            try:
                monday_client.connect_student(config.MONDAY_SESSIONS_BOARD_ID, row["item_id"],
                                              config.COL_STUDENT_CONNECTION, item_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("CONNECTION_ERROR item_id=%s student_item_id=%s error=%s", row["item_id"], item_id, exc)
                report.errors.append({"teachworks_id": plan["teachworks_id"], "error": f"connect {row['item_id']}: {exc}"})
                continue
            report.sessions_connected += 1
    return report


def print_student_report(report):
    def line(label, value):
        print(f"{label + ':':<46}{value}")

    line("Teachworks students (all statuses)", report.teachworks_students)
    line("Students board items", report.monday_students)
    line("Existing students matched by Teachworks ID", report.matched)
    line("Students created", len(report.created))
    if report.blocked:
        line("Students that WOULD be created", len(report.to_create))
    line("Exceptions requiring review", len(report.exceptions))
    line("Session Log rows connected to new students", report.sessions_connected)
    line("Errors", len(report.errors))
    if report.blocked:
        print(f"NOT CREATED: {report.blocked}")
    if report.to_create:
        print("CREATED:" if report.created and not report.blocked else "WOULD CREATE:")
        for p in report.to_create:
            print(f"  - {p['name']} (Teachworks ID {p['teachworks_id']}, Teachworks status {p.get('status') or 'n/a'}) - "
                  f"baseline {p.get('baseline', 0)}, {len(p.get('unconnected_rows', []))} Session Log row(s) to connect"
                  + (f" -> item {p['item_id']}" if p.get("item_id") else ""))
    if report.exceptions:
        print("EXCEPTIONS (nothing created - please review):")
        for e in report.exceptions:
            print(f"  - {e['name']!r} (Teachworks ID {e['teachworks_id']}): {e['reason']}")
    if report.errors:
        print("ERRORS:")
        for e in report.errors:
            print(f"  - {e['teachworks_id']}: {e['error']}")
