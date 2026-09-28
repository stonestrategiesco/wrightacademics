import sys
import io
import json
import contextlib
import tempfile
from pathlib import Path
from decimal import Decimal


def test_financials():

    import audit
    import sync_reporting as sr

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 8)]


    def dt(day, month=9, year=2026, hour=10):
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


    # =====================================================================
    # Fixture 1: a classic float-rounding trap. 0.10 + 0.10 + 0.10 in IEEE-754
    # float sums to 0.30000000000000004, not 0.30 -- three Attended records at
    # $10.10 each must sum to EXACTLY $30.30 via Decimal.
    # =====================================================================
    FLOAT_TRAP_LESSONS = [
        {"id": 1, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended", "amount": "10.10"}]},
        {"id": 2, "from_datetime": dt(2), "participants": [{"student_id": 2, "status": "Attended", "amount": "10.10"}]},
        {"id": 3, "from_datetime": dt(3), "participants": [{"student_id": 3, "status": "Attended", "amount": "10.10"}]},
    ]

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

    # Sanity-check the trap actually exists in plain float arithmetic, so this
    # test is proving something real, not a strawman.
    assert 0.10 + 0.10 + 0.10 != 0.30, "expected the classic float-rounding artifact to be present in this Python build"

    fin = sr.compute_monthly_financials(CONFIG, FLOAT_TRAP_LESSONS, 2026, 9)
    assert fin["attended_amount"] == "30.30", fin["attended_amount"]
    assert fin["total_amount"] == "30.30", fin["total_amount"]
    print(f"PASS: three $10.10 Attended records sum to exactly $30.30 via Decimal (float would give {0.10+0.10+0.10!r})")

    # =====================================================================
    # Fixture 2: full mixed-status fixture -- Attended, Missed, Cancelled,
    # Scheduled, Unclassified (null status), and a non-numeric amount string --
    # proves per-bucket sums, Total Amount excludes Unclassified, null/non-numeric
    # count is correct, and financial reconciliation includes Unclassified.
    # =====================================================================
    MIXED_LESSONS = [
        {"id": 10, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended", "amount": "50.00"}]},
        {"id": 11, "from_datetime": dt(2), "participants": [{"student_id": 2, "status": "Missed", "amount": "25.50"}]},
        {"id": 12, "from_datetime": dt(3), "participants": [{"student_id": 3, "status": "Cancelled", "amount": "30.00"}]},
        {"id": 13, "from_datetime": dt(4), "participants": [{"student_id": 4, "status": "Scheduled", "amount": "40.00"}]},
        {"id": 14, "from_datetime": dt(5), "participants": [{"student_id": 5, "amount": "15.00"}]},  # null status -> Unclassified
        {"id": 15, "from_datetime": dt(6), "participants": [{"student_id": 6, "status": "Attended", "amount": None}]},  # null amount
        {"id": 16, "from_datetime": dt(7), "participants": [{"student_id": 7, "status": "Attended", "amount": "not-a-number"}]},  # non-numeric
    ]

    fin2 = sr.compute_monthly_financials(CONFIG, MIXED_LESSONS, 2026, 9)
    assert fin2["attended_amount"] == "50.00", fin2["attended_amount"]  # only the one parseable Attended amount counts
    assert fin2["missed_amount"] == "25.50", fin2["missed_amount"]
    assert fin2["cancelled_amount"] == "30.00", fin2["cancelled_amount"]
    assert fin2["scheduled_amount"] == "40.00", fin2["scheduled_amount"]
    assert fin2["unclassified_amount"] == "15.00", fin2["unclassified_amount"]
    # Total Amount = Attended+Missed+Cancelled+Scheduled only, Unclassified excluded
    assert fin2["total_amount"] == "145.50", fin2["total_amount"]  # 50 + 25.50 + 30 + 40
    print("PASS: per-status sums correct; Total Amount excludes Unclassified (145.50, not 160.50)")

    assert fin2["null_or_non_numeric_amount_count"] == 2, fin2  # the null amount + the "not-a-number" amount
    assert fin2["total_participant_records_considered"] == 7, fin2
    print("PASS: null and non-numeric amounts are both counted and excluded from every sum (count=2 of 7)")

    # Reconciliation includes Unclassified: 50 + 25.50 + 30 + 40 + 15 = 160.50
    assert fin2["financial_reconciliation_sum"] == "160.50", fin2["financial_reconciliation_sum"]
    assert fin2["financial_reconciliation_ok"] is True
    print("PASS: financial reconciliation (including Unclassified) == 160.50, matching the full participant-amount total")

    # =====================================================================
    # Fixture 3: out-of-month lessons must be fully excluded from financials too.
    # =====================================================================
    OUT_OF_MONTH_LESSONS = [
        {"id": 20, "from_datetime": dt(15, month=8), "participants": [{"student_id": 1, "status": "Attended", "amount": "999.00"}]},
        {"id": 21, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended", "amount": "12.34"}]},
    ]
    fin3 = sr.compute_monthly_financials(CONFIG, OUT_OF_MONTH_LESSONS, 2026, 9)
    assert fin3["attended_amount"] == "12.34", fin3["attended_amount"]
    assert fin3["total_participant_records_considered"] == 1, fin3
    print("PASS: an August lesson's $999.00 is fully excluded from the September financial totals")

    # =====================================================================
    # Test 4: rounding uses ROUND_HALF_UP at the final quantize step only --
    # three records of $0.005 each (a fractional-cent input) sum to $0.015
    # before rounding, and must round to $0.02, not $0.01 (proves quantization
    # happens once, on the accumulated Decimal total, not per-record).
    # =====================================================================
    HALF_CENT_LESSONS = [
        {"id": 30, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended", "amount": "0.005"}]},
        {"id": 31, "from_datetime": dt(2), "participants": [{"student_id": 2, "status": "Attended", "amount": "0.005"}]},
        {"id": 32, "from_datetime": dt(3), "participants": [{"student_id": 3, "status": "Attended", "amount": "0.005"}]},
    ]
    fin4 = sr.compute_monthly_financials(CONFIG, HALF_CENT_LESSONS, 2026, 9)
    assert Decimal(fin4["attended_amount"]) == Decimal("0.02"), fin4["attended_amount"]
    print(f"PASS: 3x $0.005 sums to $0.015 pre-rounding, quantized ONCE at the end to $0.02 (got ${fin4['attended_amount']})")

    # =====================================================================
    # Test 5: the trend dry-run integrates financials alongside session counts,
    # for a Jan-Sep range, with September's incomplete-month warning intact.
    # =====================================================================
    def fake_request_json(config, path, params=None, max_retries=3):
        class R:
            url = "https://fake"
            status_code = 200

        page = params.get("page", 1)
        if path == config["students_path"]:
            return (STUDENTS if page == 1 else []), R()
        if path == config["lessons_path"]:
            return (MIXED_LESSONS if page == 1 else []), R()
        raise AssertionError(f"unexpected path {path}")


    audit.request_json = fake_request_json
    audit.load_config = lambda: CONFIG

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_trend_dry_run(CONFIG, output_dir, 2026, 1, 9, refresh_teachworks_cache=False)
        output = buf.getvalue()

        out_path = output_dir / "wright-teachworks-reporting-trend-2026-01-to-09.json"
        with open(out_path) as f:
            result = json.load(f)

        sept_row = next(r for r in result["months"] if r["month"] == 9)
        assert sept_row["attended_amount"] == "50.00", sept_row
        assert sept_row["total_amount"] == "145.50", sept_row
        assert "financial_reconciliation_ok" in sept_row
        print("PASS: trend JSON output includes financial fields per month, September computed correctly")

        assert "Amounts: Attended=$" in output
        assert "has not finished yet" in output  # existing incomplete-month warning, untouched
        print("PASS: trend console output shows the Amounts line per month, and September's incomplete-month warning is intact")

    print("\nALL financial aggregation TESTS PASSED")
