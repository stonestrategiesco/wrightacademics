"""
Teachworks REST API client.

Request shape (base URL, auth header, /lessons endpoint, query parameter
names, and the `status=Attended` filter) is confirmed against Wright
Academics' previously-working Zapier "Code by Zapier" implementation — this
is a known-working configuration, not a guess.

Response field mapping in `normalize_participant()` is confirmed against a
real production lesson/participant pair (2026-09-13, lesson 93279926):
`lesson_id` <- lesson["id"], `session_date` <- lesson["from_date"],
`tutor` <- lesson["employee_name"], `service` <- lesson["service_name"],
`location` <- lesson["location_name"], `student_id`/`student_name` <-
participant["student_id"]/["student_name"]. See
test_normalize_participant_matches_confirmed_2026_09_13_production_response
in tests/test_teachworks.py for the exact fixture.

STILL UNCONFIRMED: `duration_minutes` and `amount` have no confirmed field
in any real response seen so far (only from_date/from_time are confirmed on
the lesson; no duration or price/amount field has been observed). Their
current lookups in `normalize_participant()` are unverified guesses — do
not trust the Duration/Amount Monday columns until these are confirmed
against real data the same way the other fields were.
"""

import datetime
import logging
import time

import requests

logger = logging.getLogger("wright_sync.teachworks")

# Teachworks returns at most 80 records per page no matter what per_page asks
# for (confirmed: per_page=100 returns 80). Pagination must judge "last page"
# against the size Teachworks actually serves, not the size requested.
TEACHWORKS_MAX_PAGE_SIZE = 80

# Safety stop for one day's pagination; a real day is 1-2 pages.
MAX_PAGES_PER_DAY = 25


class TeachworksAPIError(RuntimeError):
    """Raised when a Teachworks API call fails permanently (after retries)."""


