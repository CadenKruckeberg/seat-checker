import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import seat_checker as sc

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def course(course_id="004293", subject_code="266", label=None):
    return sc.CourseConfig(
        term="1274",
        subject_code=subject_code,
        course_id=course_id,
        label=label,
    )


def snapshot(
    doc_id="doc-1",
    available=0,
    open_seats=0,
    status="CLOSED",
    waitlist=0,
    sections=("001 LEC",),
):
    return sc.PackageSnapshot(
        doc_id=doc_id,
        available_seats=available,
        open_seats=open_seats,
        status=status,
        waitlist_current_size=waitlist,
        waitlist_capacity=25,
        open_waitlist_spots=0,
        sections=sections,
    )


def parsed(*snapshots):
    return sc.ParsedCourse(
        display_name="COMP SCI 640",
        packages={item.doc_id: item for item in snapshots},
        malformed_doc_ids=frozenset(),
    )


class ParseTests(unittest.TestCase):
    def test_parses_array_response_and_uses_available_seats(self):
        result = sc.parse_course_payload(load_fixture("640.json"), course())

        self.assertEqual(result.display_name, "COMP SCI 640")
        self.assertEqual(len(result.packages), 1)
        package = result.packages["enrollmentPackage-1274-A1-266-640-001"]
        self.assertEqual(package.available_seats, 0)
        self.assertEqual(package.open_seats, 4)
        self.assertEqual(package.status, "CLOSED")
        self.assertEqual(package.waitlist_current_size, 40)
        self.assertEqual(package.sections, ("001 LEC",))

    def test_parses_single_object_response(self):
        payload = load_fixture("640.json")[0]

        result = sc.parse_course_payload(payload, course())

        self.assertEqual(
            list(result.packages),
            ["enrollmentPackage-1274-A1-266-640-001"],
        )

    def test_parses_multiple_packages(self):
        result = sc.parse_course_payload(
            load_fixture("461.json"),
            course(course_id="025394", subject_code="544"),
        )

        self.assertEqual(len(result.packages), 2)
        self.assertEqual(
            set(result.packages),
            {
                "enrollmentPackage-1274-A1-544-461-001",
                "enrollmentPackage-1274-A1-544-461-002",
            },
        )

    def test_rejects_mismatched_term(self):
        payload = load_fixture("640.json")
        payload[0]["termCode"] = "1272"

        with self.assertRaises(sc.FetchError):
            sc.parse_course_payload(payload, course())

    def test_rejects_mismatched_course(self):
        payload = load_fixture("640.json")
        payload[0]["courseId"] = "999999"

        with self.assertRaises(sc.FetchError):
            sc.parse_course_payload(payload, course())

    def test_rejects_response_without_usable_available_seats(self):
        payload = load_fixture("640.json")
        del payload[0]["packageEnrollmentStatus"]

        with self.assertRaises(sc.FetchError):
            sc.parse_course_payload(payload, course())

    def test_skips_packages_without_doc_id(self):
        payload = load_fixture("640.json")
        del payload[0]["docId"]

        with self.assertRaises(sc.FetchError):
            sc.parse_course_payload(payload, course())

    def test_partial_malformed_package_keeps_valid_siblings(self):
        payload = load_fixture("461.json")
        del payload[0]["packageEnrollmentStatus"]

        result = sc.parse_course_payload(
            payload,
            course(course_id="025394", subject_code="544"),
        )

        self.assertEqual(
            list(result.packages),
            ["enrollmentPackage-1274-A1-544-461-002"],
        )
        self.assertEqual(
            result.malformed_doc_ids,
            frozenset({"enrollmentPackage-1274-A1-544-461-001"}),
        )

    def test_rejects_empty_and_non_package_responses(self):
        for payload in ([], "maintenance", 42, None):
            with self.subTest(payload=payload):
                with self.assertRaises(sc.FetchError):
                    sc.parse_course_payload(payload, course())

    def test_missing_status_is_unknown(self):
        payload = load_fixture("640.json")
        del payload[0]["packageEnrollmentStatus"]["status"]

        result = sc.parse_course_payload(payload, course())

        self.assertEqual(
            result.packages["enrollmentPackage-1274-A1-266-640-001"].status,
            "UNKNOWN",
        )


