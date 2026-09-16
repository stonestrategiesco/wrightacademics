"""Tests for the one-time baseline+SET migration APPLY
(apply_baseline_migration / run_baseline_migration_apply). This performs
REAL writes (unlike every other diagnostic in this suite), so these tests
focus specifically on: writing ONLY the two approved columns and nothing
else, never touching Session Count or any other existing field, skipping
already-migrated items (safe to re-run), continuing past individual write
failures while collecting them, and the required exit-code/summary
behavior."""

import config
from sync import apply_baseline_migration, run_baseline_migration_apply
from tests.fakes import FakeMondayClient


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


def _configure_columns(monkeypatch):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", "numeric_new1")
    monkeypatch.setattr(config, "COL_PRE_BASELINE", "checkbox_new1")


def test_refuses_to_run_when_column_ids_are_unconfigured(monkeypatch, capsys):
    monkeypatch.setattr(config, "STUDENT_COL_HISTORICAL_BASELINE", None)
    monkeypatch.setattr(config, "COL_PRE_BASELINE", None)
    monday = FakeMondayClient()

    exit_code = run_baseline_migration_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out
    assert monday.student_updates == []


def test_sets_baseline_to_current_session_count(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[],
    )

    outcome = apply_baseline_migration(monday)

    assert outcome["student_writes_succeeded"] == 1
    assert monday.student_updates == [{
        "board_id": config.MONDAY_STUDENTS_BOARD_ID,
        "item_id": "s1",
        "column_values": {config.STUDENT_COL_HISTORICAL_BASELINE: 32},
    }]


def test_blank_session_count_becomes_baseline_zero(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="")],
        items=[],
    )

    apply_baseline_migration(monday)

    assert monday.student_updates[0]["column_values"] == {config.STUDENT_COL_HISTORICAL_BASELINE: 0}


def test_student_baseline_write_touches_only_that_column(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[],
    )

    apply_baseline_migration(monday)

    written_columns = monday.student_updates[0]["column_values"]
    assert set(written_columns.keys()) == {config.STUDENT_COL_HISTORICAL_BASELINE}
    assert config.STUDENT_COL_SESSION_COUNT not in written_columns
    assert config.STUDENT_COL_LAST_SESSION_DATE not in written_columns
    assert config.STUDENT_COL_TUTOR not in written_columns
    assert config.STUDENT_COL_SESSION_DATA_LAST_SYNCED not in written_columns
    assert config.STUDENT_COL_FIRST_SESSION_DATE not in written_columns


def test_already_migrated_student_is_skipped_no_write(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="99", baseline="32")],
        items=[],
    )

    outcome = apply_baseline_migration(monday)

    assert outcome["students_to_baseline"] == 0
    assert outcome["student_writes_succeeded"] == 0
    assert monday.student_updates == []


def test_marks_pre_baseline_yes_on_unmarked_session_log_row(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[],
        items=[_session_row("i1", "100_1", "1")],
    )

    outcome = apply_baseline_migration(monday)

    assert outcome["session_writes_succeeded"] == 1
    assert monday.student_updates == [{
        "board_id": config.MONDAY_SESSIONS_BOARD_ID,
        "item_id": "i1",
        "column_values": {config.COL_PRE_BASELINE: {"checked": "true"}},
    }]


def test_pre_baseline_write_touches_only_that_column(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[],
        items=[_session_row("i1", "100_1", "1")],
    )

    apply_baseline_migration(monday)

    written_columns = monday.student_updates[0]["column_values"]
    assert set(written_columns.keys()) == {config.COL_PRE_BASELINE}
    assert config.COL_UNIQUE_ID not in written_columns
    assert config.COL_TEACHWORKS_STUDENT_ID not in written_columns
    assert config.COL_SESSION_DATE not in written_columns
    assert config.COL_TUTOR not in written_columns


def test_already_marked_session_log_row_is_skipped_no_write(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[],
        items=[_session_row("i1", "100_1", "1", pre_baseline="v")],
    )

    outcome = apply_baseline_migration(monday)

    assert outcome["session_rows_to_mark"] == 0
    assert outcome["session_writes_succeeded"] == 0
    assert monday.student_updates == []


def test_duplicate_session_log_rows_are_all_marked_not_deleted_or_merged(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_1", "1"),
            _session_row("i2", "100_1", "1"),  # exact duplicate
            _session_row("i3", "101", "2"),
            _session_row("i4", "101_2", "2"),  # cross-format duplicate
        ],
    )

    outcome = apply_baseline_migration(monday)

    # All 4 physical rows get marked - none deleted, archived, or collapsed.
    assert outcome["session_rows_to_mark"] == 4
    assert outcome["session_writes_succeeded"] == 4
    marked_item_ids = {u["item_id"] for u in monday.student_updates}
    assert marked_item_ids == {"i1", "i2", "i3", "i4"}


def test_continues_past_individual_write_failure_and_collects_it(monkeypatch):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", session_count="32"),
            _student("s2", "Bob", "222", session_count="5"),
        ],
        items=[],
    )

    def fail_for_s1(item_id, column_values):
        if item_id == "s1":
            raise RuntimeError("simulated Monday API failure")

    monday.update_student_side_effect = fail_for_s1

    outcome = apply_baseline_migration(monday)

    assert outcome["student_writes_succeeded"] == 1
    assert len(outcome["student_writes_failed"]) == 1
    assert outcome["student_writes_failed"][0]["item_id"] == "s1"
    # The other student's write still went through despite s1's failure.
    assert any(u["item_id"] == "s2" for u in monday.student_updates)


def test_exit_code_zero_when_all_writes_succeed(monkeypatch, capsys):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[_session_row("i1", "100_1", "1")],
    )

    exit_code = run_baseline_migration_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL RESULT: SUCCESS" in out


def test_exit_code_nonzero_when_any_write_fails(monkeypatch, capsys):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[],
    )

    def always_fail(item_id, column_values):
        raise RuntimeError("simulated failure")

    monday.update_student_side_effect = always_fail

    exit_code = run_baseline_migration_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL RESULT: FAILED" in out


def test_summary_prints_all_required_fields(monkeypatch, capsys):
    _configure_columns(monkeypatch)
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", session_count="32")],
        items=[_session_row("i1", "100_1", "1")],
    )

    run_baseline_migration_apply(monday)

    out = capsys.readouterr().out
    assert "Student baseline writes succeeded: 1" in out
    assert "Student baseline writes failed: 0" in out
    assert "Session Log Pre-Baseline writes succeeded: 1" in out
    assert "Session Log Pre-Baseline writes failed: 0" in out
    assert "Total successful writes: 2" in out
    assert "Total failed writes: 0" in out
    assert "FINAL RESULT" in out


def test_cli_wires_the_new_flag_and_refuses_without_column_ids(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")
    monkeypatch.setattr(config_module, "STUDENT_COL_HISTORICAL_BASELINE", None)
    monkeypatch.setattr(config_module, "COL_PRE_BASELINE", None)

    monday = FakeMondayClient()
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--apply-baseline-migration"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires both new column IDs to be configured first" in out
    assert monday.student_updates == []
