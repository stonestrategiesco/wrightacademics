"""Tests for the read-only --diagnose-dedup investigation: it must never
write to Monday, must classify every session as composite/legacy/unmatched
exactly as run_sync() would, and its secondary historical-match search is
diagnostic only."""

import config
from sync import diagnose_dedup
from tests.fakes import FakeMondayClient, FakeTeachworksClient, make_lesson, make_participant


class WriteGuardedMondayClient(FakeMondayClient):
    """Raises if any write method is ever called - a hard guarantee on top
    of just checking created_items/connections stayed empty."""

    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called by --diagnose-dedup")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called by --diagnose-dedup")


def _monday_item(item_id, item_name="", unique_id="", session_date="", tw_student_id="", student_name="", tutor="", service=""):
    return {
        "item_id": item_id,
        "item_name": item_name,
        "columns": {
            config.COL_UNIQUE_ID: unique_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_STUDENT_NAME: student_name,
            config.COL_TUTOR: tutor,
            config.COL_SERVICE: service,
        },
    }


def test_diagnose_dedup_makes_zero_monday_writes(capsys):
    lessons = [
        make_lesson(1, "2026-09-13", [make_participant(101, "Alice")]),
        make_lesson(2, "2026-09-13", [make_participant(102, "Bob")]),
    ]
    tw = FakeTeachworksClient(lessons)
    monday = WriteGuardedMondayClient(items=[_monday_item("m1", unique_id="1_101")])

    exit_code = diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []


def test_composite_match_is_classified_correctly(capsys):
    lessons = [make_lesson(1, "2026-09-13", [make_participant(101, "Alice")])]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[_monday_item("m1", unique_id="1_101")])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "composite_key=1_101 composite_exists=True legacy_exists=False => MATCHED" in out
    assert "Composite-key matches: 1" in out
    assert "Legacy-key matches: 0" in out
    assert "Total exact matches: 1" in out
    assert "Unmatched sessions: 0" in out


def test_legacy_match_is_classified_correctly(capsys):
    lessons = [make_lesson(1, "2026-09-13", [make_participant(101, "Alice")])]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[_monday_item("m1", unique_id="1")])  # legacy: lesson-only

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "composite_key=1_101 composite_exists=False legacy_exists=True => MATCHED" in out
    assert "Composite-key matches: 0" in out
    assert "Legacy-key matches: 1" in out
    assert "Total exact matches: 1" in out
    assert "Unmatched sessions: 0" in out


def test_unmatched_session_with_likely_date_and_student_id_historical_match(capsys):
    lessons = [make_lesson(5, "2026-09-13", [make_participant(999, "Carol")])]
    tw = FakeTeachworksClient(lessons)
    # No matching unique key at all, but same session date + same Teachworks
    # Student ID on an existing item - a likely historical record.
    monday = FakeMondayClient(items=[
        _monday_item("old-item-1", item_name="Carol - 2026-09-13", unique_id="some-other-format",
                     session_date="2026-09-13", tw_student_id="999"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "=> UNMATCHED" in out
    assert "Unmatched with likely date+student historical Monday record: 1" in out
    assert "Unmatched with no likely historical Monday record: 0" in out
    assert "Monday item ID: old-item-1" in out
    assert "Monday item name: Carol - 2026-09-13" in out
    assert "Monday session date: 2026-09-13" in out
    assert "Monday Teachworks Student ID: 999" in out
    assert f"Monday {config.COL_UNIQUE_ID} value: some-other-format" in out


def test_unmatched_session_with_likely_match_via_blank_student_id_and_name(capsys):
    """Criterion B: same date, Monday's own Teachworks Student ID column is
    blank, but the student name matches."""
    lessons = [make_lesson(6, "2026-09-13", [make_participant(888, "Dana Lee")])]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[
        _monday_item("old-item-2", unique_id="9999", session_date="2026-09-13",
                     tw_student_id="", student_name="Dana Lee"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "Monday item ID: old-item-2" in out
    assert "Unmatched with likely date+student historical Monday record: 1" in out


def test_truly_unmatched_session_reports_no_likely_match(capsys):
    lessons = [make_lesson(7, "2026-09-13", [make_participant(111, "Dana")])]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[
        _monday_item("unrelated-item", unique_id="9999_8888", session_date="2020-01-01", tw_student_id="555"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "No likely historical Monday record found." in out
    assert "Unmatched with likely date+student historical Monday record: 0" in out
    assert "Unmatched with no likely historical Monday record: 1" in out
    assert "Unmatched Teachworks lesson IDs: [7]" in out


def test_mixed_matches_and_deterministic_ordering(capsys):
    """Composite match, legacy match, and unmatched together - rows must be
    sorted by lesson ID then student ID, not fetch/iteration order."""
    lessons = [
        make_lesson(300, "2026-09-13", [make_participant(3, "Zed")]),      # unmatched
        make_lesson(100, "2026-09-13", [make_participant(1, "Amy")]),      # legacy match
        make_lesson(200, "2026-09-13", [make_participant(2, "Ben")]),      # composite match
    ]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[
        _monday_item("m-legacy", unique_id="100"),
        _monday_item("m-composite", unique_id="200_2"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    rows_section = out.split("PER-SESSION DIAGNOSTIC ROWS")[1].split("SECONDARY INVESTIGATION")[0]
    lesson_order = [line.split("lesson_id=")[1].split(" ")[0] for line in rows_section.splitlines() if "lesson_id=" in line]
    assert lesson_order == ["100", "200", "300"]

    assert "Teachworks participant sessions: 3" in out
    assert "Composite-key matches: 1" in out
    assert "Legacy-key matches: 1" in out
    assert "Total exact matches: 2" in out
    assert "Unmatched sessions: 1" in out