class ChangeTests(unittest.TestCase):
    def test_first_run_with_zero_seats_does_not_alert(self):
        self.assertEqual(sc.find_changes({}, parsed(snapshot(available=0))), [])

    def test_open_seats_alone_do_not_trigger(self):
        result = sc.parse_course_payload(load_fixture("640.json"), course())

        self.assertEqual(sc.find_changes({}, result), [])

    def test_first_run_with_seats_alerts(self):
        changes = sc.find_changes({}, parsed(snapshot(available=2, status="OPEN")))

        self.assertEqual(len(changes), 1)
        self.assertIsNone(changes[0].previous)
        self.assertEqual(changes[0].current.available_seats, 2)

    def test_increase_alerts(self):
        previous = {"doc-1": snapshot(available=1)}
        changes = sc.find_changes(previous, parsed(snapshot(available=2)))

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].previous.available_seats, 1)

    def test_same_and_decreased_counts_do_not_alert(self):
        for current in (1, 0):
            with self.subTest(current=current):
                previous = {"doc-1": snapshot(available=1)}
                self.assertEqual(
                    sc.find_changes(previous, parsed(snapshot(available=current))),
                    [],
                )

    def test_new_section_with_seats_alerts(self):
        previous = {"doc-1": snapshot(doc_id="doc-1", available=0)}
        changes = sc.find_changes(previous, parsed(snapshot(doc_id="doc-2", available=1)))

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].doc_id, "doc-2")

    def test_new_section_without_seats_does_not_alert(self):
        previous = {"doc-1": snapshot(doc_id="doc-1", available=0)}
        changes = sc.find_changes(previous, parsed(snapshot(doc_id="doc-2", available=0)))

        self.assertEqual(changes, [])

    def test_waitlist_emptying_alone_does_not_alert(self):
        previous = {"doc-1": snapshot(available=0, waitlist=5)}
        changes = sc.find_changes(previous, parsed(snapshot(available=0, waitlist=0)))

        self.assertEqual(changes, [])

    def test_waitlist_change_is_shown_when_seats_increase(self):
        previous = {"doc-1": snapshot(available=0, waitlist=5)}
        changes = sc.find_changes(previous, parsed(snapshot(available=1, waitlist=0)))

        title, message, click = sc.build_course_notification(
            course(label="COMP SCI 640"),
            parsed(snapshot(available=1, waitlist=0)),
            changes,
        )

        self.assertEqual(title, "Seats available: COMP SCI 640")
        self.assertIn("0 -> 1 available seat", message)
        self.assertIn("waitlist 5 -> 0", message)
        self.assertIn("001 LEC", message)
        self.assertIn("https://public.enroll.wisc.edu/search?term=1274", click)

    def test_new_section_message_identifies_new_section(self):
        changes = sc.find_changes({}, parsed(snapshot(available=2, status="OPEN")))
        _, message, _ = sc.build_course_notification(
            course(label="COMP SCI 640"),
            parsed(snapshot(available=2, status="OPEN")),
            changes,
        )

        self.assertIn("new section", message)

    def test_merge_preserves_previous_for_malformed_package(self):
        previous = {
            "doc-1": snapshot(doc_id="doc-1", available=3),
            "doc-2": snapshot(doc_id="doc-2", available=1),
        }
        current = sc.ParsedCourse(
            display_name=None,
            packages={"doc-2": snapshot(doc_id="doc-2", available=2)},
            malformed_doc_ids=frozenset({"doc-1"}),
        )

        merged = sc.merge_packages(previous, current)

        self.assertEqual(set(merged), {"doc-1", "doc-2"})
        self.assertEqual(merged["doc-1"].available_seats, 3)
        self.assertEqual(merged["doc-2"].available_seats, 2)

    def test_merge_drops_package_missing_from_successful_response(self):
        previous = {"doc-1": snapshot(doc_id="doc-1", available=3)}
        current = parsed(snapshot(doc_id="doc-2", available=1))

        merged = sc.merge_packages(previous, current)

        self.assertEqual(set(merged), {"doc-2"})


class StateTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = sc.empty_state()
            state["initialized"] = True
            state["courses"]["1274:266:004293"] = {
                "packages": {
                    "doc-1": snapshot(doc_id="doc-1", available=2).to_dict(),
                }
            }

            sc.save_state(path, state)
            loaded = sc.load_state(path)

        self.assertTrue(loaded["initialized"])
        packages = sc.load_previous_packages(
            loaded["courses"]["1274:266:004293"]
        )
        self.assertEqual(packages["doc-1"].available_seats, 2)

    def test_missing_state_starts_uninitialized(self):
        with tempfile.TemporaryDirectory() as directory:
            state = sc.load_state(Path(directory) / "missing.json")

        self.assertFalse(state["initialized"])
        self.assertEqual(state["courses"], {})
        self.assertEqual(state["consecutive_total_failures"], 0)
        self.assertFalse(state["total_failure_alerted"])

    def test_legacy_state_gets_failure_tracking_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"version": 1, "initialized": True, "courses": {}}),
                encoding="utf-8",
            )

            state = sc.load_state(path)

        self.assertEqual(state["consecutive_total_failures"], 0)
        self.assertFalse(state["total_failure_alerted"])

    def test_invalid_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(sc.StateError):
                sc.load_state(path)

    def test_unsupported_state_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"version": 999, "initialized": True, "courses": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(sc.StateError):
                sc.load_state(path)


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        config = sc.load_config(ROOT / "config.example.toml")

        self.assertEqual(len(config.courses), 2)
        self.assertEqual(config.courses[0].label, "COMP SCI 542")
        self.assertEqual(config.courses[0].course_id, "025893")
        self.assertEqual(config.courses[1].label, "L I S 461")
        self.assertEqual(config.courses[1].subject_code, "544")
        self.assertEqual(config.request_delay_seconds, 1.0)
        self.assertEqual(config.state_file, ROOT / "state.json")

    def test_numeric_ids_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "\n".join(
                    [
                        'term = "1274"',
                        'ntfy_topic = "test"',
                        "[[courses]]",
                        "subject_code = 266",
                        "course_id = 4293",
                    ]
                ),
                encoding="utf-8",
            )

            config = sc.load_config(path)

        self.assertEqual(config.courses[0].subject_code, "266")
        self.assertEqual(config.courses[0].course_id, "004293")
        self.assertEqual(config.state_file, Path(directory) / "state.json")

    def test_config_requires_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'term = "1274"\nntfy_topic = "test"\n',
                encoding="utf-8",
            )

            with self.assertRaises(sc.ConfigError):
                sc.load_config(path)

    def test_duplicate_courses_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "\n".join(
                    [
                        'term = "1274"',
                        'ntfy_topic = "test"',
                        "[[courses]]",
                        'subject_code = "266"',
                        'course_id = "004293"',
                        "[[courses]]",
                        'subject_code = "266"',
                        'course_id = "004293"',
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(sc.ConfigError):
                sc.load_config(path)


class SetupNotificationTests(unittest.TestCase):
    def setUp(self):
        self.config = sc.Config(
            courses=(course(), course(course_id="025893")),
            state_file=Path("state.json"),
            api_base=sc.DEFAULT_API_BASE,
            ntfy_server=sc.DEFAULT_NTFY_SERVER,
            ntfy_topic="test",
            ntfy_token_env="NTFY_TOKEN",
            ntfy_priority=4,
            timeout_seconds=20,
            request_delay_seconds=0,
            user_agent=sc.DEFAULT_USER_AGENT,
        )

    def test_setup_success_message(self):
        title, message, click = sc.build_setup_notification(self.config, [])

        self.assertEqual(title, "Seat checker setup check")
        self.assertIn("checked 2 course(s)", message)
        self.assertIsNone(click)

    def test_setup_partial_failure_message(self):
        failures = [(self.config.courses[0], "HTTP 403")]

        _, message, _ = sc.build_setup_notification(self.config, failures)

        self.assertIn("1 of 2 course(s)", message)
        self.assertIn("266 004293", message)
        self.assertIn("Check the log", message)

    def test_failure_notification_includes_streak_and_errors(self):
        failures = [(self.config.courses[0], "HTTP 403")]

        title, message, click = sc.build_failure_notification(
            self.config,
            failures,
            2,
        )

        self.assertEqual(title, "Seat checker cannot check courses")
        self.assertIn("2 consecutive runs", message)
        self.assertIn("HTTP 403", message)
        self.assertIsNone(click)


class RunTests(unittest.TestCase):
    def write_config(self, directory):
        path = Path(directory) / "config.toml"
        path.write_text(
            "\n".join(
                [
                    'term = "1274"',
                    'state_file = "state.json"',
                    'ntfy_topic = "test"',
                    "request_delay_seconds = 0",
                    "[[courses]]",
                    'label = "COMP SCI 640"',
                    'subject_code = "266"',
                    'course_id = "004293"',
                ]
            ),
            encoding="utf-8",
        )
        return path

    def run_with_success(self, config_path, current):
        with (
            mock.patch.object(sc, "fetch_course", return_value=current),
            mock.patch.object(sc, "send_ntfy") as send,
        ):
            result = sc.run(config_path)
            titles = [call.args[1] for call in send.call_args_list]
        return result, titles

    def run_with_failure(self, config_path, error="HTTP 429"):
        with (
            mock.patch.object(sc, "fetch_course", side_effect=sc.FetchError(error)),
            mock.patch.object(sc, "send_ntfy") as send,
        ):
            result = sc.run(config_path)
            titles = [call.args[1] for call in send.call_args_list]
        return result, titles

    def test_first_run_sends_setup_and_seat_alert_then_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            current = parsed(snapshot(available=2, status="OPEN"))

            with (
                mock.patch.object(sc, "fetch_course", return_value=current),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path), 0)
                self.assertEqual(
                    [call.args[1] for call in send.call_args_list],
                    [
                        "Seat checker setup check",
                        "Seats available: COMP SCI 640",
                    ],
                )

            state = sc.load_state(state_path)
            self.assertTrue(state["initialized"])
            self.assertEqual(
                state["courses"]["1274:266:004293"]["packages"]["doc-1"][
                    "available_seats"
                ],
                2,
            )

            with (
                mock.patch.object(sc, "fetch_course", return_value=current),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path), 0)
                send.assert_not_called()

            with (
                mock.patch.object(
                    sc,
                    "fetch_course",
                    return_value=parsed(snapshot(available=3, status="OPEN")),
                ),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path), 0)
                self.assertEqual(
                    [call.args[1] for call in send.call_args_list],
                    ["Seats available: COMP SCI 640"],
                )

    def test_fetch_failure_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            current = parsed(snapshot(available=1, status="OPEN"))

            with (
                mock.patch.object(sc, "fetch_course", return_value=current),
                mock.patch.object(sc, "send_ntfy"),
            ):
                self.assertEqual(sc.run(config_path), 0)

            with (
                mock.patch.object(
                    sc,
                    "fetch_course",
                    side_effect=sc.FetchError("HTTP 429"),
                ),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path), 1)
                send.assert_not_called()

            state = sc.load_state(state_path)
            self.assertEqual(
                state["courses"]["1274:266:004293"]["packages"]["doc-1"][
                    "available_seats"
                ],
                1,
            )

            with (
                mock.patch.object(sc, "fetch_course", return_value=current),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path), 0)
                send.assert_not_called()

    def test_total_failure_alerts_after_two_runs_and_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            no_seats = parsed(snapshot(available=0))

            result, titles = self.run_with_success(config_path, no_seats)
            self.assertEqual(result, 0)
            self.assertEqual(titles, ["Seat checker setup check"])

            result, titles = self.run_with_failure(config_path)
            self.assertEqual(result, 1)
            self.assertEqual(titles, [])
            state = sc.load_state(state_path)
            self.assertEqual(state["consecutive_total_failures"], 1)
            self.assertFalse(state["total_failure_alerted"])

            result, titles = self.run_with_failure(config_path)
            self.assertEqual(result, 1)
            self.assertEqual(titles, ["Seat checker cannot check courses"])
            state = sc.load_state(state_path)
            self.assertEqual(state["consecutive_total_failures"], 2)
            self.assertTrue(state["total_failure_alerted"])

            result, titles = self.run_with_failure(config_path)
            self.assertEqual(result, 1)
            self.assertEqual(titles, [])
            state = sc.load_state(state_path)
            self.assertEqual(state["consecutive_total_failures"], 3)
            self.assertTrue(state["total_failure_alerted"])

            result, titles = self.run_with_success(config_path, no_seats)
            self.assertEqual(result, 0)
            self.assertEqual(titles, [])
            state = sc.load_state(state_path)
            self.assertEqual(state["consecutive_total_failures"], 0)
            self.assertFalse(state["total_failure_alerted"])

            result, titles = self.run_with_failure(config_path)
            self.assertEqual(result, 1)
            self.assertEqual(titles, [])
            result, titles = self.run_with_failure(config_path)
            self.assertEqual(result, 1)
            self.assertEqual(titles, ["Seat checker cannot check courses"])

    def test_dry_run_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            current = parsed(snapshot(available=1, status="OPEN"))

            with (
                mock.patch.object(sc, "fetch_course", return_value=current),
                mock.patch.object(sc, "send_ntfy") as send,
            ):
                self.assertEqual(sc.run(config_path, dry_run=True), 0)

            self.assertFalse(state_path.exists())
            self.assertTrue(
                all(call.kwargs.get("dry_run") is True for call in send.call_args_list)
            )


