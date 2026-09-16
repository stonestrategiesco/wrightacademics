"""Tests for the Stage 2 read-only migration dry run for the baseline+SET
architecture. It must never write to Monday, must refuse to run while the
two new column IDs are unconfigured, and must report exactly the STUDENTS
and SESSION LOG sections requested (including duplicate unique-key
detection)."""

import config
from sync import diagnose_baseline_migration
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called")


def _student(item_id, name, tw_id, session_count="", baseline=""):
    return {
        "item_id": item_id,
        "item_name": name,
        "columns": {
            config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_id,
            config.STUDENT_COL_SESSION_COUNT: session_count,
            config.STUDENT_COL_HISTORICAL_BASELINE: baseline,
        },
    }


def _session_row(item_id, unique_key, tw_student_id, pre_baseline=""):
    return {
        "item_id": item_id,
        "item_name": f"Session {item_id}",
        "columns": {
            config.COL_UNIQUE_ID: unique_key,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_PRE_BASELINE: pre_baseline,
        },
    }


def test_refuses_to_run_when_column_ids_are_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", None)
    monkeypatch.setattr(config, "COL_PRE_BASELINE", None)
    monday = WriteGuardedMondayClient()

    exit_code = diagnose_baseline_migration(monday)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out


def test_refuses_to_run_when_only_one_column_id_is_configured(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", None)
    monday = WriteGuardedMondayClient()

    exit_code = diagnose_baseline_migration(monday)

    assert exit_code == 1


def test_makes_zero_monday_writes(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = WriteGuardedMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[_session_row("i1", "100_111", "111")],
    )

    exit_code = diagnose_baseline_migration(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_students_section_counts_and_sample(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", session_count="32"),
            _student("s2", "Bob", "222", session_count=""),
            _student("s3", "Carla", "333", session_count="10", baseline="10"),
        ],
        items=[],
    )

    diagnose_baseline_migration(monday)

    out = capsys.readouterr().out
    assert "Students evaluated: 3" in out
    assert "Students with current Session Count: 2" in out
    assert "Students with blank/null Session Count: 1" in out
    assert "Students whose Historical Session Baseline is already populated: 1" in out
    assert "Students that WOULD receive a baseline: 2" in out
    assert "Alice" in out and "32" in out
    assert "Bob" in out
    assert "Carla" not in out


def test_session_log_section_counts(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "101_111", "111", pre_baseline="v"),
            _session_row("i3", "", "222"),
            _session_row("i4", "102_333", ""),
        ],
    )

    diagnose_baseline_migration(monday)

    out = capsys.readouterr().out
    assert "Session Log rows evaluated: 4" in out
    assert "Rows already marked Pre-Baseline: 1" in out
    assert "Rows that WOULD be marked Pre-Baseline: 3" in out
    assert "Rows missing Teachworks Student ID: 1" in out
    assert "Rows missing unique_key: 1" in out


def test_detects_duplicate_unique_keys(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "100_111", "111"),
            _session_row("i3", "101_222", "222"),
        ],
    )

    diagnose_baseline_migration(monday)

    out = capsys.readouterr().out
    assert "Duplicate unique identities detected: 1" in out
    assert "unique_key=100_111" in out
    assert "i1" in out and "i2" in out


def test_handles_empty_boards(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = WriteGuardedMondayClient(student_items=[], items=[])

    exit_code = diagnose_baseline_migration(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Students evaluated: 0" in out
    assert "Session Log rows evaluated: 0" in out


def test_cli_wires_the_new_flag_and_refuses_without_column_ids(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")
    monkeypatch.setattr(config_module, "STUDENT_COL_HISTORICAL_BASELINE", None)
    monkeypatch.setattr(config_module, "COL_PRE_BASELINE", None)

    monday = WriteGuardedMondayClient()
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--diagnose-baseline-migration"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out
