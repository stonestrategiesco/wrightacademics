"""The post-baseline student rollup (nightly step 2) only writes a Students item
whose Teachworks Student ID is a valid numeric ID held by no other item. Blank,
invalid and duplicated IDs get no Session Count and no Session Data Last Synced
write - even when the item would otherwise need both."""

import config
from sync import apply_post_baseline_student_rollup_updates, run_post_baseline_rollup_apply, \
    run_post_baseline_rollup_dry_run
from tests.fakes import FakeMondayClient


def student(item_id, tw_id):
    # Every student here is out of date on purpose: count 0 vs 1 session, timestamp blank.
    return {"item_id": item_id, "item_name": f"Student {item_id}", "columns": {
        config.STUDENT_BOARD_COL_TEACHWORKS_ID: tw_id, config.STUDENT_COL_HISTORICAL_BASELINE: "0",
        config.STUDENT_COL_SESSION_COUNT: "0", config.STUDENT_COL_LAST_SESSION_DATE: "",
        config.STUDENT_COL_TUTOR: "", config.STUDENT_COL_SESSION_DATA_LAST_SYNCED: ""}}


def session(item_id, tw_id):
    return {"item_id": item_id, "item_name": item_id, "columns": {
        config.COL_UNIQUE_ID: f"{item_id}_{tw_id}", config.COL_TEACHWORKS_STUDENT_ID: tw_id,
        config.COL_SESSION_DATE: "2026-10-06", config.COL_TUTOR: "T", config.COL_PRE_BASELINE: ""}}


def board():
    return FakeMondayClient(
        student_items=[student("ok", "100"), student("ok_spaces", " 101 "),
                       student("dup_a", "200"), student("dup_b", "200"),
                       student("blank", ""), student("spaces", "   "), student("text", "abc-12")],
        items=[session("s1", "100"), session("s2", "101"), session("s3", "200"),
               session("s4", "abc-12")])


def test_only_valid_unique_ids_are_written():
    monday = board()
    outcome = apply_post_baseline_student_rollup_updates(monday, today="2026-10-08")
    assert sorted(u["item_id"] for u in monday.student_updates) == ["ok", "ok_spaces"]
    assert sorted(r["monday_item_id"] for r in outcome["skipped_unmatched"]) == ["blank", "dup_a", "dup_b", "spaces", "text"]
    reasons = {r["monday_item_id"]: r["skip_reason"] for r in outcome["skipped_unmatched"]}
    assert reasons == {"blank": "no Teachworks Student ID", "spaces": "no Teachworks Student ID",
                       "text": "invalid Teachworks Student ID 'abc-12'",
                       "dup_a": "Teachworks Student ID 200 is on 2 Students items",
                       "dup_b": "Teachworks Student ID 200 is on 2 Students items"}


def test_valid_unique_student_still_gets_its_count_and_timestamp():
    monday = board()
    apply_post_baseline_student_rollup_updates(monday, today="2026-10-08")
    update = next(u for u in monday.student_updates if u["item_id"] == "ok")["column_values"]
    assert update[config.STUDENT_COL_SESSION_COUNT] == 1
    assert update[config.STUDENT_COL_SESSION_DATA_LAST_SYNCED] == {"date": "2026-10-08"}


def test_once_the_duplicate_is_fixed_the_remaining_item_is_updated():
    monday = board()
    monday.student_items = [s for s in monday.student_items if s["item_id"] != "dup_b"]
    apply_post_baseline_student_rollup_updates(monday, today="2026-10-08")
    assert "dup_a" in {u["item_id"] for u in monday.student_updates}


def test_reports_say_why_each_student_was_skipped(capsys):
    run_post_baseline_rollup_dry_run(board())
    dry = capsys.readouterr().out
    assert "unmatched students - blank, invalid or duplicated Teachworks Student ID: 5" in dry
    assert "Monday item dup_a (Student dup_a): Teachworks Student ID 200 is on 2 Students items - not updated" in dry
    run_post_baseline_rollup_apply(board())
    live = capsys.readouterr().out
    assert "Skipped (cannot be matched, no write): 5" in live
    assert "Monday item text (Student text): invalid Teachworks Student ID 'abc-12'" in live
