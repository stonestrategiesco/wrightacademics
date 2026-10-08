"""Rolling Session Log reconciliation (--daily-sync).

Uses the real TeachworksClient against a fake HTTP layer that behaves like
Teachworks (per-day queries, at most 80 records per page whatever per_page
asks), and a stateful fake Monday board, so a run's writes are visible to the
next run (needed to prove idempotency)."""

import datetime
import json
import types
from unittest.mock import MagicMock

import pytest

import config
import sync
from fakes import FakeMondayClient
from student_sync import StudentSyncReport
from teachworks import TeachworksAPIError, TeachworksClient

FLAG = "text_sync_flag"


# --- fakes ---------------------------------------------------------------------

def tw_lesson(lesson_id, date, participants, tutor="Frey, Bethany", service="PRIMARY SUBJECT TUTORING", location="Online"):
    return {"id": lesson_id, "from_date": date, "employee_name": tutor, "service_name": service,
            "location_name": location, "participants": [
                {"student_id": sid, "student_name": name, "status": "Attended"} for sid, name in participants]}


class FakeTeachworks:
    """HTTP layer for TeachworksClient: GET /lessons?status=Attended&from_date=D&to_date=D, 80 per page."""

    def __init__(self, lessons, students=None):
        self.lessons = lessons
        self.students = students or []
        self.requests = []
        self.fail_on_day = None
        self.repeat_pages = False

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append(dict(params))
        resp = MagicMock()
        if url.endswith("/students"):
            page = params["page"]
            resp.status_code = 200
            resp.json.return_value = json.loads(json.dumps(self.students[(page - 1) * 80: page * 80]))
            return resp
        day, page = params["from_date"], params["page"]
        if day == self.fail_on_day:
            resp.status_code, resp.text = 401, "unauthorized"
            return resp
        on_day = [l for l in self.lessons if l["from_date"] == day]
        resp.status_code = 200
        resp.json.return_value = json.loads(json.dumps(on_day[:80] if self.repeat_pages else on_day[(page - 1) * 80: page * 80]))
        return resp

    def client(self):
        session = MagicMock()
        session.get.side_effect = self.get
        return TeachworksClient(api_key="k", base_url="https://tw.test", session=session, max_retries=1)


def _text(value):
    if isinstance(value, dict) and "date" in value:
        return value["date"]
    return "" if value is None else str(value)


class FakeBoard(FakeMondayClient):
    """Session Log + Students boards whose writes are visible to later reads."""

    def __init__(self, rows, students):
        super().__init__(items=rows, student_items=students)
        self.column_updates = []
        self.next_id = 50000

    def get_existing_unique_ids(self, board_id, unique_id_column):
        return {i["columns"].get(config.COL_UNIQUE_ID) for i in self.items if i["columns"].get(config.COL_UNIQUE_ID)}

    def get_student_index(self, board_id, teachworks_id_column):
        """Same contract as MondayClient.get_student_index: unique IDs, plus IDs held by several items."""
        by_id = {}
        for s in self.student_items:
            tw_id = (s["columns"].get(config.STUDENT_BOARD_COL_TEACHWORKS_ID) or "").strip()
            if tw_id:
                by_id.setdefault(tw_id, []).append(s["item_id"])
        return ({k: v[0] for k, v in by_id.items() if len(v) == 1},
                {k: v for k, v in by_id.items() if len(v) > 1})

    def get_student_lookup(self, board_id, teachworks_id_column):
        return self.get_student_index(board_id, teachworks_id_column)[0]

    def create_session_item(self, board_id, group_id, item_name, column_values):
        item_id = super().create_session_item(board_id, group_id, item_name, column_values)
        self.items.append({"item_id": item_id, "item_name": item_name,
                           "columns": {k: _text(v) for k, v in column_values.items() if v is not None}})
        return item_id

    def create_student_item(self, board_id, group_id, item_name, column_values):
        assert board_id == config.MONDAY_STUDENTS_BOARD_ID
        self.student_creates = getattr(self, "student_creates", []) + [
            {"group_id": group_id, "item_name": item_name, "column_values": dict(column_values)}]
        item_id = f"NEW{len(self.student_creates)}"
        self.student_items.append({"item_id": item_id, "item_name": item_name,
                                   "columns": {k: _text(v) for k, v in column_values.items()}})
        return item_id

    def connect_student(self, board_id, item_id, column_id, student_item_id):
        super().connect_student(board_id, item_id, column_id, student_item_id)
        self._item(item_id)["columns"][column_id] = f"student {student_item_id}"
        return item_id

    def update_item_columns(self, board_id, item_id, column_values):
        assert board_id == config.MONDAY_SESSIONS_BOARD_ID
        self.column_updates.append((str(item_id), dict(column_values)))
        self._item(item_id)["columns"].update({k: _text(v) for k, v in column_values.items()})
        return item_id

    def update_student_columns(self, board_id, item_id, column_values):
        super().update_student_columns(board_id, item_id, column_values)
        student = next(s for s in self.student_items if s["item_id"] == item_id)
        student["columns"].update({k: _text(v) for k, v in column_values.items() if v is not None})
        return item_id

    def _item(self, item_id):
        return next(i for i in self.items if str(i["item_id"]) == str(item_id))

    def writes(self):
        return (len(self.created_items) + len(self.connections) + len(self.column_updates)
                + len(getattr(self, "student_creates", [])) + len(self.student_updates))


