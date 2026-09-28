import sys
import io
import json
import contextlib
import tempfile
from pathlib import Path
from datetime import datetime, timezone


def test_sync_reporting():

    import audit
    import sync_reporting as sr

    STUDENTS = [{"id": i, "first_name": f"S{i}", "last_name": "T"} for i in range(1, 8)]

    def dt(day, month=9, year=2026, hour=10):
        return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"

    LESSONS = [
        # September, single attended participant
        {"id": 1, "from_datetime": dt(1), "participants": [{"student_id": 1, "status": "Attended"}]},
        # September, group lesson: 1 attended, 1 missed, 1 cancelled
        {"id": 2, "from_datetime": dt(2), "participants": [
            {"student_id": 2, "status": "Attended"},
            {"student_id": 3, "status": "Missed"},
            {"student_id": 4, "status": "Cancelled"},
        ]},
        # September, scheduled (future within month, excluded from totals)
        {"id": 3, "from_datetime": dt(15), "participants": [{"student_id": 5, "status": "Scheduled"}]},
        # September, null/missing status -> Unclassified
        {"id": 4, "from_datetime": dt(3), "participants": [{"student_id": 6}]},
        # September, unrecognized status value -> Unclassified
        {"id": 5, "from_datetime": dt(4), "participants": [{"student_id": 7, "status": "Requested"}]},
        # September, duplicate participant entry (same student twice on one lesson)
        {"id": 6, "from_datetime": dt(5), "participants": [
            {"student_id": 1, "status": "Attended"},
            {"student_id": 1, "status": "Attended"},
        ]},
        # September, classified but missing student_id
        {"id": 7, "from_datetime": dt(6), "participants": [{"status": "Attended"}]},
        # OUTSIDE September -- must be excluded entirely
        {"id": 8, "from_datetime": dt(15, month=8), "participants": [{"student_id": 99, "status": "Attended"}]},
        {"id": 9, "from_datetime": dt(1, month=10), "participants": [{"student_id": 99, "status": "Attended"}]},
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
    # Test 1: full aggregation correctness (classification, month filter, reconciliation)
    # =====================================================================
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sr.run_dry_run(CONFIG, output_dir, 2026, 9, refresh_teachworks_cache=False)
        output = buf.getvalue()

        out_path = output_dir / "wright-teachworks-reporting-dry-run-2026-09.json"
        with open(out_path) as f:
            result = json.load(f)

        print(output)

        # Lesson 8 (August) and lesson 9 (October) must be fully excluded.
        assert result["total_lessons_in_month"] == 7, result["total_lessons_in_month"]

        # Attended: lesson1(sid1), lesson2(sid2), lesson6(sid1 x2), lesson7(no sid) = 1+1+2+1 = 5
        assert result["sessions_attended"] == 5, result["sessions_attended"]
        # Missed: lesson2(sid3) = 1
        assert result["sessions_missed"] == 1, result["sessions_missed"]
        assert result["total_sessions"] == 6, result["total_sessions"]  # 5 + 1
        assert result["cancelled_count"] == 1, result["cancelled_count"]  # lesson2 sid4
        assert result["scheduled_count"] == 1, result["scheduled_count"]  # lesson3 sid5
        assert result["unclassified_count"] == 2, result["unclassified_count"]  # lesson4 (null), lesson5 ("Requested")
        print("PASS: classification counts correct across Attended/Missed/Cancelled/Scheduled/Unclassified")

        # Students served: distinct student_id among Attended+Missed = {1, 2, 3} (student 1 counted once
        # despite appearing twice on lesson 6; the missing-student_id record on lesson 7 doesn't count).
        assert result["students_served"] == 3, result["students_served"]
        print("PASS: Students Served correctly dedupes by distinct student_id and excludes Cancelled/Scheduled/Unclassified")

        assert result["duplicate_participant_entries"] == 1, result["duplicate_participant_entries"]
        assert result["classified_but_missing_student_id"] == 1, result["classified_but_missing_student_id"]
        print("PASS: duplicate-participant and missing-student_id diagnostics both correct")

        total_considered = result["total_participant_records_considered"]
        # lesson1=1, lesson2=3, lesson3=1, lesson4=1, lesson5=1, lesson6=2, lesson7=1 -> 10
        assert total_considered == 10, total_considered
        assert result["reconciliation_sum"] == total_considered
        assert result["reconciliation_ok"] is True
        print(f"PASS: reconciliation holds exactly -- {result['reconciliation_sum']} == {total_considered}")

        assert result["date_range_included"]["min"] == "2026-09-01"
        assert result["date_range_included"]["max"] == "2026-09-15"
        print("PASS: date range actually included is reported correctly")

        assert len(result["unclassified_examples"]) == 2
        print("PASS: unclassified examples captured for manual review")

    # =====================================================================
    # Test 2: staleness warnings -- cache fetched 2026-09-10, requesting September 2026
    # (today is 2026-09-28 per the sandbox clock) -- BOTH warnings must fire.
    # =====================================================================
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        cache_dir = output_dir / "_cache"
        cache_dir.mkdir(parents=True)
        with open(cache_dir / "cache_meta.json", "w") as f:
            json.dump({"fetched_at": "2026-09-10T12:00:00+00:00"}, f)

        warnings, fetched_at = sr.check_data_freshness(output_dir, 2026, 9)
        assert len(warnings) == 2, warnings
        assert any("BEFORE" in w and "2026-09" in w for w in warnings)
        assert any("has not finished yet" in w for w in warnings)
        print("PASS: stale-cache AND month-not-complete warnings both fire for a Sept-10 cache + September request")

    # =====================================================================
    # Test 3: a fully-in-the-past, freshly-fetched month produces NO warnings
    # =====================================================================
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        cache_dir = output_dir / "_cache"
        cache_dir.mkdir(parents=True)
        with open(cache_dir / "cache_meta.json", "w") as f:
            json.dump({"fetched_at": datetime.now(timezone.utc).isoformat()}, f)

        warnings, fetched_at = sr.check_data_freshness(output_dir, 2026, 8)  # August is fully over
        assert warnings == [], warnings
        print("PASS: a fresh cache reporting on a fully-completed past month produces zero warnings")

    print("\nALL sync_reporting.py TESTS PASSED")
