import json
import queue
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

import prolific_monitor as monitor


class RewardFormattingTests(unittest.TestCase):
    def test_formats_raw_prolific_reward_in_pounds(self) -> None:
        self.assertEqual(
            monitor.format_prolific_reward({"reward": 398.0}),
            "\N{POUND SIGN}3.98",
        )

    def test_uses_currency_from_nested_reward(self) -> None:
        self.assertEqual(
            monitor.format_prolific_reward(
                {"reward": {"amount": 250, "currency": "USD"}}
            ),
            "$2.50",
        )

    def test_formatted_study_matches_prolific_display(self) -> None:
        message = monitor.format_study(
            {"id": "test", "title": "Test study", "reward": 398.0}
        )
        self.assertIn("Reward: \N{POUND SIGN}3.98", message)


class BrowserGuardTests(unittest.TestCase):
    def test_target_study_expires_without_disabling_manual_only_rule(self) -> None:
        script = monitor.build_persistent_take_part_script()

        self.assertIn('const targetStudyMaxAgeMs = 20000;', script)
        self.assertIn('PROLIFIC_TARGET_STUDY_EXPIRED', script)
        self.assertIn('prolificAutoTargetStudyDetectedAt', script)
        self.assertIn(
            'if (!targetStudyTitle && !targetStudyId) return true;',
            script,
        )
        self.assertIn('const manualOnlyEnabled = true;', script)
        self.assertIn('"saudi sexism detection"', script)
        self.assertIn('"sexism detection cross annotation"', script)
        self.assertIn(
            'manualOnlyTitles.some((title) => selectedTitleSimplified.includes(title))',
            script,
        )
        self.assertIn('if (isManualOnlyStudy()) return false;', script)
        self.assertIn('const activeStudyMatchesTarget = () => {', script)
        self.assertIn('if (activeStudyMatchesTarget()) {', script)
        self.assertIn('const detail = activeDetailContext();', script)
        self.assertIn('if (hadQueuedTarget || !alreadyReported) {', script)

    def test_target_study_timeout_is_configurable(self) -> None:
        script = monitor.build_persistent_take_part_script(15)

        self.assertIn('const targetStudyMaxAgeMs = 15000;', script)

    def test_guard_collects_each_requested_button_failure_state(self) -> None:
        script = monitor.build_persistent_take_part_script()

        for state in (
            "button_not_found",
            "button_disabled",
            "button_hidden",
            "possible_iframe",
            "text_not_recognized",
            "button_clickable",
        ):
            self.assertIn(f'"{state}"', script)
        self.assertIn("frame.contentDocument", script)
        self.assertIn('control.scrollIntoView({block: "center", inline: "center"})', script)
        self.assertIn("new MutationObserver", script)
        self.assertIn("PROLIFIC_TAKE_PART_NO_RESPONSE", script)


class DiagnosticStoreTests(unittest.TestCase):
    def test_records_deduplicated_lifecycle_and_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = monitor.DiagnosticStore(Path(temp_dir) / "diagnostics.sqlite3")
            attempt_id = store.begin_attempt("study-1", "Test study")

            self.assertTrue(
                store.record_event(
                    attempt_id,
                    "button_disabled",
                    "button_search",
                    {"disabledButtonCount": 1},
                )
            )
            self.assertFalse(
                store.record_event(
                    attempt_id,
                    "button_disabled",
                    "button_search",
                    {"disabledButtonCount": 1},
                )
            )
            self.assertTrue(store.complete_attempt(attempt_id, "study_full"))

            attempts = store.recent_attempts()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["final_result"], "study_full")
            self.assertEqual(
                [event["event_type"] for event in attempts[0]["events"]],
                ["study_detected", "button_disabled", "study_full"],
            )

    def test_cleanup_securely_removes_attempts_older_than_three_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "diagnostics.sqlite3"
            store = monitor.DiagnosticStore(database_path, retention_hours=72)
            old_attempt_id = store.begin_attempt("old", "Old study")
            recent_attempt_id = store.begin_attempt("recent", "Recent study")
            old_epoch = time.time() - 73 * 3600
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "UPDATE attempts SET detected_at_epoch = ? WHERE id = ?",
                    (old_epoch, old_attempt_id),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(store.cleanup(), 1)
            attempts = store.recent_attempts()
            self.assertEqual([attempt["id"] for attempt in attempts], [recent_attempt_id])
            connection = store._connect()
            try:
                orphan_count = connection.execute(
                    "SELECT COUNT(*) FROM attempt_events WHERE attempt_id = ?",
                    (old_attempt_id,),
                ).fetchone()[0]
                secure_delete = connection.execute("PRAGMA secure_delete").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(orphan_count, 0)
            self.assertEqual(secure_delete, 1)