def row(item_id, lesson_id, sid, date, name, tutor="Frey, Bethany", service="PRIMARY SUBJECT TUTORING",
        location="Online", key=None, pre_baseline=False, connected=True):
    return {"item_id": str(item_id), "item_name": f"{name} - {date}", "columns": {
        config.COL_UNIQUE_ID: key if key is not None else f"{lesson_id}_{sid}",
        config.COL_TEACHWORKS_STUDENT_ID: str(sid) if sid is not None else "",
        config.COL_SESSION_DATE: date, config.COL_STUDENT_NAME: name, config.COL_TUTOR: tutor,
        config.COL_SERVICE: service, config.COL_LOCATION: location,
        config.COL_STUDENT_CONNECTION: name if connected else "",
        config.COL_PRE_BASELINE: "v" if pre_baseline else ""}}


def student(item_id, sid, baseline=0, count=0, last="", tutor=""):
    return {"item_id": str(item_id), "item_name": f"Student {sid}", "columns": {
        config.STUDENT_BOARD_COL_TEACHWORKS_ID: str(sid), config.STUDENT_COL_HISTORICAL_BASELINE: str(baseline),
        config.STUDENT_COL_SESSION_COUNT: str(count), config.STUDENT_COL_LAST_SESSION_DATE: last,
        config.STUDENT_COL_TUTOR: tutor}}


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", "")
    monkeypatch.setattr(config, "RECONCILE_MAX_STALE_ROWS", 10)


def reconcile(tw, board, start="2026-10-01", end="2026-10-07", **kw):
    return sync.reconcile_session_log(tw.client(), board, start, end, today=datetime.date(2026, 10, 8), **kw)


# --- the October 1-7, 2026 acceptance case ---------------------------------------

JAMES_ASH = ("96795881", "2364959")
MISSING_OCT1 = 7
PENDING_OCT6 = 2
PER_DAY = {"2026-10-01": 13, "2026-10-02": 12, "2026-10-03": 10, "2026-10-04": 12,
           "2026-10-05": 11, "2026-10-06": 13, "2026-10-07": 12}   # = 83