class TeachworksClient:
    def __init__(self, api_key, base_url, timeout=30, max_retries=5, retry_base_delay=1.0, session=None,
                 request_delay_seconds=0.0):
        if not api_key:
            raise ValueError("Teachworks API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.session = session or requests.Session()
        # Pause between the per-day /lessons requests in get_lessons().
        self.request_delay_seconds = request_delay_seconds

    def _auth_headers(self):
        # Confirmed against the known-working Zapier implementation.
        return {
            "Authorization": f"Token token={self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path, params=None):
        """GET with retries/backoff for transient failures (429 / 5xx / network errors)."""
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    url, headers=self._auth_headers(), params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Teachworks request error (attempt %d/%d): %s", attempt, self.max_retries, exc)
            else:
                if response.status_code == 200:
                    return response.json()
                # Teachworks answers rate limiting with 403 "Rate Limit Exceeded", not only 429.
                if response.status_code in (403, 429) or response.status_code >= 500:
                    last_error = TeachworksAPIError(
                        f"Teachworks returned {response.status_code}: {response.text[:500]}"
                    )
                    logger.warning(
                        "Teachworks transient error %s (attempt %d/%d)",
                        response.status_code, attempt, self.max_retries,
                    )
                else:
                    raise TeachworksAPIError(
                        f"Teachworks request to {path} failed with {response.status_code}: {response.text[:500]}"
                    )

            if attempt < self.max_retries:
                delay = self.retry_base_delay * (2 ** (attempt - 1))
                time.sleep(delay)

        raise TeachworksAPIError(
            f"Teachworks request to {path} failed after {self.max_retries} attempts: {last_error}"
        )

    def diagnostic_get(self, path, params):
        """Single, non-raising GET for read-only diagnostics ONLY (used by
        `sync.py --diagnose-teachworks`). Unlike `_get()`, this never retries
        and never raises on a non-2xx status — the caller needs the raw
        status code itself to compare request variants. Returns
        (status_code, parsed_json_or_None, records_or_None)."""
        url = f"{self.base_url}{path}"
        response = self.session.get(url, headers=self._auth_headers(), params=params, timeout=self.timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = None

        records = None
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            for key in ("data", "lessons", "results", "items"):
                if key in payload and isinstance(payload[key], list):
                    records = payload[key]
                    break

        return response.status_code, payload, records

    @staticmethod
    def _extract_page(payload):
        """Normalize a page response into a plain list of records.

        Handles a bare list, or a dict wrapping the list under a common key.
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "lessons", "results", "items"):
                if key in payload and isinstance(payload[key], list):
                    return payload[key]
        raise TeachworksAPIError(f"Unrecognized Teachworks page response shape: {type(payload)}")

    @staticmethod
    def _iter_calendar_dates(start_date, end_date):
        """Yield every 'YYYY-MM-DD' date from start_date through end_date, inclusive."""
        current = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        while current <= end:
            yield current.isoformat()
            current += datetime.timedelta(days=1)

    def _get_all_pages(self, path, params, per_page, label, max_pages=MAX_PAGES_PER_DAY):
        """Every page of a list endpoint. A page is the last one when it is empty
        or shorter than the page size Teachworks actually serves
        (min(per_page, TEACHWORKS_MAX_PAGE_SIZE)); comparing against the
        requested per_page alone stopped after the first 80 records. A page that
        repeats the previous one, or more than max_pages full pages, raises
        rather than return a partial list."""
        full_page = min(per_page, TEACHWORKS_MAX_PAGE_SIZE)
        records = []
        previous_ids = None
        page = 1
        while True:
            if page > max_pages:
                raise TeachworksAPIError(f"Teachworks {path} for {label} still returning full pages after {max_pages} pages")
            payload = self._get(path, params={**params, "page": page, "per_page": per_page})
            page_records = self._extract_page(payload)
            page_ids = [record.get("id") for record in page_records if isinstance(record, dict)]
            if page_records and page_ids == previous_ids:
                raise TeachworksAPIError(
                    f"Teachworks {path} for {label} returned the same records for page {page - 1} and page {page}"
                )
            records.extend(page_records)
            logger.debug("Fetched Teachworks %s for %s page %d (%d records)", path, label, page, len(page_records))
            if len(page_records) < full_page:
                return records
            previous_ids = page_ids
            page += 1

    def _get_lessons_for_one_date(self, date_str, status, per_page):
        """Fully paginate a SINGLE day's /lessons (from_date == to_date == date_str)."""
        return self._get_all_pages(
            "/lessons", {"status": status, "from_date": date_str, "to_date": date_str}, per_page, date_str)

    def get_all_students(self, per_page=TEACHWORKS_MAX_PAGE_SIZE, max_pages=500):
        """The full Teachworks student roster (GET /students, every status)."""
        return self._get_all_pages("/students", {}, per_page, "all students", max_pages=max_pages)

    def get_lessons(self, start_date, end_date, status="Attended", per_page=100):
        """Fetch ALL lessons in [start_date, end_date] (inclusive).

        Confirmed via production diagnostics: a single request spanning a
        multi-day from_date/to_date range returns zero records, even though
        the exact same status/from_date/to_date parameters return correct
        results for a single day. So instead of one multi-day range request,
        this issues one from_date == to_date request PER CALENDAR DATE in
        the range, each fully paginated independently, and combines the
        results. An empty day does not stop later dates from being checked.
        Deduplication across dates (if the same lesson were ever returned by
        more than one day's query) is handled downstream by the unique-key
        check in sync.run_sync, not here.
        """
        lessons = []
        for index, date_str in enumerate(self._iter_calendar_dates(start_date, end_date)):
            if index and self.request_delay_seconds:
                time.sleep(self.request_delay_seconds)
            lessons.extend(self._get_lessons_for_one_date(date_str, status=status, per_page=per_page))
        return lessons

    @staticmethod
    def _is_attended(participant):
        """Best-effort check for "this participant attended this lesson".

        Handles a boolean `attended` flag or a `status` string, whichever the
        real API returns. Defaults to False (excluded) if neither is present,
        since it's safer to under-report than to bill/log a no-show.
        """
        if "attended" in participant:
            return bool(participant["attended"])
        status = str(participant.get("status", "")).strip().lower()
        return status in ("attended", "completed", "present")

    @staticmethod
    def _first(d, *keys, default=None):
        for key in keys:
            if key in d and d[key] not in (None, ""):
                return d[key]
        return default

    @classmethod
    def normalize_participant(cls, lesson, participant):
        """Flatten one attended (lesson, participant) pair into the fields
        needed for a single Monday Session Log record."""
        student_id = cls._first(participant, "student_id", "id")
        student_name = cls._first(
            participant, "student_name", "name",
            default=(participant.get("student") or {}).get("name") if isinstance(participant.get("student"), dict) else None,
        )
        tutor = lesson.get("employee_name")
        service = cls._first(
            lesson, "service_name",
            default=(lesson.get("service") or {}).get("name") if isinstance(lesson.get("service"), dict) else None,
        )
        location = cls._first(
            lesson, "location_name",
            default=(lesson.get("location") or {}).get("name") if isinstance(lesson.get("location"), dict) else None,
        )
        duration = cls._first(participant, "duration", "duration_minutes", default=cls._first(lesson, "duration", "duration_minutes"))
        amount = cls._first(participant, "price", "amount", default=cls._first(lesson, "price", "amount"))

        return {
            "lesson_id": lesson.get("id"),
            "session_date": lesson.get("from_date"),
            "student_id": student_id,
            "student_name": student_name,
            "tutor": tutor,
            "service": service,
            "duration_minutes": duration,
            "location": location,
            "amount": amount,
            "unique_key": f"{lesson.get('id')}_{student_id}",
        }

    @classmethod
    def extract_attended_sessions(cls, lessons):
        """One Teachworks lesson may contain multiple participants; return one
        normalized record per ATTENDED participant, across all given lessons."""
        sessions = []
        for lesson in lessons:
            participants = lesson.get("participants") or lesson.get("students") or []
            for participant in participants:
                if cls._is_attended(participant):
                    sessions.append(cls.normalize_participant(lesson, participant))
        return sessions
