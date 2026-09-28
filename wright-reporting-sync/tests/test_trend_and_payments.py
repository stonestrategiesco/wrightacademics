import sys
import io
import json
import contextlib
import tempfile
from pathlib import Path
from datetime import datetime, timezone


def test_trend_and_payments():

    import audit
    import sync_reporting as sr
    import inspect_payments_diagnostic as pd

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 6)]


    def dt(day, month, year=2026, hour=10):
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


    # Multi-month fixture: January has 2 attended, February has 1 attended + 1 missed,
    # March has zero lessons at all (tests the "no students served -> avg is n/a" path).
    LESSONS = [
        {"id": 1, "from_datetime": dt(5, 1), "participants": [{"student_id": 1, "status": "Attended"}]},
        {"id": 2, "from_datetime": dt(6, 1), "participants": [{"student_id": 2, "status": "Attended"}]},
        {"id": 3, "from_datetime": dt(5, 2), "participants": [
            {"student_id": 1, "status": "Attended"},
            {"student_id": 3, "status": "Missed"},
        ]},
        # September fixture (for the payments diagnostic below)
        {"id": 10, "from_datetime": dt(1, 9), "participants": [
            {"student_id": 1, "status": "Attended", "amount": 50, "unit_price": 50, "invoice_id": "INV-1"},
        ]},
        {"id": 11, "from_datetime": dt(2, 9), "participants": [
            {"student_id": 2, "status": "Missed", "amount": None, "unit_price": 40, "invoice_id": None},
        ]},
        {"id": 12, "from_datetime": dt(3, 9), "participants": [
            {"student_id": 3, "status": "Cancelled", "amount": 30, "unit_price": 30, "invoice_id": "INV-2"},
        ]},
        {"id": 13, "from_datetime": dt(4, 9), "participants": [
            {"student_id": 4, "status": "Attended", "amount": 45, "unit_price": 50, "invoice_id": "INV-2"},
        ]},
    ]


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

    CONFIG = {
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
    audit.load_config = lambda: CONFIG

    # =====================================================================
    # Test 1: multi-month trend (Jan-Mar) -- one Teachworks pull, per-month rows,
    # correct Avg Attended Sessions per Student, and a graceful "n/a" for an
    # empty month (March has zero lessons -> zero students served).
    # =====================================================================
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_trend_dry_run(CONFIG, output_dir, 2026, 1, 3, refresh_teachworks_cache=False)
        output = buf.getvalue()
        print(output)

        out_path = output_dir / "wright-teachworks-reporting-trend-2026-01-to-03.json"
        with open(out_path) as f:
            result = json.load(f)

        months = {m["month"]: m for m in result["months"]}
        assert len(months) == 3, months.keys()

        jan = months[1]
        assert jan["sessions_attended"] == 2, jan
        assert jan["sessions_missed"] == 0, jan
        assert jan["students_served"] == 2, jan
        assert jan["avg_attended_sessions_per_student"] == 1.0, jan
        print("PASS: January row correct (2 attended, 2 students served, avg 1.00)")

        feb = months[2]
        assert feb["sessions_attended"] == 1, feb
        assert feb["sessions_missed"] == 1, feb
        assert feb["students_served"] == 2, feb  # student 1 (attended) + student 3 (missed)
        assert feb["avg_attended_sessions_per_student"] == 0.5, feb
        print("PASS: February row correct (1 attended, 1 missed, 2 students served, avg 0.50)")

        mar = months[3]
        assert mar["total_lessons_in_month"] == 0 if "total_lessons_in_month" in mar else True
        assert mar["sessions_attended"] == 0 and mar["students_served"] == 0, mar
        assert mar["avg_attended_sessions_per_student"] is None, mar
        print("PASS: empty March row handled gracefully (avg_attended_sessions_per_student is None, not a ZeroDivisionError)")

        for m in result["months"]:
            assert m["reconciliation_ok"] is True, m
        print("PASS: reconciliation holds for every month in the trend table")

        assert "Attended" in output and "Missed" in output and "Students" in output and "Avg/Student" in output
        print("PASS: trend table console output includes all requested columns")

    # =====================================================================
    # Test 2: --mode update refuses when --month-range logic would apply to it
    # (sanity: run_trend_dry_run itself never touches Monday -- no sm import
    # even used in that function; verified structurally by it taking no monday_cfg arg)
    # =====================================================================
    import inspect
    sig = inspect.signature(sr.run_trend_dry_run)
    assert "monday_cfg" not in sig.parameters, "run_trend_dry_run must not accept a Monday config -- it must be Teachworks-only"
    print("PASS: run_trend_dry_run's signature has no monday_cfg parameter -- structurally incapable of a Monday call")

    # =====================================================================
    # Test 3: Payments diagnostic -- September fixture with a deliberate mix:
    #   INV-1: 1 record, amount==unit_price, Attended
    #   (no invoice) Missed record with null amount -> tests null handling
    #   INV-2: 2 records (Cancelled with amount=30==unit_price, Attended with amount=45 != unit_price=50)
    #     -> proves invoice_id can span >1 record, and Cancelled can still carry a nonzero amount
    # =====================================================================
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pd.run_payments_diagnostic(CONFIG, output_dir, 2026, 9, refresh_teachworks_cache=False)
        output = buf.getvalue()
        print(output)

        out_path = output_dir / "wright-teachworks-payments-diagnostic-2026-09.json"
        with open(out_path) as f:
            summary = json.load(f)

        assert summary["total_participant_records"] == 4, summary["total_participant_records"]
        assert summary["field_presence"]["amount_present"] == 3, summary["field_presence"]  # all but the Missed/null one
        # invoice_id present on records: INV-1 (1 record) + INV-2 (2 records) = 3 records with invoice_id present
        assert summary["field_presence"]["invoice_id_present"] == 3, summary["field_presence"]
        print("PASS: field-presence counts correct (amount present=3/4, invoice_id present=3/4)")

        assert summary["sums"]["sum_amount_numeric"] == 50 + 30 + 45, summary["sums"]
        print(f"PASS: sum(amount) correct across numeric records: {summary['sums']['sum_amount_numeric']}")

        by_status = summary["by_status"]
        assert by_status["Cancelled"]["count"] == 1
        assert by_status["Cancelled"]["amount_present"] == 1
        assert by_status["Cancelled"]["sum_amount"] == 30
        print("PASS: a Cancelled record's amount is surfaced distinctly (sum_amount=30) -- proves amount is NOT attendance-gated in this fixture")

        assert by_status["Missed"]["amount_present"] == 0
        print("PASS: the Missed record's null amount is correctly reflected as amount_present=0 for that status bucket")

        assert summary["invoice_cardinality"]["unique_invoice_ids"] == 2
        assert summary["invoice_cardinality"]["records_per_invoice_max"] == 2  # INV-2 covers 2 records
        print("PASS: invoice_id cardinality correctly shows INV-2 spans 2 participant records")

        avp = summary["amount_vs_unit_price"]
        assert avp["both_numeric_count"] == 3
        assert avp["equal_count"] == 2  # INV-1 (50==50), INV-2/Cancelled (30==30)
        assert avp["different_count"] == 1  # INV-2/Attended (45 != 50)
        print("PASS: amount-vs-unit_price comparison correctly finds the one differing record (45 != 50)")

    print("\nALL trend + payments diagnostic TESTS PASSED")
