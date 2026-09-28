#!/usr/bin/env python3
"""Wright Academics Teachworks session audit — read-only, local-only.

Standalone from the CEO Scoreboard app: makes GET requests to the Teachworks
API only, never writes to Teachworks or Monday.com, and never touches the
CEO Scoreboard database.

Modes:
  test  Pull a small sample of students/lessons and report every field that
        looks like it could represent attendance/status, so the "attended"
        rule can be defined from real data. (Already run and confirmed for
        Wright Academics: lesson["status"] == "Attended" is the rule.)
  full  Page through ALL students and ALL historical lessons, apply the
        confirmed attendance rule, and write the per-student CSV audit plus
        a full status-value audit (every unique status seen, with counts).
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent

STATUS_KEYWORDS = [
    "status", "attend", "cancel", "no_show", "noshow", "state",
    "completed", "confirm", "outcome", "occurred", "held",
]

STUDENT_NAME_CANDIDATES = ["name", "full_name", "student_name"]
STUDENT_FIRST_NAME_CANDIDATES = ["first_name", "firstname"]
STUDENT_LAST_NAME_CANDIDATES = ["last_name", "lastname"]

# Confirmed against Wright Academics' live Teachworks account: lesson dates
# use from_datetime/from_date, and tutor is embedded directly as
# employee_id/employee_name. Student attendee is NOT a direct field on the
# lesson (student_id/student.id do not exist) -- it must be resolved from
# the `participants` field; see resolve_participant_strategy() below.
LESSON_DATE_CANDIDATES = [
    "from_datetime", "from_date", "to_datetime", "to_date",
    "date", "lesson_date", "start_time", "start_at", "scheduled_at", "scheduled_date",
]
LESSON_TUTOR_ID_CANDIDATES = [
    "employee_id", "tutor_id", "user_id", "instructor_id", "tutor.id", "employee.id",
]
LESSON_TUTOR_NAME_CANDIDATES = [
    "employee_name", "tutor_name", "instructor_name", "tutor.name", "employee.name",
]
TUTOR_NAME_CANDIDATES = ["name", "full_name"]

# Only these kinds of participant-object keys are ever printed WITH their
# values (ids/type/category labels are not PII). Everything else (name,
# email, phone, address, ...) is reported by key name and type only, never
# by value.
PARTICIPANT_SAFE_VALUE_KEYWORDS = ["id", "type", "category", "role", "kind", "status", "attendance"]
PARTICIPANT_TYPE_KEY_KEYWORDS = ["type", "role", "category", "kind"]


class TeachworksApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _get(name, default=None):
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _get_int(name, default):
    val = os.environ.get(name)
    try:
        return int(val) if val not in (None, "") else default
    except ValueError:
        return default


def _get_float(name, default):
    val = os.environ.get(name)
    try:
        return float(val) if val not in (None, "") else default
    except ValueError:
        return default


def load_config():
    env_path = SCRIPT_DIR / ".env"
    load_dotenv(dotenv_path=env_path)

    config = {
        "base_url": _get("TEACHWORKS_BASE_URL"),
        "api_key": _get("TEACHWORKS_API_KEY"),
        "auth_header": _get("TEACHWORKS_AUTH_HEADER", "Authorization"),
        "auth_scheme": _get("TEACHWORKS_AUTH_SCHEME", "Token token={key}"),
        "students_path": _get("TEACHWORKS_STUDENTS_PATH", "/students"),
        "lessons_path": _get("TEACHWORKS_LESSONS_PATH", "/lessons"),
        "tutors_path": _get("TEACHWORKS_TUTORS_PATH", ""),
        "page_param": _get("TEACHWORKS_PAGE_PARAM", "page"),
        "per_page_param": _get("TEACHWORKS_PER_PAGE_PARAM", "per_page"),
        "response_data_key": _get("TEACHWORKS_RESPONSE_DATA_KEY", ""),
        "lesson_start_date_param": _get("TEACHWORKS_LESSON_START_DATE_PARAM", ""),
        "lesson_end_date_param": _get("TEACHWORKS_LESSON_END_DATE_PARAM", ""),
        "student_id_param": _get("TEACHWORKS_STUDENT_ID_PARAM", "student_id"),
        "test_student_limit": _get_int("TEST_STUDENT_LIMIT", 5),
        "test_lesson_limit": _get_int("TEST_LESSON_LIMIT", 20),
        "test_lesson_lookback_days": _get_int("TEST_LESSON_LOOKBACK_DAYS", 30),
        # Full-mode settings
        # Teachworks' confirmed real maximum page size is 80; pagination below
        # also self-adapts to whatever the API actually returns on page 1, so
        # this being wrong does not cause missed pages.
        "per_page": _get_int("TEACHWORKS_PER_PAGE", 80),
        "max_pages": _get_int("TEACHWORKS_MAX_PAGES", 1000),
        "request_delay_seconds": _get_float("TEACHWORKS_REQUEST_DELAY_SECONDS", 0.1),
        "status_field": _get("TEACHWORKS_STATUS_FIELD", "status"),
        "attended_status_value": _get("TEACHWORKS_ATTENDED_STATUS", "Attended"),
        # Field-name overrides (blank = auto-detect from real data)
        "student_id_field": _get("TEACHWORKS_STUDENT_ID_FIELD", "id"),
        "student_name_field": _get("TEACHWORKS_STUDENT_NAME_FIELD", ""),
        "student_first_name_field": _get("TEACHWORKS_STUDENT_FIRST_NAME_FIELD", ""),
        "student_last_name_field": _get("TEACHWORKS_STUDENT_LAST_NAME_FIELD", ""),
        "lesson_date_field": _get("TEACHWORKS_LESSON_DATE_FIELD", ""),
        "lesson_tutor_id_field": _get("TEACHWORKS_LESSON_TUTOR_ID_FIELD", ""),
        "lesson_tutor_name_field": _get("TEACHWORKS_LESSON_TUTOR_NAME_FIELD", ""),
        "tutor_record_id_field": _get("TEACHWORKS_TUTOR_RECORD_ID_FIELD", "id"),
        "tutor_record_name_field": _get("TEACHWORKS_TUTOR_RECORD_NAME_FIELD", ""),
        # Participant resolution (how lessons link to attending student(s))
        "participants_field": _get("TEACHWORKS_PARTICIPANTS_FIELD", "participants"),
        "participant_sample_size": _get_int("TEACHWORKS_PARTICIPANT_SAMPLE_SIZE", 20),
        "participant_id_field": _get("TEACHWORKS_PARTICIPANT_ID_FIELD", ""),
        "participant_type_field": _get("TEACHWORKS_PARTICIPANT_TYPE_FIELD", ""),
        "participant_student_type_value": _get("TEACHWORKS_PARTICIPANT_STUDENT_TYPE_VALUE", "student"),
    }

    required_env_names = {"base_url": "TEACHWORKS_BASE_URL", "api_key": "TEACHWORKS_API_KEY"}
    missing = [required_env_names[k] for k in required_env_names if not config[k]]
    if missing:
        print("Missing required configuration:")
        for name in missing:
            print(f"  - {name}")
        print(f"\nCreate a .env file at {env_path} (copy .env.example) and fill these in.")
        sys.exit(1)

    if "{key}" not in config["auth_scheme"]:
        print(
            f"WARNING: TEACHWORKS_AUTH_SCHEME ('{config['auth_scheme']}') does not contain "
            f"the '{{key}}' placeholder, so your API key will NOT be included in the "
            f"auth header. Update it in .env, e.g. 'Token token={{key}}'.",
            file=sys.stderr,
        )

    return config


def mask(header_value, key):
    if not key:
        return header_value
    masked_key = ("*" * max(len(key) - 4, 0)) + key[-4:]
    return header_value.replace(key, masked_key)


def build_headers(config):
    value = config["auth_scheme"].format(key=config["api_key"])
    return {config["auth_header"]: value, "Accept": "application/json"}


def request_json(config, path, params=None, max_retries=3):
    url = config["base_url"].rstrip("/") + "/" + path.lstrip("/")
    headers = build_headers(config)
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise TeachworksApiError(f"Network error calling {url}: {exc}") from exc
            time.sleep(2 ** (attempt - 1))
            continue

        if resp.status_code == 200:
            try:
                return resp.json(), resp
            except ValueError as exc:
                raise TeachworksApiError(
                    f"Response from {url} was not valid JSON (status 200). "
                    f"Body starts with: {resp.text[:300]!r}",
                    status_code=200,
                ) from exc

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            time.sleep(2 ** (attempt - 1))
            continue

        if resp.status_code in (401, 403):
            raise TeachworksApiError(
                f"Authentication failed ({resp.status_code}) calling {url}.\n"
                "Check TEACHWORKS_API_KEY, TEACHWORKS_AUTH_HEADER, and TEACHWORKS_AUTH_SCHEME "
                "in .env against the auth format shown on your Teachworks account's API "
                f"settings page.\nResponse body (first 500 chars): {resp.text[:500]!r}",
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            raise TeachworksApiError(
                f"Not found (404) calling {url}.\n"
                f"Check TEACHWORKS_BASE_URL and the endpoint path ('{path}') against your "
                f"account's API docs.\nResponse body (first 500 chars): {resp.text[:500]!r}",
                status_code=404,
            )
        raise TeachworksApiError(
            f"Unexpected response {resp.status_code} calling {url}.\n"
            f"Response body (first 500 chars): {resp.text[:500]!r}",
            status_code=resp.status_code,
        )


def extract_list(config, body):
    key = config["response_data_key"]
    if not key:
        if isinstance(body, list):
            return body
        raise TeachworksApiError(
            "Expected a JSON array in the response but got a JSON object instead. "
            "If your Teachworks API wraps results in an envelope (e.g. {\"data\": [...]}), "
            "set TEACHWORKS_RESPONSE_DATA_KEY in .env to the wrapping key name.\n"
            f"Top-level keys seen: {list(body.keys()) if isinstance(body, dict) else type(body)}"
        )
    if isinstance(body, dict) and key in body:
        return body[key]
    raise TeachworksApiError(
        f"Configured TEACHWORKS_RESPONSE_DATA_KEY='{key}' not found in response. "
        f"Top-level keys seen: {list(body.keys()) if isinstance(body, dict) else type(body)}"
    )


def fetch_students_sample(config):
    params = {
        config["per_page_param"]: config["test_student_limit"],
        config["page_param"]: 1,
    }
    body, resp = request_json(config, config["students_path"], params=params)
    records = extract_list(config, body)
    return records[: config["test_student_limit"]], {
        "url": resp.url,
        "status_code": resp.status_code,
        "params_sent": params,
    }


def fetch_lessons_sample(config):
    params = {
        config["per_page_param"]: config["test_lesson_limit"],
        config["page_param"]: 1,
    }
    if config["lesson_start_date_param"] and config["lesson_end_date_param"]:
        end = date.today()
        start = end - timedelta(days=config["test_lesson_lookback_days"])
        params[config["lesson_start_date_param"]] = start.isoformat()
        params[config["lesson_end_date_param"]] = end.isoformat()
    body, resp = request_json(config, config["lessons_path"], params=params)
    records = extract_list(config, body)
    return records[: config["test_lesson_limit"]], {
        "url": resp.url,
        "status_code": resp.status_code,
        "params_sent": params,
    }


def _walk(record, prefix="", depth=0, max_depth=2):
    if depth > max_depth or not isinstance(record, dict):
        return
    for k, v in record.items():
        path = f"{prefix}.{k}" if prefix else k
        yield path, v
        if isinstance(v, dict):
            yield from _walk(v, path, depth + 1, max_depth)


def analyze_lesson_fields(records):
    top_level_fields = set()
    candidate_values = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        top_level_fields.update(record.keys())
        for path, value in _walk(record):
            if isinstance(value, (dict, list)):
                continue
            if any(kw in path.lower() for kw in STATUS_KEYWORDS):
                candidate_values.setdefault(path, Counter())[repr(value)] += 1
    return {
        "top_level_fields": sorted(top_level_fields),
        "candidate_status_like_fields": {p: dict(c) for p, c in candidate_values.items()},
    }


def analyze_student_fields(records):
    top_level_fields = set()
    id_like_values = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        top_level_fields.update(record.keys())
        for k, v in record.items():
            if "id" in k.lower() and not isinstance(v, (dict, list)):
                id_like_values.setdefault(k, []).append(v)
    return {
        "top_level_fields": sorted(top_level_fields),
        "id_like_field_samples": id_like_values,
    }


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)


def run_test_mode(config, output_dir):
    print("=== Wright Academics Teachworks Audit -- TEST MODE (read-only) ===")
    print(f"Base URL: {config['base_url']}")
    headers = build_headers(config)
    print(f"Auth header: {config['auth_header']}: {mask(headers[config['auth_header']], config['api_key'])}")
    print()

    print(f"Fetching up to {config['test_student_limit']} students from {config['students_path']} ...")
    try:
        students, student_meta = fetch_students_sample(config)
    except TeachworksApiError as exc:
        print(f"\nERROR fetching students:\n{exc}\n")
        sys.exit(1)
    print(f"  -> received {len(students)} student record(s).")

    print(f"Fetching up to {config['test_lesson_limit']} lessons from {config['lessons_path']} ...")
    try:
        lessons, lesson_meta = fetch_lessons_sample(config)
    except TeachworksApiError as exc:
        print(f"\nERROR fetching lessons:\n{exc}\n")
        sys.exit(1)
    print(f"  -> received {len(lessons)} lesson record(s).")

    student_fields = analyze_student_fields(students)
    lesson_fields = analyze_lesson_fields(lessons)

    write_json(output_dir / "test_students_sample.json", students)
    write_json(output_dir / "test_lessons_sample.json", lessons)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request_meta": {"students": student_meta, "lessons": lesson_meta},
        "student_field_inventory": student_fields,
        "lesson_field_inventory": lesson_fields,
    }
    write_json(output_dir / "test_field_summary.json", summary)

    print("\n=== Candidate status/attendance-like fields found on lessons ===")
    if not lesson_fields["candidate_status_like_fields"]:
        print("  None found by keyword match -- inspect test_lessons_sample.json manually.")
    else:
        for path, values in lesson_fields["candidate_status_like_fields"].items():
            print(f"  {path}: {values}")

    print(f"\nWrote:\n  {output_dir / 'test_students_sample.json'}")
    print(f"  {output_dir / 'test_lessons_sample.json'}")
    print(f"  {output_dir / 'test_field_summary.json'}")
    print(
        "\nNothing was written to Teachworks or Monday.com, and nothing was committed. "
        "Send test_field_summary.json (and test_lessons_sample.json if useful) back for "
        "review before we define the attendance rule and build the full audit."
    )


# --- Full mode ---------------------------------------------------------------

def get_nested(record, dotted_key):
    if not dotted_key or not isinstance(record, dict):
        return None
    current = record
    for part in dotted_key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def detect_field(records, candidates, override=None, sample_size=50):
    if override:
        return override
    for cand in candidates:
        for r in records[:sample_size]:
            if get_nested(r, cand) not in (None, ""):
                return cand
    return None


def parse_date_value(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            return datetime.fromisoformat(iso_text).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def build_full_name(record, name_field, first_field, last_field):
    if name_field:
        val = get_nested(record, name_field)
        if val:
            return str(val)
    first = get_nested(record, first_field) if first_field else None
    last = get_nested(record, last_field) if last_field else None
    parts = [str(p) for p in (first, last) if p]
    return " ".join(parts) if parts else ""


def paginate_all(config, path, label, extra_params=None):
    """Page through every record at `path`.

    IMPORTANT: Teachworks may silently cap the page size below whatever
    per_page we request (confirmed: requesting 100 returns 80 per page, its
    real maximum). So we do NOT compare returned-count against the
    *requested* per_page to decide when to stop -- we compare against the
    *actual* number of records the API returned on page 1 (the "effective"
    page size), and only stop once a page comes back shorter than that, or
    empty. This means pagination self-corrects regardless of what
    TEACHWORKS_PER_PAGE is set to.

    extra_params: optional dict merged into every request's query params
    (e.g. a date-range filter) -- ignored (not merged) if None/empty, so
    existing callers are unaffected.
    """
    requested_per_page = config["per_page"]
    max_pages = config["max_pages"]
    page = 1
    all_records = []
    pages_fetched = 0
    effective_page_size = None
    while True:
        params = {config["page_param"]: page, config["per_page_param"]: requested_per_page}
        if extra_params:
            params.update(extra_params)
        try:
            body, resp = request_json(config, path, params=params)
        except TeachworksApiError as exc:
            if page == 1 and exc.status_code == 400 and requested_per_page > 10:
                new_per_page = max(requested_per_page // 2, 10)
                print(f"  per_page={requested_per_page} rejected (400) for {label}; retrying with per_page={new_per_page} ...")
                requested_per_page = new_per_page
                continue
            raise

        records = extract_list(config, body)
        pages_fetched += 1
        all_records.extend(records)

        if page == 1:
            effective_page_size = len(records)
            if effective_page_size and effective_page_size != requested_per_page:
                print(
                    f"  NOTE: requested per_page={requested_per_page} for {label}, but the API "
                    f"returned {effective_page_size} on page 1. Using {effective_page_size} as "
                    "the real page size for pagination (will not stop early because of this)."
                )

        print(f"  page {page}: +{len(records)} {label} (running total {len(all_records)})")

        if len(records) == 0:
            break
        if effective_page_size and len(records) < effective_page_size:
            break
        page += 1
        if page > max_pages:
            print(
                f"  WARNING: reached TEACHWORKS_MAX_PAGES={max_pages} while paging {label}; "
                "stopping early. Increase TEACHWORKS_MAX_PAGES in .env if more data remains.",
                file=sys.stderr,
            )
            break
        if config["request_delay_seconds"]:
            time.sleep(config["request_delay_seconds"])
    return all_records, pages_fetched


def month_bucket(d):
    return (d.year, d.month)


def previous_month_bucket(today):
    if today.month == 1:
        return (today.year - 1, 12)
    return (today.year, today.month - 1)


def _is_safe_participant_value_key(key):
    lower = key.lower()
    return any(kw in lower for kw in PARTICIPANT_SAFE_VALUE_KEYWORDS)


def inspect_participants(lessons_sample, participants_field):
    """Safely inspect the shape of the participants field on a small sample.

    Reports structure only -- container/item types, key names + Python
    types, and REAL VALUES only for keys that look like an id/type/category/
    role/kind/status (never for anything else, so names/emails/phone/address
    etc. are never printed or written to disk).
    """
    result = {
        "participants_field": participants_field,
        "sample_size": len(lessons_sample),
        "field_present_on_sample": 0,
        "container_types_seen": Counter(),
        "item_types_seen": Counter(),
        "keys_seen_on_participant_objects": {},
    }
    for lesson in lessons_sample:
        val = lesson.get(participants_field) if isinstance(lesson, dict) else None
        if val is None:
            continue
        result["field_present_on_sample"] += 1
        result["container_types_seen"][type(val).__name__] += 1
        items = val if isinstance(val, list) else [val]
        for item in items:
            result["item_types_seen"][type(item).__name__] += 1
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                entry = result["keys_seen_on_participant_objects"].setdefault(
                    k, {"type_seen": set(), "safe_sample_values": set()}
                )
                entry["type_seen"].add(type(v).__name__)
                if _is_safe_participant_value_key(k) and not isinstance(v, (dict, list)):
                    entry["safe_sample_values"].add(repr(v))

    for entry in result["keys_seen_on_participant_objects"].values():
        entry["type_seen"] = sorted(entry["type_seen"])
        entry["safe_sample_values"] = sorted(entry["safe_sample_values"])[:10]
    result["container_types_seen"] = dict(result["container_types_seen"])
    result["item_types_seen"] = dict(result["item_types_seen"])
    return result


def print_participant_inspection(inspection):
    print("=== PARTICIPANT STRUCTURE INSPECTION (sanitized -- no PII) ===")
    print(
        f"Sampled {inspection['sample_size']} lesson record(s); "
        f"'{inspection['participants_field']}' present on {inspection['field_present_on_sample']} of them."
    )
    print(f"Container type(s) seen: {inspection['container_types_seen']}")
    print(f"Participant item type(s) seen: {inspection['item_types_seen']}")
    if inspection["keys_seen_on_participant_objects"]:
        print("Keys on participant objects (values shown ONLY for id/type/category/role/kind/status-like keys):")
        for k, info in inspection["keys_seen_on_participant_objects"].items():
            if info["safe_sample_values"]:
                shown = f", sample values: {info['safe_sample_values']}"
            else:
                shown = " (value withheld -- not an id/type-like field, could be PII)"
            print(f"  - {k} (type: {', '.join(info['type_seen'])}){shown}")
    else:
        print("No participant object keys found (participants may be missing, empty, or not object-shaped).")


def resolve_participant_strategy(inspection, config):
    """Decide, from the sanitized inspection, how to pull student id(s) out
    of a lesson's participants. Returns None if it cannot be done safely --
    callers must stop rather than guess.
    """
    override_id = config["participant_id_field"]
    if override_id:
        return {
            "strategy": "manual_override",
            "id_field": override_id,
            "type_field": config["participant_type_field"] or None,
            "student_marker": config["participant_student_type_value"],
        }

    keys_info = inspection["keys_seen_on_participant_objects"]
    key_names = list(keys_info.keys())
    if not key_names:
        return None

    # Strategy A: an id-like key whose own name says "student" (unambiguous by name alone).
    direct_student_id_keys = [k for k in key_names if "student" in k.lower() and "id" in k.lower()]
    if len(direct_student_id_keys) == 1:
        return {
            "strategy": "direct_student_id_field",
            "id_field": direct_student_id_keys[0],
            "type_field": None,
            "student_marker": None,
        }

    # Strategy B: a type/role/category/kind key whose observed values mention
    # "student", paired with exactly one other id-like key to read the id from.
    type_like_keys = [k for k in key_names if any(t in k.lower() for t in PARTICIPANT_TYPE_KEY_KEYWORDS)]
    for tk in type_like_keys:
        values = keys_info[tk]["safe_sample_values"]
        if any("student" in v.lower() for v in values):
            id_like_keys = [k for k in key_names if "id" in k.lower() and k != tk]
            if len(id_like_keys) == 1:
                return {
                    "strategy": "type_discrimination",
                    "id_field": id_like_keys[0],
                    "type_field": tk,
                    "student_marker": "student",
                }

    return None


def extract_student_participant_ids(lesson, participants_field, id_field, type_field, student_marker):
    """Returns a list of student id(s) for one lesson (may be empty, one, or many)."""
    val = lesson.get(participants_field) if isinstance(lesson, dict) else None
    if val is None:
        return []
    items = val if isinstance(val, list) else [val]
    ids = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if type_field:
            type_val = item.get(type_field)
            if type_val is None or student_marker.lower() not in str(type_val).lower():
                continue
        sid = item.get(id_field) if id_field else None
        if sid is not None:
            ids.append(sid)
    return ids


def load_or_fetch_all(config, output_dir, refresh_cache):
    """Fetch ALL students and lessons via paginate_all(), or reuse a prior
    successful fetch from disk. This exists purely so that a downstream
    failure (e.g. writing the CSV) doesn't force re-downloading tens of
    thousands of lessons -- it does not change what paginate_all() does or
    how records are counted.
    """
    cache_dir = output_dir / "_cache"
    students_cache = cache_dir / "students_raw.json"
    lessons_cache = cache_dir / "lessons_raw.json"
    meta_cache = cache_dir / "cache_meta.json"

    if not refresh_cache and students_cache.exists() and lessons_cache.exists() and meta_cache.exists():
        try:
            with open(students_cache, "r", encoding="utf-8") as f:
                students = json.load(f)
            with open(lessons_cache, "r", encoding="utf-8") as f:
                lessons = json.load(f)
            with open(meta_cache, "r", encoding="utf-8") as f:
                meta = json.load(f)
            print(
                f"Using cached Teachworks data fetched at {meta.get('fetched_at')}: "
                f"{meta.get('students_total')} students ({meta.get('students_pages')} page(s)), "
                f"{meta.get('lessons_total')} lessons ({meta.get('lessons_pages')} page(s)).\n"
                "Pass --refresh-cache to re-fetch from Teachworks instead.\n"
            )
            return students, meta.get("students_pages", 0), lessons, meta.get("lessons_pages", 0)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"  WARNING: could not read cache ({exc}); re-fetching from Teachworks.\n", file=sys.stderr)

    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"Paging through ALL students from {config['students_path']} ...")
    try:
        students, students_pages = paginate_all(config, config["students_path"], "students")
    except TeachworksApiError as exc:
        print(f"\nERROR fetching students:\n{exc}\n")
        sys.exit(1)
    print(f"  -> total students fetched: {len(students)} across {students_pages} page(s)\n")

    print(f"Paging through ALL lessons from {config['lessons_path']} ...")
    try:
        lessons, lessons_pages = paginate_all(config, config["lessons_path"], "lessons")
    except TeachworksApiError as exc:
        print(f"\nERROR fetching lessons:\n{exc}\n")
        sys.exit(1)
    print(f"  -> total lessons fetched: {len(lessons)} across {lessons_pages} page(s)\n")

    write_json(students_cache, students)
    write_json(lessons_cache, lessons)
    write_json(meta_cache, {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "students_total": len(students),
        "lessons_total": len(lessons),
        "students_pages": students_pages,
        "lessons_pages": lessons_pages,
        "base_url": config["base_url"],
        "students_path": config["students_path"],
        "lessons_path": config["lessons_path"],
    })
    print(
        f"Cached raw students/lessons to {cache_dir} so a later failure (e.g. writing the CSV) "
        "won't require re-downloading everything -- pass --refresh-cache to force a fresh pull, "
        "or delete that folder.\n"
    )

    return students, students_pages, lessons, lessons_pages


def compute_attendance_rows(config, students, lessons, participant_strategy):
    """Core attendance computation, shared by the CSV audit (run_full_mode)
    and any other consumer (e.g. the Monday.com sync) that needs the same
    validated per-student numbers. Pure function of already-fetched
    students/lessons -- does no network I/O itself.

    Returns (rows, field_resolution, diagnostics):
      rows: one dict per Teachworks student (every student, including those
        with zero attended sessions), with keys "Student Name",
        "Teachworks Student ID", "Lifetime Attended Sessions",
        "First Attended Session Date", "Most Recent Attended Session Date",
        "Sessions This Month", "Sessions Last Month", "Most Recent Tutor ID",
        "Most Recent Tutor Name".
      field_resolution: which real field names were used/detected.
      diagnostics: counts useful for a summary report (status_counts,
        all_lesson_dates, unparseable_dates, future_excluded,
        orphaned_attended, attended_no_participant_resolved, zero_attended).
    """
    student_id_field = config["student_id_field"] or "id"
    student_name_field = detect_field(students, STUDENT_NAME_CANDIDATES, override=config["student_name_field"])
    student_first_field = detect_field(students, STUDENT_FIRST_NAME_CANDIDATES, override=config["student_first_name_field"])
    student_last_field = detect_field(students, STUDENT_LAST_NAME_CANDIDATES, override=config["student_last_name_field"])

    lesson_date_field = detect_field(lessons, LESSON_DATE_CANDIDATES, override=config["lesson_date_field"])
    lesson_tutor_id_field = detect_field(lessons, LESSON_TUTOR_ID_CANDIDATES, override=config["lesson_tutor_id_field"])
    lesson_tutor_name_field = detect_field(lessons, LESSON_TUTOR_NAME_CANDIDATES, override=config["lesson_tutor_name_field"])

    if not lesson_date_field:
        keys_seen = sorted(lessons[0].keys()) if lessons else []
        print(
            "ERROR: could not find a date field on lesson records.\n"
            f"Tried: {LESSON_DATE_CANDIDATES}. Fields seen on first lesson: {keys_seen}\n"
            "Set TEACHWORKS_LESSON_DATE_FIELD in .env to the correct field name and re-run."
        )
        sys.exit(1)

    status_field = config["status_field"]
    if lessons and not any(status_field in r for r in lessons[:50] if isinstance(r, dict)):
        print(
            f"WARNING: configured TEACHWORKS_STATUS_FIELD='{status_field}' was not found on the "
            f"first 50 lesson records. Fields seen on first lesson: {sorted(lessons[0].keys())}",
            file=sys.stderr,
        )

    # --- Optional external tutor lookup (only used if lessons don't embed a tutor name) ---
    tutor_name_lookup = {}
    if not lesson_tutor_name_field and config["tutors_path"]:
        print(f"Paging through tutors/employees from {config['tutors_path']} to resolve tutor names ...")
        try:
            tutors, _tutors_pages = paginate_all(config, config["tutors_path"], "tutors")
        except TeachworksApiError as exc:
            print(f"  WARNING: could not fetch tutors ({exc}); tutor names will be left blank.", file=sys.stderr)
            tutors = []
        tutor_id_field = config["tutor_record_id_field"] or "id"
        tutor_name_field_on_record = detect_field(tutors, TUTOR_NAME_CANDIDATES, override=config["tutor_record_name_field"])
        tutor_first = detect_field(tutors, STUDENT_FIRST_NAME_CANDIDATES)
        tutor_last = detect_field(tutors, STUDENT_LAST_NAME_CANDIDATES)
        for t in tutors:
            tid = get_nested(t, tutor_id_field)
            if tid is None:
                continue
            tutor_name_lookup[str(tid)] = build_full_name(t, tutor_name_field_on_record, tutor_first, tutor_last)
        print(f"  -> resolved {len(tutor_name_lookup)} tutor name(s).\n")

    field_resolution = {
        "student_id_field": student_id_field,
        "student_name_field": student_name_field,
        "student_first_name_field": student_first_field,
        "student_last_name_field": student_last_field,
        "lesson_date_field": lesson_date_field,
        "lesson_status_field": status_field,
        "lesson_tutor_id_field": lesson_tutor_id_field,
        "lesson_tutor_name_field": lesson_tutor_name_field,
        "tutor_name_lookup_used": bool(tutor_name_lookup),
        "tutor_names_resolved": len(tutor_name_lookup),
        "participant_resolution": participant_strategy,
    }
    print("Field resolution used for this run:")
    for k, v in field_resolution.items():
        print(f"  {k}: {v}")
    print()

    # --- Build students index ---
    students_by_id = {}
    for s in students:
        sid = get_nested(s, student_id_field)
        if sid is None:
            continue
        students_by_id[str(sid)] = s

    # --- Walk all lessons once: status audit + attended bucketing ---
    attended_value = config["attended_status_value"]
    today = date.today()

    attended_by_student = defaultdict(list)  # student_id(str) -> [(date, tutor_id, tutor_name)]
    status_counts = Counter()
    all_lesson_dates = []
    unparseable_dates = 0
    future_excluded = 0
    orphaned_attended = 0
    attended_no_participant_resolved = 0

    for lesson in lessons:
        status_value = get_nested(lesson, status_field)
        status_counts[str(status_value)] += 1

        parsed = parse_date_value(get_nested(lesson, lesson_date_field))
        if parsed:
            all_lesson_dates.append(parsed)
        else:
            unparseable_dates += 1

        if status_value != attended_value:
            continue
        if parsed is None:
            continue
        if parsed > today:
            future_excluded += 1
            continue

        student_ids = extract_student_participant_ids(
            lesson,
            config["participants_field"],
            participant_strategy["id_field"],
            participant_strategy["type_field"],
            participant_strategy["student_marker"],
        )
        if not student_ids:
            attended_no_participant_resolved += 1
            continue

        tutor_id = get_nested(lesson, lesson_tutor_id_field) if lesson_tutor_id_field else None
        if lesson_tutor_name_field:
            tutor_name = get_nested(lesson, lesson_tutor_name_field) or ""
        elif tutor_id is not None:
            tutor_name = tutor_name_lookup.get(str(tutor_id), "")
        else:
            tutor_name = ""

        # A lesson can have more than one student participant; credit each of them.
        for sid in student_ids:
            sid_str = str(sid)
            if sid_str not in students_by_id:
                orphaned_attended += 1
                continue
            attended_by_student[sid_str].append((parsed, tutor_id, tutor_name))

    # --- Build per-student rows ---
    this_month = month_bucket(today)
    last_month = previous_month_bucket(today)
    rows = []
    zero_attended = 0

    for sid_str, s in students_by_id.items():
        name = build_full_name(s, student_name_field, student_first_field, student_last_field)
        sessions = sorted(attended_by_student.get(sid_str, []), key=lambda t: t[0])
        lifetime = len(sessions)

        if lifetime == 0:
            zero_attended += 1
            row = {
                "Student Name": name,
                "Teachworks Student ID": sid_str,
                "Lifetime Attended Sessions": 0,
                "First Attended Session Date": "",
                "Most Recent Attended Session Date": "",
                "Sessions This Month": 0,
                "Sessions Last Month": 0,
                "Most Recent Tutor ID": "",
                "Most Recent Tutor Name": "",
            }
        else:
            this_month_count = sum(1 for d, _, _ in sessions if month_bucket(d) == this_month)
            last_month_count = sum(1 for d, _, _ in sessions if month_bucket(d) == last_month)
            last_date, last_tutor_id, last_tutor_name = sessions[-1]
            row = {
                "Student Name": name,
                "Teachworks Student ID": sid_str,
                "Lifetime Attended Sessions": lifetime,
                "First Attended Session Date": sessions[0][0].isoformat(),
                "Most Recent Attended Session Date": last_date.isoformat(),
                "Sessions This Month": this_month_count,
                "Sessions Last Month": last_month_count,
                "Most Recent Tutor ID": last_tutor_id if last_tutor_id is not None else "",
                "Most Recent Tutor Name": last_tutor_name or "",
            }
        rows.append(row)

    rows.sort(key=lambda r: (r["Student Name"].lower(), r["Teachworks Student ID"]))

    diagnostics = {
        "status_counts": status_counts,
        "all_lesson_dates": all_lesson_dates,
        "unparseable_dates": unparseable_dates,
        "future_excluded": future_excluded,
        "orphaned_attended": orphaned_attended,
        "attended_no_participant_resolved": attended_no_participant_resolved,
        "zero_attended": zero_attended,
    }
    return rows, field_resolution, diagnostics


def run_participant_preflight(config, output_dir):
    """Sample a few real lessons and safely resolve how to pull student
    participant id(s) out of them, BEFORE any expensive full pull. Shared by
    the CSV audit and the Monday sync so both use the identical, validated
    resolution -- never re-derived or guessed independently.

    Exits the process (no CSV/report is written) if the structure can't be
    resolved safely; otherwise returns the participant_strategy dict.
    """
    print(
        f"Pre-flight: sampling {config['participant_sample_size']} lesson(s) from "
        f"{config['lessons_path']} to inspect '{config['participants_field']}' structure ..."
    )
    preflight_params = {
        config["per_page_param"]: config["participant_sample_size"],
        config["page_param"]: 1,
    }
    try:
        body, resp = request_json(config, config["lessons_path"], params=preflight_params)
    except TeachworksApiError as exc:
        print(f"\nERROR fetching lesson sample for participant inspection:\n{exc}\n")
        sys.exit(1)
    sample_lessons = extract_list(config, body)[: config["participant_sample_size"]]

    inspection = inspect_participants(sample_lessons, config["participants_field"])
    print()
    print_participant_inspection(inspection)
    write_json(output_dir / "wright-teachworks-participant-structure.json", inspection)
    print(f"\nWrote sanitized structure to: {output_dir / 'wright-teachworks-participant-structure.json'}")

    participant_strategy = resolve_participant_strategy(inspection, config)
    if participant_strategy is None:
        print(
            "\nSTOPPING: could not safely determine which participant field identifies the "
            "attending student(s) from the structure above. No output was written -- producing "
            "counts on a guess would risk incorrect data.\n"
            "Review 'wright-teachworks-participant-structure.json', then set "
            "TEACHWORKS_PARTICIPANT_ID_FIELD (and, if participants mixes roles, "
            "TEACHWORKS_PARTICIPANT_TYPE_FIELD / TEACHWORKS_PARTICIPANT_STUDENT_TYPE_VALUE) "
            "in .env to the correct field name(s), then re-run."
        )
        sys.exit(1)
    print("\nParticipant resolution strategy:")
    for k, v in participant_strategy.items():
        print(f"  {k}: {v}")
    print()
    return participant_strategy


def run_full_mode(config, output_dir, refresh_cache=False):
    print("=== Wright Academics Teachworks Audit -- FULL MODE (read-only) ===")
    print(f"Base URL: {config['base_url']}")
    headers = build_headers(config)
    print(f"Auth header: {config['auth_header']}: {mask(headers[config['auth_header']], config['api_key'])}")
    print(f"Attendance rule: lesson['{config['status_field']}'] == '{config['attended_status_value']}' (exact match)")
    print()

    participant_strategy = run_participant_preflight(config, output_dir)

    # --- Full pagination (only after the above confirms we can count safely) ---
    # Cached to disk on success so a later failure downstream (e.g. writing
    # the CSV) doesn't force re-downloading everything again.
    students, students_pages, lessons, lessons_pages = load_or_fetch_all(config, output_dir, refresh_cache)

    rows, field_resolution, diagnostics = compute_attendance_rows(config, students, lessons, participant_strategy)
    status_counts = diagnostics["status_counts"]
    all_lesson_dates = diagnostics["all_lesson_dates"]
    unparseable_dates = diagnostics["unparseable_dates"]
    future_excluded = diagnostics["future_excluded"]
    orphaned_attended = diagnostics["orphaned_attended"]
    attended_no_participant_resolved = diagnostics["attended_no_participant_resolved"]
    zero_attended = diagnostics["zero_attended"]
    attended_value = config["attended_status_value"]

    # --- Write CSV ---
    csv_path = output_dir / "wright-teachworks-session-audit.csv"
    fieldnames = [
        "Student Name", "Teachworks Student ID", "Lifetime Attended Sessions",
        "First Attended Session Date", "Most Recent Attended Session Date",
        "Sessions This Month", "Sessions Last Month",
        "Most Recent Tutor ID", "Most Recent Tutor Name",
    ]
    # utf-8-sig: plain "utf-8" makes Excel on Windows misread accented names as
    # garbled bytes (no BOM to signal the encoding); the BOM fixes that while
    # still opening cleanly everywhere else.
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # --- Write status/summary JSON ---
    total_attended_by_status = status_counts.get(attended_value, 0)
    total_attended_counted = sum(r["Lifetime Attended Sessions"] for r in rows)
    earliest = min(all_lesson_dates) if all_lesson_dates else None
    latest = max(all_lesson_dates) if all_lesson_dates else None

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "attendance_rule": f"lesson['{config['status_field']}'] == '{attended_value}' (exact match, case-sensitive)",
        "pagination": {
            "students_pages_fetched": students_pages,
            "students_total": len(students),
            "lessons_pages_fetched": lessons_pages,
            "lessons_total": len(lessons),
        },
        "field_resolution": field_resolution,
        "totals": {
            "total_students_fetched": len(students),
            "total_lessons_fetched": len(lessons),
            "total_attended_lessons_by_status": total_attended_by_status,
            "total_attended_lessons_counted_per_student": total_attended_counted,
            "attended_lessons_excluded_future_dated": future_excluded,
            "attended_lessons_excluded_no_matching_student": orphaned_attended,
            "attended_lessons_with_no_resolved_student_participant": attended_no_participant_resolved,
            "lessons_with_unparseable_date": unparseable_dates,
            "students_with_zero_attended_sessions": zero_attended,
        },
        "unique_statuses_discovered": dict(status_counts.most_common()),
        "earliest_lesson_date_retrieved": earliest.isoformat() if earliest else None,
        "latest_lesson_date_retrieved": latest.isoformat() if latest else None,
    }
    summary_path = output_dir / "wright-teachworks-status-summary.json"
    write_json(summary_path, summary)

    # --- Console summary ---
    print("\n=== PAGINATION ===")
    print(f"Pages of students fetched: {students_pages}")
    print(f"Total students: {len(students)}")
    print(f"Pages of lessons fetched: {lessons_pages}")
    print(f"Total lessons: {len(lessons)}")

    print("\n=== SUMMARY ===")
    print(f"Total students: {len(students)}")
    print(f"Total lessons retrieved: {len(lessons)}")
    print(f"Total attended lessons (status == '{attended_value}'): {total_attended_by_status}")
    print(f"  of which counted toward per-student totals: {total_attended_counted}")
    print(f"  excluded (future-dated): {future_excluded}")
    print(f"  excluded (no matching student record): {orphaned_attended}")
    print(f"  excluded (no student participant could be resolved on the lesson): {attended_no_participant_resolved}")
    print(f"  skipped (unparseable date): {unparseable_dates}")
    print(f"Earliest lesson date retrieved: {earliest.isoformat() if earliest else 'N/A'}")
    print(f"Latest lesson date retrieved: {latest.isoformat() if latest else 'N/A'}")
    print("Unique statuses discovered:")
    for status_value, count in status_counts.most_common():
        print(f"  {status_value}: {count}")
    print(f"Students with zero attended lessons: {zero_attended}")

    print(f"\nWrote:\n  {csv_path}\n  {summary_path}")
    print(
        "\nNothing was written to Teachworks or Monday.com, and nothing was committed. "
        "This is a local, read-only audit report."
    )


def main():
    parser = argparse.ArgumentParser(description="Wright Academics Teachworks read-only audit.")
    parser.add_argument(
        "--mode",
        choices=["test", "full"],
        default="test",
        help=(
            "'test': small sample + field discovery (safe default). "
            "'full': page through ALL students/lessons and write the full CSV audit."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write output files to (default: ./output next to this script).",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help=(
            "Full mode caches the raw students/lessons pull in output/_cache/ so a later "
            "failure (e.g. writing the CSV) doesn't require re-downloading everything. By "
            "default a full-mode run reuses that cache if present; pass this flag to ignore "
            "it and re-fetch fresh data from Teachworks."
        ),
    )
    args = parser.parse_args()

    config = load_config()
    output_dir = Path(args.output_dir) if args.output_dir else SCRIPT_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "test":
        run_test_mode(config, output_dir)
    else:
        run_full_mode(config, output_dir, refresh_cache=args.refresh_cache)


if __name__ == "__main__":
    main()