class BrowserStudyQueueTests(unittest.TestCase):
    def test_browser_detected_studies_are_forwarded_to_auto_open_queue(self) -> None:
        studies = [
            {"id": "study-1", "title": "First study"},
            {"id": "study-2", "title": "Second study"},
        ]
        detected_studies: queue.Queue[list[dict[str, object]]] = queue.Queue()

        with (
            patch.object(
                monitor,
                "load_config",
                return_value={"state_file": "unused-state.json"},
            ),
            patch.object(
                monitor,
                "process_studies",
                return_value=studies,
            ) as process_studies,
        ):
            monitor.handle_browser_studies_body_with_devtools(
                {"body": json.dumps({"results": studies})},
                Path("prolific-monitor.json"),
                "https://internal-api.prolific.com/studies",
                devtools=None,
                study_queue=detected_studies,
            )

        self.assertEqual(detected_studies.get_nowait(), studies)
        process_studies.assert_called_once_with(
            studies,
            {"state_file": "unused-state.json"},
            Path("unused-state.json"),
            notify=True,
            source="browser",
        )

    def test_browser_watcher_processes_batch_until_a_seat_is_reserved(self) -> None:
        stop_event = threading.Event()
        detected_studies: queue.Queue[list[dict[str, object]]] = queue.Queue()
        detected_studies.put(
            [
                {"id": "study-1", "title": "First study"},
                {"id": "study-2", "title": "Second study"},
            ]
        )

        class FakeDiagnosticStore:
            def __init__(self) -> None:
                self.started: list[tuple[int, str, str]] = []
                self.events: list[tuple[int, str, str]] = []
                self.completed: list[tuple[int, str]] = []

            def begin_attempt(self, study_id: str, title: str, source: str) -> int:
                attempt_id = len(self.started) + 1
                self.started.append((attempt_id, study_id, title))
                return attempt_id

            def record_event(
                self,
                attempt_id: int,
                event_type: str,
                stage: str,
                _details: dict[str, object] | None = None,
            ) -> bool:
                self.events.append((attempt_id, event_type, stage))
                return True

            def complete_attempt(
                self,
                attempt_id: int,
                final_result: str,
                _details: dict[str, object] | None = None,
            ) -> bool:
                self.completed.append((attempt_id, final_result))
                return True

            def cleanup(self) -> int:
                return 0

        diagnostic_store = FakeDiagnosticStore()

        class FakeDevTools:
            instance: "FakeDevTools | None" = None

            def __init__(self, _websocket_url: str):
                self.commands: list[tuple[str, dict[str, object] | None]] = []
                self.messages = [
                    {
                        "method": "Runtime.consoleAPICalled",
                        "params": {
                            "args": [{"value": "PROLIFIC_STUDY_FULL"}],
                        },
                    },
                    {
                        "method": "Runtime.consoleAPICalled",
                        "params": {
                            "args": [{"value": "PROLIFIC_SEAT_RESERVED"}],
                        },
                    },
                ]
                FakeDevTools.instance = self

            def __enter__(self) -> "FakeDevTools":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def command(
                self,
                method: str,
                params: dict[str, object] | None = None,
            ) -> int:
                self.commands.append((method, params))
                return len(self.commands)

            def recv_json(self) -> dict[str, object]:
                message = self.messages.pop(0)
                if not self.messages:
                    stop_event.set()
                return message

        with (
            patch.object(monitor, "wait_for_devtools"),
            patch.object(
                monitor,
                "get_or_create_page_target",
                return_value="ws://127.0.0.1/fake",
            ),
            patch.object(
                monitor,
                "close_duplicate_prolific_targets",
                return_value=0,
            ),
            patch.object(
                monitor,
                "prolific_target_changed",
                return_value=False,
            ),
            patch.object(
                monitor,
                "create_diagnostic_store",
                return_value=diagnostic_store,
            ),
            patch.object(monitor, "DevToolsWebSocket", FakeDevTools),
            patch.object(monitor, "handle_reservation_result") as reservation_result,
        ):
            monitor.watch_browser_tokens(
                config_path=Path("prolific-monitor.json"),
                stop_event=stop_event,
                study_queue=detected_studies,
            )

        self.assertIsNotNone(FakeDevTools.instance)
        runtime_expressions = [
            str((params or {}).get("expression", ""))
            for method, params in FakeDevTools.instance.commands
            if method == "Runtime.evaluate"
        ]
        self.assertEqual(len(runtime_expressions), 2)
        self.assertIn("First study", runtime_expressions[0])
        self.assertIn("Second study", runtime_expressions[1])
        self.assertEqual(
            reservation_result.call_args_list,
            [
                call(Path("prolific-monitor.json"), reserved=False),
                call(Path("prolific-monitor.json"), reserved=True),
            ],
        )
        self.assertEqual(
            diagnostic_store.started,
            [
                (1, "study-1", "First study"),
                (2, "study-2", "Second study"),
            ],
        )
        self.assertEqual(
            diagnostic_store.completed,
            [(1, "study_full"), (2, "reserved")],
        )

    def test_browser_watcher_releases_a_stale_target_after_twenty_seconds(
        self,
    ) -> None:
        stop_event = threading.Event()
        detected_studies: queue.Queue[list[dict[str, object]]] = queue.Queue()
        detected_studies.put(
            [{"id": "study-1", "title": "Stale study"}]
        )

        class TimeoutDevTools:
            instance: "TimeoutDevTools | None" = None

            def __init__(self, _websocket_url: str):
                self.commands: list[tuple[str, dict[str, object] | None]] = []
                self.receive_count = 0
                TimeoutDevTools.instance = self

            def __enter__(self) -> "TimeoutDevTools":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def command(
                self,
                method: str,
                params: dict[str, object] | None = None,
            ) -> int:
                self.commands.append((method, params))
                return len(self.commands)

            def recv_json(self) -> dict[str, object]:
                self.receive_count += 1
                if self.receive_count >= 2:
                    stop_event.set()
                raise socket.timeout

        with (
            patch.object(monitor, "wait_for_devtools"),
            patch.object(
                monitor,
                "get_or_create_page_target",
                return_value="ws://127.0.0.1/fake",
            ),
            patch.object(
                monitor,
                "close_duplicate_prolific_targets",
                return_value=0,
            ),
            patch.object(
                monitor,
                "prolific_target_changed",
                return_value=False,
            ),
            patch.object(monitor, "install_take_part_guard"),
            patch.object(monitor, "DevToolsWebSocket", TimeoutDevTools),
            patch.object(
                monitor,
                "load_config",
                return_value={
                    "browser": {"target_study_timeout_seconds": 20},
                },
            ),
            patch.object(
                monitor.time,
                "monotonic",
                side_effect=[100.0, 121.0],
            ),
            patch.object(monitor, "log") as monitor_log,
        ):
            monitor.watch_browser_tokens(
                config_path=Path("prolific-monitor.json"),
                stop_event=stop_event,
                study_queue=detected_studies,
            )

        self.assertIsNotNone(TimeoutDevTools.instance)
        runtime_expressions = [
            str((params or {}).get("expression", ""))
            for method, params in TimeoutDevTools.instance.commands
            if method == "Runtime.evaluate"
        ]
        self.assertTrue(
            any(
                "prolificAutoTargetStudyTitle" in expression
                and "setItem" in expression
                for expression in runtime_expressions
            )
        )
        self.assertTrue(
            any(
                "prolificAutoTargetStudyTitle" in expression
                and "removeItem" in expression
                for expression in runtime_expressions
            )
        )
        self.assertTrue(
            any(
                "timed out after 20 seconds" in str(log_call)
                for log_call in monitor_log.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
