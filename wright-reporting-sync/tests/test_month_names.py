"""Numbered month names ("09 - September 2026") are the canonical Reporting-board
item names; legacy names ("September 2026") are still recognised; both at once is
a duplicate that aborts. Uses monkeypatch so no module state leaks between tests."""

import builtins
import contextlib
import datetime as real_datetime_module
import io
import json
import sys

import pytest

import audit
import sync_monday as sm
import sync_reporting as sr

MONDAY_CFG = {
    "api_url": "https://fake-monday", "api_token": "fake-token", "auth_header": "Authorization",
    "auth_scheme": "{key}", "api_version": "", "items_page_size": 100, "board_id": "18432993218",
    "column_sessions_attended": "numeric_mm7mrpe4", "column_sessions_missed": "numeric_mm7mh4qd",
    "column_total_sessions": "numeric_mm7me68f", "column_students_served": "numeric_mm7mhsf3",
    "column_last_updated": "date_mm7m4799", "column_avg_sessions_per_student": "numeric_mm7mg9ve",
    "column_attended_amount": "numeric_mm7mymrr", "column_missed_amount": "numeric_mm7m84fd",
    "column_cancelled_amount": "numeric_mm7m2mg5", "column_scheduled_amount": "numeric_mm7m7y3x",
    "column_total_amount": "numeric_mm7mymtn",
}
TW_CONFIG = {
    "base_url": "https://fake", "api_key": "fake", "auth_header": "Authorization", "auth_scheme": "Token token={key}",
    "students_path": "/students", "lessons_path": "/lessons", "tutors_path": "", "page_param": "page",
    "per_page_param": "per_page", "response_data_key": "", "lesson_start_date_param": "", "lesson_end_date_param": "",
    "student_id_param": "student_id", "test_student_limit": 5, "test_lesson_limit": 20, "test_lesson_lookback_days": 30,
    "per_page": 80, "max_pages": 30, "request_delay_seconds": 0, "status_field": "status",
    "attended_status_value": "Attended", "student_id_field": "id", "student_name_field": "",
    "student_first_name_field": "", "student_last_name_field": "", "lesson_date_field": "",
    "lesson_tutor_id_field": "", "lesson_tutor_name_field": "", "tutor_record_id_field": "id",
    "tutor_record_name_field": "", "participants_field": "participants", "participant_sample_size": 20,
    "participant_id_field": "", "participant_type_field": "", "participant_student_type_value": "student",
}
STUDENTS = [{"id": 1, "first_name": "A", "last_name": "T"}]


def lessons_for(year, month):
    return [{"id": 10 + month * 100 + d, "from_datetime": f"{year:04d}-{month:02d}-{d:02d}T10:00:00Z",
             "participants": [{"student_id": 1, "status": "Attended", "amount": "10.00"}]} for d in (1, 2)]


def make_item(item_id, name):
    return {"id": item_id, "name": name,
            "column_values": [{"id": col, "text": "", "value": json.dumps("")}
                              for key, col in MONDAY_CFG.items() if key.startswith("column_")]}


