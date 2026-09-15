"""Tests for the read-only checkpoint-migration-readiness diagnostic: it
must never write to Monday, and must report the raw column settings_str
and a sample of Session Log items' created_at exactly as returned."""

import config
from sync import diagnose_checkpoint_migration_readiness
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called")


def _session_row(item_id, tw_student_id, session_date):
    return {
        "item_id": item_id,
        "columns": {
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
        },
    }


def test_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        column_settings={config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: {
            "id": config.STUDENT_COL_SESSION_DATA_LAST_SYNCED, "title": "Session Data Last Synced",
            "type": "date", "settings_str": '{"hide_footer":false}',
        }},
        items=[_session_row("s1", "111", "2026-09-15")],
        created_at_by_item_id={"s1": "2026-09-16T01:23:45Z"},
    )

    exit_code = diagnose_checkpoint_migration_readiness(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_reports_raw_column_settings(capsys):
    monday = FakeMondayClient(
        column_settings={config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: {
            "id": config.STUDENT_COL_SESSION_DATA_LAST_SYNCED, "title": "Session Data Last Synced",
            "type": "date", "settings_str": '{"time_enabled":true}',
        }},
    )

    diagnose_checkpoint_migration_readiness(monday)

    out = capsys.readouterr().out
    assert f"Column ID: {config.STUDENT_COL_SESSION_DATA_LAST_SYNCED}" in out
    assert "Type: date" in out
    assert '{"time_enabled":true}' in out


def test_reports_sample_items_with_created_at(capsys):
    monday = FakeMondayClient(
        items=[
            _session_row("s1", "111", "2026-09-15"),
            _session_row("s2", "222", "2026-09-16"),
        ],
        created_at_by_item_id={
            "s1": "2026-09-15T18:30:00Z",
            "s2": "2026-09-16T09:00:00Z",
        },
    )

    diagnose_checkpoint_migration_readiness(monday, sample_size=5)

    out = capsys.readouterr().out
    assert "item_id=s1 session_date=2026-09-15 created_at=2026-09-15T18:30:00Z teachworks_student_id=111" in out
    assert "item_id=s2 session_date=2026-09-16 created_at=2026-09-16T09:00:00Z teachworks_student_id=222" in out


def test_sample_size_limits_the_number_of_items_requested(capsys):
    monday = FakeMondayClient(
        items=[_session_row(f"s{i}", str(i), "2026-09-15") for i in range(10)],
        created_at_by_item_id={f"s{i}": "2026-09-15T12:00:00Z" for i in range(10)},
    )

    diagnose_checkpoint_migration_readiness(monday, sample_size=3)

    out = capsys.readouterr().out
    assert out.count("item_id=") == 3


def test_handles_empty_session_log_board(capsys):
    monday = FakeMondayClient(items=[])

    exit_code = diagnose_checkpoint_migration_readiness(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "No Session Log items found." in out


def test_cli_wires_the_new_flag(monkeypatch, capsys):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = WriteGuardedMondayClient(
        column_settings={config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: {
            "id": config.STUDENT_COL_SESSION_DATA_LAST_SYNCED, "title": "Session Data Last Synced",
            "type": "date", "settings_str": "{}",
        }},
        items=[],
    )
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--diagnose-checkpoint-migration"])

    assert exit_code == 0
    assert monday.student_updates == []
