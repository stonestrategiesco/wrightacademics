"""Tests for the Stage 1 read-only schema diagnostic that discovers the two
new column IDs the baseline+SET migration needs (Historical Session Baseline
on Students, Pre-Baseline on Session Log). It must never write to Monday,
and must never guess a column ID that isn't actually present."""

import config
from sync import diagnose_baseline_migration_columns
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called")


def _fake_client(student_columns, session_columns):
    """board_columns is shared per-client in FakeMondayClient, so route by
    board_id via a tiny subclass rather than changing the shared fake."""

    class _Client(WriteGuardedMondayClient):
        def get_board_columns(self, board_id):
            if board_id == config.MONDAY_STUDENTS_BOARD_ID:
                return list(student_columns)
            if board_id == config.MONDAY_SESSIONS_BOARD_ID:
                return list(session_columns)
            raise AssertionError(f"unexpected board_id {board_id}")

    return _Client()


def test_makes_zero_monday_writes():
    monday = _fake_client(
        student_columns=[{"id": "numeric_new1", "title": "Historical Session Baseline", "type": "numbers"}],
        session_columns=[{"id": "checkbox_new1", "title": "Pre-Baseline", "type": "checkbox"}],
    )

    exit_code = diagnose_baseline_migration_columns(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_reports_found_column_ids_when_present(capsys):
    monday = _fake_client(
        student_columns=[{"id": "numeric_new1", "title": "Historical Session Baseline", "type": "numbers"}],
        session_columns=[{"id": "checkbox_new1", "title": "Pre-Baseline", "type": "checkbox"}],
    )

    exit_code = diagnose_baseline_migration_columns(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "FOUND -> id=numeric_new1  type=numbers" in out
    assert "FOUND -> id=checkbox_new1  type=checkbox" in out
    assert "Both columns found." in out


def test_reports_not_found_with_creation_instructions_when_missing(capsys):
    monday = _fake_client(student_columns=[], session_columns=[])

    exit_code = diagnose_baseline_migration_columns(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "NOT FOUND. Create it manually in Monday first:" in out
    assert "Board: Students" in out
    assert "Column name: Historical Session Baseline" in out
    assert "Column type: Numbers" in out
    assert "Board: Session Log" in out
    assert "Column name: Pre-Baseline" in out
    assert "Column type: Checkbox" in out
    assert "One or more columns are missing." in out


def test_reports_partial_match_correctly(capsys):
    monday = _fake_client(
        student_columns=[{"id": "numeric_new1", "title": "Historical Session Baseline", "type": "numbers"}],
        session_columns=[],
    )

    diagnose_baseline_migration_columns(monday)

    out = capsys.readouterr().out
    assert "FOUND -> id=numeric_new1  type=numbers" in out
    assert "NOT FOUND. Create it manually in Monday first:" in out
    assert "One or more columns are missing." in out


def test_cli_wires_the_new_flag(monkeypatch):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = _fake_client(student_columns=[], session_columns=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--diagnose-baseline-migration-columns"])

    assert exit_code == 0
    assert monday.student_updates == []