def october_world():
    """Teachworks: 83 attended participant sessions Oct 1-7 (one lesson is a group lesson).
    Monday: 75 rows = 74 of those sessions + James Ash's Oct 4 row, which Teachworks no
    longer has. Missing: 7 dated Oct 1 (marked Attended after the 3-day lookback passed),
    2 dated Oct 6 (still inside the lookback)."""
    lessons, rows, students, missing = [], [], [], []
    lesson_id, sid = 96790000, 3000000
    for day, count in PER_DAY.items():
        made = 0
        while made < count:
            if day == "2026-10-03" and made == 0:   # one group lesson with two students
                parts = [(sid, f"Student {sid}"), (sid + 1, f"Student {sid + 1}")]
            else:
                parts = [(sid, f"Student {sid}")]
            lessons.append(tw_lesson(lesson_id, day, parts, tutor=["Frey, Bethany", "Lee, Sam", "Ortiz, Ana"][lesson_id % 3]))
            for p_sid, name in parts:
                is_missing = ((day == "2026-10-01" and made < MISSING_OCT1)
                              or (day == "2026-10-06" and made < PENDING_OCT6))
                if is_missing:
                    missing.append((str(lesson_id), str(p_sid), day))
                else:
                    rows.append(row(len(rows) + 1, lesson_id, p_sid, day, name,
                                    tutor=lessons[-1]["employee_name"]))
                students.append(student(f"S{p_sid}", p_sid))
                made += 1
            sid += len(parts)
            lesson_id += 1
    # James Ash: one real attended session on Oct 2 is among the rows above via a normal
    # student; his stale Oct 4 row is the extra one.
    rows.append(row(len(rows) + 1, JAMES_ASH[0], JAMES_ASH[1], "2026-10-04", "James Ash"))
    students.append(student("S_JA", JAMES_ASH[1], baseline=10, count=11, last="2026-10-04", tutor="Frey, Bethany"))
    return lessons, rows, students, missing


def test_acceptance_october_1_to_7_report_mode():
    lessons, rows, students, missing = october_world()
    tw, board = FakeTeachworks(lessons), FakeBoard(rows, students)
    assert len(rows) == 75 and sum(len(l["participants"]) for l in lessons) == 83

    report = reconcile(tw, board)

    assert report.succeeded
    assert report.teachworks_sessions == 83
    assert report.monday_rows_in_window == 75
    assert sorted((c["identity"][0], c["identity"][1], c["session_date"]) for c in report.created) == sorted(missing)
    assert sum(1 for m in missing if m[2] == "2026-10-01") == 7          # the 7 Oct 1 sessions are accounted for
    assert len(report.created) == 9 and report.updated == [] and not report.errors
    assert [(r["unique_key"], r["session_date"], r["student_name"]) for r in report.stale] == [
        ("96795881_2364959", "2026-10-04", "James Ash")]
    assert report.duplicates == [] and report.ambiguous == [] and report.integrity_failures == []
    assert report.attended_rows == 83                                     # James Ash's row does not count
    # Board after: 84 rows in Oct 1-7, of which exactly 83 are Teachworks-attended identities, 0 duplicates.
    oct_rows = [i for i in board.items if "2026-10-01" <= i["columns"][config.COL_SESSION_DATE] <= "2026-10-07"]
    identities = [sync._canonical_session_identity(i) for i in oct_rows]
    attended = {(str(l["id"]), str(p["student_id"])) for l in lessons for p in l["participants"]}
    assert len(oct_rows) == 84 and len(set(identities)) == 84
    assert sum(1 for i in identities if (str(i[0]), str(i[1])) in attended) == 83
    # The stale row itself is untouched in report mode.
    assert next(i for i in board.items if i["columns"][config.COL_UNIQUE_ID] == "96795881_2364959")["columns"] == \
        rows[-1]["columns"]
    # Every created row is exactly what the production row builder makes, Pre-Baseline unset.
    for c in board.created_items:
        assert config.COL_PRE_BASELINE not in c["column_values"]
        assert c["group_id"] == config.MONDAY_SESSION_GROUP_ID
    assert len(board.connections) == 9

    # Idempotent: the same Teachworks data again changes nothing.
    writes = board.writes()
    again = reconcile(tw, board)
    assert board.writes() == writes
    assert again.created == [] and again.updated == [] and again.connected == []
    assert again.attended_rows == 83 and len(again.stale) == 1 and again.duplicates == []


