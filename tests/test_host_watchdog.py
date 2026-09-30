import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.host_watchdog import (
    HostWatchdog,
    heartbeat_is_fresh,
    parse_compose_ps,
)


class HostWatchdogTests(unittest.TestCase):
    def test_parses_json_array_and_json_lines_compose_status(self):
        array_status = parse_compose_ps(
            json.dumps(
                [
                    {
                        "Service": "postgres",
                        "State": "running",
                        "Health": "healthy",
                        "Status": "Up 1 minute (healthy)",
                    },
                    {
                        "Service": "redis",
                        "State": "running",
                        "Health": "unhealthy",
                        "Status": "Up 2 minutes (unhealthy)",
                    },
                ]
            )
        )
        line_status = parse_compose_ps(
            '{"Service":"risk-engine","Status":"Up 2 minutes (healthy)"}\n'
        )

        self.assertEqual(array_status[0].service, "postgres")
        self.assertEqual(array_status[1].health, "unhealthy")
        self.assertEqual(line_status[0].state, "up")
        self.assertEqual(line_status[0].health, "healthy")

    def test_heartbeat_freshness_detects_absent_and_stale_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat.txt"
            self.assertFalse(
                heartbeat_is_fresh(heartbeat, timeout_seconds=45, now=100)
            )
            heartbeat.write_text("heartbeat", encoding="ascii")
            heartbeat.touch()
            current = heartbeat.stat().st_mtime

            self.assertTrue(
                heartbeat_is_fresh(
                    heartbeat,
                    timeout_seconds=45,
                    now=current + 44,
                )
            )
            self.assertFalse(
                heartbeat_is_fresh(
                    heartbeat,
                    timeout_seconds=45,
                    now=current + 46,
                )
            )

    def test_compose_watchdog_restarts_unhealthy_and_starts_missing_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = []
            output = [
                SimpleNamespace(stdout="postgres\nrisk-engine\n"),
                SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {
                                "Service": "postgres",
                                "State": "running",
                                "Health": "healthy",
                            },
                            {
                                "Service": "risk-engine",
                                "State": "running",
                                "Health": "unhealthy",
                            },
                        ]
                    )
                ),
            ]

            def run(command, **kwargs):
                commands.append(command)
                if command[-2:] == ["restart", "risk-engine"] or command[-3:] == [
                    "up",
                    "-d",
                    "--remove-orphans",
                ]:
                    return SimpleNamespace(stdout="")
                return output.pop(0)

            watchdog = HostWatchdog(
                terminal_path=root / "terminal64.exe",
                startup_config=root / "terminal.ini",
                heartbeat_path=root / "heartbeat.txt",
                project_directory=root,
                compose_file=root / "compose.yml",
                run=run,
                clock=lambda: 100,
            )

            self.assertFalse(watchdog.check_compose())
            self.assertEqual(commands[-1][-2:], ["restart", "risk-engine"])

            output.extend(
                [
                    SimpleNamespace(stdout="postgres\nrisk-engine\n"),
                    SimpleNamespace(
                        stdout=json.dumps(
                            [
                                {
                                    "Service": "postgres",
                                    "State": "running",
                                    "Health": "healthy",
                                }
                            ]
                        )
                    ),
                ]
            )
            self.assertFalse(watchdog.check_compose())
            self.assertEqual(commands[-1][-3:], ["up", "-d", "--remove-orphans"])

    def test_terminal_missing_is_started_with_startup_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            terminal = root / "terminal64.exe"
            startup = root / "mt5.ini"
            terminal.touch()
            startup.write_text("[Common]\n", encoding="ascii")
            launched = []
            watchdog = HostWatchdog(
                terminal_path=terminal,
                startup_config=startup,
                heartbeat_path=root / "heartbeat.txt",
                project_directory=root,
                compose_file=root / "compose.yml",
                process_iter=lambda *_args: [],
                popen=lambda command, **kwargs: launched.append((command, kwargs)),
            )

            self.assertFalse(watchdog.check_terminal())
            self.assertEqual(launched[0][0][0], str(terminal))
            self.assertEqual(launched[0][0][1], f"/config:{startup}")

    def test_stale_mt5_heartbeat_gracefully_restarts_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            terminal = root / "terminal64.exe"
            startup = root / "mt5.ini"
            heartbeat = root / "heartbeat.txt"
            terminal.touch()
            startup.write_text("[Common]\n", encoding="ascii")
            heartbeat.write_text("stale", encoding="ascii")
            os.utime(heartbeat, (time.time() - 100, time.time() - 100))
            process = SimpleNamespace(
                info={"name": "terminal64.exe", "exe": str(terminal)},
                pid=101,
                wait=lambda **_kwargs: None,
            )
            process_queries = iter([[process], []])
            launched = []
            watchdog = HostWatchdog(
                terminal_path=terminal,
                startup_config=startup,
                heartbeat_path=heartbeat,
                project_directory=root,
                compose_file=root / "compose.yml",
                heartbeat_timeout_seconds=45,
                startup_grace_seconds=0,
                restart_grace_seconds=0,
                process_iter=lambda *_args: next(process_queries),
                popen=lambda command, **kwargs: launched.append((command, kwargs)),
                sleep=lambda _seconds: None,
            )

            self.assertFalse(watchdog.check_terminal())
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0][0][0], str(terminal))


if __name__ == "__main__":
    unittest.main()
