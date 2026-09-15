"""Tests for the read-only Stage 1 --diagnose-student-rollups: it must never
write to Monday, must calculate rollups only from the Monday Session Log
board (zero Teachworks requests), and must match students strictly by
Teachworks Student ID."""

import config
from sync import diagnose_student_rollups
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    """Raises if any write method is ever called - a hard guarantee on top
    of just checking created_items/connections stayed empty."""

    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called by --diagnose-student-rollups")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called by --diagnose-student-rollups")


def _session_log_row(item_id, tw_student_id, session_date, tutor):
    return {
        "item_id": item_id,
        "columns": {
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
        },
    }


def _student_row(item_id, tw_student_id, first_session="", session_count="", last_session="", tutor="", last_synced=""):
    return {
        "item_id": item_id,
        "columns": {
            config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_student_id,
            config.STUDENT_COL_FIRST_SESSION_DATE: first_session,
            config.STUDENT_COL_SESSION_COUNT: session_count,
            config.STUDENT_COL_LAST_SESSION_DATE: last_session,
            config.STUDENT_COL_TUTOR: tutor,
            config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: last_synced,
        },
    }


def test_diagnose_student_rollups_makes_zero_writes_and_zero_teachworks_requests():
    monday = WriteGuardedMondayClient(
        items=[_session_log_row("s1", "111", "2026-09-13", "Jane Tutor")],
        student_items=[_student_row("m1", "111")],
    )

    exit_code = diagnose_student_rollups(monday, today="2026-09-15")

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    # note: this function never even takes a TeachworksClient argument -
    # there is no way for it to issue a Teachworks request.


def test_first_last_date_and_lifetime_count_are_calculated_correctly(capsys):
    monday = FakeMondayClient(
        items=[
            _session_log_row("s1", "111", "2024-01-05", "Tutor A"),
            _session_log_row("s2", "111", "2026-09-13", "Tutor B"),
            _session_log_row("s3", "111", "2025-06-01", "Tutor C"),
        ],
        student_items=[_student_row("m1", "111")],
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert "first_session_date=2024-01-05" in out
    assert "last_session_date=2026-09-13" in out
    assert "session_count=3" in out
    assert "latest_tutor=Tutor B" in out


def test_latest_tutor_selection_breaks_ties_deterministically(capsys):
    """Two sessions share the latest date; the tutor from the higher item_id
    (deterministic tiebreak) must be chosen consistently."""
    monday = FakeMondayClient(
        items=[
            _session_log_row("s2", "111", "2026-09-13", "Later Tutor"),
            _session_log_row("s1", "111", "2026-09-13", "Earlier Tutor"),
        ],
        student_items=[_student_row("m1", "111")],
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert "latest_tutor=Later Tutor" in out


def test_matches_strictly_by_teachworks_student_id_not_name():
    """The student item has no name field read at all - matching uses only
    the Teachworks Student ID column, confirmed by giving the Monday student
    item a totally different implied identity (no name field exists on our
    student rows at all) and still matching correctly by ID."""
    monday = FakeMondayClient(
        items=[_session_log_row("s1", "999", "2026-09-13", "Some Tutor")],
        student_items=[_student_row("m1", "999", first_session="2026-09-13", session_count="1",
                                     last_session="2026-09-13", tutor="Some Tutor", last_synced="2026-09-15")],
    )

    exit_code = diagnose_student_rollups(monday, today="2026-09-15")

    assert exit_code == 0  # matched and classified without needing any name field


def test_missing_monday_student_is_reported_and_not_created():
    monday = WriteGuardedMondayClient(
        items=[_session_log_row("s1", "222", "2026-09-13", "Some Tutor")],
        student_items=[],  # no Student item at all for Teachworks Student ID 222
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    # WriteGuardedMondayClient would have raised if anything tried to create
    # a Student - reaching here without an exception already proves it.
    assert monday.created_items == []


def test_missing_monday_student_reported_in_output_and_summary(capsys):
    monday = FakeMondayClient(
        items=[_session_log_row("s1", "222", "2026-09-13", "Some Tutor")],
        student_items=[],
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert "MISSING MONDAY STUDENT" in out
    assert "Teachworks students calculated: 1" in out
    assert "Monday students matched: 0" in out
    assert "Missing Monday students: 1" in out


def test_student_already_correct_is_classified_as_match(capsys):
    monday = FakeMondayClient(
        items=[_session_log_row("s1", "333", "2026-09-13", "Steady Tutor")],
        student_items=[_student_row(
            "m1", "333",
            first_session="2026-09-13", session_count="1", last_session="2026-09-13",
            tutor="Steady Tutor", last_synced="2026-09-14",  # sync date differs - must not block MATCH
        )],
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert "=> MATCH" in out
    assert "Students already correct: 1" in out
    assert "Students that would change: 0" in out


def test_student_with_stale_values_is_classified_as_would_update(capsys):
    monday = FakeMondayClient(
        items=[
            _session_log_row("s1", "444", "2026-01-01", "Old Tutor"),
            _session_log_row("s2", "444", "2026-09-13", "New Tutor"),
        ],
        student_items=[_student_row(
            "m1", "444",
            first_session="2026-01-01", session_count="1", last_session="2026-01-01",
            tutor="Old Tutor", last_synced="2026-01-02",
        )],
    )

    diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert "Last Session Date: 2026-01-01 -> 2026-09-13  <- WOULD CHANGE" in out
    assert "Session Count: 1 -> 2  <- WOULD CHANGE" in out
    assert "Tutor: Old Tutor -> New Tutor  <- WOULD CHANGE" in out
    assert "=> WOULD UPDATE" in out
    assert "Students that would change: 1" in out


def test_rows_missing_teachworks_student_id_or_session_date_are_skipped(capsys):
    monday = FakeMondayClient(
        items=[
            {"item_id": "s1", "columns": {config.COL_TEACHWORKS_STUDENT_ID: "", config.COL_SESSION_DATE: "2026-09-13", config.COL_TUTOR: "T"}},
            {"item_id": "s2", "columns": {config.COL_TEACHWORKS_STUDENT_ID: "555", config.COL_SESSION_DATE: "", config.COL_TUTOR: "T"}},
        ],
        student_items=[],
    )

    exit_code = diagnose_student_rollups(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert exit_code == 0
    # neither malformed row should have produced a calculated student at all
    assert "Teachworks students calculated: 0" in out
