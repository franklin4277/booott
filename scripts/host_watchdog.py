"""Windows host watchdog for the MT5 terminal and the production Compose stack."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import psutil

logger = logging.getLogger("host-watchdog")


@dataclass(frozen=True)
class ComposeContainer:
    service: str
    state: str
    health: str
    status: str


def parse_compose_ps(output: str) -> list[ComposeContainer]:
    """Parse docker compose's JSON-array and JSON-lines output formats."""
    stripped = output.strip()
    if not stripped:
        return []
    try:
        decoded = json.loads(stripped)
        values = decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        values = []
        for line in stripped.splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Ignoring invalid docker compose ps JSON line")
    containers = []
    for item in values:
        if not isinstance(item, dict):
            continue
        service = item.get("Service") or item.get("service")
        if not isinstance(service, str) or not service:
            continue
        status = str(item.get("Status") or item.get("status") or "")
        health = str(item.get("Health") or item.get("health") or "")
        state = str(item.get("State") or item.get("state") or "")
        if not health and "(healthy)" in status.casefold():
            health = "healthy"
        elif not health and "(unhealthy)" in status.casefold():
            health = "unhealthy"
        if not state:
            state = status.split(maxsplit=1)[0] if status else ""
        containers.append(
            ComposeContainer(
                service=service,
                state=state.casefold(),
                health=health.casefold(),
                status=status,
            )
        )
    return containers


