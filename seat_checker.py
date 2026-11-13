from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_VERSION = "v2"
DEFAULT_API_BASE = "https://public.enroll.wisc.edu"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_NTFY_PRIORITY = 4
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
STATE_VERSION = 1
TOTAL_FAILURE_ALERT_THRESHOLD = 2


class SeatCheckerError(Exception):
    pass


class ConfigError(SeatCheckerError):
    pass


class FetchError(SeatCheckerError):
    pass


class StateError(SeatCheckerError):
    pass


class NotifyError(SeatCheckerError):
    pass


class LockError(SeatCheckerError):
    pass


@dataclass(frozen=True)
class CourseConfig:
    term: str
    subject_code: str
    course_id: str
    label: str | None = None

    @property
    def key(self) -> str:
        return f"{self.term}:{self.subject_code}:{self.course_id}"

    @property
    def fallback_label(self) -> str:
        return f"{self.subject_code} {self.course_id}"


@dataclass(frozen=True)
class Config:
    courses: tuple[CourseConfig, ...]
    state_file: Path
    api_base: str
    ntfy_server: str
    ntfy_topic: str
    ntfy_token_env: str
    ntfy_priority: int
    timeout_seconds: float
    request_delay_seconds: float
    user_agent: str


@dataclass(frozen=True)
class PackageSnapshot:
    doc_id: str
    available_seats: int
    open_seats: int | None
    status: str
    waitlist_current_size: int | None
    waitlist_capacity: int | None
    open_waitlist_spots: int | None
    sections: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "available_seats": self.available_seats,
            "open_seats": self.open_seats,
            "status": self.status,
            "waitlist_current_size": self.waitlist_current_size,
            "waitlist_capacity": self.waitlist_capacity,
            "open_waitlist_spots": self.open_waitlist_spots,
            "sections": list(self.sections),
        }

    @classmethod
    def from_dict(cls, data: Any) -> PackageSnapshot:
        if not isinstance(data, dict):
            raise StateError("state contains an invalid package entry")
        doc_id = _as_str(data.get("doc_id"))
        available_seats = _as_int(data.get("available_seats"))
        if not doc_id or available_seats is None or available_seats < 0:
            raise StateError("state contains an invalid package entry")
        raw_sections = data.get("sections", [])
        if not isinstance(raw_sections, (list, tuple)):
            raise StateError("state contains an invalid sections entry")
        sections = tuple(str(item) for item in raw_sections if isinstance(item, str))
        status = _as_str(data.get("status")) or "UNKNOWN"
        return cls(
            doc_id=doc_id,
            available_seats=available_seats,
            open_seats=_as_int(data.get("open_seats")),
            status=status,
            waitlist_current_size=_as_int(data.get("waitlist_current_size")),
            waitlist_capacity=_as_int(data.get("waitlist_capacity")),
            open_waitlist_spots=_as_int(data.get("open_waitlist_spots")),
            sections=sections,
        )


@dataclass(frozen=True)
class ParsedCourse:
    display_name: str | None
    packages: dict[str, PackageSnapshot]
    malformed_doc_ids: frozenset[str]


@dataclass(frozen=True)
class PackageChange:
    doc_id: str
    current: PackageSnapshot
    previous: PackageSnapshot | None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.isdigit() or (
            candidate.startswith("-") and candidate[1:].isdigit()
        ):
            return int(candidate)
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_string(value: Any, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise ConfigError(f"'{field}' must be a non-empty string")


def _course_id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:06d}"
    if isinstance(value, str) and value.strip():
        candidate = value.strip()
        if candidate.isdigit():
            return candidate.zfill(6)
        return candidate
    raise ConfigError("each course needs a non-empty 'course_id'")


def _subject_code(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:03d}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigError("each course needs a non-empty 'subject_code'")


