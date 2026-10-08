"""Students board "Session Data Last Synced" (date_mm5gk46f) records when a
student's session data last actually changed: set to the run date in the same
write as Session Count / Last Session Date / Tutor, never on a student whose
data didn't change, plus a one-time catch-up for students left behind when the
nightly didn't maintain it (e.g. still 2026-09-15 with a 2026-10-06 last session)."""

import types

import pytest

import config
import sync
from sync import apply_post_baseline_student_rollup_updates
from tests.fakes import FakeMondayClient

COL = config.STUDENT_COL_SESSION_DATA_LAST_SYNCED


def student(item_id, tw_id, count, last, tutor, data_updated):
    return {"item_id": item_id, "item_name": item_id, "columns": {
        config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_id, config.STUDENT_COL_HISTORICAL_BASELINE: "10",
        config.STUDENT_COL_SESSION_COUNT: str(count), config.STUDENT_COL_LAST_SESSION_DATE: last,
        config.STUDENT_COL_TUTOR: tutor, COL: data_updated}}


def session(item_id, tw_id, date, tutor="T"):
    return {"item_id": item_id, "item_name": item_id, "columns": {
        config.COL_UNIQUE_ID: f"{item_id}_{tw_id}", config.COL_TEACHWORKS_STUDENT_ID: tw_id,
        config.COL_SESSION_DATE: date, config.COL_TUTOR: tutor, config.COL_PRE_BASELINE: ""}}


def apply_and_reflect(monday, today):
    """Apply, then make the fake board hold what was written (as Monday would)."""
    outcome = apply_post_baseline_student_rollup_updates(monday, today=today)
    for update in monday.student_updates:
        cols = next(s for s in monday.student_items if s["item_id"] == update["item_id"])["columns"]
        for col, value in update["column_values"].items():
            cols[col] = value["date"] if isinstance(value, dict) else ("" if value is None else str(value))
    monday.student_updates = []
    return outcome


def board():
    return FakeMondayClient(
        student_items=[
            student("changed", "1", 11, "2026-10-02", "T", "2026-10-02"),     # new session on 10-06 -> real update
            student("stuck", "2", 11, "2026-10-06", "T", "2026-09-15"),       # the reported symptom
            student("current", "3", 11, "2026-10-04", "T", "2026-10-04"),     # up to date: no write
            student("blank", "4", 11, "2026-10-01", "T", ""),                 # never set
        ],
        items=[session("a", "1", "2026-10-02"), session("b", "1", "2026-10-06"),
               session("c", "2", "2026-10-06"), session("d", "3", "2026-10-04"), session("e", "4", "2026-10-01")],
    )


def test_real_change_writes_the_run_date_in_the_same_update():
    monday = board()
    apply_post_baseline_student_rollup_updates(monday, today="2026-10-08")
    update = next(u for u in monday.student_updates if u["item_id"] == "changed")
    assert update["column_values"] == {config.STUDENT_COL_SESSION_COUNT: 12,
                                       config.STUDENT_COL_LAST_SESSION_DATE: {"date": "2026-10-06"},
                                       config.STUDENT_COL_TUTOR: "T", COL: {"date": "2026-10-08"}}


def test_stuck_and_blank_students_are_caught_up_once_with_nothing_else_written():
    monday = board()
    apply_post_baseline_student_rollup_updates(monday, today="2026-10-08")
    by_item = {u["item_id"]: u["column_values"] for u in monday.student_updates}
    assert by_item["stuck"] == {COL: {"date": "2026-10-08"}}
    assert by_item["blank"] == {COL: {"date": "2026-10-08"}}
    assert "current" not in by_item                                    # up to date: no write at all
    assert len(monday.student_updates) == 3                            # one mutation per written student


def test_second_run_and_next_nights_without_changes_write_nothing():
    monday = board()
    apply_and_reflect(monday, "2026-10-08")
    assert apply_and_reflect(monday, "2026-10-08")["written"] == []     # same night
    assert apply_and_reflect(monday, "2026-10-09")["written"] == []     # next night, nothing changed
    stamps = {s["item_id"]: s["columns"][COL] for s in monday.student_items}
    assert stamps == {"changed": "2026-10-08", "stuck": "2026-10-08", "current": "2026-10-04", "blank": "2026-10-08"}


def test_date_moves_only_when_the_students_data_changes():
    monday = board()
    apply_and_reflect(monday, "2026-10-08")
    monday.items.append(session("f", "3", "2026-10-09"))               # only student 3 gets a new session
    outcome = apply_and_reflect(monday, "2026-10-10")
    assert [r["monday_item_id"] for r in outcome["written"]] == ["current"]
    assert next(s for s in monday.student_items if s["item_id"] == "current")["columns"][COL] == "2026-10-10"
    assert next(s for s in monday.student_items if s["item_id"] == "stuck")["columns"][COL] == "2026-10-08"


def test_dry_run_preview_shows_the_catch_up(capsys):
    sync.run_post_baseline_rollup_dry_run(board())
    out = capsys.readouterr().out
    assert "would only be caught up (blank/older than Last Session Date): 2" in out
    assert "Session Data Last Synced: 2026-10-02 -> (run date)" in out


# --- banner -------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, banner", [
    (["--diagnose-student-columns"], "READ ONLY - this command makes no Monday writes"),
    (["--diagnose-teachworks"], "READ ONLY - this command makes no Monday writes"),
    (["--post-baseline-rollups", "--dry-run"], "DRY RUN - Monday writes are blocked"),
    (["--daily-sync", "--dry-run"], "DRY RUN - Monday writes are blocked"),
    (["--post-baseline-rollups"], "READ ONLY - this command makes no Monday writes"),
    (["--post-baseline-rollups", "--apply"], "LIVE - Monday writes enabled"),
    (["--daily-sync"], "LIVE - Monday writes enabled"),
    ([], "LIVE - Monday writes enabled"),
])
def test_banner_says_what_the_command_can_do(monkeypatch, capsys, argv, banner):
    monkeypatch.setattr(config, "TEACHWORKS_API_KEY", "k")
    monkeypatch.setattr(config, "MONDAY_API_TOKEN", "t")
    stub = types.SimpleNamespace()
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: stub)
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: FakeMondayClient())
    for name in ("run_daily_sync", "diagnose_student_columns", "diagnose_teachworks",
                 "run_post_baseline_rollup_dry_run", "run_post_baseline_rollup_apply"):
        monkeypatch.setattr(sync, name, lambda *a, **k: 0)
    monkeypatch.setattr(sync, "run_sync", lambda *a, **k: sync.SyncReport("m", "s", "e"))
    monkeypatch.setattr(sync, "print_report", lambda report: None)
    sync.main(argv)
    assert capsys.readouterr().out.splitlines()[0].endswith(banner)
