"""Orchestration-level tests for --daily-sync (perform_daily_sync /
run_daily_sync). These test the GLUE only: ordering, the step-1 success
gate, and step-2 outcome handling. They monkeypatch sync.run_sync and
sync.apply_post_baseline_student_rollup_updates directly rather than
exercising their full internal behavior - that behavior is already fully
covered by test_sync.py and test_apply_post_baseline_rollup.py, and is
not re-tested here."""

import sync
from sync import SyncReport, perform_daily_sync, run_daily_sync


def _report(creation_errors=None, missing_students=None, connection_errors=None):
    return SyncReport(
        mode="SCHEDULED (rolling lookback)",
        start_date="2026-09-13",
        end_date="2026-09-16",
        creation_errors=creation_errors or [],
        missing_students=missing_students or [],
        connection_errors=connection_errors or [],
    )


def _rollup_outcome(failed=None):
    return {
        "results": [{"monday_item_id": "s1"}],
        "written": [],
        "unchanged": [{"monday_item_id": "s1"}],
        "skipped_unmatched": [],
        "failed": failed or [],
    }


def test_session_log_runs_before_rollups(monkeypatch):
    call_order = []

    def fake_run_sync(*args, **kwargs):
        call_order.append("sync")
        return _report()

    def fake_rollup(*args, **kwargs):
        call_order.append("rollup")
        return _rollup_outcome()

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", fake_rollup)

    perform_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    assert call_order == ["sync", "rollup"]


def test_rollups_do_not_run_if_session_log_has_creation_errors(monkeypatch):
    rollup_called = []
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report(
        creation_errors=[{"unique_key": "100_1", "error": "boom"}]
    ))
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: rollup_called.append(True))

    outcome = perform_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    assert rollup_called == []
    assert outcome["sync_succeeded"] is False
    assert outcome["rollup_ran"] is False
    assert outcome["rollup_outcome"] is None


def test_rollups_do_not_run_if_session_log_raises(monkeypatch):
    rollup_called = []

    def boom(*args, **kwargs):
        raise RuntimeError("Teachworks is down")

    monkeypatch.setattr(sync, "run_sync", boom)
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: rollup_called.append(True))

    outcome = perform_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    assert rollup_called == []
    assert outcome["sync_succeeded"] is False
    assert outcome["sync_report"] is None
    assert isinstance(outcome["sync_exception"], RuntimeError)


def test_successful_session_log_proceeds_to_rollups_even_with_missing_students_and_connection_errors(monkeypatch):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report(
        missing_students=[{"teachworks_student_id": "1", "student_name": "A", "lesson_id": "L1"}],
        connection_errors=[{"item_id": "i1", "error": "conn fail"}],
    ))
    outcome_stub = _rollup_outcome()
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: outcome_stub)

    outcome = perform_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    assert outcome["sync_succeeded"] is True
    assert outcome["rollup_ran"] is True
    assert outcome["rollup_outcome"] is outcome_stub


def test_rollup_write_failures_return_nonzero_and_partial_failure(monkeypatch, capsys):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report())
    monkeypatch.setattr(
        sync, "apply_post_baseline_student_rollup_updates",
        lambda *a, **k: _rollup_outcome(failed=[{"monday_item_id": "s1", "error": "write failed"}]),
    )

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL RESULT: PARTIAL FAILURE" in out


def test_rollup_exception_returns_nonzero_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report())

    def boom(*args, **kwargs):
        raise RuntimeError("Monday API is down")

    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", boom)

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FINAL RESULT: FAILURE" in out
    assert "EXCEPTION: Monday API is down" in out


def test_creation_errors_produce_failure_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report(
        creation_errors=[{"unique_key": "100_1", "error": "boom"}]
    ))
    rollup_called = []
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: rollup_called.append(True))

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert rollup_called == []
    assert "FINAL RESULT: FAILURE" in out
    assert "SKIPPED - Session Log sync did not succeed" in out


def test_session_log_exception_produces_failure_and_nonzero_exit(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("Teachworks is down")

    monkeypatch.setattr(sync, "run_sync", boom)
    rollup_called = []
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: rollup_called.append(True))

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 1
    assert rollup_called == []
    assert "FINAL RESULT: FAILURE" in out
    assert "EXCEPTION: Teachworks is down" in out


def test_completely_successful_run_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report())
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: _rollup_outcome())

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "FINAL RESULT: SUCCESS" in out


def test_missing_students_and_connection_errors_are_surfaced_but_not_blocking(monkeypatch, capsys):
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report(
        missing_students=[{"teachworks_student_id": "1", "student_name": "A", "lesson_id": "L1"}],
        connection_errors=[{"item_id": "i1", "error": "conn fail"}],
    ))
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: _rollup_outcome())

    exit_code = run_daily_sync(object(), object(), "2026-09-13", "2026-09-16")

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Missing students:               1" in out
    assert "Connection errors:              1" in out
    assert "FINAL RESULT: SUCCESS" in out


def test_cli_wires_daily_sync_flag(monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: _report())
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates", lambda *a, **k: _rollup_outcome())

    exit_code = sync.main(["--daily-sync"])

    assert exit_code == 0