def load_config(path: Path | str) -> Config:
    config_path = Path(path).expanduser()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a TOML table")

    term = _require_string(raw.get("term"), "term")

    courses_raw = raw.get("courses")
    if not isinstance(courses_raw, list) or not courses_raw:
        raise ConfigError("config must contain at least one [[courses]] entry")

    courses: list[CourseConfig] = []
    seen_keys: set[str] = set()
    for index, entry in enumerate(courses_raw, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"courses entry {index} must be a table")
        course = CourseConfig(
            term=term,
            subject_code=_subject_code(entry.get("subject_code")),
            course_id=_course_id(entry.get("course_id")),
            label=_as_str(entry.get("label")),
        )
        if course.key in seen_keys:
            raise ConfigError(f"duplicate course entry: {course.key}")
        seen_keys.add(course.key)
        courses.append(course)

    state_value = raw.get("state_file", "state.json")
    if not isinstance(state_value, str) or not state_value.strip():
        raise ConfigError("'state_file' must be a non-empty string")
    state_file = Path(state_value).expanduser()
    if not state_file.is_absolute():
        state_file = config_path.parent / state_file

    api_base = _as_str(raw.get("api_base")) or DEFAULT_API_BASE
    ntfy_server = _as_str(raw.get("ntfy_server")) or DEFAULT_NTFY_SERVER
    ntfy_topic = _require_string(raw.get("ntfy_topic"), "ntfy_topic")
    ntfy_token_env = _as_str(raw.get("ntfy_token_env")) or "NTFY_TOKEN"
    ntfy_priority_raw = raw.get("ntfy_priority", DEFAULT_NTFY_PRIORITY)
    ntfy_priority = _as_int(ntfy_priority_raw)
    if ntfy_priority is None or not 1 <= ntfy_priority <= 5:
        raise ConfigError("'ntfy_priority' must be an integer from 1 to 5")
    timeout_raw = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    timeout_seconds = float(timeout_raw) if isinstance(timeout_raw, (int, float)) else 0.0
    if timeout_seconds <= 0:
        raise ConfigError("'timeout_seconds' must be greater than zero")
    delay_raw = raw.get("request_delay_seconds", DEFAULT_REQUEST_DELAY_SECONDS)
    request_delay_seconds = (
        float(delay_raw) if isinstance(delay_raw, (int, float)) else -1.0
    )
    if request_delay_seconds < 0:
        raise ConfigError("'request_delay_seconds' must be zero or greater")
    user_agent = _as_str(raw.get("user_agent")) or DEFAULT_USER_AGENT

    return Config(
        courses=tuple(courses),
        state_file=state_file,
        api_base=api_base.rstrip("/"),
        ntfy_server=ntfy_server.rstrip("/"),
        ntfy_topic=ntfy_topic,
        ntfy_token_env=ntfy_token_env,
        ntfy_priority=ntfy_priority,
        timeout_seconds=timeout_seconds,
        request_delay_seconds=request_delay_seconds,
        user_agent=user_agent,
    )


def _course_url(config: Config, course: CourseConfig) -> str:
    term = urllib.parse.quote(course.term, safe="")
    subject = urllib.parse.quote(course.subject_code, safe="")
    course_id = urllib.parse.quote(course.course_id, safe="")
    return (
        f"{config.api_base}/api/search/{API_VERSION}/enrollmentPackages/"
        f"{term}/{subject}/{course_id}"
    )


def _course_click_url(course: CourseConfig, display_name: str) -> str:
    keywords = urllib.parse.quote_plus(display_name)
    return f"https://public.enroll.wisc.edu/search?term={course.term}&keywords={keywords}"


def _display_name_from_package(raw: dict[str, Any]) -> str | None:
    sections = raw.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            subject = section.get("subject")
            short_name = (
                _as_str(subject.get("shortDescription"))
                if isinstance(subject, dict)
                else None
            )
            catalog = _as_str(section.get("catalogNumber")) or _as_str(
                raw.get("catalogNumber")
            )
            if short_name and catalog:
                return f"{short_name} {catalog}"
    catalog = _as_str(raw.get("catalogNumber"))
    subject_code = _as_str(raw.get("subjectCode"))
    if catalog:
        return f"{subject_code or 'COURSE'} {catalog}"
    return None


def _section_labels(raw: dict[str, Any]) -> tuple[str, ...]:
    labels: list[str] = []
    sections = raw.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            number = _as_str(section.get("sectionNumber"))
            section_type = _as_str(section.get("type"))
            if number and section_type:
                label = f"{number} {section_type}"
            elif number:
                label = number
            elif section_type:
                label = section_type
            else:
                continue
            if label not in labels:
                labels.append(label)
    if not labels:
        class_number = _as_str(raw.get("enrollmentClassNumber"))
        if class_number:
            labels.append(f"class {class_number}")
    return tuple(labels)