def test_acceptance_october_1_to_7_with_proposed_flagging(monkeypatch):
    """With the proposed Sync Flag column configured, James Ash's row is flagged (not deleted),
    stops counting in the student rollup, and is cleared if Teachworks shows it attended again."""
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", FLAG)
    lessons, rows, students, _missing = october_world()
    tw, board = FakeTeachworks(lessons), FakeBoard(rows, students)

    outcome = sync.perform_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07")
    report = outcome["sync_report"]
    assert outcome["sync_succeeded"] and outcome["rollup_ran"]
    assert report.attended_rows == 83 and len(report.created) == 9
    assert [r["unique_key"] for r in report.flagged] == ["96795881_2364959"]
    ja_row = next(i for i in board.items if i["columns"][config.COL_UNIQUE_ID] == "96795881_2364959")
    assert ja_row["columns"][FLAG].startswith("Not attended in Teachworks (flagged 20")
    assert len(board.items) == 84                                         # nothing deleted
    ja = next(s for s in board.student_items if s["item_id"] == "S_JA")
    assert ja["columns"][config.STUDENT_COL_SESSION_COUNT] == "10"        # baseline 10 + 0 attended rows

    writes = board.writes()
    again = reconcile(tw, board)
    assert board.writes() == writes and again.flagged == [] and again.stale[0]["flagged"]

    # Teachworks shows it attended again -> flag cleared, counts again.
    lessons.append(tw_lesson(int(JAMES_ASH[0]), "2026-10-04", [(int(JAMES_ASH[1]), "James Ash")]))
    third = reconcile(tw, board)
    assert [u["unique_key"] for u in third.unflagged] == ["96795881_2364959"] and third.stale == []
    assert ja_row["columns"][FLAG] == ""


def test_old_three_day_lookback_leaves_the_october_1_sessions_missing():
    """Contrast: the previous nightly (run_sync over today-3 .. today) on Oct 8 only finds Oct 6."""
    lessons, rows, students, _missing = october_world()
    tw, board = FakeTeachworks(lessons), FakeBoard(rows, students)
    old = sync.run_sync(tw.client(), board, "2026-10-05", "2026-10-08")
    assert sorted(c["column_values"][config.COL_SESSION_DATE]["date"] for c in board.created_items) == ["2026-10-06"] * 2
    assert old.sessions_created == 2


# --- field updates ----------------------------------------------------------------

def test_drifted_fields_are_corrected_once_and_blanks_never_written():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann Lee")], tutor="Lee, Sam", service="SAT PREP", location="")]
    board = FakeBoard([row(1, 1, 10, "2026-10-01", "Ann  Lee", tutor="Frey, Bethany", service="SAT PREP",
                           location="Center")], [student("S10", 10)])
    report = reconcile(FakeTeachworks(lessons), board)
    assert report.updated[0]["changes"] == {
        config.COL_SESSION_DATE: ("2026-10-01", "2026-10-02"),
        config.COL_STUDENT_NAME: ("Ann  Lee", "Ann Lee"),
        config.COL_TUTOR: ("Frey, Bethany", "Lee, Sam")}
    assert board.column_updates == [("1", {config.COL_SESSION_DATE: {"date": "2026-10-02"},
                                           config.COL_STUDENT_NAME: "Ann Lee", config.COL_TUTOR: "Lee, Sam"})]
    assert board.items[0]["columns"][config.COL_LOCATION] == "Center"     # Teachworks blank -> left alone
    assert reconcile(FakeTeachworks(lessons), board).updated == []


def test_unconnected_row_is_connected_once_its_student_exists():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")])]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann", connected=False)], [])
    assert reconcile(FakeTeachworks(lessons), board).connected == []
    board.student_items.append(student("S10", 10))
    assert [c["item_id"] for c in reconcile(FakeTeachworks(lessons), board).connected] == ["1"]
    assert reconcile(FakeTeachworks(lessons), board).connected == []


def test_pre_baseline_rows_are_never_edited_or_reported():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")], tutor="New, Tutor")]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann", tutor="Old", pre_baseline=True),
                       row(2, 2, 20, "2026-10-03", "Bo", pre_baseline=True)], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert board.writes() == 0 and report.stale == [] and report.created == []


# --- duplicates / legacy keys ---------------------------------------------------------

def test_duplicates_are_reported_and_never_added_to():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")], tutor="New")]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann"), row(2, 1, 10, "2026-10-02", "Ann", key="1")], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert report.duplicates == [{"identity": ("1", "10"), "item_ids": ["1", "2"]}]
    assert board.writes() == 0 and report.attended_rows == 1


def test_legacy_bare_key_row_matches_its_own_student_and_other_participants_are_created():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann"), (11, "Bo")])]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann", key="1")], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert [c["unique_key"] for c in report.created] == ["1_11"] and report.duplicates == []


