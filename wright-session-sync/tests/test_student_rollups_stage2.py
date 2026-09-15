"""Tests for Stage 2: production-ready student rollup calculation
(compute_student_rollup_updates / run_student_rollup_dry_run). Covers
checkpoint boundaries, multi-session/latest-tutor selection, zero-session
students, missing students, blank checkpoints, duplicate Session Log
rows/unique IDs, and proof that the dry run makes zero Monday writes."""

import config
from sync import compute_student_rollup_updates, run_student_rollup_dry_run
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called by the dry run")


def _session_row(item_id, tw_student_id, session_date, tutor, unique_id=None):
    return {
        "item_id": item_id,
        "columns": {
            config.COL_UNIQUE_ID: unique_id if unique_id is not None else f"uid-{item_id}",
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
        },
    }


def _student_row(item_id, tw_student_id="", item_name="", first_session="2020-01-01",
                  session_count="", last_session="", tutor="", last_synced=""):
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


# --- Checkpoint boundaries -------------------------------------------------

def test_session_exactly_on_checkpoint_is_not_new():
    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-10", "Jane")],  # ON the checkpoint
        student_items=[_student_row("m1", "111", session_count="5", last_session="2026-09-10", tutor="Jane", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    r = results[0]
    assert r["new_session_count"] == 0
    assert r["proposed_count"] == 5
    assert r["has_new_sessions"] is False


def test_session_one_day_after_checkpoint_is_new():
    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-11", "Jane")],
        student_items=[_student_row("m1", "111", session_count="5", last_session="2026-09-10", tutor="Old", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    r = results[0]
    assert r["new_session_count"] == 1
    assert r["proposed_count"] == 6
    assert r["proposed_last_session"] == "2026-09-11"
    assert r["proposed_tutor"] == "Jane"


# --- Multiple sessions after checkpoint + latest tutor selection ----------

def test_multiple_new_sessions_sum_correctly_and_latest_tutor_wins():
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-11", "Tutor A"),
            _session_row("s2", "111", "2026-09-13", "Tutor B"),
            _session_row("s3", "111", "2026-09-12", "Tutor C"),
        ],
        student_items=[_student_row("m1", "111", session_count="2", last_session="2026-09-10", tutor="Old Tutor", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    r = results[0]
    assert r["new_session_count"] == 3
    assert r["proposed_count"] == 5
    assert r["proposed_last_session"] == "2026-09-13"
    assert r["proposed_tutor"] == "Tutor B"


def test_latest_tutor_tie_on_same_date_broken_deterministically():
    monday = FakeMondayClient(
        items=[
            _session_row("s2", "111", "2026-09-13", "Later Item"),
            _session_row("s1", "111", "2026-09-13", "Earlier Item"),
        ],
        student_items=[_student_row("m1", "111", session_count="0", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    assert results[0]["proposed_tutor"] == "Later Item"


# --- Zero-session students (no new sessions -> nothing changes) ----------

def test_zero_new_sessions_leaves_everything_unchanged():
    monday = FakeMondayClient(
        items=[],  # no Session Log rows for this student at all
        student_items=[_student_row("m1", "111", session_count="7", last_session="2026-09-01", tutor="Steady", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    r = results[0]
    assert r["has_new_sessions"] is False
    assert r["proposed_count"] == 7
    assert r["proposed_last_session"] == "2026-09-01"
    assert r["proposed_tutor"] == "Steady"
    assert r["new_checkpoint"] == "2026-09-10"  # checkpoint NOT advanced


def test_checkpoint_only_advances_when_new_sessions_exist():
    with_new = compute_student_rollup_updates(
        FakeMondayClient(
            items=[_session_row("s1", "111", "2026-09-12", "Jane")],
            student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
        ),
        today="2026-09-15",
    )[0]
    without_new = compute_student_rollup_updates(
        FakeMondayClient(
            items=[],
            student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
        ),
        today="2026-09-15",
    )[0]

    assert with_new["new_checkpoint"] == "2026-09-15"  # advanced to run date
    assert without_new["new_checkpoint"] == "2026-09-10"  # unchanged


# --- Missing / unmatched students ------------------------------------------

def test_student_with_blank_teachworks_id_is_reported_as_unmatched_not_created():
    monday = WriteGuardedMondayClient(
        items=[],
        student_items=[_student_row("m1", tw_student_id="", item_name="No ID Student")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    assert len(results) == 1
    assert results[0]["matched"] is False
    assert results[0]["student_name"] == "No ID Student"
    assert results[0]["monday_item_id"] == "m1"


def test_dry_run_reports_unmatched_student_in_output_and_summary(capsys):
    monday = FakeMondayClient(
        items=[],
        student_items=[_student_row("m1", tw_student_id="", item_name="No ID Student")],
    )
    run_student_rollup_dry_run(monday, today="2026-09-15")
    out = capsys.readouterr().out
    assert "could not be matched" in out
    assert "No ID Student" in out
    assert "Students evaluated: 1" in out
    assert "Missing Monday students: 1" in out


# --- Blank checkpoint handling ---------------------------------------------

def test_blank_checkpoint_treats_all_existing_sessions_as_new():
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2024-01-01", "Old Tutor"),
            _session_row("s2", "111", "2026-09-13", "New Tutor"),
        ],
        student_items=[_student_row("m1", "111", session_count="0", last_synced="")],  # never synced
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    r = results[0]
    assert r["new_session_count"] == 2
    assert r["proposed_count"] == 2
    assert r["proposed_last_session"] == "2026-09-13"
    assert r["proposed_tutor"] == "New Tutor"
    assert r["checkpoint_used"] == "(none - all sessions treated as new)"


# --- Duplicate Session Log rows / unique IDs -------------------------------

def test_duplicate_unique_id_rows_are_counted_only_once():
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-12", "Jane", unique_id="94419922_111"),
            _session_row("s2", "111", "2026-09-12", "Jane", unique_id="94419922_111"),  # same unique key, different Monday item
        ],
        student_items=[_student_row("m1", "111", session_count="0", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    assert results[0]["new_session_count"] == 1  # not 2
    assert results[0]["proposed_count"] == 1


def test_distinct_unique_ids_on_same_date_are_both_counted():
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-12", "Jane", unique_id="94419922_111"),
            _session_row("s2", "111", "2026-09-12", "Ben", unique_id="94419923_111"),
        ],
        student_items=[_student_row("m1", "111", session_count="0", last_synced="2026-09-10")],
    )
    results = compute_student_rollup_updates(monday, today="2026-09-15")
    assert results[0]["new_session_count"] == 2


# --- Zero writes -------------------------------------------------------

def test_compute_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "Jane")],
        student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
    )
    compute_student_rollup_updates(monday, today="2026-09-15")
    assert monday.created_items == []
    assert monday.connections == []


def test_dry_run_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-11", "Jane"),
            _session_row("s2", "222", "2026-09-12", "Ben"),
        ],
        student_items=[
            _student_row("m1", "111", session_count="1", last_synced="2026-09-10"),
            _student_row("m2", "222", session_count="4", last_session="2026-09-01", tutor="Ben", last_synced="2026-09-09"),
        ],
    )
    exit_code = run_student_rollup_dry_run(monday, today="2026-09-15")
    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []


def test_dry_run_summary_and_table_are_correct(capsys):
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-11", "Jane"),  # new for Alice
        ],
        student_items=[
            _student_row("m1", "111", item_name="Alice", session_count="2", last_session="2026-09-01", tutor="Old", last_synced="2026-09-10"),
            _student_row("m2", "222", item_name="Bob", session_count="5", last_session="2026-09-05", tutor="Steady", last_synced="2026-09-10"),  # no new sessions
        ],
    )
    run_student_rollup_dry_run(monday, today="2026-09-15")
    out = capsys.readouterr().out

    assert "Alice | 2 | 1 | 3 | 2026-09-01 | 2026-09-11 | Old | Jane" in out
    assert "Bob | 5 |" not in out  # Bob has no new sessions - not shown as a row
    assert "Students evaluated: 2" in out
    assert "Students with new sessions: 1" in out
    assert "Total new sessions represented in student rollups: 1" in out
    assert "Students with no changes: 1" in out
    assert "Missing Monday students: 0" in out
    assert "Students that WOULD be updated: 1" in out


def test_dry_run_never_calls_update_student_columns_even_when_changes_exist():
    monday = WriteGuardedMondayClient(
        items=[_session_row("s1", "111", "2026-09-11", "Jane")],
        student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
    )
    # Would have raised inside update_student_columns if it were ever called.
    run_student_rollup_dry_run(monday, today="2026-09-15")


def test_cli_refuses_student_rollups_without_dry_run(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: WriteGuardedMondayClient())

    exit_code = sync.main(["--student-rollups"])  # no --dry-run

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "only supports --dry-run" in out