class FakeMonday:
    """Reporting board with the given item names; records every mutation."""

    def __init__(self, names):
        self.items = {str(500 + i): make_item(str(500 + i), n) for i, n in enumerate(names)}
        self.creates, self.writes, self.group_lookups = [], [], 0

    def __call__(self, cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            cols = [{"id": MONDAY_CFG[k], "title": t, "type": ty} for k, (t, ty) in sr.EXPECTED_REPORTING_COLUMNS.items()]
            return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "columns": cols}]}
        if query == sr.BOARD_GROUPS_QUERY:
            self.group_lookups += 1
            return {"boards": [{"id": MONDAY_CFG["board_id"], "groups": [{"id": "group_2026", "title": "2026"}]}]}
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": list(self.items.values())}}]}
        if "create_item" in query:
            self.creates.append(variables)
            new_id = str(900 + len(self.creates))
            self.items[new_id] = make_item(new_id, variables["itemName"])
            return {"create_item": {"id": new_id}}
        if "change_simple_column_value" in query:
            self.writes.append(variables)
            item = self.items[str(variables["itemId"])]
            item["column_values"] = [c if c["id"] != variables["columnId"] else
                                     {"id": c["id"], "text": variables["value"], "value": json.dumps(variables["value"])}
                                     for c in item["column_values"]]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "itemIds" in query or "items (ids" in query:
            item = self.items[str(variables["itemIds"][0])]
            return {"items": [{"id": item["id"], "name": item["name"], "column_values": item["column_values"]}]}
        raise AssertionError(f"unexpected query: {query[:60]}")


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Runs sync_reporting.main() end to end against a FakeMonday, pinned to a given 'today'."""

    def run(argv, board_names, today):
        year, month = today

        class FakeDateTime(real_datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime_module.datetime(year, month, 15, 12, 0, 0, tzinfo=tz)

        def fake_request_json(config, path, params=None, max_retries=3):
            class R:
                url, status_code = "https://fake", 200
            page = (params or {}).get("page", 1)
            if path == config["students_path"]:
                return (STUDENTS if page == 1 else []), R()
            return ([l for m in range(1, 13) for l in lessons_for(year, m)] if page == 1 else []), R()

        monday = FakeMonday(board_names)
        monkeypatch.setattr(audit, "request_json", fake_request_json)
        monkeypatch.setattr(audit, "load_config", lambda: TW_CONFIG)
        monkeypatch.setattr(sr, "load_reporting_monday_config", lambda: MONDAY_CFG)
        monkeypatch.setattr(sm, "monday_graphql", monday)
        monkeypatch.setattr(sr, "datetime", FakeDateTime)
        monkeypatch.setattr(builtins, "input", lambda prompt="": (_ for _ in ()).throw(AssertionError("stdin read")))
        out_dir = tmp_path / f"out-{len(list(tmp_path.iterdir()))}"
        monkeypatch.setattr(sys, "argv", ["sync_reporting.py"] + argv + ["--output-dir", str(out_dir)])
        buf, code = io.StringIO(), None
        try:
            with contextlib.redirect_stdout(buf):
                sr.main()
        except SystemExit as e:
            code = e.code
        return buf.getvalue(), code, monday

    return run


# --- names -------------------------------------------------------------------

def test_january_through_december_format():
    expected = ["01 - January 2026", "02 - February 2026", "03 - March 2026", "04 - April 2026", "05 - May 2026",
                "06 - June 2026", "07 - July 2026", "08 - August 2026", "09 - September 2026", "10 - October 2026",
                "11 - November 2026", "12 - December 2026"]
    assert [sr.month_item_name(2026, m) for m in range(1, 13)] == expected
    assert sr.legacy_month_item_name(2026, 9) == "September 2026"
    assert sr.month_item_names(2026, 9) == ("09 - September 2026", "September 2026")


def test_lookup_accepts_numbered_or_legacy_and_nothing_else():
    items = [make_item("1", "09 - September 2026"), make_item("2", "  September 2026  "), make_item("3", "September 2025"),
             make_item("4", "09 - September 2025"), make_item("5", "08 - August 2026"), make_item("6", "September 2026 (old)")]
    assert [i["id"] for i in sr.find_month_matches(items, 2026, 9)] == ["1", "2"]
    assert [i["id"] for i in sr.find_month_matches(items[:1], 2026, 9)] == ["1"]
    assert [i["id"] for i in sr.find_month_matches(items[1:], 2026, 9)] == ["2"]
    assert sr.find_month_matches(items[2:], 2026, 9) == []


# --- nightly --current-month ---------------------------------------------------

def test_current_month_updates_existing_numbered_item(cli):
    out, code, monday = cli(["--mode", "update", "--current-month", "--yes"], ["08 - August 2026", "09 - September 2026"], (2026, 9))
    assert code is None, out
    assert monday.creates == [] and monday.group_lookups == 0
    assert monday.writes and {w["itemId"] for w in monday.writes} == {"501"}
    assert "All 1 month(s) updated and verified successfully." in out


def test_current_month_updates_existing_legacy_item(cli):
    out, code, monday = cli(["--mode", "update", "--current-month", "--yes"], ["September 2026"], (2026, 9))
    assert code is None, out
    assert monday.creates == [] and {w["itemId"] for w in monday.writes} == {"500"}


def test_current_month_both_names_is_a_duplicate_and_aborts(cli):
    out, code, monday = cli(["--mode", "update", "--current-month", "--yes"], ["09 - September 2026", "September 2026"], (2026, 9))
    assert code == 1
    assert "09 - September 2026: DUPLICATE -- 2 items found (id=500 '09 - September 2026', id=501 'September 2026')" in out
    assert monday.creates == [] and monday.writes == [] and monday.group_lookups == 0


def test_current_month_missing_october_creates_numbered_item(cli):
    out, code, monday = cli(["--mode", "update", "--current-month", "--yes"], ["09 - September 2026"], (2026, 10))
    assert code is None, out
    assert [c["itemName"] for c in monday.creates] == ["10 - October 2026"]
    assert monday.creates[0]["groupId"] == "group_2026"
    assert "will create '10 - October 2026'" in out
    assert "Verified exactly 1 item named '10 - October 2026'" in out


def test_current_month_legacy_october_is_not_recreated(cli):
    out, code, monday = cli(["--mode", "update", "--current-month", "--yes"], ["October 2026"], (2026, 10))
    assert code is None, out
    assert monday.creates == []


# --- historical / manual paths --------------------------------------------------

def test_month_range_matches_a_mix_of_legacy_and_numbered_names(cli):
    board = ["07 - July 2026", "August 2026"]
    out, code, monday = cli(["--mode", "update", "--year", "2026", "--month-range", "7-8", "--yes"], board, (2026, 9))
    assert code is None, out
    assert monday.creates == []
    assert {w["itemId"] for w in monday.writes} == {"500", "501"}


def test_month_range_missing_month_still_never_creates(cli):
    out, code, monday = cli(["--mode", "update", "--year", "2026", "--month-range", "7-8", "--yes"], ["07 - July 2026"], (2026, 9))
    assert code == 1
    assert "08 - August 2026: NOT FOUND -- no existing item named '08 - August 2026' or 'August 2026'" in out
    assert monday.creates == [] and monday.writes == []


def test_single_month_dry_run_matches_numbered_item_read_only(cli):
    out, code, monday = cli(["--mode", "dry-run", "--year", "2026", "--month", "9"], ["09 - September 2026"], (2026, 9))
    assert "Matched Monday item id='500' name='09 - September 2026'" in out
    assert monday.creates == [] and monday.writes == []


def test_single_month_dry_run_reports_duplicate_when_both_exist(cli):
    out, code, monday = cli(["--mode", "dry-run", "--year", "2026", "--month", "9"], ["09 - September 2026", "September 2026"], (2026, 9))
    assert "DUPLICATE: 2 items for '09 - September 2026' found" in out
    assert monday.creates == [] and monday.writes == []
