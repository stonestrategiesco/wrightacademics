"""Tests for Stage 3: the REAL Monday write path for student rollups
(apply_student_rollup_updates / run_student_rollup_apply). Covers rollup
writes, checkpoint-only writes, unmatched students being skipped, First
Session Date never being written, individual failures not stopping the
run, failed students not advancing their checkpoint, and --apply being
explicitly required at the CLI level."""

import config
from sync import apply_student_rollup_updates, run_student_rollup_apply
from tests.fakes import FakeMondayClient


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


# --- Rollup writes ----------------------------------------------------

def test_rollup_write_sends_correct_column_values():
    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "New Tutor")],
        student_items=[_student_row("m1", "111", first_session="2020-05-01", session_count="3",
                                     last_session="2026-09-01", tutor="Old Tutor", last_synced="2026-09-10")],
    )

    outcome = apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(outcome["written_rollup"]) == 1
    assert outcome["failed"] == []
    assert len(monday.student_updates) == 1
    update = monday.student_updates[0]
    assert update["board_id"] == config.MONDAY_STUDENTS_BOARD_ID
    assert update["item_id"] == "m1"
    assert update["column_values"] == {
        config.STUDENT_COL_SESSION_COUNT: 4,
        config.STUDENT_COL_LAST_SESSION_DATE: {"date": "2026-09-12"},
        config.STUDENT_COL_TUTOR: "New Tutor",
        config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: {"date": "2026-09-15"},
    }


# --- Checkpoint-only writes ---------------------------------------------

def test_checkpoint_only_write_sends_only_the_checkpoint_column():
    monday = FakeMondayClient(
        items=[],  # no new sessions
        student_items=[_student_row("m1", "111", first_session="2020-05-01", session_count="7",
                                     last_session="2026-09-01", tutor="Steady", last_synced="2026-09-10")],
    )

    outcome = apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(outcome["written_checkpoint_only"]) == 1
    assert outcome["written_rollup"] == []
    assert len(monday.student_updates) == 1
    update = monday.student_updates[0]
    assert update["item_id"] == "m1"
    # ONLY the checkpoint column - nothing else, not even a count/tutor/date
    assert update["column_values"] == {config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: {"date": "2026-09-15"}}


# --- Unmatched students are skipped ---------------------------------------

def test_unmatched_student_receives_no_write():
    monday = FakeMondayClient(
        items=[],
        student_items=[_student_row("m1", tw_student_id="", item_name="No ID Student")],
    )

    outcome = apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(outcome["skipped_unmatched"]) == 1
    assert outcome["written_rollup"] == []
    assert outcome["written_checkpoint_only"] == []
    assert monday.student_updates == []


# --- First Session Date is never written ----------------------------------

def test_first_session_date_is_never_written_for_rollup_or_checkpoint_only():
    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "Jane")],
        student_items=[
            _student_row("m1", "111", first_session="2020-05-01", session_count="1", last_synced="2026-09-10"),  # rollup
            _student_row("m2", "222", first_session="2019-01-01", session_count="5", last_synced="2026-09-10"),  # checkpoint-only
        ],
    )

    apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(monday.student_updates) == 2
    for update in monday.student_updates:
        assert config.STUDENT_COL_FIRST_SESSION_DATE not in update["column_values"]


# --- Individual failures don't stop the run / don't advance checkpoint ----

def test_individual_failure_does_not_stop_remaining_students_and_is_reported():
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-12", "Jane"),  # rollup for 111
            _session_row("s2", "222", "2026-09-12", "Ben"),   # rollup for 222
        ],
        student_items=[
            _student_row("m1", "111", session_count="1", last_synced="2026-09-10"),  # will fail
            _student_row("m2", "222", session_count="2", last_synced="2026-09-10"),  # will succeed
        ],
    )

    def fail_m1(item_id, column_values):
        if item_id == "m1":
            raise RuntimeError("Monday API rejected the request")

    monday.update_student_side_effect = fail_m1

    outcome = apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["monday_item_id"] == "m1"
    assert "Monday API rejected the request" in outcome["failed"][0]["error"]
    # the OTHER student was still processed despite the first one failing
    assert len(outcome["written_rollup"]) == 1
    assert outcome["written_rollup"][0]["monday_item_id"] == "m2"


def test_failed_student_write_is_never_recorded_as_applied():
    """The failed student's checkpoint must not advance. Since a single
    combined change_multiple_column_values mutation carries every field
    including the checkpoint, a raised exception means the write for that
    student never happened at all - proven here by the fake never
    recording it in student_updates."""
    monday = FakeMondayClient(
        items=[],
        student_items=[_student_row("m1", "111", session_count="5", last_synced="2026-09-10")],
    )
    monday.update_student_side_effect = lambda item_id, column_values: (_ for _ in ()).throw(RuntimeError("boom"))

    outcome = apply_student_rollup_updates(monday, today="2026-09-15")

    assert len(outcome["failed"]) == 1
    assert monday.student_updates == []  # nothing was ever actually written


def test_run_student_rollup_apply_reports_failures_and_exits_nonzero(capsys):
    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "Jane")],
        student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
    )
    monday.update_student_side_effect = lambda item_id, column_values: (_ for _ in ()).throw(RuntimeError("boom"))

    exit_code = run_student_rollup_apply(monday, today="2026-09-15")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "Failed writes: 1" in out
    assert "checkpoint NOT advanced" in out


# --- --apply is explicitly required at the CLI level ----------------------

def test_cli_apply_actually_performs_writes(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "Jane")],
        student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
    )
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--student-rollups", "--apply"])

    assert exit_code == 0
    assert len(monday.student_updates) == 1  # the real write happened


def test_cli_dry_run_never_performs_writes_even_though_apply_path_exists(monkeypatch):
    """Guards against a regression where --dry-run silently starts writing
    now that the write path exists."""
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = FakeMondayClient(
        items=[_session_row("s1", "111", "2026-09-12", "Jane")],
        student_items=[_student_row("m1", "111", session_count="1", last_synced="2026-09-10")],
    )
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    sync.main(["--student-rollups", "--dry-run"])

    assert monday.student_updates == []
