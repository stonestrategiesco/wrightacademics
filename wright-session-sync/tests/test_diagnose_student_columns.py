"""Tests for the read-only --diagnose-student-columns discovery command:
it must never write to Monday, and must accurately report which of the
columns we're looking for actually exist on the real Students board."""

from sync import diagnose_student_columns
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    """Raises if any write method is ever called - a hard guarantee on top
    of just checking created_items/connections stayed empty."""

    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called by --diagnose-student-columns")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called by --diagnose-student-columns")


SAMPLE_COLUMNS = [
    {"id": "text_mm3gj3hy", "title": "Teachworks Student ID", "type": "text"},
    {"id": "date_abc111", "title": "First Session", "type": "date"},
    {"id": "numeric_abc222", "title": "Session Count", "type": "numeric"},
    {"id": "date_abc333", "title": "Last session Date", "type": "date"},
    {"id": "text_abc444", "title": "Tutor", "type": "text"},
    {"id": "board_relation_abc555", "title": "Some Other Column", "type": "board_relation"},
]


def test_diagnose_student_columns_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(board_columns=SAMPLE_COLUMNS)

    exit_code = diagnose_student_columns(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []


def test_diagnose_student_columns_prints_every_column(capsys):
    monday = FakeMondayClient(board_columns=SAMPLE_COLUMNS)

    diagnose_student_columns(monday)

    out = capsys.readouterr().out
    assert "6 column(s) found" in out
    for col in SAMPLE_COLUMNS:
        assert col["id"] in out
        assert col["title"] in out
        assert col["type"] in out


def test_diagnose_student_columns_flags_found_columns_of_interest(capsys):
    monday = FakeMondayClient(board_columns=SAMPLE_COLUMNS)

    diagnose_student_columns(monday)

    out = capsys.readouterr().out
    assert "FOUND     'Teachworks Student ID'" in out
    assert "id=text_mm3gj3hy" in out
    assert "FOUND     'First Session'" in out
    assert "id=date_abc111" in out
    assert "FOUND     'Session Count'" in out
    assert "FOUND     'Last session Date'" in out
    assert "FOUND     'Tutor'" in out


def test_diagnose_student_columns_flags_missing_columns_of_interest(capsys):
    monday = FakeMondayClient(board_columns=SAMPLE_COLUMNS)  # no "Milestones" or "Session Data Last Updated"

    diagnose_student_columns(monday)

    out = capsys.readouterr().out
    assert "NOT FOUND 'Session Data Last Updated'" in out
    assert "NOT FOUND 'Milestones'" in out


def test_diagnose_student_columns_handles_empty_board(capsys):
    monday = FakeMondayClient(board_columns=[])

    exit_code = diagnose_student_columns(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "0 column(s) found" in out
    assert "NOT FOUND" in out  # none of the columns of interest exist
    assert monday.created_items == []
    assert monday.connections == []
