"""Safe background lifecycle control for the TokenSQL workspace."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from .config import PROJECT_ROOT, WorkspaceSettings

MODULE = "tokensql.workspace.runtime"
FALLBACK_BIN = Path("/data1/luxiaofeng/opencode-runtime/node_modules/.bin/opencode")
FALLBACK_CONFIG = Path("/home/user/luxiaofeng/local-coding-agent/opencode.json")


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Spec:
    host: str
    port: int
    opencode_port: int
    workspace: Path
    external: bool
    agent: Path | None = None


@dataclass(frozen=True)
class Process:
    pid: int
    starttime: int
    state: str
    cwd: Path
    argv: tuple[str, ...]
    environ: dict[str, str]


def option(argv, name, default=None):
    for index in range(len(argv) - 1, -1, -1):
        item = argv[index]
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return default


def has_option(argv, name):
    return any(item == name or item.startswith(name + "=") for item in argv)


def read_process(pid, proc=Path("/proc")):
    base = proc / str(pid)
    try:
        stat = (base / "stat").read_text()
        fields = stat[stat.rfind(")") + 1 :].split()
        argv = tuple(
            value.decode(errors="surrogateescape")
            for value in (base / "cmdline").read_bytes().split(b"\0") if value
        )
        environment = {}
        for value in (base / "environ").read_bytes().split(b"\0"):
            if b"=" in value:
                key, data = value.split(b"=", 1)
                environment[key.decode(errors="surrogateescape")] = data.decode(
                    errors="surrogateescape"
                )
        return Process(
            pid, int(fields[19]), fields[0], (base / "cwd").resolve(strict=True),
            argv, environment,
        )
    except (OSError, ValueError, IndexError):
        return None


def identity(process):
    data = "\0".join((
        str(process.pid), str(process.starttime), str(process.cwd), *process.argv,
    ))
    return hashlib.sha256(data.encode(errors="surrogateescape")).hexdigest()


def make_spec(args, settings):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--opencode-port", type=int, default=8766)
    parser.add_argument("--workspace-dir", type=Path)
    parser.add_argument("--external-opencode", action="store_true")
    parser.add_argument("--agent-dir")
    parser.add_argument("--opencode-bin")
    parser.add_argument("--provider-config")
    values, _ = parser.parse_known_args(args)
    if not 1 <= values.port <= 65535 or not 1 <= values.opencode_port <= 65535:
        raise ServiceError("Ports must be between 1 and 65535")
    return Spec(
        values.host, values.port, values.opencode_port,
        (values.workspace_dir or settings.workspace_dir).resolve(),
        values.external_opencode,
        Path(values.agent_dir or settings.agent_dir).resolve(),
    )


def process_path(process, argument, variable, default):
    value = option(process.argv, argument) or process.environ.get(variable)
    try:
        path = Path(value) if value else Path(default)
        return (path if path.is_absolute() else process.cwd / path).resolve()
    except (OSError, ValueError):
        return None


def workspace_of(process, default):
    return process_path(process, "--workspace-dir", "TOKENSQL_WORKSPACE_DIR", default)


def matches(process, spec, root, default_workspace, default_agent=None):
    module = any(
        process.argv[index:index + 2] == ("-m", MODULE)
        for index in range(len(process.argv) - 1)
    )
    try:
        port = int(option(process.argv, "--port", 8765))
        worker_port = int(option(process.argv, "--opencode-port", 8766))
    except ValueError:
        return False
    interpreter = bool(process.argv) and Path(process.argv[0]).name.startswith("python")
    agent_matches = spec.agent is None or process_path(
        process, "--agent-dir", "TOKENSQL_AGENT_DIR",
        default_agent or root.parent / "TokenSQL-opencode",
    ) == spec.agent
    return (
        process.state != "Z"
        and process.cwd == root.resolve()
        and interpreter and module
        and port == spec.port
        and worker_port == spec.opencode_port
        and has_option(process.argv, "--external-opencode") == spec.external
        and option(process.argv, "--host", "127.0.0.1") == spec.host
        and workspace_of(process, default_workspace) == spec.workspace
        and agent_matches
    )


class Controller:
    def __init__(self, args=(), *, root=PROJECT_ROOT, proc=Path("/proc"), settings=None):
        self.root = root.resolve()
        self.proc = proc
        self.settings = settings or WorkspaceSettings()
        self.args = list(args)
        self.spec = make_spec(self.args, self.settings)
        self.workspace = self.spec.workspace
        self.metadata = self.workspace / "service.json"
        self.lockfile = self.workspace / "service.lock"
        self.logfile = self.workspace / "service.log"
        self.start_timeout = float(os.getenv("TOKENSQL_SERVICE_START_TIMEOUT", "60"))
        self.stop_timeout = float(os.getenv("TOKENSQL_SERVICE_STOP_TIMEOUT", "20"))
        if not all(math.isfinite(v) and 0 < v <= 300 for v in (self.start_timeout, self.stop_timeout)):
            raise ServiceError("Service timeouts must be finite and between 0 and 300 seconds")

    @contextmanager
    def lock(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        handle = self.lockfile.open("a+")
        os.set_inheritable(handle.fileno(), False)
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ServiceError("Another service operation is in progress; retry shortly") from error
            yield
        finally:
            handle.close()


    def load(self):
        try:
            value = json.loads(self.metadata.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise ServiceError(f"cannot read service metadata: {error}") from error
        if not isinstance(value, dict):
            raise ServiceError("invalid service metadata")
        return value

    def save(self, process, token):
        value = {
            "version": 1, "pid": process.pid, "starttime": process.starttime,
            "project_cwd": str(self.root), "workspace_dir": str(self.workspace),
            "host": self.spec.host, "port": self.spec.port,
            "identity_token": identity(process), "service_token": token,
            "argv": list(process.argv), "started_at": int(time.time()),
        }
        temporary = self.metadata.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.metadata)
        finally:
            temporary.unlink(missing_ok=True)

    def validate(self, value):
        try:
            process = read_process(int(value["pid"]), self.proc)
            starttime = int(value["starttime"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            process is None or process.starttime != starttime
            or value.get("project_cwd") != str(self.root)
            or value.get("workspace_dir") != str(self.workspace)
            or not matches(process, self.spec, self.root, self.settings.workspace_dir, self.settings.agent_dir)
            or value.get("identity_token") != identity(process)
        ):
            return None
        token = value.get("service_token")
        if token is not None and (
            not isinstance(token, str)
            or process.environ.get("TOKENSQL_SERVICE_TOKEN") != token
        ):
            return None
        return process

    def candidates(self):
        found = []
        try:
            entries = self.proc.iterdir()
        except OSError:
            return found
        for entry in entries:
            if entry.name.isdigit():
                process = read_process(int(entry.name), self.proc)
                if process and matches(
                    process, self.spec, self.root, self.settings.workspace_dir, self.settings.agent_dir
                ):
                    found.append(process)
        if len(found) > 1:
            raise ServiceError("multiple matching runtimes; refusing to choose")
        return found

    def port_available(self, host, port):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family) as probe:
                # Match Uvicorn's reuse behavior; TIME_WAIT is not a listener.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
            return True
        except OSError:
            return False


    def command(self):
        args = list(self.args)
        if (
            not has_option(args, "--auto-refresh-fx")
            and not has_option(args, "--no-auto-refresh-fx")
            and "fx_auto_refresh" not in self.settings.model_fields_set
        ):
            args.append("--auto-refresh-fx")
        if not self.spec.external:
            if not has_option(args, "--opencode-bin"):
                value = self.settings.opencode_bin
                if value is not None or FALLBACK_BIN.is_file():
                    args += ["--opencode-bin", str(value or FALLBACK_BIN)]
            if not has_option(args, "--provider-config"):
                value = self.settings.opencode_config
                if value is not None or FALLBACK_CONFIG.is_file():
                    args += ["--provider-config", str(value or FALLBACK_CONFIG)]
        return [str(self.root / ".venv/bin/python"), "-m", MODULE, *args]

    def spawn(self, token):
        environment = os.environ.copy()
        environment["TOKENSQL_SERVICE_TOKEN"] = token
        log = self.logfile.open("ab", buffering=0)
        try:
            return subprocess.Popen(
                self.command(), cwd=self.root, env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
        finally:
            log.close()

    @staticmethod
    def health(url, kind):
        try:
            opener = build_opener(ProxyHandler({}))
            with opener.open(url, timeout=1.5) as response:
                value = json.loads(response.read())
                if response.status != 200:
                    return False
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict):
            return False
        if kind == "workspace":
            return (
                value.get("status") == "ok"
                and value.get("application") == "tokensql-workspace"
            )
        return value.get("healthy") is True

    def wait_identity(self, child, token):
        deadline = time.monotonic() + min(self.start_timeout, 5)
        while time.monotonic() < deadline:
            process = read_process(child.pid, self.proc)
            if (process is not None
                    and matches(process, self.spec, self.root, self.settings.workspace_dir, self.settings.agent_dir)
                    and process.environ.get("TOKENSQL_SERVICE_TOKEN") == token):
                return process
            if child.poll() is not None:
                raise ServiceError("runtime exited during startup; see service.log")
            time.sleep(0.02)
        raise ServiceError("new runtime identity did not stabilize; see service.log")

    def wait_ready(self, pid):
        deadline = time.monotonic() + self.start_timeout
        host = "127.0.0.1" if self.spec.host in ("0.0.0.0", "::") else self.spec.host
        workspace_url = f"http://{host}:{self.spec.port}/health"
        opencode_url = (
            f"http://127.0.0.1:{self.spec.opencode_port}/global/health"
        )
        while time.monotonic() < deadline:
            process = read_process(pid, self.proc)
            if process is None or process.state == "Z":
                raise ServiceError("runtime exited during startup; see service.log")
            record = self.load()
            if not record or self.validate(record) is None:
                raise ServiceError("runtime identity changed during startup")
            if self.health(workspace_url, "workspace") and (
                self.spec.external or self.health(opencode_url, "opencode")
            ):
                return
            time.sleep(0.25)
        raise ServiceError(
            f"runtime not ready within {self.start_timeout:g}s; see service.log"
        )

    def wait_exited(self, pid):
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            process = read_process(pid, self.proc)
            if process is None or process.state == "Z":
                return True
            time.sleep(0.1)
        process = read_process(pid, self.proc)
        return process is None or process.state == "Z"


    def expired_metadata(self, value):
        """Only forget an exited process record, never an alive/reused PID."""
        try:
            if (value.get("project_cwd") != str(self.root)
                    or value.get("workspace_dir") != str(self.workspace)):
                return False
            process = read_process(int(value["pid"]), self.proc)
        except (KeyError, TypeError, ValueError):
            return False
        return process is None or (
            process.state == "Z" and process.starttime == int(value.get("starttime", -1))
        )

    def start(self):
        with self.lock():
            value = self.load()
            if value:
                process = self.validate(value)
                if process:
                    print(f"TokenSQL already running (PID {process.pid})")
                    return 0
                if self.expired_metadata(value):
                    self.metadata.unlink(missing_ok=True)
                else:
                    raise ServiceError("mismatched metadata; refusing to replace a live process record")
            found = self.candidates()
            if found:
                process = found[0]
                self.save(process, None)
                print(f"Adopted TokenSQL runtime (PID {process.pid})")
                return 0
            if not self.port_available(self.spec.host, self.spec.port):
                raise ServiceError(
                    f"{self.spec.host}:{self.spec.port} is occupied by a foreign "
                    "process; it will not be stopped"
                )
            if (
                not self.spec.external
                and not self.port_available("127.0.0.1", self.spec.opencode_port)
            ):
                raise ServiceError(
                    f"OpenCode port {self.spec.opencode_port} is occupied; "
                    "the process will not be stopped"
                )
            token = secrets.token_urlsafe(24)
            child = self.spawn(token)
            try:
                process = self.wait_identity(child, token)
                self.save(process, token)
            except (OSError, ServiceError):
                # This unreaped Popen child is ours, even before /proc settles.
                if child.poll() is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    print(f"New child PID {child.pid} did not stop; SIGKILL was not sent", file=sys.stderr)
                raise
            try:
                self.wait_ready(child.pid)
            except ServiceError:
                record = self.load() or {}
                current = self.validate(record)
                if current:
                    try:
                        os.kill(current.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    if self.wait_exited(current.pid):
                        self.metadata.unlink(missing_ok=True)
                        child.poll()
                elif self.expired_metadata(record):
                    self.metadata.unlink(missing_ok=True)
                    child.poll()
                raise
            print(
                f"TokenSQL started (PID {child.pid}): "
                f"http://{self.spec.host}:{self.spec.port}"
            )
            print(f"Log: {self.logfile}")
            return 0

    def stop(self):
        with self.lock():
            value = self.load()
            if value:
                process = self.validate(value)
                if process is None:
                    if self.expired_metadata(value):
                        self.metadata.unlink(missing_ok=True)
                        print("TokenSQL is not running (removed exited process record)")
                        return 0
                    raise ServiceError(
                        "stale or mismatched metadata; refusing to signal any process"
                    )
            else:
                found = self.candidates()
                if not found:
                    print("TokenSQL is not running")
                    return 0
                process = found[0]

            if value:
                current = self.validate(value)
            else:
                current = read_process(process.pid, self.proc)
                if (
                    current is None
                    or not matches(
                        current, self.spec, self.root, self.settings.workspace_dir, self.settings.agent_dir
                    )
                    or identity(current) != identity(process)
                ):
                    current = None
            if current is None:
                raise ServiceError("process identity changed; refusing SIGTERM")
            try:
                os.kill(current.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not self.wait_exited(current.pid):
                raise ServiceError(
                    f"PID {current.pid} did not stop within {self.stop_timeout:g}s; "
                    "SIGKILL was not sent"
                )
            self.metadata.unlink(missing_ok=True)
            print(f"TokenSQL stopped (PID {current.pid})")
            return 0

    def status(self):
        value = self.load()
        if value:
            process = self.validate(value)
            if process:
                print(
                    f"TokenSQL running (PID {process.pid}, managed): "
                    f"http://{self.spec.host}:{self.spec.port}"
                )
                return 0
            print("TokenSQL metadata is stale; no process or file was changed")
            return 3
        found = self.candidates()
        if found:
            print(
                f"TokenSQL running (PID {found[0].pid}, unmanaged): "
                f"http://{self.spec.host}:{self.spec.port}"
            )
            return 0
        print("TokenSQL is not running")
        return 3

    def logs(self, lines=100):
        try:
            with self.logfile.open(encoding="utf-8", errors="replace") as handle:
                for line in deque(handle, maxlen=lines):
                    print(line, end="")
            return 0
        except FileNotFoundError:
            raise ServiceError(f"log does not exist: {self.logfile}")


USAGE = """Usage: service.sh {start|stop|restart|status|logs} [-- runtime-options]

