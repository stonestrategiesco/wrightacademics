import sys
import io
import json
import contextlib
import tempfile
from pathlib import Path


def test_sync_reporting_phase2():

    import audit
    import sync_monday as sm
    import sync_reporting as sr

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 3)]


    def dt(day, month=9, year=2026, hour=10):
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


    LESSONS = [
        {"id": 1, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended"}]},
        {"id": 2, "from_datetime": dt(2), "participants": [
            {"student_id": 2, "status": "Attended"},
            {"student_id": 1, "status": "Missed"},
        ]},
    ]
    # Expected: Attended=2, Missed=1, Total=3, StudentsServed=2


    def fake_request_json(config, path, params=None, max_retries=3):
        class R:
            url = "https://fake"
            status_code = 200

        page = params.get("page", 1)
        if path == config["students_path"]:
            return (STUDENTS if page == 1 else []), R()
        if path == config["lessons_path"]:
            return (LESSONS if page == 1 else []), R()
        raise AssertionError(f"unexpected path {path}")


    audit.request_json = fake_request_json

    TW_CONFIG = {
        "base_url": "https://fake", "api_key": "fake", "auth_header": "Authorization",
        "auth_scheme": "Token token={key}", "students_path": "/students", "lessons_path": "/lessons",
        "tutors_path": "", "page_param": "page", "per_page_param": "per_page", "response_data_key": "",
        "lesson_start_date_param": "", "lesson_end_date_param": "", "student_id_param": "student_id",
        "test_student_limit": 5, "test_lesson_limit": 20, "test_lesson_lookback_days": 30,
        "per_page": 80, "max_pages": 30, "request_delay_seconds": 0, "status_field": "status",
        "attended_status_value": "Attended", "student_id_field": "id", "student_name_field": "",
        "student_first_name_field": "", "student_last_name_field": "", "lesson_date_field": "",
        "lesson_tutor_id_field": "", "lesson_tutor_name_field": "", "tutor_record_id_field": "id",
        "tutor_record_name_field": "", "participants_field": "participants", "participant_sample_size": 20,
        "participant_id_field": "", "participant_type_field": "", "participant_student_type_value": "student",
    }
    audit.load_config = lambda: TW_CONFIG

    MONDAY_CFG = {
        "api_url": "https://fake-monday", "api_token": "fake-token", "auth_header": "Authorization",
        "auth_scheme": "{key}", "api_version": "", "items_page_size": 100,
        "board_id": "18432993218",
        "column_sessions_attended": "numeric_mm7mrpe4",
        "column_sessions_missed": "numeric_mm7mh4qd",
        "column_total_sessions": "numeric_mm7me68f",
        "column_students_served": "numeric_mm7mhsf3",
        "column_last_updated": "date_mm7m4799",
    }


    def make_item(item_id, name, sessions_attended, sessions_missed, total_sessions, students_served, last_updated=""):
        def cv(col_id, text):
            return {"id": col_id, "text": text, "value": json.dumps(text)}
        return {
            "id": item_id, "name": name,
            "column_values": [
                cv(MONDAY_CFG["column_sessions_attended"], str(sessions_attended)),
                cv(MONDAY_CFG["column_sessions_missed"], str(sessions_missed)),
                cv(MONDAY_CFG["column_total_sessions"], str(total_sessions)),
                cv(MONDAY_CFG["column_students_served"], str(students_served)),
                cv(MONDAY_CFG["column_last_updated"], last_updated),
            ],
        }


    # =====================================================================
    # Test 1: dry-run preview -- MATCHED item, stale values -> diffs reported, NO writes made.
    # =====================================================================
    write_calls = []


    def fake_monday_graphql_matched(cfg, query, variables=None, max_retries=3):
        if "items_page" in query and "next_items_page" not in query:
            item = make_item("111", "September 2026", 0, 0, 0, 0, "")
            return {"boards": [{"items_page": {"cursor": None, "items": [item]}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:50]}")


    sm.monday_graphql = fake_monday_graphql_matched

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_dry_run(TW_CONFIG, output_dir, 2026, 9, refresh_teachworks_cache=False, monday_cfg=MONDAY_CFG)
        output = buf.getvalue()

        assert write_calls == [], f"dry-run must NEVER write to Monday, but got: {write_calls}"
        assert "Matched Monday item id='111'" in output
        assert "Sessions Attended: '0' -> '2'" in output
        assert "Sessions Missed: '0' -> '1'" in output
        assert "Total Sessions: '0' -> '3'" in output
        assert "Students Served: '0' -> '2'" in output
        print("PASS: dry-run preview shows exact diffs for a MATCHED item and makes zero Monday writes")

        out_path = output_dir / "wright-teachworks-reporting-dry-run-2026-09.json"
        with open(out_path) as f:
            result = json.load(f)
        assert result["monday_preview"]["status"] == "MATCHED"
        assert result["monday_preview"]["item_id"] == "111"
        assert result["monday_preview"]["diffs"]["sessions_attended"] == {"current": "0", "new": "2"}
        print("PASS: dry-run JSON output records the Monday preview status/diffs")

    # =====================================================================
    # Test 2: update mode -- MATCHED item, confirmed with typed YES -> writes exactly the diffs + Last Updated.
    # =====================================================================
    write_calls.clear()
    import builtins
    orig_input = builtins.input
    builtins.input = lambda prompt="": "YES"

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, 9, refresh_teachworks_cache=False)
        output = buf.getvalue()

        written_columns = {c["columnId"]: c["value"] for c in write_calls}
        assert written_columns[MONDAY_CFG["column_sessions_attended"]] == "2"
        assert written_columns[MONDAY_CFG["column_sessions_missed"]] == "1"
        assert written_columns[MONDAY_CFG["column_total_sessions"]] == "3"
        assert written_columns[MONDAY_CFG["column_students_served"]] == "2"
        assert MONDAY_CFG["column_last_updated"] in written_columns
        assert "Update complete." in output
        print(f"PASS: update mode (after typed YES) writes exactly the 4 KPI fields + Last Updated: {written_columns}")

        log_path = output_dir / "wright-teachworks-reporting-update-2026-09.json"
        with open(log_path) as f:
            log = json.load(f)
        assert log["errors"] == []
        assert log["fields_written"] == {
            "sessions_attended": "2", "sessions_missed": "1", "total_sessions": "3", "students_served": "2",
        }
        print("PASS: update log JSON records exactly what was written, no errors")

    builtins.input = orig_input

    # =====================================================================
    # Test 3: update mode refuses any month other than September 2026 (Phase 2 restriction) -- no Monday calls at all.
    # =====================================================================
    write_calls.clear()
    calls_made = []
    sm.monday_graphql = lambda *a, **k: calls_made.append(1) or (_ for _ in ()).throw(AssertionError("should never call Monday"))

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sr.run_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, 8, refresh_teachworks_cache=False)
            raise AssertionError("expected SystemExit for non-September-2026 update")
        except SystemExit as e:
            assert e.code == 1
        output = buf.getvalue()
        assert "restricts writes to September 2026 ONLY" in output
        assert calls_made == [], "must not touch Monday at all before the month check"
        print("PASS: update mode refuses (year, month) != (2026, 9) before making any Monday call")

    # =====================================================================
    # Test 4: update mode aborts cleanly on NOT_FOUND (no item named "September 2026") -- never creates one.
    # =====================================================================
    def fake_monday_graphql_not_found(cfg, query, variables=None, max_retries=3):
        if "items_page" in query and "next_items_page" not in query:
            return {"boards": [{"items_page": {"cursor": None, "items": []}}]}
        raise AssertionError("must not attempt to write/create when NOT_FOUND")


    sm.monday_graphql = fake_monday_graphql_not_found
    builtins.input = lambda prompt="": (_ for _ in ()).throw(AssertionError("must never prompt to confirm a write when NOT_FOUND"))

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sr.run_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, 9, refresh_teachworks_cache=False)
            raise AssertionError("expected SystemExit for NOT_FOUND")
        except SystemExit as e:
            assert e.code == 1
        output = buf.getvalue()
        assert "NOT FOUND" in output
        assert "Aborting -- nothing was written to Monday" in output
        print("PASS: update mode aborts cleanly on NOT_FOUND, never creates an item, never prompts")

    builtins.input = orig_input

    print("\nALL Phase 2 (Monday preview/update) TESTS PASSED")
