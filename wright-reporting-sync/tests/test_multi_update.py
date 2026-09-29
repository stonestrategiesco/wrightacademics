import sys
import io
import json
import builtins
import contextlib
import tempfile
from pathlib import Path


def test_multi_update():

    import audit
    import sync_monday as sm
    import sync_reporting as sr

    # --- Teachworks fixture: Jan-Aug 2026, one attended lesson per month so every
    # month has a clean, distinct, non-zero Sessions Attended count (month N has
    # N attended lessons -- easy to eyeball in assertions). Each attended
    # participant carries amount="10.00", so month N's Attended Amount is
    # exactly $10.00 * N. No Missed/Cancelled/Scheduled/Unclassified noise here;
    # that logic (and the financial classification itself) is already covered
    # by test_financials.py.
    LESSONS = []
    lesson_id = 1
    for month in range(1, 9):
        for i in range(month):  # month 1 -> 1 lesson, month 8 -> 8 lessons
            LESSONS.append({
                "id": lesson_id,
                "from_datetime": f"2026-{month:02d}-{(i % 27) + 1:02d}T10:00:00Z",
                "participants": [{"student_id": (i % 3) + 1, "status": "Attended", "amount": "10.00"}],
            })
            lesson_id += 1

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 4)]


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
        "column_avg_sessions_per_student": "numeric_mm7mg9ve",
        "column_attended_amount": "numeric_mm7mymrr",
        "column_missed_amount": "numeric_mm7m84fd",
        "column_cancelled_amount": "numeric_mm7m2mg5",
        "column_scheduled_amount": "numeric_mm7m7y3x",
        "column_total_amount": "numeric_mm7mvmtn",
    }


    def cv(col_id, text):
        return {"id": col_id, "text": text, "value": json.dumps(text)}


    def make_item(item_id, name, attended=0, missed=0, total=0, served=0, avg="", last_updated="",
                  attended_amount="0.00", missed_amount="0.00", cancelled_amount="0.00",
                  scheduled_amount="0.00", total_amount="0.00"):
        return {
            "id": item_id, "name": name,
            "column_values": [
                cv(MONDAY_CFG["column_sessions_attended"], str(attended)),
                cv(MONDAY_CFG["column_sessions_missed"], str(missed)),
                cv(MONDAY_CFG["column_total_sessions"], str(total)),
                cv(MONDAY_CFG["column_students_served"], str(served)),
                cv(MONDAY_CFG["column_last_updated"], last_updated),
                cv(MONDAY_CFG["column_avg_sessions_per_student"], str(avg)),
                cv(MONDAY_CFG["column_attended_amount"], attended_amount),
                cv(MONDAY_CFG["column_missed_amount"], missed_amount),
                cv(MONDAY_CFG["column_cancelled_amount"], cancelled_amount),
                cv(MONDAY_CFG["column_scheduled_amount"], scheduled_amount),
                cv(MONDAY_CFG["column_total_amount"], total_amount),
            ],
        }


    def board_schema_response(omit_config_keys=(), rename={}):
        """A valid live-board schema response matching every column id in
        MONDAY_CFG, used by the board-schema preflight (sm.BOARD_COLUMNS_QUERY).
        omit_config_keys simulates a column id that doesn't exist on the board
        (the exact live failure); rename simulates a column that exists but
        under an unexpected title."""
        columns = []
        for config_key, (title, col_type) in sr.EXPECTED_REPORTING_COLUMNS.items():
            if config_key in omit_config_keys:
                continue
            columns.append({
                "id": MONDAY_CFG[config_key],
                "title": rename.get(config_key, title),
                "type": col_type,
            })
        return {"boards": [{"id": MONDAY_CFG["board_id"], "name": "Teachworks Reporting", "columns": columns}]}


    MONTH_NAMES_1_8 = [sr.month_item_name(2026, m) for m in range(1, 9)]

    orig_input = builtins.input
    orig_compute_monthly_financials = sr.compute_monthly_financials


    def run_multi(monday_graphql_fn, start=1, end=8, confirm_answer="YES", allow_incomplete=False):
        sm.monday_graphql = monday_graphql_fn
        builtins.input = lambda prompt="": confirm_answer
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            buf = io.StringIO()
            exit_code = None
            try:
                with contextlib.redirect_stdout(buf):
                    sr.run_multi_update(TW_CONFIG, MONDAY_CFG, output_dir, 2026, start, end, refresh_teachworks_cache=False, allow_incomplete_month=allow_incomplete)
            except SystemExit as e:
                exit_code = e.code
            output = buf.getvalue()
            log_files = list(output_dir.glob("wright-teachworks-reporting-multi-update-*.json"))
            log = json.loads(log_files[0].read_text()) if log_files else None
            return output, exit_code, log


    # =====================================================================
    # Test 1: missing month (July 2026 item doesn't exist on the board) ->
    # aborts the ENTIRE batch, writes NOTHING (no set_monday_column_value calls at all).
    # =====================================================================
    write_calls = []


    def graphql_missing_july(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item(str(100 + i), name) for i, name in enumerate(MONTH_NAMES_1_8) if name != sr.month_item_name(2026, 7)]
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:60]}")


    write_calls.clear()
    output, exit_code, log = run_multi(graphql_missing_july)
    assert exit_code == 1, exit_code
    assert "Board-schema preflight OK" in output, output
    assert "July 2026: NOT FOUND" in output, output
    assert "Aborting the ENTIRE multi-month update" in output
    assert write_calls == [], f"must write nothing when any month is missing: {write_calls}"
    assert log is None, "no update log should be written when the batch is aborted pre-write"
    builtins.input = orig_input
    print("PASS: a missing month (NOT FOUND) aborts the entire batch, zero writes made")

    # =====================================================================
    # Test 2: duplicate month ("03 - March 2026" plus a legacy "March 2026") -> aborts
    # the ENTIRE batch, writes NOTHING.
    # =====================================================================
    write_calls.clear()


    def graphql_duplicate_march(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item(str(100 + i), name) for i, name in enumerate(MONTH_NAMES_1_8)]
            items.append(make_item("999", "March 2026"))  # legacy-named copy of the same month -> duplicate
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_multi(graphql_duplicate_march)
    assert exit_code == 1, exit_code
    assert "March 2026: DUPLICATE" in output, output
    assert "Aborting the ENTIRE multi-month update" in output
    assert write_calls == [], f"must write nothing when any month is duplicated: {write_calls}"
    builtins.input = orig_input
    print("PASS: a duplicated month (DUPLICATE) aborts the entire batch, zero writes made")

    # =====================================================================
    # Test 3: all 8 months matched cleanly, but the user does NOT type YES ->
    # aborts, writes NOTHING.
    # =====================================================================
    write_calls.clear()


    def graphql_clean_board(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items" in query and "itemIds" in query:
            # single-item read-back query (verification) -- not expected to be hit in this test
            raise AssertionError("must not reach verification when aborted before writing")
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item(str(100 + i), name) for i, name in enumerate(MONTH_NAMES_1_8)]
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_multi(graphql_clean_board, confirm_answer="nope")
    assert exit_code == 0, exit_code
    assert "Aborted -- nothing was written to Monday." in output
    assert write_calls == [], f"must write nothing without typed YES: {write_calls}"
    builtins.input = orig_input
    print("PASS: declining the single typed-YES confirmation aborts the whole run, zero writes made")

    # =====================================================================
    # Test 4: current-month protection -- including September (2026-09, in
    # progress per the sandbox clock) in the range refuses the WHOLE batch
    # before any Monday call is even made (including the schema preflight),
    # unless --allow-incomplete-month.
    # =====================================================================
    calls_made = []


    def graphql_should_never_be_called(cfg, query, variables=None, max_retries=3):
        calls_made.append(1)
        raise AssertionError("must not touch Monday at all when an incomplete month is in range")


    calls_made.clear()
    output, exit_code, log = run_multi(graphql_should_never_be_called, start=1, end=9)
    assert exit_code == 1, exit_code
    assert "has/have not finished yet" in output, output
    assert "September 2026" in output
    assert calls_made == [], "must not call Monday before the incomplete-month guard"
    builtins.input = orig_input
    print("PASS: an in-progress month (September) in the range refuses the whole batch before any Monday call")

    # =====================================================================
    # Test 5: an invalid/missing configured column id (simulating exactly the
    # live failure: Total Amount's id doesn't exist on the board) is caught by
    # the board-schema preflight -- aborts before any items fetch or mutation.
    # This is the core regression test for the bug that hit live: previously
    # nothing checked the board's REAL column list, so a stale Total Amount id
    # passed silently through fetch_reporting_items (whose column_values(ids:
    # [...]) tolerates an unknown id) and only failed at the write mutation,
    # after the four other financial fields had already been written.
    # =====================================================================
    def graphql_invalid_total_amount_column(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response(omit_config_keys=["column_total_amount"])
        raise AssertionError(f"must not make any other Monday call once the schema preflight fails: {query[:60]}")


    output, exit_code, log = run_multi(graphql_invalid_total_amount_column)
    assert exit_code == 1, (exit_code, output)
    assert "board-schema preflight failed" in output, output
    assert "Total Amount" in output and MONDAY_CFG["column_total_amount"] in output, output
    assert "does not exist on board" in output, output
    assert log is None, "no update log should be written when the schema preflight aborts the batch"
    builtins.input = orig_input
    print("PASS: an invalid configured column id (Total Amount) is caught by the board-schema preflight -- zero items fetches, zero mutations")

    # Direct unit test of verify_board_schema: a column that EXISTS but under
    # the WRONG title (e.g. its id got silently repointed to a different column)
    # must also be flagged -- not just an outright-missing id.
    sm.monday_graphql = lambda cfg, query, variables=None, max_retries=3: board_schema_response(
        rename={"column_total_amount": "Some Other Column"}
    )
    required_keys = list(sr.REPORTING_FIELD_COLUMN_KEY.values()) + ["column_last_updated"]
    problems = sr.verify_board_schema(MONDAY_CFG, required_keys)
    assert any("Total Amount" in p and "Some Other Column" in p for p in problems), problems
    print("PASS: verify_board_schema also catches a column id that exists but under the wrong title, not just a missing id")

    # =====================================================================
    # Test 6: financial reconciliation failure aborts the ENTIRE batch before
    # any items fetch or mutation -- simulated by monkeypatching
    # compute_monthly_financials to report a broken reconciliation for July.
    # The board-schema preflight (a legitimate read-only check) DOES run first
    # and succeeds; what must never happen afterward is an items fetch or write.
    # =====================================================================
    calls_made.clear()


    def broken_financials_for_july(config, lessons, year, month):
        fin = orig_compute_monthly_financials(config, lessons, year, month)
        if month == 7:
            fin = dict(fin)
            fin["financial_reconciliation_ok"] = False
        return fin


    def graphql_schema_ok_then_nothing_else(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        calls_made.append(1)
        raise AssertionError("must not fetch items or write when financial reconciliation fails")


    sr.compute_monthly_financials = broken_financials_for_july
    try:
        output, exit_code, log = run_multi(graphql_schema_ok_then_nothing_else, start=1, end=8)
    finally:
        sr.compute_monthly_financials = orig_compute_monthly_financials
    assert exit_code == 1, exit_code
    assert "Board-schema preflight OK" in output, output
    assert "financial reconciliation failed for: 07 - July 2026" in output, output
    assert "Nothing was written to Monday." in output
    assert calls_made == [], "must not fetch items or write when financial reconciliation fails"
    assert log is None
    builtins.input = orig_input
    print("PASS: a financial reconciliation failure aborts the entire batch after a successful schema preflight but before any items fetch or write")

    # =====================================================================
    # Test 7: successful 1-8 update -- writes exactly the expected KPI fields +
    # Avg Sessions/Student + all 5 financial fields + Last Updated for all 8
    # months, verifies each via a read-back, and logs success for every month.
    # =====================================================================
    write_calls.clear()
    written_state = {}  # item_id -> {column_id: value}, used to serve the read-back verification


    def graphql_full_success(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item(str(100 + i), name) for i, name in enumerate(MONTH_NAMES_1_8)]
            for it in items:
                written_state[it["id"]] = {cv["id"]: cv["text"] for cv in it["column_values"]}
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            written_state.setdefault(variables["itemId"], {})[variables["columnId"]] = variables["value"]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            item_id = variables["itemIds"][0]
            state = written_state.get(item_id, {})
            return {"items": [{
                "id": item_id, "name": "verify",
                "column_values": [{"id": k, "text": v, "value": json.dumps(v)} for k, v in state.items()],
            }]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_multi(graphql_full_success)
    assert exit_code is None, (exit_code, output)
    assert "Board-schema preflight OK" in output, output
    assert "All 8 month(s) updated and verified successfully." in output, output
    assert log is not None
    assert len(log["months"]) == 8
    for i, row in enumerate(log["months"], start=1):
        assert row["success"] is True, row
        assert row["verify_status"] == "OK", row
        assert row["fields_written"]["sessions_attended"] == str(i), row  # month i has i attended lessons
        assert "avg_sessions_per_student" in row["fields_written"], row
        # attended/total amount actually change from the "0.00" starting fixture
        # value, so they're written; missed/cancelled/scheduled correctly have
        # NO diff to write (fixture has none of those statuses, so both current
        # and new are "0.00") -- diff_reporting_fields correctly omits a
        # no-op field from fields_written. The read-back "after" state is the
        # authoritative check that ALL 5 financial columns hold the right value
        # regardless of whether each one needed an actual write this run.
        assert "attended_amount" in row["fields_written"], row
        assert "total_amount" in row["fields_written"], row
        for fin_key in ("attended_amount", "missed_amount", "cancelled_amount", "scheduled_amount", "total_amount"):
            assert fin_key in row["after"], (fin_key, row)
        assert row["after"]["missed_amount"] == "0.00", row
        assert row["after"]["cancelled_amount"] == "0.00", row
        assert row["after"]["scheduled_amount"] == "0.00", row
    print("PASS: a clean 1-8 update writes every month's KPI fields + Avg Sessions/Student + the financial fields that actually changed, and the read-back confirms all 5 financial columns hold the correct value")

    # Spot-check the actual written column values for January (1 attended lesson
    # @ $10.00 -> Attended Amount = Total Amount = $10.00, Missed/Cancelled/
    # Scheduled = $0.00) and August (8 attended lessons @ $10.00 -> $80.00).
    jan_written = {c["columnId"]: c["value"] for c in write_calls if c["itemId"] == "100"}
    assert jan_written[MONDAY_CFG["column_sessions_attended"]] == "1"
    assert jan_written[MONDAY_CFG["column_students_served"]] == "1"
    assert jan_written[MONDAY_CFG["column_avg_sessions_per_student"]] == "1.0"
    assert jan_written[MONDAY_CFG["column_attended_amount"]] == "10.00"
    assert jan_written[MONDAY_CFG["column_total_amount"]] == "10.00"
    # Missed/Cancelled/Scheduled amount had no diff to write (fixture's current
    # AND new value are both "0.00") -- confirmed correct via the read-back
    # "after" state above instead, not via write_calls.
    print("PASS: January's written values are numerically correct (1 attended, 1 served, avg 1.0, Attended/Total Amount $10.00)")

    aug_written = {c["columnId"]: c["value"] for c in write_calls if c["itemId"] == "107"}
    assert aug_written[MONDAY_CFG["column_sessions_attended"]] == "8"
    assert aug_written[MONDAY_CFG["column_students_served"]] == "3"
    assert aug_written[MONDAY_CFG["column_avg_sessions_per_student"]] == "2.67"
    assert aug_written[MONDAY_CFG["column_attended_amount"]] == "80.00"
    assert aug_written[MONDAY_CFG["column_total_amount"]] == "80.00"
    print("PASS: August's written values are numerically correct (8 attended, 3 served, avg 2.67, Attended/Total Amount $80.00)")

    builtins.input = orig_input

    # =====================================================================
    # Test 8: read-back verification actually catches a financial mismatch --
    # simulate Monday silently returning a DIFFERENT Attended Amount than what
    # was written (e.g. a partial/lagged write) and confirm verify_status
    # reports the mismatch and the month is marked FAILED, not SUCCESS.
    # =====================================================================
    write_calls.clear()
    written_state.clear()


    def graphql_financial_mismatch_on_readback(cfg, query, variables=None, max_retries=3):
        if query == sm.BOARD_COLUMNS_QUERY:
            return board_schema_response()
        if "items_page" in query and "next_items_page" not in query:
            items = [make_item(str(100 + i), name) for i, name in enumerate(MONTH_NAMES_1_8)]
            for it in items:
                written_state[it["id"]] = {cv["id"]: cv["text"] for cv in it["column_values"]}
            return {"boards": [{"items_page": {"cursor": None, "items": items}}]}
        if "change_simple_column_value" in query:
            write_calls.append(variables)
            # Deliberately do NOT apply the Attended Amount write for item 100
            # (January) to written_state, simulating Monday not actually
            # persisting that one field -- every other field/item applies normally.
            if variables["itemId"] == "100" and variables["columnId"] == MONDAY_CFG["column_attended_amount"]:
                pass
            else:
                written_state.setdefault(variables["itemId"], {})[variables["columnId"]] = variables["value"]
            return {"change_simple_column_value": {"id": variables["itemId"]}}
        if "items (ids" in query or "itemIds" in query:
            item_id = variables["itemIds"][0]
            state = written_state.get(item_id, {})
            return {"items": [{
                "id": item_id, "name": "verify",
                "column_values": [{"id": k, "text": v, "value": json.dumps(v)} for k, v in state.items()],
            }]}
        raise AssertionError(f"unexpected query: {query[:60]}")


    output, exit_code, log = run_multi(graphql_financial_mismatch_on_readback)
    assert exit_code == 1, (exit_code, output)  # overall run reports failure since one month failed
    jan_row = next(r for r in log["months"] if r["month_label"] == sr.month_item_name(2026, 1))
    assert jan_row["success"] is False, jan_row
    assert "attended_amount" in jan_row["verify_status"], jan_row["verify_status"]
    assert "MISMATCH" in jan_row["verify_status"], jan_row["verify_status"]
    other_rows = [r for r in log["months"] if r["month_label"] != sr.month_item_name(2026, 1)]
    assert all(r["success"] for r in other_rows), other_rows
    print(f"PASS: read-back verification catches a financial-field mismatch (January verify_status={jan_row['verify_status']!r}), other months still SUCCEED")

    builtins.input = orig_input

    # =====================================================================
    # Test 9: Payments has no column config, no field mapping, and is never a
    # key anywhere in the actual write/read machinery (checked structurally,
    # not by grepping comments -- the docstrings legitimately say "Payments is
    # never touched", which would false-positive a raw text search).
    # =====================================================================
    structures_to_check = {
        "MONDAY_REPORTING_ENV_DEFAULTS": sr.MONDAY_REPORTING_ENV_DEFAULTS,
        "REPORTING_FIELD_COLUMN_KEY": sr.REPORTING_FIELD_COLUMN_KEY,
        "REPORTING_FIELD_LABELS": sr.REPORTING_FIELD_LABELS,
        "EXPECTED_REPORTING_COLUMNS": sr.EXPECTED_REPORTING_COLUMNS,
        "MULTI_MONTH_ONLY_COLUMNS (as a dict keyed by itself)": {c: c for c in sr.MULTI_MONTH_ONLY_COLUMNS},
    }
    for name, d in structures_to_check.items():
        for key, value in d.items():
            haystack = f"{key} {value}".lower()
            assert "payment" not in haystack, f"found a Payments-related key/value in {name}: {key!r} -> {value!r}"
    print("PASS: no Payments key or value exists in any of the column-config/field-mapping/schema data structures")

    # And structurally: the set of fields the multi-month update can ever write
    # is exactly the 10 KPI/financial fields -- Payments is not among them.
    all_writable_fields = set(sr.REPORTING_FIELD_COLUMN_KEY.keys())
    assert "payments" not in {f.lower() for f in all_writable_fields}
    assert all_writable_fields == {
        "sessions_attended", "sessions_missed", "total_sessions", "students_served",
        "avg_sessions_per_student", "attended_amount", "missed_amount",
        "cancelled_amount", "scheduled_amount", "total_amount",
    }, all_writable_fields
    print("PASS: the complete set of writable fields is exactly the 10 session/financial fields -- Payments is not among them")

    print("\nALL multi-month update TESTS PASSED")