class DiagnosticsTests(unittest.TestCase):
    def write_config(self, directory):
        path = Path(directory) / "config.toml"
        path.write_text(
            "\n".join(
                [
                    'term = "1274"',
                    'state_file = "state.json"',
                    'ntfy_topic = "test"',
                    "[[courses]]",
                    'label = "COMP SCI 640"',
                    'subject_code = "266"',
                    'course_id = "004293"',
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_check_config_without_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            output = io.StringIO()

            with redirect_stdout(output):
                result = sc.check_config(config_path)

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Config OK: 1 course(s)", text)
        self.assertIn("COMP SCI 640 (1274:266:004293)", text)
        self.assertIn("State file not present", text)

    def test_check_config_with_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            sc.save_state(state_path, sc.empty_state())
            output = io.StringIO()

            with redirect_stdout(output):
                result = sc.check_config(config_path)

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("State OK: initialized=False", text)
        self.assertIn("consecutive_total_failures=0", text)

    def test_status_shows_saved_seats_and_failure_health(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = self.write_config(directory)
            state_path = Path(directory) / "state.json"
            state = sc.empty_state()
            state["initialized"] = True
            state["consecutive_total_failures"] = 2
            state["total_failure_alerted"] = True
            state["courses"]["1274:266:004293"] = {
                "last_checked": "2026-11-13T22:00:00+00:00",
                "packages": {
                    "doc-1": snapshot(
                        available=2,
                        status="OPEN",
                        waitlist=0,
                        sections=("001 LEC",),
                    ).to_dict(),
                },
            }
            sc.save_state(state_path, state)
            output = io.StringIO()

            with redirect_stdout(output):
                result = sc.show_status(config_path)

        text = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("Seat checker status", text)
        self.assertIn("Initialized: yes", text)
        self.assertIn("Consecutive total failures: 2", text)
        self.assertIn("Failure alert sent for current streak: yes", text)
        self.assertIn("last checked: 2026-11-13T22:00:00+00:00", text)
        self.assertIn("total available seats: 2", text)
        self.assertIn("001 LEC: 2 available, status OPEN, waitlist 0", text)


if __name__ == "__main__":
    unittest.main()