def _package_value(raw: dict[str, Any], field: str) -> int | None:
    status = raw.get("enrollmentStatus")
    if isinstance(status, dict):
        value = _as_int(status.get(field))
        if value is not None:
            return value
    sections = raw.get("sections")
    if isinstance(sections, list):
        values: list[int] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_status = section.get("enrollmentStatus")
            if not isinstance(section_status, dict):
                continue
            value = _as_int(section_status.get(field))
            if value is not None:
                values.append(value)
        if values:
            return sum(values)
    return None


def parse_course_payload(payload: Any, course: CourseConfig) -> ParsedCourse:
    if isinstance(payload, dict):
        raw_packages = [payload]
    elif isinstance(payload, list):
        raw_packages = payload
    else:
        raise FetchError("response was neither a package array nor a package object")
    if not raw_packages:
        raise FetchError("response contained no enrollment packages")

    packages: dict[str, PackageSnapshot] = {}
    malformed_doc_ids: set[str] = set()
    display_name: str | None = None

    for raw in raw_packages:
        if not isinstance(raw, dict):
            continue

        term = _as_str(raw.get("termCode"))
        if term is not None and term != course.term:
            raise FetchError(
                f"response term {term} does not match requested term {course.term}"
            )
        course_id = _as_str(raw.get("courseId"))
        if course_id is not None and course_id != course.course_id:
            raise FetchError(
                f"response course {course_id} does not match requested course {course.course_id}"
            )

        doc_id = _as_str(raw.get("docId"))
        if doc_id is None:
            logging.warning("skipping package without docId for %s", course.key)
            continue

        if display_name is None:
            display_name = _display_name_from_package(raw)

        package_status = raw.get("packageEnrollmentStatus")
        if not isinstance(package_status, dict):
            logging.warning("package %s has no packageEnrollmentStatus", doc_id)
            malformed_doc_ids.add(doc_id)
            continue
        available_seats = _as_int(package_status.get("availableSeats"))
        if available_seats is None or available_seats < 0:
            logging.warning("package %s has no usable availableSeats", doc_id)
            malformed_doc_ids.add(doc_id)
            continue

        status = _as_str(package_status.get("status")) or "UNKNOWN"
        snapshot = PackageSnapshot(
            doc_id=doc_id,
            available_seats=available_seats,
            open_seats=_package_value(raw, "openSeats"),
            status=status,
            waitlist_current_size=_package_value(raw, "waitlistCurrentSize"),
            waitlist_capacity=_package_value(raw, "waitlistCapacity"),
            open_waitlist_spots=_package_value(raw, "openWaitlistSpots"),
            sections=_section_labels(raw),
        )
        packages[doc_id] = snapshot

    if not packages:
        raise FetchError("response contained no usable enrollment packages")

    return ParsedCourse(
        display_name=display_name,
        packages=packages,
        malformed_doc_ids=frozenset(malformed_doc_ids),
    )


def fetch_course(config: Config, course: CourseConfig) -> ParsedCourse:
    url = _course_url(config, course)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": config.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://public.enroll.wisc.edu",
            "Referer": f"https://public.enroll.wisc.edu/search?term={course.term}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            status = response.getcode()
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError("request timed out") from exc

    if status != 200:
        raise FetchError(f"HTTP {status}")
    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError("response exceeded size limit")
    if not body.strip():
        raise FetchError("empty response")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError("response was not valid JSON") from exc

    return parse_course_payload(payload, course)


def find_changes(
    previous: dict[str, PackageSnapshot],
    current: ParsedCourse,
) -> list[PackageChange]:
    changes: list[PackageChange] = []
    for doc_id, snapshot in current.packages.items():
        if snapshot.available_seats <= 0:
            continue
        prior = previous.get(doc_id)
        if prior is None or snapshot.available_seats > prior.available_seats:
            changes.append(PackageChange(doc_id=doc_id, current=snapshot, previous=prior))
    return changes


