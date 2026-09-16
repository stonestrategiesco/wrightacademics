"""Tests for the read-only baseline+SET student rollup dry run
(compute_post_baseline_student_rollup_updates / run_post_baseline_rollup_dry_run).

Session Count = Historical Session Baseline + count of distinct canonical
post-baseline Session Log identities. Last Session Date / Tutor only move
forward relative to the student's currently-stored value. First Session
Date is never read or written. Must never write to Monday."""

import config
from sync import compute_post_baseline_student_rollup_updates, run_post_baseline_rollup_dry_run
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called")


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


def test_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="32", session_count="32")],
        items=[_session_row("i1", "100_111", "111", pre_baseline="v")],
    )

    exit_code = run_post_baseline_rollup_dry_run(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_session_count_equals_baseline_plus_distinct_post_baseline_identities():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[
            _session_row("i1", "100_111", "111"),
            _session_row("i2", "101_111", "111"),
        ],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["proposed_count"] == 32  # 30 + 2 distinct post-baseline sessions


def test_pre_baseline_rows_are_excluded_from_the_count():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111", pre_baseline="v")],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["proposed_count"] == 30


def test_bare_and_composite_duplicate_rows_collapse_to_one_count():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[
            _session_row("i1", "100", "111"),
            _session_row("i2", "100_111", "111"),
        ],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["proposed_count"] == 31  # 30 + 1 distinct canonical identity


def test_blank_baseline_treated_as_zero():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="", session_count="0")],
        items=[_session_row("i1", "100_111", "111")],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["historical_baseline"] == 0
    assert result["proposed_count"] == 1


def test_last_session_and_tutor_move_forward_when_post_baseline_session_is_later():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Old Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-14", tutor="New Tutor")],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["proposed_last_session"] == "2026-09-14"
    assert result["proposed_tutor"] == "New Tutor"


def test_last_session_and_tutor_do_not_regress_for_an_older_post_baseline_session():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="31",
                                 last_session="2026-09-10", tutor="Current Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-05", tutor="Older Tutor")],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    # Session Count still increases (a legitimate late-arriving session)...
    assert result["proposed_count"] == 31
    # ...but Last Session Date / Tutor are untouched since 2026-09-05 < 2026-09-10.
    assert result["proposed_last_session"] == "2026-09-10"
    assert result["proposed_tutor"] == "Current Tutor"


def test_no_post_baseline_sessions_leaves_last_session_and_tutor_unchanged():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Current Tutor")],
        items=[],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["proposed_count"] == 30
    assert result["proposed_last_session"] == "2026-09-10"
    assert result["proposed_tutor"] == "Current Tutor"


def test_students_with_blank_teachworks_id_are_unmatched():
    monday = FakeMondayClient(
        student_items=[_student("s1", "NoId", "")],
        items=[],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)
    result = outcome["results"][0]

    assert result["matched"] is False
    assert "proposed_count" not in result


def test_distinct_post_baseline_sessions_and_duplicate_collapse_counts():
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "100_1", "1"),
            _session_row("i2", "100_1", "1"),  # exact duplicate
            _session_row("i3", "101", "2"),
            _session_row("i4", "101_2", "2"),  # cross-format duplicate
            _session_row("i5", "102_3", "3", pre_baseline="v"),  # excluded
        ],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)

    # Post-baseline pool (i1-i4): identities (100,1) and (101,2) = 2 distinct.
    assert outcome["distinct_post_baseline_sessions"] == 2
    assert outcome["duplicate_rows_collapsed"] == 2  # 4 physical rows - 2 distinct


def test_missing_identity_rows_are_counted():
    monday = FakeMondayClient(
        student_items=[],
        items=[
            _session_row("i1", "", "1"),  # missing unique_key
            _session_row("i2", "200", ""),  # missing teachworks student id
        ],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)

    assert outcome["missing_identity_rows"] == 2


def test_dry_run_report_shows_current_and_proposed_values(capsys):
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Old Tutor")],
        items=[_session_row("i1", "100_111", "111", session_date="2026-09-14", tutor="New Tutor")],
    )

    run_post_baseline_rollup_dry_run(monday)

    out = capsys.readouterr().out
    assert "Students evaluated: 1" in out
    assert "Historical baseline total (across matched students): 30" in out
    assert "Distinct post-baseline sessions: 1" in out
    assert "Students whose calculated Session Count differs from Monday: 1" in out
    assert "Session Count:      30 -> 31" in out
    assert "Last Session Date:  2026-09-10 -> 2026-09-14" in out
    assert "Tutor:              Old Tutor -> New Tutor" in out


def test_dry_run_reports_zero_differences_when_nothing_changed(capsys):
    monday = WriteGuardedMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30",
                                 last_session="2026-09-10", tutor="Jane Tutor")],
        items=[],
    )

    run_post_baseline_rollup_dry_run(monday)

    out = capsys.readouterr().out
    assert "Students whose calculated Session Count differs from Monday: 0" in out


def test_first_session_date_column_is_never_read_or_written():
    monday = FakeMondayClient(
        student_items=[_student("s1", "Alice", "111", baseline="30", session_count="30")],
        items=[_session_row("i1", "100_111", "111")],
    )

    outcome = compute_post_baseline_student_rollup_updates(monday)

    assert "first_session_date" not in outcome["results"][0]
    assert monday.student_updates == []


def test_cli_wires_dry_run_flag(monkeypatch):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = WriteGuardedMondayClient(student_items=[], items=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--post-baseline-rollups", "--dry-run"])

    assert exit_code == 0
    assert monday.student_updates == []


def test_cli_refuses_apply_since_it_is_not_yet_implemented(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = WriteGuardedMondayClient(student_items=[], items=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--post-baseline-rollups", "--apply"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "not yet implemented" in out
    assert monday.student_updates == []


def test_cli_requires_dry_run_flag(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = WriteGuardedMondayClient(student_items=[], items=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--post-baseline-rollups"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "requires --dry-run" in out
