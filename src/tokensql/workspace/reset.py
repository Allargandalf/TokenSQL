"""Recoverable reset of this project's database and dedicated OpenCode state."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from uuid import uuid4

from .config import PROJECT_ROOT, WorkspaceSettings
from .memory import AgentMemory, atomic_json
from .sandbox import prepare_agent_directories
from .service import Controller, MODULE, ServiceError, option, process_path, read_process
from .store import WorkspaceStore
from .workflow import WorkflowService


@dataclass(frozen=True)
class ResetPaths:
    root: Path
    workspace: Path
    agent: Path
    backup_base: Path


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _unaliased(path: Path) -> Path:
    value = path.absolute()
    if value.resolve() != value or value.is_symlink():
        raise ServiceError(f"Refusing symlink/redirected cleanup path: {value}")
    if value.exists() and not value.is_dir():
        raise ServiceError(f"Cleanup target is not a directory: {value}")
    return value


def validate_paths(root: Path, settings: WorkspaceSettings) -> ResetPaths:
    root = root.resolve()
    workspace = _unaliased(settings.workspace_dir)
    agent = _unaliased(settings.agent_dir)
    if workspace != root / "var" / "workspace":
        raise ServiceError("Cleanup only supports this project's var/workspace directory")
    if agent != root.parent / (root.name + "-opencode"):
        raise ServiceError("Cleanup only supports this project's dedicated sibling OpenCode directory")
    backup_base = _unaliased(root.parent / "TokenSQL-maintenance-backups")
    protected = [
        settings.input_dir.resolve(), settings.progress_dir.resolve(),
        settings.data_dir.resolve(), root / "tests", root / ".git",
        root / "src", root / "scripts", root / "docs", root / ".env",
    ]
    protected.extend(p for p in (settings.opencode_config, settings.opencode_bin) if p is not None)
    protected = [p.resolve() for p in protected]
    for target in (workspace, agent, backup_base):
        if any(_overlap(target, item) for item in protected):
            raise ServiceError(f"Cleanup path overlaps preserved files: {target}")
    if any(_overlap(a, b) for a, b in (
        (workspace, agent), (workspace, backup_base), (agent, backup_base),
    )):
        raise ServiceError("Cleanup and backup directories must be separate")
    return ResetPaths(root, workspace, agent, backup_base)


def _running(controller: Controller):
    value = controller.load()
    if value:
        process = controller.validate(value)
        if process:
            return process
        if not controller.expired_metadata(value):
            raise ServiceError("Mismatched live process metadata; refusing cleanup")
    found = controller.candidates()
    return found[0] if found else None


def _quiescent(controller: Controller, paths: ResetPaths) -> None:
    if controller.load() or controller.candidates():
        raise ServiceError("A runtime started during cleanup; no data was moved")
    if not controller.port_available(controller.spec.host, controller.spec.port):
        raise ServiceError("Web port is still occupied; no data was moved")
    if not controller.port_available("127.0.0.1", controller.spec.opencode_port):
        raise ServiceError("OpenCode port is still occupied; no data was moved")
    # A different port/spec or an orphan worker can still use these directories.
    # Refuse it, never stop an unknown process or a shared model service.
    for entry in controller.proc.iterdir():
        if not entry.name.isdigit():
            continue
        process = read_process(int(entry.name), controller.proc)
        if process is None or process.state == "Z" or process.pid == os.getpid():
            continue
        owned_directory = any(
            process.cwd == target or process.cwd.is_relative_to(target)
            for target in (paths.workspace, paths.agent)
        )
        runtime = any(
            process.argv[i:i + 2] == ("-m", MODULE)
            for i in range(len(process.argv) - 1)
        )
        if runtime:
            work = process_path(
                process, "--workspace-dir", "TOKENSQL_WORKSPACE_DIR",
                process.cwd / "var" / "workspace",
            )
            agent = process_path(
                process, "--agent-dir", "TOKENSQL_AGENT_DIR",
                process.cwd.parent / (process.cwd.name + "-opencode"),
            )
            owned_directory |= any(
                value is not None and _overlap(value, target)
                for value in (work, agent) for target in (paths.workspace, paths.agent)
            )
        for name in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                     "XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"):
            value = process.environ.get(name)
            if value:
                candidate = Path(value).resolve()
                owned_directory |= candidate == paths.agent or candidate.is_relative_to(paths.agent)
        if owned_directory:
            raise ServiceError(f"PID {process.pid} still uses cleanup directories; no data was moved")


def _initialize(settings: WorkspaceSettings) -> None:
    settings.agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepare_agent_directories(settings)
    store = WorkspaceStore(settings.database_path)
    try:
        memory = AgentMemory(settings.agent_dir)
        WorkflowService(settings, store, memory)
    finally:
        store.close()


def reset(controller: Controller, paths: ResetPaths, *, restart: bool = True) -> Path:
    if (controller.root != paths.root or controller.workspace != paths.workspace
            or controller.spec.agent != paths.agent):
        raise ServiceError("Controller and cleanup paths do not match")
    if controller.spec.external:
        raise ServiceError("External/shared OpenCode instances cannot be reset")
    if validate_paths(paths.root, controller.settings) != paths:
        raise ServiceError("Cleanup paths changed")
    process = _running(controller)
    original_args = None
    if process:
        index = next(i for i in range(len(process.argv) - 1)
                     if process.argv[i:i + 2] == ("-m", MODULE))
        original_args = list(process.argv[index + 2:])
    command = process.argv if process is not None else controller.command()
    for flag in ("--provider-config", "--opencode-bin"):
        raw = option(command, flag)
        if raw:
            resource = Path(raw)
            resource = (resource if resource.is_absolute() else paths.root / resource).resolve()
            if any(_overlap(resource, target) for target in
                   (paths.workspace, paths.agent, paths.backup_base)):
                raise ServiceError(f"Cleanup would move a required {flag} resource")
    controller.stop()  # Existing identity check and graceful owned-child shutdown.
    backup = None
    manifest = None
    old_umask = os.umask(0o077)
    try:
        # Do not rename workspace or service.lock: another service command must
        # see this same lock inode throughout the entire reset.
        with controller.lock():
            if validate_paths(paths.root, controller.settings) != paths:
                raise ServiceError("Cleanup paths changed after shutdown")
            _quiescent(controller, paths)
            paths.backup_base.mkdir(mode=0o700, parents=True, exist_ok=True)
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-reset-" + uuid4().hex[:12]
            backup = paths.backup_base / name
            backup.mkdir(mode=0o700)
            archived_workspace = backup / "workspace"
            archived_workspace.mkdir(mode=0o700)
            # rename is atomic per entry and does not follow child symlinks.
            # Refuse cross-filesystem moves instead of copy/delete fallbacks.
            device = backup.stat().st_dev
            sources = [p for p in paths.workspace.iterdir() if p.name != "service.lock"]
            if paths.agent.exists():
                sources.append(paths.agent)
            if any(p.lstat().st_dev != device for p in sources):
                raise ServiceError("Cross-filesystem reset refused; data was not moved")
            manifest = {
                "status": "moving", "project": str(paths.root),
                "workspace": str(paths.workspace), "agent": str(paths.agent),
                "backup": str(backup), "moved": [], "restarted": False,
                "preserved": ["input/", "progress/", "data/", "tests/", ".env", "source code"],
                "clears": ["all database records including audit", "OpenCode sessions, drafts, logs and learned memory"],
            }
            atomic_json(backup / "manifest.json", manifest)
            print(f"备份目录：{backup}", flush=True)
            for source in sources:
                destination = (backup / "agent" if source == paths.agent
                               else archived_workspace / source.name)
                source.rename(destination)
                manifest["moved"].append({"from": str(source), "to": str(destination)})
                atomic_json(backup / "manifest.json", manifest)
            _initialize(controller.settings)
            manifest["status"] = "cleared"
            atomic_json(backup / "manifest.json", manifest)
        if restart and process is not None:
            controller.args = original_args
            controller.start()
            manifest["restarted"] = True
        manifest["status"] = "completed"
        atomic_json(backup / "manifest.json", manifest)
        print("清理完成：数据库、OpenCode会话/草案/纠错记忆已重置；Excel和项目代码保留。")
        print("原progress中的Excel仍保留，但旧任务不可续答，需要重新选择文件导入。")
        if not manifest["restarted"]:
            print("服务保持停止；需要时执行 sh service.sh start。")
        return backup
    except BaseException as error:
        if backup is not None and manifest is not None:
            manifest.update(status="failed", error=str(error)[:1000])
            try:
                atomic_json(backup / "manifest.json", manifest)
            except OSError:
                pass
            print(f"清理未完成；原资料仍在原路径或备份 {backup}，不自动回灌，请人工核查。", file=sys.stderr)
        raise
    finally:
        os.umask(old_umask)


def main(argv=None, *, root: Path = PROJECT_ROOT, settings: WorkspaceSettings | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理本项目数据库及专属OpenCode记录；原资料移至项目外私有备份，保留Excel。")
    parser.add_argument("--yes", action="store_true", help="跳过一次人工确认")
    parser.add_argument("--dry-run", action="store_true", help="只显示范围，不停服、不创建文件")
    parser.add_argument("--no-restart", action="store_true", help="清理后保持停服")
    args = parser.parse_args(argv)
    try:
        settings = settings or WorkspaceSettings()
        paths = validate_paths(root, settings)
        controller = Controller(root=paths.root, settings=settings)
        process = _running(controller)
        print(f"清理数据库目录：{paths.workspace}")
        print(f"清理专属OpenCode：{paths.agent}")
        print("包含：模型/官网价/报价/审计/汇率上下文，以及会话、任务草案、日志和长期纠错记忆。")
        print("保留：input/progress/data中的Excel、tests、代码、.env；不影响共享模型服务。")
        print(f"备份根目录：{paths.backup_base}（旧记录可人工恢复，不自动导入）")
        print("完成后：" + ("自动重启原来运行中的服务" if process and not args.no_restart else "保持服务停止"))
        if args.dry_run:
            print("仅预览：未停服、未移动或创建任何数据。")
            return 0
        if not args.yes:
            try:
                answer = input("确认清理请输入 RESET：").strip()
            except EOFError:
                answer = ""
            if answer != "RESET":
                print("已取消，未改动数据或服务。")
                return 0
        reset(controller, paths, restart=not args.no_restart)
        print("浏览器请Ctrl+F5刷新，并新建会话。")
        return 0
    except KeyboardInterrupt:
        print("已中断；若已经开始清理，请检查已打印的备份路径和服务状态。", file=sys.stderr)
        return 130
    except (OSError, ServiceError, ValueError) as error:
        print(f"清理中止：{error}\n未强杀任何进程；服务可能已停止，请检查 sh service.sh status。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