def test_row_without_student_id_blocks_creation_as_ambiguous():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")])]
    board = FakeBoard([row(1, 1, None, "2026-10-02", "Ann", key="1")], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert report.created == [] and report.ambiguous == [{"identity": ("1", "10"), "session_date": "2026-10-02"}]
    assert [r["item_id"] for r in report.unidentifiable_rows] == ["1"] and board.writes() == 0


# --- fail-safe ----------------------------------------------------------------------------

def test_teachworks_failure_mid_window_writes_nothing():
    lessons = [tw_lesson(1, "2026-10-01", [(10, "Ann")]), tw_lesson(2, "2026-10-05", [(11, "Bo")])]
    tw = FakeTeachworks(lessons)
    tw.fail_on_day = "2026-10-05"
    board = FakeBoard([], [])
    outcome = sync.perform_daily_sync(tw.client(), board, "2026-10-01", "2026-10-07")
    assert isinstance(outcome["sync_exception"], TeachworksAPIError)
    assert not outcome["rollup_ran"] and board.writes() == 0


def test_repeating_pages_abort_before_any_write():
    lessons = [tw_lesson(i, "2026-10-02", [(i, f"S{i}")]) for i in range(1, 91)]
    tw = FakeTeachworks(lessons)
    tw.repeat_pages = True
    board = FakeBoard([], [])
    with pytest.raises(TeachworksAPIError, match="same records"):
        reconcile(tw, board)
    assert board.writes() == 0


def test_more_than_80_lessons_in_a_day_are_all_reconciled():
    lessons = [tw_lesson(i, "2026-10-02", [(i, f"S{i}")]) for i in range(1, 91)]
    report = reconcile(FakeTeachworks(lessons), FakeBoard([], []))
    assert report.teachworks_sessions == 90 and len(report.created) == 90


def test_day_with_rows_but_no_teachworks_sessions_fails_and_flags_nothing(monkeypatch):
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", FLAG)
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")])]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann")] +
                      [row(10 + i, 100 + i, 50 + i, "2026-10-03", f"X{i}") for i in range(3)], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert not report.succeeded
    assert report.integrity_failures == [
        "Teachworks returned no attended sessions for 2026-10-03, but the Session Log has 3 row(s) that day"]
    assert len(report.stale) == 3 and report.flagged == [] and board.writes() == 0