def merge_packages(
    previous: dict[str, PackageSnapshot],
    current: ParsedCourse,
) -> dict[str, PackageSnapshot]:
    merged = dict(current.packages)
    for doc_id in current.malformed_doc_ids:
        if doc_id not in merged and doc_id in previous:
            merged[doc_id] = previous[doc_id]
    return merged


def load_previous_packages(course_state: Any) -> dict[str, PackageSnapshot]:
    if not isinstance(course_state, dict):
        return {}
    raw_packages = course_state.get("packages", {})
    if not isinstance(raw_packages, dict):
        raise StateError("state contains an invalid course package map")
    return {
        str(doc_id): PackageSnapshot.from_dict(package)
        for doc_id, package in raw_packages.items()
    }


def _seat_phrase(count: int) -> str:
    return f"{count} available seat" if count == 1 else f"{count} available seats"


def _change_details(change: PackageChange) -> list[str]:
    details: list[str] = []
    current = change.current
    previous = change.previous
    if previous is None:
        if current.status != "UNKNOWN":
            details.append(f"status {current.status}")
        if current.waitlist_current_size is not None:
            details.append(f"waitlist {current.waitlist_current_size}")
        return details
    if previous.status != current.status:
        details.append(f"status {previous.status} -> {current.status}")
    elif current.status != "UNKNOWN":
        details.append(f"status {current.status}")
    if previous.waitlist_current_size != current.waitlist_current_size:
        before = (
            "unknown"
            if previous.waitlist_current_size is None
            else str(previous.waitlist_current_size)
        )
        after = (
            "unknown"
            if current.waitlist_current_size is None
            else str(current.waitlist_current_size)
        )
        details.append(f"waitlist {before} -> {after}")
    return details


def build_course_notification(
    course: CourseConfig,
    parsed: ParsedCourse,
    changes: list[PackageChange],
) -> tuple[str, str, str]:
    display_name = course.label or parsed.display_name or course.fallback_label
    click_url = _course_click_url(course, display_name)
    lines = [f"{display_name} has real seats available.", ""]
    for change in changes:
        section_label = ", ".join(change.current.sections) or change.doc_id
        if change.previous is None:
            lines.append(
                f"- {section_label}: {_seat_phrase(change.current.available_seats)} "
                "(new section)"
            )
        else:
            lines.append(
                f"- {section_label}: {change.previous.available_seats} -> "
                f"{_seat_phrase(change.current.available_seats)}"
            )
        details = _change_details(change)
        if details:
            lines.append(f"  {', '.join(details)}")
    lines.extend(["", click_url])
    return f"Seats available: {display_name}", "\n".join(lines), click_url


def build_setup_notification(
    config: Config,
    failures: list[tuple[CourseConfig, str]],
) -> tuple[str, str, None]:
    total = len(config.courses)
    succeeded = total - len(failures)
    if failures:
        failed_labels = ", ".join(
            course.label or course.fallback_label for course, _ in failures
        )
        first_line = (
            f"Setup check ran: {succeeded} of {total} course(s) checked successfully."
        )
        lines = [
            first_line,
            f"Failed: {failed_labels}.",
            "",
            "Check the log for details.",
        ]
    else:
        lines = [f"Setup check succeeded: checked {total} course(s)."]
    lines.extend(
        [
            "",
            "You will receive a separate notification for each course when real seats become available.",
        ]
    )
    return "Seat checker setup check", "\n".join(lines), None


def build_failure_notification(
    config: Config,
    failures: list[tuple[CourseConfig, str]],
    streak: int,
) -> tuple[str, str, None]:
    lines = [
        f"All {len(config.courses)} course checks have failed for {streak} consecutive runs.",
        "",
        "Saved seat data has not been changed.",
        "",
        "Last errors:",
    ]
    for course, error in failures:
        lines.append(f"- {course.label or course.fallback_label}: {error}")
    return "Seat checker cannot check courses", "\n".join(lines), None


