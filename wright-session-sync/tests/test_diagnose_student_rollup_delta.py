"""Tests for the read-only baseline+delta validation report
(--diagnose-student-rollup-delta): filters students by an exact current
Session Data Last Synced match, counts Session Log records strictly after
that date, and proposes new values without writing anything."""

import config
from sync import diagnose_student_rollup_delta
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called by --diagnose-student-rollup-delta")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called by --diagnose-student-rollup-delta")


def _session_log_row(item_id, tw_student_id, session_date, tutor):
    return {
        "item_id": item_id,
        "columns": {
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
        },
    }


def _student_row(item_id, tw_student_id, item_name="", first_session="", session_count="", last_session="", tutor="", last_synced=""):
    return {
        "item_id": item_id,
        "item_name": item_name,
        "columns": {
            config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_student_id,
            config.STUDENT_COL_FIRST_SESSION_DATE: first_session,
            config.STUDENT_COL_SESSION_COUNT: session_count,
            config.STUDENT_COL_LAST_SESSION_DATE: last_session,
            config.STUDENT_COL_TUTOR: tutor,
            config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: last_synced,
        },
    }


def test_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        student_items=[_student_row("m1", "111", "Alice", "2026-01-01", "5", "2026-09-10", "Jane", "2026-09-10")],
        items=[_session_log_row("s1", "111", "2026-09-12", "Jane")],
    )

    exit_code = diagnose_student_rollup_delta(monday, "2026-09-10")

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []


def test_only_students_with_exact_baseline_last_synced_are_included(capsys):
    monday = FakeMondayClient(
        student_items=[
            _student_row("m1", "111", "Alice", "2026-01-01", "5", "2026-09-10", "Jane", "2026-09-10"),
            _student_row("m2", "222", "Bob", "2026-01-01", "3", "2026-09-05", "Tom", "2026-09-05"),  # different sync date
        ],
        items=[
            _session_log_row("s1", "111", "2026-09-12", "Jane"),
            _session_log_row("s2", "222", "2026-09-12", "Tom"),
        ],
    )

    diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    assert "1 student(s) with Session Data Last Synced == 2026-09-10" in out
    assert "Alice" in out
    assert "Bob" not in out


def test_counts_only_sessions_strictly_after_baseline_date(capsys):
    monday = FakeMondayClient(
        student_items=[_student_row("m1", "111", "Alice", "2026-01-01", "5", "2026-09-10", "Jane", "2026-09-10")],
        items=[
            _session_log_row("s0", "111", "2026-09-09", "Jane"),  # before baseline - not counted
            _session_log_row("s1", "111", "2026-09-10", "Jane"),  # ON baseline - not counted ("after", not "on or after")
            _session_log_row("s2", "111", "2026-09-11", "Jane"),  # after - counted
            _session_log_row("s3", "111", "2026-09-13", "Ben"),   # after - counted
        ],
    )

    diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    assert "Alice | 5 | 2 | 7 | 2026-09-10 | 2026-09-13 | Jane | Ben" in out


def test_proposed_new_count_is_current_plus_delta(capsys):
    monday = FakeMondayClient(
        student_items=[_student_row("m1", "111", "Alice", "2026-01-01", "10", "2026-09-10", "Jane", "2026-09-10")],
        items=[
            _session_log_row("s1", "111", "2026-09-11", "Jane"),
            _session_log_row("s2", "111", "2026-09-12", "Jane"),
            _session_log_row("s3", "111", "2026-09-13", "Jane"),
        ],
    )

    diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    assert "Alice | 10 | 3 | 13 |" in out


def test_no_new_sessions_leaves_last_session_and_tutor_unchanged(capsys):
    monday = FakeMondayClient(
        student_items=[_student_row("m1", "111", "Alice", "2026-01-01", "5", "2026-09-10", "Jane", "2026-09-10")],
        items=[],  # no Session Log rows at all for this student
    )

    diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    assert "Alice | 5 | 0 | 5 | 2026-09-10 | 2026-09-10 | Jane | Jane" in out


def test_new_tutor_only_updates_when_delta_advances_last_session(capsys):
    """A delta session exists but is earlier than the current last session
    date is impossible by construction (delta is always > baseline, and
    current last session should be <= baseline in a consistent baseline),
    but guard the logic anyway: if current_last_session already exceeds the
    delta's max date for some reason, tutor must not be overwritten."""
    monday = FakeMondayClient(
        student_items=[_student_row("m1", "111", "Alice", "2026-01-01", "5", "2026-09-20", "Jane", "2026-09-10")],
        items=[_session_log_row("s1", "111", "2026-09-11", "Ben")],  # after baseline, but before current last session
    )

    diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    # count still increases by the delta, but last session / tutor stay as current
    assert "Alice | 5 | 1 | 6 | 2026-09-20 | 2026-09-20 | Jane | Jane" in out


def test_no_matching_students_reports_zero_and_makes_no_further_calls(capsys):
    monday = WriteGuardedMondayClient(
        student_items=[_student_row("m1", "111", "Alice", last_synced="2026-09-05")],
        items=[_session_log_row("s1", "111", "2026-09-12", "Jane")],
    )

    exit_code = diagnose_student_rollup_delta(monday, "2026-09-10")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 student(s) with Session Data Last Synced == 2026-09-10" in out
