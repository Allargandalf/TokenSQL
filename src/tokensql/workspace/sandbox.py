"""Read-only host filesystem and an isolated writable OpenCode workspace.

Network is shared for the existing inference server. This is accidental-write
containment for a trusted single-user deployment, not a hostile-code sandbox.
"""
from pathlib import Path
import shutil

from .config import PROJECT_ROOT, WorkspaceSettings


def prepare_agent_directories(settings: WorkspaceSettings) -> None:
    root = settings.agent_dir.resolve()
    if root == PROJECT_ROOT or root.is_relative_to(PROJECT_ROOT):
        raise ValueError("agent_dir must be outside the source repository")
    for relative in ("runs", "catalogs", "home", "opencode/config",
                     "opencode/data", "opencode/state", "opencode/cache"):
        (root / relative).mkdir(parents=True, exist_ok=True)


def sandbox_command(settings: WorkspaceSettings, command: list[str],
                    *, unsafe: bool = False) -> list[str]:
    prepare_agent_directories(settings)
    if unsafe:
        return command
    binary = shutil.which("bwrap")
    if not binary:
        raise RuntimeError("bubblewrap is required; install it or explicitly use --unsafe-no-sandbox")
    root = settings.agent_dir.resolve()
    args = [binary, "--ro-bind", "/", "/", "--unshare-user", "--uid", "0",
            "--gid", "0", "--unshare-pid", "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp", "--die-with-parent", "--new-session"]
    args.extend(["--ro-bind", str(root), str(root)])
    for name in ("runs", "opencode", "home"):
        path = str(root / name)
        args.extend(["--bind", path, path])
    hidden = {settings.workspace_dir.resolve(), settings.input_dir.resolve(),
              settings.progress_dir.resolve(), settings.data_dir.resolve(),
              PROJECT_ROOT / "var", PROJECT_ROOT / "tests" / "data"}
    hidden.update({PROJECT_ROOT / "handoff", PROJECT_ROOT / ".private-docs",
                   PROJECT_ROOT.parent / "handoff", PROJECT_ROOT.parent / ".private-docs"})
    for name in ("TokenSQL-cleanup-backups", "TokenSQL-maintenance-backups"):
        backup = PROJECT_ROOT.parent / name
        if backup.is_dir():
            hidden.add(backup)
    for path in sorted(hidden, key=lambda item: len(str(item))):
        if root == path or root.is_relative_to(path):
            raise ValueError("agent_dir cannot be inside a hidden data directory")
        if path.is_dir():
            args.extend(["--tmpfs", str(path)])
    args.extend(["--chdir", str(root), "--"])
    return args + command