def send_ntfy(
    config: Config,
    title: str,
    message: str,
    click: str | None = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        logging.info("dry run: would send ntfy notification %r", title)
        return

    payload: dict[str, Any] = {
        "topic": config.ntfy_topic,
        "title": title,
        "message": message,
        "priority": config.ntfy_priority,
        "tags": ["seat"],
    }
    if click:
        payload["click"] = click

    headers = {
        "Content-Type": "application/json",
        "User-Agent": config.user_agent,
    }
    token = os.environ.get(config.ntfy_token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        config.ntfy_server,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            status = response.getcode()
            response.read(4096)
    except urllib.error.HTTPError as exc:
        raise NotifyError(f"ntfy HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NotifyError(f"ntfy network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise NotifyError("ntfy request timed out") from exc

    if status >= 300:
        raise NotifyError(f"ntfy HTTP {status}")


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "consecutive_total_failures": 0,
        "total_failure_alerted": False,
        "courses": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"could not read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError("state file root must be an object")
    if data.get("version") != STATE_VERSION:
        raise StateError(f"unsupported state version in {path}")
    if not isinstance(data.get("initialized"), bool):
        raise StateError("state file has an invalid 'initialized' value")
    if not isinstance(data.get("courses"), dict):
        raise StateError("state file has an invalid 'courses' value")

    consecutive_failures = data.get("consecutive_total_failures", 0)
    if (
        not isinstance(consecutive_failures, int)
        or isinstance(consecutive_failures, bool)
        or consecutive_failures < 0
    ):
        raise StateError("state file has an invalid failure count")
    failure_alerted = data.get("total_failure_alerted", False)
    if not isinstance(failure_alerted, bool):
        raise StateError("state file has an invalid failure alert value")
    data["consecutive_total_failures"] = consecutive_failures
    data["total_failure_alerted"] = failure_alerted
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise LockError("another seat checker run is already active") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _resolve_state_path(config: Config, state_override: Path | str | None) -> Path:
    return Path(state_override).expanduser() if state_override else config.state_file


def _record_check_health(state: dict[str, Any], total_failure: bool) -> bool:
    if not total_failure:
        state["consecutive_total_failures"] = 0
        state["total_failure_alerted"] = False
        return False
    state["consecutive_total_failures"] += 1
    return (
        state["consecutive_total_failures"] >= TOTAL_FAILURE_ALERT_THRESHOLD
        and not state["total_failure_alerted"]
    )


def run(
    config_path: Path | str,
    state_override: Path | str | None = None,
    dry_run: bool = False,
) -> int:
    config = load_config(config_path)
    state_path = _resolve_state_path(config, state_override)
    lock_path = Path(f"{state_path}.lock")

    with FileLock(lock_path):
        state = load_state(state_path)
        first_run = not state["initialized"]
        parsed_courses: dict[str, ParsedCourse] = {}
        failures: list[tuple[CourseConfig, str]] = []

        for index, course in enumerate(config.courses):
            logging.info("checking %s", course.label or course.fallback_label)
            try:
                parsed_courses[course.key] = fetch_course(config, course)
            except FetchError as exc:
                logging.error(
                    "course %s failed: %s",
                    course.label or course.fallback_label,
                    exc,
                )
                failures.append((course, str(exc)))
            if index + 1 < len(config.courses) and config.request_delay_seconds:
                time.sleep(config.request_delay_seconds)

        total_failure = bool(config.courses) and not parsed_courses
        failure_alert_due = _record_check_health(state, total_failure)
        if total_failure:
            logging.error(
                "all course checks failed for %d consecutive run(s)",
                state["consecutive_total_failures"],
            )

        if first_run:
            title, message, _ = build_setup_notification(config, failures)
            send_ntfy(config, title, message, dry_run=dry_run)
            if not dry_run:
                state["initialized"] = True
                save_state(state_path, state)

        notifications: list[tuple[CourseConfig, ParsedCourse, list[PackageChange]]] = []
        for course in config.courses:
            parsed = parsed_courses.get(course.key)
            if parsed is None:
                continue
            previous = load_previous_packages(state["courses"].get(course.key))
            changes = find_changes(previous, parsed)
            if changes:
                notifications.append((course, parsed, changes))
            merged = merge_packages(previous, parsed)
            state["courses"][course.key] = {
                "term": course.term,
                "subject_code": course.subject_code,
                "course_id": course.course_id,
                "label": course.label,
                "display_name": parsed.display_name,
                "last_checked": _utc_now_iso(),
                "packages": {
                    doc_id: snapshot.to_dict()
                    for doc_id, snapshot in merged.items()
                },
            }

        for course, parsed, changes in notifications:
            title, message, click_url = build_course_notification(course, parsed, changes)
            send_ntfy(config, title, message, click=click_url, dry_run=dry_run)

        if failure_alert_due:
            title, message, _ = build_failure_notification(
                config,
                failures,
                state["consecutive_total_failures"],
            )
            send_ntfy(config, title, message, dry_run=dry_run)
            state["total_failure_alerted"] = True

        if not dry_run:
            save_state(state_path, state)

        if dry_run:
            suffix = ""
            if first_run:
                suffix += ", plus the setup notification"
            if failure_alert_due:
                suffix += ", plus the failure alert"
            logging.info(
                "dry run: checked %d/%d course(s), would send %d seat notification(s)%s",
                len(parsed_courses),
                len(config.courses),
                len(notifications),
                suffix,
            )
        else:
            suffix = ""
            if first_run:
                suffix += ", setup notification sent"
            if failure_alert_due:
                suffix += ", failure alert sent"
            logging.info(
                "checked %d/%d course(s), sent %d seat notification(s)%s",
                len(parsed_courses),
                len(config.courses),
                len(notifications),
                suffix,
            )

        if config.courses and not parsed_courses:
            return 1
        return 0


def check_config(
    config_path: Path | str,
    state_override: Path | str | None = None,
) -> int:
    config = load_config(config_path)
    state_path = _resolve_state_path(config, state_override)
    terms = ", ".join(sorted({course.term for course in config.courses}))

    print(f"Config OK: {len(config.courses)} course(s), term(s) {terms}")
    print(f"State file: {state_path}")
    for course in config.courses:
        print(f"- {course.label or course.fallback_label} ({course.key})")

    if state_path.exists():
        state = load_state(state_path)
        print(
            "State OK: "
            f"initialized={state['initialized']}, "
            f"courses={len(state['courses'])}, "
            f"consecutive_total_failures={state['consecutive_total_failures']}"
        )
    else:
        print("State file not present; the first run will send a setup notification.")
    return 0


def show_status(
    config_path: Path | str,
    state_override: Path | str | None = None,
) -> int:
    config = load_config(config_path)
    state_path = _resolve_state_path(config, state_override)
    state = load_state(state_path)

    print("Seat checker status")
    print(f"State file: {state_path}")
    print(f"Initialized: {'yes' if state['initialized'] else 'no'}")
    print(f"Consecutive total failures: {state['consecutive_total_failures']}")
    print(
        "Failure alert sent for current streak: "
        f"{'yes' if state['total_failure_alerted'] else 'no'}"
    )

    for course in config.courses:
        print()
        print(f"{course.label or course.fallback_label} ({course.key})")
        course_state = state["courses"].get(course.key)
        if not isinstance(course_state, dict):
            print("  not checked yet")
            continue

        last_checked = _as_str(course_state.get("last_checked")) or "unknown"
        packages = load_previous_packages(course_state)
        total_available = sum(item.available_seats for item in packages.values())
        print(f"  last checked: {last_checked}")
        print(f"  total available seats: {total_available}")
        for doc_id in sorted(packages):
            package = packages[doc_id]
            section_label = ", ".join(package.sections) or doc_id
            waitlist = (
                "unknown"
                if package.waitlist_current_size is None
                else str(package.waitlist_current_size)
            )
            print(
                f"  - {section_label}: {package.available_seats} available, "
                f"status {package.status}, waitlist {waitlist}"
            )

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check UW-Madison course availability and notify with ntfy."
    )
    parser.add_argument(
        "--config",
        default="config.toml",
        help="path to the TOML config file (default: config.toml)",
    )
    parser.add_argument(
        "--state",
        help="override the state file path from the config",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and evaluate courses without sending notifications or updating state",
    )
    actions.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration and saved state without network access",
    )
    actions.add_argument(
        "--status",
        action="store_true",
        help="show saved seat counts without network access",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.check_config:
            return check_config(args.config, args.state)
        if args.status:
            return show_status(args.config, args.state)
        return run(args.config, args.state, args.dry_run)
    except LockError as exc:
        logging.warning("%s", exc)
        return 0
    except SeatCheckerError as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("unexpected error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
