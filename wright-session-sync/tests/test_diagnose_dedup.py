"""Tests for the read-only --diagnose-dedup investigation: it must never
write to Monday, and must correctly separate exact unique-key matches from
sessions with no match, plus a best-effort (diagnostic-only) secondary match."""

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


def _monday_item(item_id, unique_id="", session_date="", tw_student_id="", tutor="", service=""):
    return {
        "item_id": item_id,
        "columns": {
            config.COL_UNIQUE_ID: unique_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
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


def test_diagnose_dedup_reports_exact_and_no_match_counts(capsys):
    lessons = [
        make_lesson(1, "2026-09-13", [make_participant(101, "Alice")]),  # will match
        make_lesson(2, "2026-09-13", [make_participant(102, "Bob")]),    # will NOT match
    ]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[_monday_item("m1", unique_id="1_101")])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "Teachworks participant sessions: 2" in out
    assert "Exact unique-key matches: 1" in out
    assert "No unique-key match: 1" in out
    # the unmatched session's identifying info is printed
    assert "Teachworks lesson ID: 2" in out
    assert "Expected new unique key: 2_102" in out


def test_diagnose_dedup_finds_likely_secondary_match_on_date_and_student_id(capsys):
    lessons = [make_lesson(5, "2026-09-13", [make_participant(999, "Carol")])]
    tw = FakeTeachworksClient(lessons)
    # This existing item has NO matching unique key, but the same session
    # date and the same Teachworks Student ID - a likely candidate for what
    # the old Zap stored under a different key format.
    monday = FakeMondayClient(items=[
        _monday_item("old-item-1", unique_id="some-other-format", session_date="2026-09-13", tw_student_id="999"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "No unique-key match: 1" in out
    assert "Existing Monday item ID: old-item-1" in out
    assert "Existing Monday unique-ID value: some-other-format" in out
    assert "Session date: 2026-09-13" in out


def test_diagnose_dedup_reports_no_likely_match_when_nothing_lines_up(capsys):
    lessons = [make_lesson(7, "2026-09-13", [make_participant(111, "Dana")])]
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[
        _monday_item("unrelated-item", unique_id="9999_8888", session_date="2020-01-01", tw_student_id="555"),
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "No likely match found" in out


def test_diagnose_dedup_reports_sample_of_nonblank_existing_unique_ids(capsys):
    lessons = []
    tw = FakeTeachworksClient(lessons)
    monday = FakeMondayClient(items=[
        _monday_item("m1", unique_id="111_1"),
        _monday_item("m2", unique_id="222_2"),
        _monday_item("m3", unique_id=""),  # blank - must not appear in the sample
    ])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "111_1" in out
    assert "222_2" in out


def test_diagnose_dedup_never_used_for_production_decisions_label_present(capsys):
    """The secondary-match section must be clearly labeled diagnostic-only."""
    tw = FakeTeachworksClient([])
    monday = FakeMondayClient(items=[])

    diagnose_dedup(tw, monday, "2026-09-13", "2026-09-13")

    out = capsys.readouterr().out
    assert "NEVER used for production deduplication" in out
