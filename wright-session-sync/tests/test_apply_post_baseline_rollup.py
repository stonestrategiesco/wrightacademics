"""Tests for the baseline+SET student rollup APPLY
(apply_post_baseline_student_rollup_updates / run_post_baseline_rollup_apply).

Reuses compute_post_baseline_student_rollup_updates() exactly as-is (no
second calculation path). Focus areas: SET (not increment) semantics,
writing only students whose calculated values differ, never touching
First Session Date, continuing past individual failures, and - most
importantly - that re-running apply against a board that already reflects
the first run's writes produces zero further writes."""

import config
from sync import apply_post_baseline_student_rollup_updates, run_post_baseline_rollup_apply
from tests.fakes import FakeMondayClient


def _student(item_id, name, tw_id, baseline="0", session_count="", last_session="", tutor=""):
    return {
        "item_id": item_id,
        "item_name": name,
        "columns": {
            config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_id,
            config.STUDENT_COL_HISTORICAL_BASELINE: baseline,
            config.STUDENT_COL_SESSION_COUNT: session_count,
            config.STUDENT_COL_LAST_SESSION_DATE: last_session,
            config.STUDENT_COL_TUTOR: tutor,
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


def test_sets_session_count_to_calculated_value_not_incremented():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "101_111", "111"),
        ],
    )

    outcome = apply_post_baseline_student_rollup_updates(monday)

    assert len(outcome["written"]) == 1
    written_columns = monday.student_updates[0]["column_values"]
    assert written_columns[config.STUDENT_COL_SESSION_COUNT] == 32  # 30 + 2, a plain SET


def test_only_writes_students_whose_calculated_values_differ():
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", baseline="30", session_count="30",
                      last_session="2026-09-10", tutor="Jane Tutor"),  # already correct, no post-baseline rows
            _student("s2", "Bob", "222", baseline="5", session_count="5",
                      last_session="2026-09-10", tutor="Jane Tutor"),  # will gain a session
        ],
        items=[_session_row("i1", "200_222", "222", session_date="2026-09-14", tutor="New Tutor")],
    )

    outcome = apply_post_baseline_student_rollup_updates(monday)

    assert len(outcome["unchanged"]) == 1
    assert outcome["unchanged"][0]["monday_item_id"] == "s1"
    assert len(outcome["written"]) == 1
    assert outcome["written"][0]["monday_item_id"] == "s2"
    assert len(monday.student_updates) == 1
    assert monday.student_updates[0]["item_id"] == "s2"


def test_write_touches_only_session_count_last_session_and_tutor():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Old Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-14", tutor="New Tutor")],
    )

    apply_post_baseline_student_rollup_updates(monday)

    written_columns = monday.student_updates[0]["column_values"]
    assert set(written_columns.keys()) == {
        config.STUDENT_COL_SESSION_COUNT,
        config.STUDENT_COL_LAST_SESSION_DATE,
        config.STUDENT_COL_TUTOR,
    }
    assert config.STUDENT_COL_FIRST_SESSION_DATE not in written_columns
    assert config.STUDENT_COL_SESSION_DATA_LAST_SYNCED not in written_columns
    assert config.STUDENT_COL_HISTORICAL_BASELINE not in written_columns
    assert written_columns[config.STUDENT_COL_LAST_SESSION_DATE] == {"date": "2026-09-14"}
    assert written_columns[config.STUDENT_COL_TUTOR] == "New Tutor"


def test_last_session_and_tutor_no_regression_is_preserved_in_the_write():
    # An older post-baseline session raises Session Count (current is 30,
    # not yet reflecting this session) but must NOT move Last Session
    # Date/Tutor backward - the write must still carry the CURRENT
    # (unchanged) Last Session Date/Tutor, never the older session's.
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Current Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-05", tutor="Older Tutor")],
    )

    apply_post_baseline_student_rollup_updates(monday)

    written_columns = monday.student_updates[0]["column_values"]
    assert written_columns[config.STUDENT_COL_SESSION_COUNT] == 31
    assert written_columns[config.STUDENT_COL_LAST_SESSION_DATE] == {"date": "2026-09-10"}
    assert written_columns[config.STUDENT_COL_TUTOR] == "Current Tutor"


def test_unmatched_student_receives_no_write():
    monday = FakeMondayClient(
        student_items=[_student("s1", "NoId", "")],
        items=[],
    )

    outcome = apply_post_baseline_student_rollup_updates(monday)

    assert len(outcome["skipped_unmatched"]) == 1
    assert monday.student_updates == []