def heartbeat_is_fresh(path: Path, *, timeout_seconds: float, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return -1 <= age <= timeout_seconds


def find_terminal_processes(
    terminal_path: Path,
    process_iter: Callable = psutil.process_iter,
) -> list[psutil.Process]:
    expected_path = os.path.normcase(os.path.abspath(str(terminal_path)))
    matches = []
    for process in process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").casefold() != "terminal64.exe":
                continue
            executable = process.info.get("exe")
            if executable and os.path.normcase(os.path.abspath(executable)) != expected_path:
                continue
            matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return matches


def request_graceful_windows_close(pid: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
    window_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )
    enum_windows.argtypes = [window_enum_proc, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_pid = user32.GetWindowThreadProcessId
    get_window_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_pid.restype = wintypes.DWORD
    post_message = user32.PostMessageW
    post_message.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    post_message.restype = wintypes.BOOL
    wm_close = 0x0010

    @window_enum_proc
    def callback(hwnd, _lparam):
        window_pid = wintypes.DWORD()
        get_window_pid(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            post_message(hwnd, wm_close, 0, 0)
        return True

    enum_windows(callback, 0)


class HostWatchdog:
    def __init__(
        self,
        *,
        terminal_path: Path,
        startup_config: Path,
        heartbeat_path: Path,
        project_directory: Path,
        compose_file: Path,
        poll_seconds: float = 10,
        heartbeat_timeout_seconds: float = 15,
        startup_grace_seconds: float = 120,
        restart_grace_seconds: float = 20,
        compose_cooldown_seconds: float = 60,
        run: Callable = subprocess.run,
        popen: Callable = subprocess.Popen,
        process_iter: Callable = psutil.process_iter,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_seconds <= 0 or heartbeat_timeout_seconds <= 0:
            raise ValueError("watchdog polling and heartbeat timeouts must be positive")
        self.terminal_path = terminal_path.resolve()
        self.startup_config = startup_config.resolve()
        self.heartbeat_path = heartbeat_path
        self.project_directory = project_directory.resolve()
        self.compose_file = compose_file.resolve()
        self.poll_seconds = poll_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.startup_grace_seconds = startup_grace_seconds
        self.restart_grace_seconds = restart_grace_seconds
        self.compose_cooldown_seconds = compose_cooldown_seconds
        self.run = run
        self.popen = popen
        self.process_iter = process_iter
        self.clock = clock
        self.sleep = sleep
        self._terminal_launch_time: float | None = None
        self._last_compose_repair: dict[str, float] = {}

    def check_terminal(self) -> bool:
        processes = find_terminal_processes(self.terminal_path, self.process_iter)
        if not processes:
            logger.error("MT5 terminal is not running; launching %s", self.terminal_path)
            self._launch_terminal()
            return False

        heartbeat_fresh = heartbeat_is_fresh(
            self.heartbeat_path,
            timeout_seconds=self.heartbeat_timeout_seconds,
        )
        if heartbeat_fresh:
            self._terminal_launch_time = None
            return True

        now = self.clock()
        if self._terminal_launch_time is None:
            self._terminal_launch_time = now
        age = now - self._terminal_launch_time
        if age < self.startup_grace_seconds:
            logger.warning(
                "MT5 heartbeat not yet fresh; startup grace has %.1f seconds remaining",
                self.startup_grace_seconds - age,
            )
            return False

        logger.critical(
            "MT5 terminal has not updated heartbeat file %s within %.1f seconds; restarting",
            self.heartbeat_path,
            self.heartbeat_timeout_seconds,
        )
        self._stop_terminals(processes)
        remaining = find_terminal_processes(self.terminal_path, self.process_iter)
        if remaining:
            raise RuntimeError(
                "MT5 process did not exit after graceful close and termination attempts."
            )
        self.sleep(self.restart_grace_seconds)
        self._launch_terminal()
        return False

    def _stop_terminals(self, processes: Sequence[psutil.Process]) -> None:
        for process in processes:
            try:
                request_graceful_windows_close(process.pid)
                process.wait(timeout=self.restart_grace_seconds)
            except psutil.TimeoutExpired:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    process.wait(timeout=10)
                except (OSError, psutil.TimeoutExpired, psutil.NoSuchProcess):
                    try:
                        process.kill()
                        process.wait(timeout=10)
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                        logger.exception(
                            "Could not stop unresponsive MT5 process pid=%s",
                            process.pid,
                        )
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                logger.exception(
                    "Access denied while stopping MT5 process pid=%s",
                    process.pid,
                )

    def _launch_terminal(self) -> None:
        if not self.terminal_path.is_file():
            raise FileNotFoundError(f"MT5 terminal not found: {self.terminal_path}")
        if not self.startup_config.is_file():
            raise FileNotFoundError(f"MT5 startup config not found: {self.startup_config}")
        self.popen(
            [
                str(self.terminal_path),
                f"/config:{self.startup_config}",
            ],
            cwd=str(self.terminal_path.parent),
            close_fds=True,
        )
        self._terminal_launch_time = self.clock()
        logger.info("Launched MT5 terminal using startup config %s", self.startup_config)

    def check_compose(self) -> bool:
        base_command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.project_directory),
            "--env-file",
            str(self.project_directory / ".env"),
            "-f",
            str(self.compose_file),
        ]
        try:
            services_result = self.run(
                [*base_command, "config", "--services"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            expected_services = {
                line.strip()
                for line in services_result.stdout.splitlines()
                if line.strip()
            }
            if not expected_services:
                logger.error("Docker Compose did not report any configured services")
                return False
            ps_result = self.run(
                [*base_command, "ps", "--all", "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Could not query Docker Compose health: %s", exc)
            return False

        containers = parse_compose_ps(ps_result.stdout)
        by_service: dict[str, list[ComposeContainer]] = {}
        for container in containers:
            by_service.setdefault(container.service, []).append(container)
        unhealthy: set[str] = set()
        absent: set[str] = set()
        starting: set[str] = set()
        for service in expected_services:
            instances = by_service.get(service, [])
            if not instances or any(item.state not in {"running", "up"} for item in instances):
                absent.add(service)
                continue
            if any(item.health == "starting" for item in instances):
                starting.add(service)
                continue
            if any(item.health == "unhealthy" for item in instances):
                unhealthy.add(service)
        now = self.clock()
        if absent:
            last_attempt = self._last_compose_repair.get("__up__", float("-inf"))
            if now - last_attempt >= self.compose_cooldown_seconds:
                logger.error("Starting absent/stopped Compose services: %s", sorted(absent))
                self._last_compose_repair["__up__"] = now
                try:
                    self.run(
                        [*base_command, "up", "-d", "--remove-orphans"],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                except (OSError, subprocess.SubprocessError):
                    logger.exception("Could not start missing Compose services")
            return False
        if unhealthy:
            for service in sorted(unhealthy):
                last_attempt = self._last_compose_repair.get(service, float("-inf"))
                if now - last_attempt < self.compose_cooldown_seconds:
                    continue
                self._last_compose_repair[service] = now
                try:
                    self.run(
                        [*base_command, "restart", service],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except (OSError, subprocess.SubprocessError):
                    logger.exception("Could not restart Compose service %s", service)
            return False
        if starting:
            logger.info("Compose services are still starting: %s", sorted(starting))
            return False
        return not unhealthy

    def run_forever(self) -> None:
        logger.info(
            "Host watchdog started; terminal=%s heartbeat=%s",
            self.terminal_path,
            self.heartbeat_path,
        )
        while True:
            try:
                self.check_terminal()
            except Exception:
                logger.exception("MT5 watchdog check failed")
            try:
                self.check_compose()
            except Exception:
                logger.exception("Docker watchdog check failed")
            self.sleep(self.poll_seconds)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--terminal-path",
        type=Path,
        default=os.environ.get("MT5_TERMINAL_PATH"),
        required=os.environ.get("MT5_TERMINAL_PATH") is None,
    )
    parser.add_argument(
        "--startup-config",
        type=Path,
        default=Path(__file__).with_name("mt5_startup.ini"),
    )
    parser.add_argument(
        "--heartbeat-path",
        type=Path,
        default=(
            Path(os.environ["MT5_WATCHDOG_HEARTBEAT_PATH"])
            if os.environ.get("MT5_WATCHDOG_HEARTBEAT_PATH")
            else None
        ),
    )
    parser.add_argument(
        "--project-directory",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("docker-compose.yml"),
    )
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--initial-delay-seconds", type=float, default=30)
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=15)
    parser.add_argument("--startup-grace-seconds", type=float, default=120)
    parser.add_argument("--restart-grace-seconds", type=float, default=20)
    parser.add_argument("--compose-cooldown-seconds", type=float, default=60)
    parser.add_argument("--log-file", type=Path)
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    log_path = args.log_file or (
        args.project_directory / "logs" / "host_watchdog.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("HOST_WATCHDOG_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )
    terminal_path = args.terminal_path.resolve()
    heartbeat_path = args.heartbeat_path
    if heartbeat_path is None:
        common_files = (
            Path(os.environ.get("APPDATA", Path.home()))
            / "MetaQuotes"
            / "Terminal"
            / "Common"
            / "Files"
        )
        heartbeat_path = common_files / "equity_guard_heartbeat.dat"
    if not heartbeat_path.is_absolute():
        heartbeat_path = heartbeat_path.resolve()
    compose_file = args.compose_file
    if not compose_file.is_absolute():
        compose_file = args.project_directory / compose_file
    watchdog = HostWatchdog(
        terminal_path=terminal_path,
        startup_config=args.startup_config,
        heartbeat_path=heartbeat_path,
        project_directory=args.project_directory,
        compose_file=compose_file,
        poll_seconds=args.poll_seconds,
        heartbeat_timeout_seconds=args.heartbeat_timeout_seconds,
        startup_grace_seconds=args.startup_grace_seconds,
        restart_grace_seconds=args.restart_grace_seconds,
        compose_cooldown_seconds=args.compose_cooldown_seconds,
    )
    if args.initial_delay_seconds < 0:
        raise ValueError("initial delay must not be negative")
    if args.initial_delay_seconds:
        time.sleep(args.initial_delay_seconds)
    watchdog.run_forever()


if __name__ == "__main__":
    main()