def test_one_or_two_cancelled_sessions_on_a_quiet_day_are_just_reported():
    lessons = [tw_lesson(1, "2026-10-02", [(10, "Ann")])]
    board = FakeBoard([row(1, 1, 10, "2026-10-02", "Ann"), row(2, 9, 90, "2026-10-03", "Zed")], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert report.succeeded and [r["item_id"] for r in report.stale] == ["2"]


def test_too_many_stale_rows_is_an_integrity_failure(monkeypatch):
    monkeypatch.setattr(config, "COL_TEACHWORKS_SYNC_FLAG", FLAG)
    monkeypatch.setattr(config, "RECONCILE_MAX_STALE_ROWS", 2)
    lessons = [tw_lesson(i, f"2026-10-0{i}", [(i, f"S{i}")]) for i in range(1, 4)]
    board = FakeBoard([row(i, i, i, f"2026-10-0{i}", f"S{i}") for i in range(1, 4)] +
                      [row(10 + i, 100 + i, 50 + i, f"2026-10-0{i}", f"X{i}") for i in range(1, 4)], [])
    report = reconcile(FakeTeachworks(lessons), board)
    assert not report.succeeded and len(report.stale) == 3 and report.flagged == []
    assert "limit 2" in report.integrity_failures[0]


def test_dry_run_writes_nothing_and_reports_the_same_plan():
    lessons, rows, students, missing = october_world()
    board = FakeBoard(rows, students)
    report = reconcile(FakeTeachworks(lessons), board, dry_run=True)
    assert board.writes() == 0 and len(report.created) == 9 and report.attended_rows == 83 and len(report.stale) == 1


# --- window ---------------------------------------------------------------------------------

@pytest.mark.parametrize("today, start", [
    ("2026-10-08", "2026-10-01"),   # floor
    ("2026-10-31", "2026-10-01"),
    ("2026-11-15", "2026-10-16"),
    ("2026-12-01", "2026-11-01"),
    ("2027-01-10", "2026-12-11"),   # year boundary
    ("2027-03-01", "2027-01-30"),   # February
])
def test_window_is_thirty_days_through_today_never_before_the_floor(today, start):
    assert sync.reconcile_window(datetime.date.fromisoformat(today), 30, "2026-10-01") == (start, today)


def test_every_date_is_reconciled_by_30_consecutive_nightlies():
    for offset in range(120):
        day = datetime.date(2026, 10, 1) + datetime.timedelta(days=offset)
        covering = [d for d in range(200)
                    if (w := sync.reconcile_window(datetime.date(2026, 10, 1) + datetime.timedelta(days=d), 30, "2026-10-01"))
                    and w[0] <= day.isoformat() <= w[1]]
        assert len(covering) == 31, day


def test_cli_daily_sync_uses_the_reconciliation_window(monkeypatch):
    class Today(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 15)

    monkeypatch.setattr(sync, "datetime", types.SimpleNamespace(date=Today, timedelta=datetime.timedelta))
    monkeypatch.setattr(config, "TEACHWORKS_API_KEY", "k")
    monkeypatch.setattr(config, "MONDAY_API_TOKEN", "t")
    monkeypatch.setattr(sync, "TeachworksClient", lambda **kwargs: object())
    monkeypatch.setattr(sync, "MondayClient", lambda **kwargs: FakeMondayClient())
    monkeypatch.setattr(sync, "sync_students", lambda *a, **k: StudentSyncReport())
    seen = []
    monkeypatch.setattr(sync, "reconcile_session_log",
                        lambda tw, mon, start, end, dry_run=False: seen.append((start, end, dry_run)) or sync.ReconcileReport(start, end))
    monkeypatch.setattr(sync, "apply_post_baseline_student_rollup_updates",
                        lambda *a, **k: {"results": [], "written": [], "unchanged": [], "skipped_unmatched": [], "failed": []})
    assert sync.main(["--daily-sync"]) == 0
    assert sync.main(["--daily-sync", "--dry-run"]) == 0
    assert sync.main(["--daily-sync", "--full", "--dry-run"]) == 0            # floor still applies
    assert seen == [("2026-10-16", "2026-11-15", False), ("2026-10-16", "2026-11-15", True),
                    ("2026-10-01", "2026-11-15", True)]


def test_report_mode_leaves_the_stale_row_counted_in_the_student_rollup():
    """Without the Sync Flag column, a stale row is only reported: it is not attended for the
    reconciliation's counts, but the Students rollup still counts it until a person acts."""
    lessons, rows, students, _missing = october_world()
    board = FakeBoard(rows, students)
    outcome = sync.perform_daily_sync(FakeTeachworks(lessons).client(), board, "2026-10-01", "2026-10-07")
    assert outcome["sync_succeeded"] and outcome["sync_report"].attended_rows == 83
    ja = next(s for s in board.student_items if s["item_id"] == "S_JA")
    assert ja["columns"][config.STUDENT_COL_SESSION_COUNT] == "11"        # baseline 10 + the stale row


def test_daily_sync_log_shows_every_count(capsys):
    lessons, rows, students, _missing = october_world()
    board = FakeBoard(rows, students)
    assert sync.run_daily_sync(FakeTeachworks(lessons).client(), board, "2026-10-01", "2026-10-07") == 0
    out = capsys.readouterr().out
    counts = [("Teachworks attended participant sessions", 83), ("Monday Session Log rows dated in the window", 75),
              ("Created", 9), ("Updated", 0), ("Student connections made", 9),
              ("Not attended in Teachworks (stale)", "1 - reported only (no Sync Flag column configured)"),
              ("Duplicates detected", 0), ("Errors", 0), ("Integrity failures", 0),
              ("Attended sessions with a Session Log row", "83 of 83")]
    for line in [f"{label + ':':<46}{value}" for label, value in counts] + [
            "2026-10-04 item 75 96795881_2364959 James Ash", "FINAL RESULT: SUCCESS"]:
        assert line in out, line