def test_continues_past_individual_write_failure_and_collects_it():
    monday = FakeMondayClient(
        student_items=[
            _student("s1", "Alice", "111", baseline="30", session_count="30"),
            _student("s2", "Bob", "222", baseline="5", session_count="5"),
        ],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "200_222", "222"),
        ],
    )

    def fail_for_s1(item_id, column_values):
        if item_id == "s1":
            raise RuntimeError("simulated Monday API failure")

    monday.update_student_side_effect = fail_for_s1

    outcome = apply_post_baseline_student_rollup_updates(monday)

    assert len(outcome["failed"]) == 1
    assert outcome["failed"][0]["monday_item_id"] == "s1"
    assert len(outcome["written"]) == 1
    assert outcome["written"][0]["monday_item_id"] == "s2"


def test_exit_code_zero_when_all_writes_succeed(capsys):
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111")],
    )

    exit_code = run_post_baseline_rollup_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL RESULT: SUCCESS" in out


def test_exit_code_nonzero_when_any_write_fails(capsys):
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111")],
    )

    def always_fail(item_id, column_values):
        raise RuntimeError("simulated failure")

    monday.update_student_side_effect = always_fail

    exit_code = run_post_baseline_rollup_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL RESULT: FAILED" in out


def test_rerunning_apply_twice_produces_zero_writes_on_second_run():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Old Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-14", tutor="New Tutor")],
    )

    first_outcome = apply_post_baseline_student_rollup_updates(monday)
    assert len(first_outcome["written"]) == 1
    assert len(monday.student_updates) == 1

    # Simulate the Monday board now reflecting exactly what the first run wrote.
    written_columns = monday.student_updates[0]["column_values"]
    monday.student_items[0]["columns"][config.STUDENT_COL_SESSION_COUNT] = str(written_columns[config.STUDENT_COL_SESSION_COUNT])
    monday.student_items[0]["columns"][config.STUDENT_COL_LAST_SESSION_DATE] = written_columns[config.STUDENT_COL_LAST_SESSION_DATE]["date"]
    monday.student_items[0]["columns"][config.STUDENT_COL_TUTOR] = written_columns[config.STUDENT_COL_TUTOR]
    monday.student_updates = []  # isolate the second run's writes

    second_outcome = apply_post_baseline_student_rollup_updates(monday)

    assert len(second_outcome["written"]) == 0
    assert len(second_outcome["unchanged"]) == 1
    assert monday.student_updates == []


def test_rerunning_apply_twice_via_cli_wrapper_second_run_reports_zero_writes(capsys):
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Old Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-14", tutor="New Tutor")],
    )

    run_post_baseline_rollup_apply(monday)
    written_columns = monday.student_updates[0]["column_values"]
    monday.student_items[0]["columns"][config.STUDENT_COL_SESSION_COUNT] = str(written_columns[config.STUDENT_COL_SESSION_COUNT])
    monday.student_items[0]["columns"][config.STUDENT_COL_LAST_SESSION_DATE] = written_columns[config.STUDENT_COL_LAST_SESSION_DATE]["date"]
    monday.student_items[0]["columns"][config.STUDENT_COL_TUTOR] = written_columns[config.STUDENT_COL_TUTOR]
    monday.student_updates = []
    capsys.readouterr()  # discard first run's output

    exit_code = run_post_baseline_rollup_apply(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Writes performed: 0" in out
    assert "Already correct (no write needed): 1" in out
    assert monday.student_updates == []


def test_apply_uses_the_exact_same_calculation_as_the_dry_run():
    from sync import compute_post_baseline_student_rollup_updates

    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111")],
    )

    dry_run_result = compute_post_baseline_student_rollup_updates(monday)["results"][0]
    apply_outcome = apply_post_baseline_student_rollup_updates(monday)
    apply_result = apply_outcome["written"][0]

    assert apply_result["proposed_count"] == dry_run_result["proposed_count"]
    assert apply_result["proposed_last_session"] == dry_run_result["proposed_last_session"]
    assert apply_result["proposed_tutor"] == dry_run_result["proposed_tutor"]


def test_cli_apply_flag_performs_writes(monkeypatch):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111")],
    )
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--post-baseline-rollups", "--apply"])

    assert exit_code == 0
    assert len(monday.student_updates) == 1


def test_cli_rejects_both_dry_run_and_apply_together(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = FakeMondayClient(student_items=[], items=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--post-baseline-rollups", "--dry-run", "--apply"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "not both" in out
    assert monday.student_updates == []