Examples:
  sh service.sh start
  sh service.sh status
  sh service.sh start -- --port 8795 --workspace-dir /tmp/tokensql
"""


def menu():
    print("\u0054\u006f\u006b\u0065\u006e\u0053\u0051\u004c \u670d\u52a1\u7ba1\u7406")
    print("\u0031\u0029 \u542f\u52a8  \u0032\u0029 \u505c\u6b62  \u0033\u0029 \u91cd\u542f  \u0034\u0029 \u72b6\u6001  \u0035\u0029 \u65e5\u5fd7  \u0030\u0029 \u9000\u51fa")
    try:
        choice = input("\u8bf7\u9009\u62e9\uff1a").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return {
        "1": "start", "2": "stop", "3": "restart",
        "4": "status", "5": "logs", "0": None,
    }.get(choice, "")


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if not sys.stdin.isatty():
            print(USAGE, file=sys.stderr)
            return 2
        command = menu()
        if command is None:
            return 0
        if not command:
            print("Invalid selection", file=sys.stderr)
            return 2
        arguments = [command]
    command = arguments.pop(0)
    if command not in {"start", "stop", "restart", "status", "logs"}:
        print(USAGE, file=sys.stderr)
        return 2
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    try:
        controller = Controller(arguments)
        if command == "start":
            return controller.start()
        if command == "stop":
            return controller.stop()
        if command == "status":
            return controller.status()
        if command == "logs":
            return controller.logs()
        result = controller.stop()
        return controller.start() if result == 0 else result
    except SystemExit as error:
        return int(error.code)
    except (OSError, ServiceError, ValueError) as error:
        print(f"{command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
