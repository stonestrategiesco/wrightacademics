"""Tests for the read-only Session Log duplicate-identity investigation.
It must never write to Monday, and must correctly distinguish exact
literal-string duplicate unique keys from cross-format (legacy bare vs
current composite) lesson_id collisions."""

import config
from sync import diagnose_session_log_duplicates
from tests.fakes import FakeMondayClient


class WriteGuardedMondayClient(FakeMondayClient):
    def create_session_item(self, *args, **kwargs):
        raise AssertionError("create_session_item must never be called")

    def connect_student(self, *args, **kwargs):
        raise AssertionError("connect_student must never be called")

    def update_student_columns(self, *args, **kwargs):
        raise AssertionError("update_student_columns must never be called")


def _row(item_id, unique_key, tw_student_id, session_date="2026-09-01", tutor="Jane Tutor"):
    return {
        "item_id": item_id,
        "item_name": f"Session {item_id}",
        "columns": {
            config.COL_UNIQUE_ID: unique_key,
            config.COL_TEACHWORKS_STUDENT_ID: tw_student_id,
            config.COL_SESSION_DATE: session_date,
            config.COL_TUTOR: tutor,
        },
    }


def test_makes_zero_monday_writes():
    monday = WriteGuardedMondayClient(
        items=[_row("i1", "89187387_2203327", "2203327")],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z"},
    )

    exit_code = diagnose_session_log_duplicates(monday)

    assert exit_code == 0
    assert monday.created_items == []
    assert monday.connections == []
    assert monday.student_updates == []


def test_reports_no_duplicates_when_all_keys_are_unique(capsys):
    monday = FakeMondayClient(
        items=[
            _row("i1", "100_1", "1"),
            _row("i2", "101_2", "2"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-02T12:00:00Z"},
    )

    exit_code = diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Exact duplicate unique-key groups: 0" in out
    assert "Cross-format lesson_id collisions: 0" in out
    assert "None found." in out


def test_detects_exact_duplicate_group_with_five_rows(capsys):
    items = [_row(f"i{n}", "89187387_2203327", "2203327") for n in range(5)]
    created_at = {f"i{n}": f"2026-09-0{n+1}T12:00:00Z" for n in range(5)}
    monday = FakeMondayClient(items=items, created_at_by_item_id=created_at)

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "Exact duplicate unique-key groups: 1" in out
    assert "unique_key='89187387_2203327'" in out
    assert "format=composite" in out
    assert "derived_lesson_id=89187387" in out
    for n in range(5):
        assert f"i{n}" in out


def test_detects_exact_duplicate_bare_legacy_keys(capsys):
    monday = FakeMondayClient(
        items=[
            _row("i1", "89515688", "111"),
            _row("i2", "89515688", "111"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-01T12:05:00Z"},
    )

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "Exact duplicate unique-key groups: 1" in out
    assert "unique_key='89515688'" in out
    assert "format=legacy_bare" in out
    assert "derived_lesson_id=89515688" in out


def test_flags_exact_duplicate_rows_with_disagreeing_student_id(capsys):
    monday = FakeMondayClient(
        items=[
            _row("i1", "89515688", "111"),
            _row("i2", "89515688", "222"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-01T12:05:00Z"},
    )

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "NOTE: rows disagree on Teachworks Student ID" in out


def test_detects_cross_format_collision_same_student_as_likely_duplicate(capsys):
    monday = FakeMondayClient(
        items=[
            _row("i1", "89515688", "111"),
            _row("i2", "89515688_111", "111"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-20T09:00:00Z"},
    )

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "Lesson IDs with both a legacy bare key and a composite key: 1" in out
    assert "lesson_id=89515688" in out
    assert "SAME Teachworks Student ID" in out
    assert "LIKELY TRUE DUPLICATE" in out
    assert "of which same-student (likely true duplicate): 1" in out
    # Exact-duplicate detection must NOT also fire for this pair - the two
    # literal unique_key strings differ.
    assert "Exact duplicate unique-key groups: 0" in out


def test_detects_cross_format_collision_different_student_as_legitimate(capsys):
    monday = FakeMondayClient(
        items=[
            _row("i1", "89515688", "111"),
            _row("i2", "89515688_222", "222"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-20T09:00:00Z"},
    )

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "DIFFERENT Teachworks Student ID" in out
    assert "LIKELY LEGITIMATE" in out
    assert "of which same-student (likely true duplicate): 0" in out
    assert "of which different-student (likely legitimate multi-participant lesson): 1" in out


def test_composite_keys_for_the_same_lesson_with_different_students_are_not_flagged_as_cross_format(capsys):
    # Two legitimate composite-format participants in the same lesson -
    # both composite, no bare key involved - must not appear under
    # cross-format collisions at all.
    monday = FakeMondayClient(
        items=[
            _row("i1", "500_1", "1"),
            _row("i2", "500_2", "2"),
        ],
        created_at_by_item_id={"i1": "2026-09-01T12:00:00Z", "i2": "2026-09-01T12:00:05Z"},
    )

    diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert "Cross-format lesson_id collisions: 0" in out
    assert "Exact duplicate unique-key groups: 0" in out


def test_handles_empty_board(capsys):
    monday = WriteGuardedMondayClient(items=[])

    exit_code = diagnose_session_log_duplicates(monday)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Session Log rows evaluated: 0" in out


def test_cli_wires_the_new_flag(monkeypatch):
    import config as config_module
    import sync

    monkeypatch.setattr(config_module, "TEACHWORKS_API_KEY", "fake-key")
    monkeypatch.setattr(config_module, "MONDAY_API_TOKEN", "fake-token")

    monday = WriteGuardedMondayClient(items=[])
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: monday)

    exit_code = sync.main(["--diagnose-session-log-duplicates"])

    assert exit_code == 0
    assert monday.student_updates == []
