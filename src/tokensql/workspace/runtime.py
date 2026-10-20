"""Launch the quote webpage and an isolated OpenCode server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

import httpx
import uvicorn
from .config import WorkspaceSettings
from .sandbox import prepare_agent_directories, sandbox_command


def available_port(host: str, port: int) -> None:
    with socket.socket() as probe:
        # A recently closed listener may leave TIME_WAIT sockets; Uvicorn reuses them.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))


def isolated_config(settings: WorkspaceSettings, source: Path) -> dict:
    with source.open(encoding="utf-8") as handle:
        provider = json.load(handle).get("provider", {}).get(settings.opencode_provider)
    if not isinstance(provider, dict):
        raise ValueError("Configured provider is missing from provider-config")
    runs = str(settings.agent_dir.resolve() / "runs")
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{settings.opencode_provider}/{settings.opencode_model}",
        "provider": {settings.opencode_provider: provider},
        "instructions": [str(settings.agent_dir.resolve() / "agent.md")],
        "permission": {
            "read": {"*": "allow", "*handoff*": "deny", "*.private-docs*": "deny"},
            "edit": {"*": "deny", f"{runs}/*": "allow"},
            "bash": "allow", "external_directory": "allow",
            # Network tools remain denied by default; only an official lookup session may allow them.
            "webfetch": "deny", "websearch": "deny", "question": "deny",
        },
        "share": "disabled", "autoupdate": False, "snapshot": False,
        "lsp": False, "formatter": False,
    }


def wait_for_server(process: subprocess.Popen, url: str) -> None:
    deadline = time.monotonic() + 45
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("OpenCode exited during startup")
            try:
                if client.get(url + "/global/health").json().get("healthy"):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
    raise RuntimeError("OpenCode did not become ready within 45 seconds")


def _terminate(signum, frame) -> None:
    # Uvicorn restores this handler before re-raising SIGTERM on shutdown.
    raise SystemExit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--opencode-port", type=int, default=8766)
    parser.add_argument("--workspace-dir", type=Path)
    parser.add_argument("--agent-dir", type=Path)
    parser.add_argument("--unsafe-no-sandbox", action="store_true")
    parser.add_argument("--opencode-bin", default=os.environ.get("TOKENSQL_OPENCODE_BIN"))
    parser.add_argument("--provider-config", type=Path, default=os.environ.get("TOKENSQL_OPENCODE_CONFIG"))
    parser.add_argument("--external-opencode", action="store_true")
    parser.add_argument("--auto-refresh-fx", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.opencode_port <= 65535:
        parser.error("Ports must be between 1 and 65535")
    if not args.external_opencode and args.port == args.opencode_port:
        parser.error("HTTP and OpenCode ports must be different")
    settings = WorkspaceSettings()
    args.opencode_bin = args.opencode_bin or settings.opencode_bin
    args.provider_config = args.provider_config or settings.opencode_config
    if args.auto_refresh_fx is not None:
        settings.fx_auto_refresh = args.auto_refresh_fx
    if args.workspace_dir:
        settings.workspace_dir = args.workspace_dir.resolve()
    if args.agent_dir:
        settings.agent_dir = args.agent_dir.resolve()
    if not args.external_opencode:
        settings.opencode_url = f"http://127.0.0.1:{args.opencode_port}"
    available_port(args.host, args.port)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    prepare_agent_directories(settings)
    process = None
    log = None
    previous_handler = signal.signal(signal.SIGTERM, _terminate)
    try:
        if not args.external_opencode:
            if not args.opencode_bin or not Path(args.opencode_bin).is_file():
                parser.error("Set --opencode-bin to the real executable, not a shared-env wrapper")
            if not args.provider_config or not args.provider_config.is_file():
                parser.error("Set --provider-config to an existing OpenCode provider JSON")
            available_port("127.0.0.1", args.opencode_port)
            environment = os.environ.copy()
            for name in list(environment):
                if name.startswith("OPENCODE_"):
                    environment.pop(name, None)
            environment["HOME"] = str(settings.agent_dir.resolve() / "home")
            environment["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
            # Register the built-in search tool for this custom provider, not a permission grant.
            # Global and ordinary-session permissions deny it; official lookup alone overrides.
            environment["OPENCODE_ENABLE_EXA"] = "true"
            environment["OPENCODE_CONFIG_DIR"] = str(settings.agent_dir.resolve() / "opencode" / "config")
            from .memory import AgentMemory
            AgentMemory(settings.agent_dir)
            environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(isolated_config(settings, args.provider_config))
            for variable, folder in (
                ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
            ):
                location = settings.agent_dir / "opencode" / folder
                location.mkdir(parents=True, exist_ok=True)
                environment[variable] = str(location.resolve())
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            log = (settings.agent_dir / "opencode.log").open("a", encoding="utf-8")
            command = [str(Path(args.opencode_bin).resolve()), "serve", "--hostname",
                       "127.0.0.1", "--port", str(args.opencode_port)]
            command = sandbox_command(settings, command, unsafe=args.unsafe_no_sandbox)
            process = subprocess.Popen(
                command, cwd=settings.agent_dir, env=environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
            wait_for_server(process, settings.opencode_url)
        os.environ["TOKENSQL_WORKSPACE_DIR"] = str(settings.workspace_dir.resolve())
        os.environ["TOKENSQL_OPENCODE_URL"] = settings.opencode_url
        from .app import create_app
        api = create_app(settings)
        print(f"TokenSQL workspace: http://{args.host}:{args.port}", flush=True)
        uvicorn.run(api, host=args.host, port=args.port, log_level="info")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log is not None:
            log.close()


if __name__ == "__main__":
    main()
