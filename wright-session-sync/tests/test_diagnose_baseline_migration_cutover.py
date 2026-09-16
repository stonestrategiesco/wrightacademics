"""Tests for the read-only migration cutover preview: one frozen snapshot
of Students + Session Log, a report of exactly what a real migration would
do, and an in-memory post-migration simulation proving post-baseline
identities start at zero. Must never write to Monday."""

import config
from sync import diagnose_baseline_migration_cutover
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


def _session_row(item_id, unique_key, tw_student_id, session_date="2026-09-01", tutor="Jane Tutor", pre_baseline=""):
    return {
        "item_id": item_id,
        "item_name": f"Session {item_id}",
        "columns": {
            config.COL_UNIQUE_ID: unique_key,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
            config.COL_PRE_BASELINE: pre_baseline,
        },
    }


def test_refuses_to_run_when_column_ids_are_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", None)
    monkeypatch.setattr(config, "COL_PRE_BASELINE", None)
    monday = WriteGuardedMondayClient()

    exit_code = diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out


def test_makes_zero_monday_writes(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = WriteGuardedMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[_session_row("i1", "100_111", "111")],
    )

    exit_code = diagnose_baseline_migration_cutover(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_students_section(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", session_count="32"),
            _student("s2", "Bob", "222", session_count=""),
            _student("s3", "Carla", "333", session_count="0"),
            _student("s4", "Dana", "444", session_count="10", baseline="10"),
        ],
        items=[],
    )

    diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert "Students evaluated: 4" in out
    assert "Students that would receive Historical Session Baseline: 3" in out
    assert "Sum of current Session Count values that would be frozen into Historical Session Baseline: 32" in out
    assert "Students with blank/zero Session Count: 2" in out


def test_session_log_cutover_section_counts(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_1", "1"),
            _session_row("i2", "100_1", "1"),  # exact duplicate
            _session_row("i3", "101", "2"),
            _session_row("i4", "101_2", "2"),  # cross-format collision with i3
            _session_row("i5", "102_3", "3", pre_baseline="v"),  # already marked
        ],
    )

    diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert "Total Session Log rows in the frozen snapshot: 5" in out
    assert "Rows that would be marked Pre-Baseline = Yes: 4" in out
    assert "already marked Pre-Baseline = Yes: 1" in out
    # Identities: (100,1) once [i1+i2 collapse], (101,2) once [i3+i4 collapse], (102,3) once = 3 distinct
    assert "Distinct canonical session identities represented: 3" in out
    assert "Duplicate physical rows collapsed by canonical identity: 2" in out
    assert "Exact-key duplicate groups: 1" in out
    assert "Cross-format bare/composite duplicate groups: 1" in out
    assert "Rows missing canonical identity information: 0" in out


def test_rows_missing_canonical_identity_are_counted(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "", "1"),
            _session_row("i2", "200", ""),
        ],
    )

    diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert "Rows missing canonical identity information: 2" in out


def test_post_migration_validation_reports_zero_and_confirms(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_1", "1"),
            _session_row("i2", "100_1", "1"),
            _session_row("i3", "101", "2"),
            _session_row("i4", "101_2", "2"),
        ],
    )

    diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert "Post-baseline session identities immediately after migration: 0" in out
    assert "CONFIRMED: no existing historical Session Log row can contribute again after cutover." in out
    assert "WARNING" not in out


def test_proposed_migration_write_counts(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", session_count="32"),
            _student("s2", "Bob", "222", session_count="5", baseline="5"),  # already has a baseline
        ],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "101_222", "222", pre_baseline="v"),  # already marked
        ],
    )

    diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert "Student baseline writes: 1" in out
    assert "Session Log Pre-Baseline writes: 1" in out
    assert "Total Monday items that would be modified: 2" in out


def test_handles_empty_boards(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")
    monday = WriteGuardedMondayClient(student_items=[], items=[])

    exit_code = diagnose_baseline_migration_cutover(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Students evaluated: 0" in out
    assert "Total Session Log rows in the frozen snapshot: 0" in out
    assert "Post-baseline session identities immediately after migration: 0" in out


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

    exit_code = sync.main(["--diagnose-baseline-migration-cutover"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out
