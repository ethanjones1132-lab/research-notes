#!/usr/bin/env python3
"""Deterministic coordinator utilities for queue, worktree, gate, and merge."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import signal
import shlex
import subprocess
import tempfile
import time
import sys
from typing import Any


WRITE_FOOTER = (
    "\n\n---\n"
    "When done, commit your changes in this worktree with a conventional commit message.\n"
    "Final report: STATUS: done|blocked|partial, files changed, check command + output, "
    "open questions (<=150 words).\n"
)
# OpenCode workers run contained (shell deny-by-default): telling them to commit/test only burns turns on denials.
EDIT_ONLY_FOOTER = (
    "\n\n---\n"
    "How this run works: edit files with your file tools only, inside the current directory. Your shell is "
    "restricted to read-only git (status/log/diff/show/ls-files/grep) and ls: you CANNOT run tests, builds, "
    "installs, or git add/commit, and you must not try to work around that. The coordinator commits your edits "
    "and runs the checks after you finish, so just make the change carefully and stop.\n"
    "Final report: STATUS: done|blocked|partial, files changed, what the check should verify, "
    "open questions (<=150 words).\n"
)
SKIP_MARKERS = (
    ".skip(", "xit(", "xdescribe(", "test.todo", "@pytest.mark.skip",
    "#[ignore]", "pytest.skip(",
)
OPENCODE_WORKERS = frozenset({"deepseek", "mimo", "free", "grokbuild"})
SECRET_LINE_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"[\"']?(?:api[_-]?key|secret|token|password|passwd|client[_-]?secret|access[_-]?key)[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{12,}[\"']", re.I),
)
MAX_SCANNED_BLOB_BYTES = 2 * 1024 * 1024


class RhError(Exception):
    """Expected, user-actionable command failure."""


class BusyError(RhError):
    """Another coordinator mutation owns the global lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


@contextmanager
def mutation_lock(root: Path):
    """Hold the cross-command coordinator lock until the mutation completes."""
    path = root / "state" / "rh.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()}\n{utc_now()}\n"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
    except OSError as exc:
        os.close(descriptor)
        raise BusyError("busy: another rh command is running") from exc

    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        encoded = payload.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def ops_dir() -> Path:
    value = os.environ.get("RH_OPS_DIR")
    return Path(value).expanduser().resolve() if value else Path(__file__).resolve().parent


def resolve_config_path(value: str, base: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def load_config() -> tuple[Path, dict[str, Any]]:
    root = ops_dir()
    path = root / "projects.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RhError(f"Missing {path}; create it from projects.example.json") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RhError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("projects"), dict):
        raise RhError("projects.json must contain an object named 'projects'")
    config = dict(config)
    config["worker_py_path"] = resolve_config_path(str(config.get("worker_py", "worker.py")), root)
    config["worktree_root_path"] = resolve_config_path(str(config.get("worktree_root", "worktrees")), root)
    for name, project in config["projects"].items():
        if not isinstance(project, dict) or not project.get("path"):
            raise RhError(f"Project {name!r} needs a path")
        project["repo_path"] = resolve_config_path(str(project["path"]), root)
        project.setdefault("default_branch", "main")
        project.setdefault("push", False)
        project.setdefault("link_dirs", [])
        project.setdefault("verify", [])
    return root, config


def git_executable() -> str:
    if os.name == "nt":
        # Git for Windows exposes a stable native shim here. Prefer it over the
        # mingw64 executable, which is not launchable in some embedded hosts.
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(entry) / "git.exe"
            if Path(entry).name.lower() == "cmd" and candidate.is_file():
                return str(candidate)
    return shutil.which("git") or "git"


GIT = git_executable()


GIT_TIMEOUT_SECONDS = 600
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}  # never block a beat on a credential prompt


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [GIT, "-C", str(repo), *map(str, args)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=GIT_ENV,
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(args, 124, "", f"git {' '.join(map(str, args))} timed out after {GIT_TIMEOUT_SECONDS}s")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RhError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return result


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_log(root: Path, message: str) -> None:
    path = root / "LOG.md"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"- {utc_now()} {message}\n")


def parse_brief(path: Path) -> tuple[dict[str, Any], str, str]:
    try:
        full_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RhError(f"Cannot read brief {path}: {exc}") from exc
    lines = full_text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise RhError(f"Brief {path.name} must start with a front-matter block")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise RhError(f"Brief {path.name} has no closing front-matter delimiter") from exc
    fields: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise RhError(f"Invalid front-matter line in {path.name}: {line}")
        key, value = line.split(":", 1)
        value = value.split("#", 1)[0].strip()
        fields[key.strip()] = value
    body = "\n".join(lines[end + 1 :]).strip()
    project = str(fields.get("project", "")).strip()
    title = str(fields.get("title", "")).strip() or path.stem
    worker = str(fields.get("worker", "")).strip()
    if not project:
        raise RhError(f"Brief {path.name} needs project:")
    if not worker:
        raise RhError(f"Brief {path.name} needs worker:")
    mode = str(fields.get("mode", "write") or "write").strip().lower()
    if mode not in ("write", "readonly"):
        raise RhError(f"Brief {path.name} mode must be write or readonly")
    try:
        timeout = int(fields.get("timeout", "3600") or "3600")
    except ValueError as exc:
        raise RhError(f"Brief {path.name} timeout must be an integer") from exc
    if timeout <= 0:
        raise RhError(f"Brief {path.name} timeout must be greater than zero")
    fields["project"] = project
    fields["worker"] = worker
    fields["title"] = title
    fields["mode"] = mode
    fields["timeout"] = timeout
    fields["check"] = str(fields.get("check", "")).strip() or None
    raw_scope = str(fields.get("scope", "")).strip()
    fields["scope"] = [item.strip() for item in raw_scope.split(",") if item.strip()] or None
    return fields, body, full_text


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"-+", "-", value) or "task"


def run_id_for(project: str, title: str, stamp: datetime) -> str:
    return f"{stamp.strftime('%Y%m%d-%H%M')}-{project}-{slug(title)[:24].rstrip('-')}"


def get_project(config: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return config["projects"][name]
    except KeyError as exc:
        raise RhError(f"Unknown project {name!r}; add it to projects.json") from exc


def ensure_repo(project: dict[str, Any], name: str) -> Path:
    repo = project["repo_path"]
    if not repo.is_dir():
        raise RhError(f"Project {name!r} repository is missing: {repo}")
    result = run_git(repo, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise RhError(f"Project {name!r} is not a usable git repository: {repo}")
    return repo


def list_queue_files(root: Path, queue_name: str) -> list[Path]:
    base = root / "queue"
    queue_path = (base / queue_name).resolve()
    if not queue_path.is_relative_to(base.resolve()):
        raise RhError("Queue name must be a child of the queue directory")
    if not queue_path.is_dir():
        raise RhError(f"Queue does not exist: {queue_path}")
    return sorted((p for p in queue_path.glob("*.md") if p.is_file()), key=lambda p: p.name.casefold())


def dispatch(root: Path, config: dict[str, Any], queue_name: str, dry_run: bool, maximum: int | None) -> int:
    files = list_queue_files(root, queue_name)
    if maximum is not None:
        if maximum < 0:
            raise RhError("--max must be zero or greater")
        files = files[:maximum]
    stamp = datetime.now()
    planned: list[tuple[Path, dict[str, Any], Path, Path, str | None, Path, str, str]] = []
    used_ids: set[str] = set()
    for brief_path in files:
        fields, _, _ = parse_brief(brief_path)
        project = get_project(config, fields["project"])
        repo = ensure_repo(project, fields["project"])
        default_branch = str(project["default_branch"])
        default_at_dispatch_result = run_git(repo, "rev-parse", f"refs/heads/{default_branch}", check=False)
        if default_at_dispatch_result.returncode:
            raise RhError(f"Project {fields['project']!r} default branch {default_branch} is missing")
        default_at_dispatch = default_at_dispatch_result.stdout.strip()
        base_id = run_id_for(fields["project"], fields["title"], stamp)
        run_id = base_id
        suffix = 2
        while run_id in used_ids:
            run_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(run_id)
        run_dir = root / "runs" / run_id
        if run_dir.exists():
            raise RhError(f"Run already exists for {run_id}")
        branch = f"rh/{run_id}" if fields["mode"] == "write" else None
        # OpenCode has no filesystem sandbox, so even readonly OpenCode runs get a throwaway detached worktree
        # (never Ethan's main checkout) to carry the containment opencode.json.
        isolated = bool(branch) or fields["worker"] in OPENCODE_WORKERS
        worktree = config["worktree_root_path"] / run_id if isolated else repo
        planned.append((brief_path, fields, project, repo, branch, worktree, run_id, default_at_dispatch))

    if dry_run:
        for brief_path, fields, _project, _repo, branch, worktree, run_id, _default_at_dispatch in planned:
            mode = fields["mode"]
            cwd = worktree
            print(f"DRY-RUN {run_id} {mode} cwd={cwd} branch={branch or '-'} brief={brief_path}")
        if not planned:
            print(f"Queue {queue_name}: no briefs")
        return 0

    failures = 0
    go_running = running_go_plan_runs(root)
    launched_go = 0
    for brief_path, fields, project, repo, branch, worktree, run_id, default_at_dispatch in planned:
        if fields["worker"] in GO_PLAN_WORKERS:
            if go_running + launched_go >= GO_PLAN_MAX_RUNNING:
                print(f"HELD {brief_path.name}: {GO_PLAN_MAX_RUNNING} Go-plan runs already in flight; stays queued")
                continue
            if launched_go:
                time.sleep(GO_PLAN_STAGGER_SECONDS)
        run_dir = root / "runs" / run_id
        run_brief = run_dir / "brief.md"
        created_resources: list[str] = []
        try:
            # Claim all run state before asking Git to create the shared worktree.
            run_dir.mkdir(parents=True, exist_ok=False)
            created_resources.append("run_dir")
            if worktree != repo:
                config["worktree_root_path"].mkdir(parents=True, exist_ok=True)
                if branch:
                    add = run_git(repo, "worktree", "add", str(worktree), "-b", branch, str(project["default_branch"]), check=False)
                else:
                    add = run_git(repo, "worktree", "add", "--detach", str(worktree), str(project["default_branch"]), check=False)
                if add.returncode:
                    raise RhError(f"Cannot create worktree {worktree}: {(add.stderr or add.stdout).strip()}")
                created_resources.append("worktree")
                link_project_dirs(repo, worktree, project.get("link_dirs", []))
                if fields["worker"] in OPENCODE_WORKERS:
                    install_opencode_containment(worktree)

            brief_path.replace(run_brief)
            created_resources.append("brief")
            # Workers get the body only: CLIs parse a leading "---" as flags (opencode/codex usage errors, 2026-10-15).
            _, brief_body, _ = parse_brief(run_brief)
            prompt_text = "Task brief from the Right Hand coordinator.\n\n" + brief_body.strip()
            if fields["mode"] == "write":
                prompt_text += EDIT_ONLY_FOOTER if fields["worker"] in OPENCODE_WORKERS else WRITE_FOOTER
            run_prompt = run_dir / "prompt.md"
            run_prompt.write_text(prompt_text + "\n", encoding="utf-8")

            started_at = datetime.now(timezone.utc)
            timeout = int(fields["timeout"])
            meta = {
                "run_id": run_id,
                "project": fields["project"],
                "worker": fields["worker"],
                "title": fields["title"],
                "mode": fields["mode"],
                "branch": branch,
                "worktree": str(worktree) if worktree != repo else None,
                "check": fields["check"],
                "scope": fields["scope"],
                "default_at_dispatch": default_at_dispatch,
                "started": iso_utc(started_at),
                "deadline": iso_utc(started_at + timedelta(seconds=timeout + 900)),
                "pid": None,
                "pid_created": None,
                "status": "running",
                "timeout": timeout,
            }
            write_json(run_dir / "meta.json", meta)
            out_path = run_dir / "out.txt"
            log_path = run_dir / "worker.log"
            argv = [
                sys.executable,
                "-I",
                str(config["worker_py_path"]),
                fields["worker"],
                str(run_prompt),
                str(out_path),
                "--cwd",
                str(worktree),
                "--timeout",
                str(fields["timeout"]),
                "--sandbox",
                "workspace-write" if fields["mode"] == "write" else "read-only",
            ]
            with log_path.open("ab") as log_file:
                kwargs: dict[str, Any] = {"stdout": log_file, "stderr": subprocess.STDOUT, "close_fds": True}
                if os.name == "nt":
                    kwargs["creationflags"] = (
                        # Hidden console, NOT DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW with DETACHED_PROCESS,
                        # and a console-less worker makes every git/node/opencode child pop its own window.
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                    )
                else:
                    kwargs["start_new_session"] = True
                process = subprocess.Popen(argv, **kwargs)
            meta["pid"] = process.pid
            meta["pid_created"] = process_creation_time(process.pid)
            write_json(run_dir / "meta.json", meta)
            if fields["worker"] in GO_PLAN_WORKERS:
                launched_go += 1
            append_log(root, f"dispatch {run_id} project={fields['project']} worker={fields['worker']} mode={fields['mode']} pid={process.pid}")
            print(f"Dispatched {run_id} pid={process.pid} mode={fields['mode']}")
        except (RhError, OSError, subprocess.SubprocessError) as exc:
            failures += 1
            print(f"ERROR {run_id}: {exc}", file=sys.stderr)
            cleanup_safe = True
            if "worktree" in created_resources:
                try:
                    unlink_project_dirs(worktree, project.get("link_dirs", []))
                    unlink_reparse_points(worktree)
                except RhError as unlink_exc:
                    print(f"ERROR {run_id}: {unlink_exc}; worktree left in place", file=sys.stderr)
                    cleanup_safe = False
                if cleanup_safe:
                    run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)
                if cleanup_safe and branch:
                    run_git(repo, "branch", "-D", branch, check=False)
            if "brief" in created_resources:
                run_brief.replace(brief_path)
            if "run_dir" in created_resources and cleanup_safe and run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
    if not planned:
        print(f"Queue {queue_name}: no briefs")
    return 1 if failures else 0


GO_PLAN_WORKERS = {"deepseek", "mimo"}
GO_PLAN_MAX_RUNNING = 3   # OpenCode Go plan answers "Model unavailable" with ~5 runs in flight (2026-10-15)
GO_PLAN_STAGGER_SECONDS = 20


def unlink_project_dirs(worktree: Path, entries: list[str]) -> None:
    """Remove linked dirs (junctions/symlinks) so a forced worktree removal can never recurse into the real repo's copy."""
    for entry in entries:
        link = worktree / entry
        try:
            if link.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(link)):
                os.unlink(link) if link.is_symlink() and not link.is_dir() else os.rmdir(link)
        except OSError as exc:
            raise RhError(f"Cannot unlink linked dir {link}: {exc}") from exc


def is_reparse_point(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def unlink_reparse_points(worktree: Path) -> None:
    """Unlink every symlink or junction under a worktree before Git removes it."""
    if is_reparse_point(worktree):
        raise RhError(f"Refusing worktree removal: worktree root is a reparse point: {worktree}")

    def walk_error(exc: OSError) -> None:
        raise RhError(f"Cannot inspect worktree for reparse points at {exc.filename or worktree}: {exc}") from exc

    for directory, dirnames, filenames in os.walk(worktree, topdown=True, followlinks=False, onerror=walk_error):
        for name in list(dirnames):
            entry = Path(directory) / name
            if not is_reparse_point(entry):
                continue
            try:
                if os.path.isjunction(entry) or entry.is_dir():
                    os.rmdir(entry)
                else:
                    os.unlink(entry)
            except OSError as exc:
                raise RhError(f"Cannot unlink reparse point {entry}: {exc}") from exc
            dirnames.remove(name)
        for name in filenames:
            entry = Path(directory) / name
            if not is_reparse_point(entry):
                continue
            try:
                if os.path.isjunction(entry) or entry.is_dir():
                    os.rmdir(entry)
                else:
                    os.unlink(entry)
            except OSError as exc:
                raise RhError(f"Cannot unlink reparse point {entry}: {exc}") from exc


def bash_executable() -> str | None:
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and Path(candidate).exists() and "system32" not in candidate.lower():
            return candidate
    return None


def running_go_plan_runs(root: Path) -> int:
    count = 0
    for meta_path in (root / "runs").glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("worker") in GO_PLAN_WORKERS and run_state(meta_path.parent, meta) == "running":
            count += 1
    return count


# Verified 2026-10-16 against opencode-cli 2.0.28: a PROJECT opencode.json is honoured (OPENCODE_CONFIG and
# OPENCODE_CONFIG_CONTENT env vars are ignored), external_directory=deny blocks file tools, and only a
# deny-by-default bash allowlist stops shell escapes like `echo x > C:/elsewhere` or `cd .. && ...`.
# Workers edit with file tools; the gate commits and runs checks, so they need no shell writes.
OPENCODE_CONTAINMENT = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
        "external_directory": "deny",
        "edit": "allow",
        "bash": {
            "*": "deny",
            "git status*": "allow", "git log*": "allow", "git diff*": "allow", "git show*": "allow",
            "git ls-files*": "allow", "git grep*": "allow", "ls*": "allow", "pwd": "allow",
        },
    },
}


def install_opencode_containment(worktree: Path) -> None:
    (worktree / "opencode.json").write_text(json.dumps(OPENCODE_CONTAINMENT, indent=2) + "\n", encoding="utf-8")
    exclude = run_git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    exclude_path = Path(exclude) if Path(exclude).is_absolute() else worktree / exclude
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if "/opencode.json" not in existing.splitlines():
        with exclude_path.open("a", encoding="utf-8") as stream:
            stream.write(("" if existing.endswith("\n") or not existing else "\n") + "/opencode.json\n")


def link_project_dirs(repo: Path, worktree: Path, entries: list[str]) -> None:
    if os.name != "nt":
        return
    for entry in entries:
        source = repo / entry
        if not source.is_dir():
            continue
        destination = worktree / entry
        if destination.exists():
            if destination.is_dir() and not any(destination.iterdir()):
                destination.rmdir()
            else:
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode:
            raise RhError(f"Cannot create junction {destination}: {(result.stderr or result.stdout).strip()}")


def read_run(root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise RhError("Invalid run id")
    run_dir = root / "runs" / run_id
    try:
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RhError(f"Unknown run: {run_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RhError(f"Cannot read metadata for {run_id}: {exc}") from exc
    return run_dir, meta


def exit_marker(out_path: Path) -> int | None:
    try:
        lines = out_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    last = next((line.strip() for line in reversed(lines) if line.strip()), "")
    match = re.fullmatch(r"EXIT=(-?\d+)", last)
    return int(match.group(1)) if match else None


def process_creation_time(pid_value: Any) -> str | None:
    try:
        pid = int(pid_value)
        if pid <= 0:
            return None
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            stat = stat_path.read_text(encoding="ascii")
            command_end = stat.rfind(")")
            fields = stat[command_end + 1 :].split()
            return fields[19] if len(fields) > 19 else None
        except OSError:
            return None
    return None


def process_identity_state(pid_value: Any, created: Any = None) -> bool | None:
    """Return true for the recorded process, false if absent/reused, or None if identity is unknown."""
    try:
        pid = int(pid_value)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            return None if error == 5 else False
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        created_time = wintypes.FILETIME()
        exited_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != 259:  # STILL_ACTIVE
                return False
            if created is None:
                return True
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created_time),
                ctypes.byref(exited_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            actual = str((created_time.dwHighDateTime << 32) | created_time.dwLowDateTime)
            return actual == str(created)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        if created is None:
            return None
    except OSError:
        return False
    if created is None:
        return True
    actual = process_creation_time(pid)
    if actual is None:
        return None
    return actual == str(created)


def pid_alive(pid: Any, created: Any = None) -> bool:
    return process_identity_state(pid, created) is True


def run_state(run_dir: Path, meta: dict[str, Any]) -> str:
    # worker.py prints RUNG=/EXIT= on stdout (worker.log); out.txt holds the model's answer.
    code = exit_marker(run_dir / "worker.log")
    if code is None:
        code = exit_marker(run_dir / "out.txt")
    if code is not None:
        return "finished" if code == 0 else "failed"
    deadline = parse_datetime(meta.get("deadline"))
    if deadline is None:
        started = parse_datetime(meta.get("started"))
        try:
            timeout = int(meta.get("timeout"))
        except (TypeError, ValueError):
            timeout = 0
        if started is not None and timeout > 0:
            deadline = started + timedelta(seconds=timeout + 900)
    if deadline is not None and datetime.now(timezone.utc) > deadline:
        return "timeout"
    return "running" if pid_alive(meta.get("pid"), meta.get("pid_created")) else "stale"


def terminate_worker_tree(pid_value: Any, created: Any) -> None:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError) as exc:
        raise RhError("Cannot force-discard a running run without a valid worker pid") from exc
    if pid <= 0:
        raise RhError("Cannot force-discard a running run without a valid worker pid")
    if created is None:
        raise RhError(f"Cannot safely terminate worker process {pid} without its recorded creation time")
    identity = process_identity_state(pid, created)
    if identity is False:
        return
    if identity is None:
        raise RhError(f"Cannot confirm worker process identity for {pid}; refusing cleanup")
    if os.name == "nt":
        killed = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if killed.returncode:
            detail = (killed.stderr or killed.stdout).strip()
            raise RhError(f"Cannot terminate worker process tree {pid}: {detail or killed.returncode}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            identity = process_identity_state(pid, created)
            if identity is False:
                return
            time.sleep(0.2)
        raise RhError(f"Cannot confirm worker process {pid} exited after taskkill")

    try:
        if os.getpgid(pid) != pid:
            raise RhError(f"Worker process {pid} is not the leader of its process group")
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise RhError(f"Cannot terminate worker process group {pid}: {exc}") from exc
    deadline = time.monotonic() + 30
    kill_sent = False
    while time.monotonic() < deadline:
        identity = process_identity_state(pid, created)
        if identity is False:
            return
        if identity is True and not kill_sent and time.monotonic() >= deadline - 5:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError as exc:
                raise RhError(f"Cannot kill worker process group {pid}: {exc}") from exc
            kill_sent = True
        time.sleep(0.2)
    raise RhError(f"Cannot confirm worker process {pid} exited after 30 seconds")


def queue_counts(root: Path) -> dict[str, int]:
    queue_root = root / "queue"
    if not queue_root.is_dir():
        return {}
    return {
        child.name: sum(1 for path in child.glob("*.md") if path.is_file())
        for child in sorted(queue_root.iterdir(), key=lambda p: p.name.casefold())
        if child.is_dir()
    }


def collect(root: Path, config: dict[str, Any]) -> int:
    collected = utc_now()
    previous = None
    previous_path = root / "state" / "last_collect.json"
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8")).get("collected_at")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    since = str(previous or (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z"))
    project_rows: list[dict[str, Any]] = []
    for name, project in sorted(config["projects"].items()):
        repo = project["repo_path"]
        if not repo.is_dir():
            project_rows.append({"project": name, "missing": str(repo)})
            continue
        try:
            branch = run_git(repo, "branch", "--show-current").stdout.strip() or "(detached)"
            dirty = len([line for line in run_git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines() if line.strip()])
            commits_result = run_git(repo, "log", f"--since={since}", "--format=%H%x09%s", str(project["default_branch"]), check=False)
            commits = commits_result.stdout.splitlines() if commits_result.returncode == 0 else []
            backlog = backlog_now(root / "backlogs" / f"{name}.md") or backlog_now(repo / "BACKLOG.md")
            default_branch = str(project["default_branch"])
            sync = run_git(repo, "rev-list", "--left-right", "--count", f"{default_branch}...origin/{default_branch}", check=False)
            ahead_behind = sync.stdout.split() if sync.returncode == 0 else ["?", "?"]
            unexpected_local_commits: list[str] = []
            if sync.returncode == 0 and ahead_behind and ahead_behind[0].isdecimal() and int(ahead_behind[0]) > 0:
                ahead = run_git(
                    repo,
                    "rev-list",
                    f"refs/heads/{default_branch}",
                    f"^refs/remotes/origin/{default_branch}",
                    check=False,
                )
                if ahead.returncode == 0:
                    known = recorded_rh_merges(root, name)
                    unexpected_local_commits = [sha for sha in ahead.stdout.splitlines() if sha and sha not in known]
            worktrees_text = run_git(repo, "worktree", "list", "--porcelain").stdout
            worktree_count = sum(1 for line in worktrees_text.splitlines() if line.startswith("worktree "))
            project_rows.append({
                "project": name,
                "branch": branch,
                "dirty_files": dirty,
                "commits_since_last_collect": commits,
                "origin_ahead_behind": ahead_behind,
                "backlog_now": backlog,
                "worktrees": worktree_count,
                "unexpected_local_commits": unexpected_local_commits,
            })
        except RhError as exc:
            project_rows.append({"project": name, "error": str(exc)})

    run_rows: list[dict[str, Any]] = []
    runs_dir = root / "runs"
    if runs_dir.is_dir():
        for meta_path in sorted(runs_dir.glob("*/meta.json"), key=lambda p: p.parent.name):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                state = run_state(meta_path.parent, meta)
                run_rows.append({
                    "run_id": meta.get("run_id", meta_path.parent.name),
                    "project": meta.get("project"),
                    "status": state,
                    "gate": gate_state(meta_path.parent, meta),
                    "merge": meta.get("status", "not-merged"),
                    "pid": meta.get("pid"),
                })
            except (OSError, json.JSONDecodeError):
                run_rows.append({"run_id": meta_path.parent.name, "status": "invalid-meta"})
    queues = queue_counts(root)
    digest = {"collected_at": collected, "since": since, "projects": project_rows, "runs": run_rows, "queues": queues}
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_dir / "digest.json", digest)
    write_json(previous_path, {"collected_at": collected})

    lines = ["# Right hand digest", "", f"Collected: {collected}", f"Since last collect: {since}", "", "## Projects"]
    for row in project_rows:
        name = row["project"]
        if "missing" in row:
            lines.append(f"- {name}: MISSING repo {row['missing']}")
            continue
        if "error" in row:
            lines.append(f"- {name}: ERROR {row['error']}")
            continue
        lines.append(
            f"- {name}: branch={row['branch']}; dirty={row['dirty_files']}; "
            f"commits since collect={len(row['commits_since_last_collect'])}; worktrees={row['worktrees']}; "
            f"vs origin: ahead {row.get('origin_ahead_behind', ['?', '?'])[0]} / behind {row.get('origin_ahead_behind', ['?', '?'])[-1]}"
        )
        if row.get("unexpected_local_commits"):
            lines.append("  - UNEXPECTED LOCAL COMMITS: " + ", ".join(row["unexpected_local_commits"]))
        lines.extend(f"  - [ ] {item}" for item in row["backlog_now"])
        shown = row["commits_since_last_collect"][:5]
        lines.extend(f"  - commit {commit.split(chr(9), 1)[-1][:100]}" for commit in shown)
        if len(row["commits_since_last_collect"]) > len(shown):
            lines.append(f"  - (+{len(row['commits_since_last_collect']) - len(shown)} more)")
    lines.extend(["", "## Runs"])
    if run_rows:
        for row in run_rows:
            lines.append(f"- {row['run_id']}: {row['status']}; gate={row['gate']}; merge={row['merge']}")
    else:
        lines.append("- none")
    lines.extend(["", "## Queues"])
    if queues:
        lines.extend(f"- {name}: {count}" for name, count in queues.items())
    else:
        lines.append("- none")
    if len(lines) > 150:
        lines = lines[:149] + ["... (digest truncated to 150 lines; see digest.json)"]
    (state_dir / "digest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Collected {collected}: {len(project_rows)} projects, {len(run_rows)} runs, {len(queues)} queues")
    for row in project_rows:
        if row.get("unexpected_local_commits"):
            print(f"UNEXPECTED LOCAL COMMITS {row['project']}: " + ", ".join(row["unexpected_local_commits"]))
    return 0


def backlog_now(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    in_now = False
    items: list[str] = []
    for line in lines:
        if re.match(r"^##\s+", line):
            in_now = line.strip().lower().startswith("## now")
            if not in_now and items:
                break
            continue
        if in_now and re.match(r"^\s*-\s+\[ \]", line):
            items.append(re.sub(r"^\s*-\s+\[ \]\s*", "", line).strip())
            if len(items) == 5:
                break
    return items


def gate_state(run_dir: Path, meta: dict[str, Any]) -> str:
    try:
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        if not gate.get("pass"):
            return "fail"
        return "needs-review" if gate.get("needs_review") else "pass"
    except (OSError, json.JSONDecodeError):
        value = meta.get("gate")
        if isinstance(value, dict):
            if not value.get("pass"):
                return "fail"
            return "needs-review" if value.get("needs_review") else "pass"
        return "pending"


def git_status(repo: Path) -> list[str]:
    return [line for line in run_git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines() if line.strip()]


def comparable_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path))).rstrip("\\/")


def worktree_branch_problem(repo: Path, worktree: Path, branch: str, run_id: str) -> str | None:
    expected = f"rh/{run_id}"
    if branch != expected:
        return f"worktree branch metadata mismatch: expected {expected}, found {branch or '(missing)'}"
    symbolic = run_git(worktree, "symbolic-ref", "--short", "HEAD", check=False)
    current_branch = symbolic.stdout.strip()
    if symbolic.returncode or current_branch != branch:
        return f"worktree branch mismatch: expected {branch}, found {current_branch or '(detached)'}"
    listed = run_git(repo, "worktree", "list", "--porcelain", check=False)
    if listed.returncode:
        return "cannot verify registered worktree and branch attachment"
    for block in re.split(r"\r?\n\r?\n", listed.stdout.strip()):
        lines = block.splitlines()
        registered_path = next((line[len("worktree ") :] for line in lines if line.startswith("worktree ")), None)
        registered_branch = next((line[len("branch ") :] for line in lines if line.startswith("branch ")), None)
        if registered_path and comparable_path(registered_path) == comparable_path(worktree) and registered_branch == f"refs/heads/{branch}":
            return None
    return f"worktree {worktree} is not registered and attached to refs/heads/{branch}"


def recorded_rh_merges(root: Path, project_name: str) -> set[str]:
    known: set[str] = set()
    for meta_path in (root / "runs").glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        commit = meta.get("merge_commit")
        if meta.get("project") == project_name and isinstance(commit, str) and commit:
            known.add(commit)
    return known


def default_commits_outside_rh(root: Path, project_name: str, repo: Path, default_branch: str,
                               default_at_dispatch: Any) -> tuple[str, list[str]]:
    current = run_git(repo, "rev-parse", f"refs/heads/{default_branch}", check=False)
    if current.returncode:
        return "", []
    current_sha = current.stdout.strip()
    if not isinstance(default_at_dispatch, str) or not default_at_dispatch:
        return current_sha, [current_sha]
    if current_sha == default_at_dispatch:
        return current_sha, []
    ancestry = run_git(repo, "merge-base", "--is-ancestor", str(default_at_dispatch), current_sha, check=False)
    if ancestry.returncode:
        return current_sha, [current_sha]
    commits = run_git(repo, "rev-list", "--reverse", f"{default_at_dispatch}..{current_sha}", check=False)
    if commits.returncode:
        return current_sha, [current_sha]
    known = recorded_rh_merges(root, project_name)
    return current_sha, [sha for sha in commits.stdout.splitlines() if sha and sha not in known]


def test_path(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    if any(part in {"test", "tests", "__tests__", "spec"} for part in parts[:-1]):
        return True
    name = parts[-1]
    return any(marker in name for marker in ("_test.", ".test.", ".spec.")) or name.startswith("test_")


def new_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").lower().split("/")
    name = parts[-1]
    suffix = Path(name).suffix
    source_suffixes = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs",
        ".java", ".kt", ".rb", ".php", ".cs", ".sh", ".bash", ".feature",
    }
    if suffix not in source_suffixes:
        return False
    return (
        name.startswith("test_")
        or any(marker in name for marker in ("_test.", ".test.", ".spec."))
        or "__tests__" in parts[:-1]
    )


def changed_paths(repo: Path, default_branch: str) -> list[tuple[str, list[str]]]:
    text = run_git(repo, "diff", "--name-status", "-z", "-M", f"{default_branch}...HEAD").stdout
    results: list[tuple[str, list[str]]] = []
    fields = text.split("\0")
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        path_count = 2 if status.startswith(("R", "C")) else 1
        paths = [path for path in fields[index : index + path_count] if path]
        index += path_count
        results.append((status, paths))
    return results


def changed_numstat(repo: Path, base_sha: str, head_sha: str) -> list[tuple[str, str, str]]:
    text = run_git(repo, "diff", "--numstat", "-z", f"{base_sha}...{head_sha}").stdout
    fields = text.split("\0")
    rows: list[tuple[str, str, str]] = []
    index = 0
    while index < len(fields):
        item = fields[index]
        index += 1
        if not item:
            continue
        columns = item.split("\t", 2)
        if len(columns) != 3:
            continue
        added, deleted, path = columns
        if path:
            rows.append((added, deleted, path))
            continue
        # With -z, Git emits an empty pathname followed by old and new names for renames.
        if index + 1 < len(fields):
            old_path, new_path = fields[index], fields[index + 1]
            index += 2
            rows.append((added, deleted, new_path or old_path))
    return rows


def is_verification_config(path: str) -> bool:
    value = path.replace("\\", "/").lower()
    name = value.rsplit("/", 1)[-1]
    exact = {
        "package.json", "package-lock.json", "bun.lockb", "pytest.ini", "pyproject.toml",
        "setup.cfg", "tox.ini", "cargo.toml", "makefile", "cmakelists.txt", "cmakepresets.json",
    }
    if name in exact or name.startswith("jest.config.") or name.startswith("vitest.config."):
        return True
    if re.fullmatch(r".*\.config\.(?:js|ts|mjs|cjs)", name):
        return True
    if name.startswith("tsconfig") and name.endswith(".json"):
        return True
    if value.startswith(("scripts/", "test/")):
        return True
    if name.startswith(("jest.setup", "vitest.setup")) or name in {"conftest.py", ".coveragerc"}:
        return True
    if any(term in name for term in ("verify", "ratchet", "coverage", "lint")):
        return True
    # Nested toolchain/runner config can turn `cargo test` etc. into a no-op (Sol 2026-10-16).
    if "/.cargo/" in "/" + value or name in {"build.rs", "rust-toolchain", "rust-toolchain.toml", ".npmrc", ".bunfig.toml", "bunfig.toml", "deno.json"}:
        return True
    return value.startswith(".github/")


def added_patch_lines(diff: str) -> list[str]:
    """Return only added lines inside hunks, including content that starts with +++."""
    in_hunk = False
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk or not line:
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return added


# Test code can live in ordinary source files (Rust #[test] / #[cfg(test)] modules), so removed test markers
# anywhere in the diff require review (Sol 2026-10-16).
TEST_CODE_MARKER = re.compile(
    r"#\[(?:tokio::)?test\b|#\[cfg\(test\)\]|^\s*(?:async\s+)?def\s+test_|\b(?:it|test|describe)\s*\(\s*['\"`]|\bassert(?:_eq|_ne)?!?\s*\(|\bexpect\s*\("
)


def removed_patch_lines(diff: str) -> list[str]:
    in_hunk = False
    removed: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line.startswith("-"):
            removed.append(line[1:])
    return removed


def decode_blob_for_scan(blob: bytes) -> str:
    if b"\x00" not in blob:
        try:
            return blob.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
    if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
        return blob.decode("utf-16", errors="strict")
    raise UnicodeError("content is neither strict UTF-8 nor BOM-marked UTF-16")


def inspect_blob(repo: Path, path: str, reasons: list[str], needs_review: list[str]) -> None:
    object_name = f"HEAD:{path}"
    size = run_git(repo, "cat-file", "-s", object_name, check=False)
    if size.returncode:
        reasons.append(f"cannot inspect committed file: {path}")
        return
    try:
        byte_count = int(size.stdout.strip())
    except ValueError:
        reasons.append(f"cannot determine committed file size: {path}")
        return
    if byte_count > MAX_SCANNED_BLOB_BYTES:
        needs_review.append(f"file exceeds 2 MB scan limit: {path}")
        return
    blob = subprocess.run(
        [GIT, "-C", str(repo), "show", object_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if blob.returncode:
        reasons.append(f"cannot inspect committed file: {path}")
        return
    try:
        content = decode_blob_for_scan(blob.stdout)
    except UnicodeError:
        needs_review.append(f"undecodable content: {path}")
        return
    # Only content that is new can leak: lines already on the base (HEAD~1 after the squash) are public already.
    # Without this, a pre-existing fixture like token: 'device-token-1' fails every run that touches the file.
    base = subprocess.run([GIT, "-C", str(repo), "show", f"HEAD~1:{path}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    try:
        base_lines = set(decode_blob_for_scan(base.stdout).splitlines()) if base.returncode == 0 else set()
    except UnicodeError:
        base_lines = set()
    for line in content.splitlines():
        if line in base_lines:
            continue
        if any(pattern.search(line) for pattern in SECRET_LINE_PATTERNS):
            reasons.append(f"secret pattern in committed file: {path}")
            break


def output_tail(output: str | bytes | None, limit: int = 40) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return "\n".join(output.splitlines()[-limit:])


def run_checks(repo: Path, project: dict[str, Any], override: str | None) -> list[dict[str, Any]]:
    checks = [{"cmd": override, "cwd": "."}] if override else project.get("verify", [])
    results: list[dict[str, Any]] = []
    for item in checks:
        if isinstance(item, str):
            item = {"cmd": item, "cwd": "."}
        command = str(item.get("cmd", "")).strip()
        if not command:
            continue
        cwd_value = Path(str(item.get("cwd", ".")))
        cwd = cwd_value if cwd_value.is_absolute() else repo / cwd_value
        # Output goes to a temp FILE, not a pipe: a grandchild that inherits a pipe keeps communicate() blocked
        # after a timeout (Sol 2026-10-16). On timeout the whole process tree is killed.
        try:
            bash = bash_executable()
            with tempfile.TemporaryFile() as sink:
                proc = subprocess.Popen(
                    [bash, "-c", command] if bash else command,
                    cwd=cwd,
                    shell=not bash,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                try:
                    code = proc.wait(timeout=900)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                    else:
                        proc.kill()
                    try:
                        proc.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        pass
                    code = 124
                sink.seek(0)
                tail = output_tail(sink.read()[-200_000:])
                if timed_out:
                    tail = (tail + "\n" if tail else "") + "Check timed out after 900 seconds; process tree killed"
        except OSError as exc:
            code, tail = 127, str(exc)
        results.append({"cmd": command, "exit": code, "tail": tail})
    return results


def gate(root: Path, config: dict[str, Any], run_id: str, quiet: bool = False, run_checks_after_review: bool = False) -> dict[str, Any]:
    run_dir, meta = read_run(root, run_id)
    if meta.get("mode") != "write":
        raise RhError(f"Run {run_id} is not a write-mode run")
    meta.pop("reviewed", None)
    meta["reviewed_sha"] = None
    write_json(run_dir / "meta.json", meta)
    if run_state(run_dir, meta) != "finished":
        raise RhError(f"Run {run_id} is not finished successfully (state={run_state(run_dir, meta)})")
    project = get_project(config, str(meta.get("project", "")))
    repo = ensure_repo(project, str(meta.get("project", "")))
    worktree = Path(str(meta.get("worktree", "")))
    if not worktree.is_dir():
        raise RhError(f"Run worktree is missing: {worktree}")
    reasons: list[str] = []
    needs_review: list[str] = []

    def record_early_failure(reason: str, base_sha: str = "") -> dict[str, Any]:
        head_result = run_git(worktree, "rev-parse", "HEAD", check=False)
        head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
        result = {
            "pass": False,
            "reasons": [reason],
            "needs_review": [],
            "diffstat": "",
            "ahead": 0,
            "base_commit": base_sha,
            "head_sha": head_sha,
            "origin_commit": None,
            "check": [],
            "gated_at": utc_now(),
        }
        write_json(run_dir / "gate.json", result)
        meta["gate"] = result
        meta["reviewed_sha"] = None
        write_json(run_dir / "meta.json", meta)
        if not quiet:
            print(f"GATE FAIL {run_id}: {reason}")
        return result

    branch_problem = worktree_branch_problem(repo, worktree, str(meta.get("branch", "")), run_id)
    if branch_problem:
        return record_early_failure(branch_problem)

    default_branch = str(project["default_branch"])
    current_default, escaped_commits = default_commits_outside_rh(
        root,
        str(meta.get("project", "")),
        repo,
        default_branch,
        meta.get("default_at_dispatch"),
    )
    if not current_default:
        return record_early_failure(f"default branch {default_branch} is missing")
    if escaped_commits:
        detail = ", ".join(escaped_commits)
        return record_early_failure(
            f"default branch changed outside rh during this run (possible worker escape): {detail}",
            current_default,
        )

    dirty = git_status(worktree)
    if dirty:
        add = run_git(worktree, "add", "-A", check=False)
        if add.returncode:
            reasons.append(f"git add failed: {(add.stderr or add.stdout).strip()}")
        else:
            commit = run_git(worktree, "commit", "-m", f"rh({meta['project']}): {meta['title']} [{run_id}]", check=False)
            if commit.returncode:
                reasons.append(f"commit failed: {(commit.stderr or commit.stdout).strip()}")

    base_before = run_git(repo, "rev-parse", f"refs/heads/{default_branch}", check=False)
    if base_before.returncode:
        reasons.append(f"default branch {default_branch} is missing")
        base_sha = ""
    else:
        base_sha = base_before.stdout.strip()

    ahead_before = 0
    if base_sha:
        count_result = run_git(worktree, "rev-list", "--count", f"{base_sha}..HEAD", check=False)
        if count_result.returncode:
            reasons.append(f"cannot count commits ahead of {default_branch}")
        else:
            ahead_before = int(count_result.stdout.strip() or "0")
            if ahead_before == 0:
                reasons.append(f"no commits ahead of {default_branch}")
        rebased = run_git(worktree, "rebase", base_sha, check=False)
        if rebased.returncode:
            detail = (rebased.stderr or rebased.stdout).strip()
            run_git(worktree, "rebase", "--abort", check=False)
            reasons.append(f"rebase conflict onto {default_branch}: {detail[-500:]}")
        else:
            reset = run_git(worktree, "reset", "--soft", base_sha, check=False)
            if reset.returncode:
                reasons.append(f"cannot prepare squashed commit: {(reset.stderr or reset.stdout).strip()}")
            else:
                staged = run_git(worktree, "diff", "--cached", "--quiet", check=False)
                if staged.returncode == 0:
                    reasons.append("no changes to publish after squash")
                elif staged.returncode != 1:
                    reasons.append(f"cannot inspect staged squash: {(staged.stderr or staged.stdout).strip()}")
                else:
                    commit = run_git(
                        worktree,
                        "commit",
                        "-m",
                        f"rh({meta['project']}): {meta['title']} [{run_id}]",
                        check=False,
                    )
                    if commit.returncode:
                        reasons.append(f"squash commit failed: {(commit.stderr or commit.stdout).strip()}")

    base_after = run_git(repo, "rev-parse", f"refs/heads/{default_branch}", check=False)
    if base_after.returncode == 0 and base_sha and base_after.stdout.strip() != base_sha:
        reasons.append(f"default branch moved during gate: {default_branch}")
    head_result = run_git(worktree, "rev-parse", "HEAD", check=False)
    head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if not head_sha:
        reasons.append("cannot record worktree HEAD after squash")
    ahead_result = run_git(worktree, "rev-list", "--count", f"{base_sha}..{head_sha}", check=False) if base_sha and head_sha else None
    ahead = int(ahead_result.stdout.strip() or "0") if ahead_result and ahead_result.returncode == 0 else 0
    if base_sha and head_sha and head_sha == base_sha:
        if "no changes to publish after squash" not in reasons:
            reasons.append("no changes to publish after squash")

    try:
        paths = changed_paths(worktree, base_sha) if base_sha and head_sha else []
    except RhError as exc:
        paths = []
        reasons.append(f"cannot inspect diff: {exc}")
    scope = meta.get("scope")
    for status, status_paths in paths:
        if scope:
            for changed in status_paths:
                if not any(fnmatch.fnmatchcase(changed, pattern) for pattern in scope):
                    reasons.append(f"out-of-scope file: {changed}")
        for changed in status_paths:
            new_test_addition = status.startswith("A") and new_test_file(changed)
            if test_path(changed) and not new_test_addition:
                needs_review.append(f"existing test file changed: {changed}")
            if is_verification_config(changed) and not new_test_addition:
                needs_review.append(f"verification config changed: {changed}")

    numstats: dict[str, tuple[str, str]] = {}
    if base_sha and head_sha:
        try:
            for added, deleted, path in changed_numstat(worktree, base_sha, head_sha):
                numstats[path] = (added, deleted)
                if added == "-" or deleted == "-":
                    needs_review.append(f"binary file changed: {path}")
        except RhError as exc:
            reasons.append(f"cannot inspect file change sizes: {exc}")

    for status, status_paths in paths:
        if status.startswith(("M", "R", "T", "D")):
            test_names = [changed for changed in status_paths if test_path(changed)]
            if test_names:
                added, deleted = numstats.get(status_paths[-1], ("0", "0"))
                if added.isdecimal() and deleted.isdecimal() and int(deleted) > int(added):
                    needs_review.append(f"existing test file shrunk: {test_names[0]}")

    added_files: set[str] = set()
    for status, status_paths in paths:
        if status.startswith(("A", "M", "R", "T")) and status_paths:
            added_files.add(status_paths[-1])
    for path in sorted(added_files):
        basename = path.rsplit("/", 1)[-1].lower()
        if basename.startswith(".env") or basename.endswith(".pem") or ("token" in basename and basename.endswith(".json")):
            reasons.append(f"sensitive filename: {path}")
        if any(pattern.search(path) for pattern in SECRET_LINE_PATTERNS):
            reasons.append(f"secret pattern in added filename: {path}")
        inspect_blob(worktree, path, reasons, needs_review)

    diff = run_git(
        worktree,
        "diff",
        "--text",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        f"{base_sha}...{head_sha}",
        check=False,
    ) if base_sha and head_sha else subprocess.CompletedProcess([], 1, "", "missing base or HEAD")
    if diff.returncode:
        reasons.append(f"cannot inspect added lines: {(diff.stderr or diff.stdout).strip()}")
    else:
        removed_test_code = [line for line in removed_patch_lines(diff.stdout) if TEST_CODE_MARKER.search(line)]
        if removed_test_code:
            needs_review.append(f"test code removed ({len(removed_test_code)} line(s), e.g. {removed_test_code[0].strip()[:60]!r})")
        for added in added_patch_lines(diff.stdout):
            if any(marker in added for marker in SKIP_MARKERS):
                marker = next(marker for marker in SKIP_MARKERS if marker in added)
                reasons.append(f"skip marker added: {marker}")
            if any(pattern.search(added) for pattern in SECRET_LINE_PATTERNS):
                reasons.append("secret pattern in added line")

    diffstat = run_git(worktree, "diff", "--stat", f"{base_sha}...{head_sha}", check=False) if base_sha and head_sha else subprocess.CompletedProcess([], 1, "", "missing base or HEAD")

    origin_commit: str | None = None
    if project.get("push"):
        origin = run_git(repo, "rev-parse", f"refs/remotes/origin/{default_branch}", check=False)
        if origin.returncode == 0:
            origin_commit = origin.stdout.strip()

    # Checks execute worker-controlled code outside any sandbox. When the diff touches the verification harness
    # (needs_review), defer them until a reviewer has read the diff and re-runs `gate <id> --run-checks`.
    if needs_review and not run_checks_after_review:
        checks = [{"cmd": "(deferred)", "exit": -1, "tail": "checks deferred until the diff is reviewed: rh.py gate <id> --run-checks"}]
        reasons.append("checks deferred until review (needs_review)")
    else:
        checks = run_checks(worktree, project, meta.get("check"))
    for item in checks:
        if item["cmd"] == "(deferred)":
            continue
        if item["exit"] != 0:
            reasons.append(f"check failed ({item['exit']}): {item['cmd']}")
    after_check_head = run_git(worktree, "rev-parse", "HEAD", check=False)
    after_check_status = git_status(worktree)
    if after_check_head.returncode or after_check_head.stdout.strip() != head_sha or after_check_status:
        reasons.append("check modified the worktree")

    reasons = list(dict.fromkeys(reasons))
    needs_review = list(dict.fromkeys(needs_review))
    result = {
        "pass": not reasons,
        "reasons": reasons,
        "needs_review": needs_review,
        "diffstat": diffstat.stdout.strip() if diffstat.returncode == 0 else "",
        "ahead": ahead,
        "base_commit": base_sha,
        "head_sha": head_sha,
        "origin_commit": origin_commit,
        "check": checks,
        "gated_at": utc_now(),
    }
    write_json(run_dir / "gate.json", result)
    meta["gate"] = result
    write_json(run_dir / "meta.json", meta)
    if not quiet:
        if result["pass"]:
            suffix = " needs-review" if needs_review else ""
            print(f"GATE PASS{suffix} {run_id}: ahead={ahead}")
        else:
            print(f"GATE FAIL {run_id}: " + "; ".join(reasons))
    return result


def fast_forward_default(repo: Path, default_branch: str, commit: str) -> None:
    ref = f"refs/heads/{default_branch}"
    current = run_git(repo, "rev-parse", ref, check=False)
    if current.returncode:
        raise RhError(f"Default branch {default_branch} is missing")
    old_sha = current.stdout.strip()
    if old_sha == commit:
        return
    target_is_ancestor = run_git(repo, "merge-base", "--is-ancestor", commit, old_sha, check=False)
    if target_is_ancestor.returncode == 0:
        return
    old_is_ancestor = run_git(repo, "merge-base", "--is-ancestor", old_sha, commit, check=False)
    if old_is_ancestor.returncode != 0:
        raise RhError(f"Cannot fast-forward {default_branch} to gated commit {commit}: branches diverged")

    checked_out = run_git(repo, "branch", "--show-current").stdout.strip() == default_branch
    if checked_out:
        dirty = git_status(repo)
        if dirty:
            raise RhError(f"Refusing merge: {default_branch} is checked out and dirty ({len(dirty)} changed paths)")
        result = run_git(repo, "merge", "--ff-only", commit, check=False)
        if result.returncode:
            raise RhError(f"Fast-forward merge failed: {(result.stderr or result.stdout).strip()}")
        return

    result = run_git(repo, "fetch", ".", f"{commit}:{ref}", check=False)
    if result.returncode == 0:
        return
    updated = run_git(repo, "update-ref", ref, commit, old_sha, check=False)
    if updated.returncode:
        detail = (updated.stderr or updated.stdout or result.stderr or result.stdout).strip()
        raise RhError(f"Fast-forward merge failed: {detail}")


def merge(root: Path, config: dict[str, Any], run_id: str, no_push: bool, reviewed: bool = False) -> int:
    run_dir, meta = read_run(root, run_id)
    meta.pop("reviewed", None)
    gate_path = run_dir / "gate.json"
    try:
        gate_result = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RhError(f"Run {run_id} needs a passing gate first: {exc}") from exc
    if not gate_result.get("pass"):
        raise RhError(f"Run {run_id} gate did not pass")
    gated_sha = str(gate_result.get("head_sha", ""))
    if not gated_sha:
        raise RhError(f"Run {run_id} gate has no recorded head SHA")
    if reviewed:
        meta["reviewed_sha"] = gated_sha
    approved = meta.get("reviewed_sha") == gated_sha
    if gate_result.get("needs_review") and not approved:
        raise RhError(f"Run {run_id} needs explicit review; re-run merge with --reviewed")
    if reviewed:
        write_json(run_dir / "meta.json", meta)

    project = get_project(config, str(meta.get("project", "")))
    repo = ensure_repo(project, str(meta.get("project", "")))
    expected_branch = f"rh/{run_id}"
    branch = str(meta.get("branch", ""))
    if branch != expected_branch:
        raise RhError(f"Run {run_id} has an unexpected branch name")
    branch_tip = run_git(repo, "rev-parse", f"refs/heads/{expected_branch}", check=False)
    if branch_tip.returncode or branch_tip.stdout.strip() != gated_sha:
        raise RhError(f"Run {run_id} branch changed since gate")

    default_branch = str(project["default_branch"])
    if meta.get("status") != "merged_local":
        current_tip = run_git(repo, "rev-parse", f"refs/heads/{default_branch}").stdout.strip()
        if gate_result.get("base_commit") != current_tip:
            print(f"Default branch moved since gate; re-gating {run_id}.")
            gate_result = gate(root, config, run_id, quiet=True)
            if not gate_result.get("pass"):
                raise RhError(f"Re-gate failed for {run_id}: " + "; ".join(gate_result.get("reasons", [])))
            meta["reviewed_sha"] = None
            meta.pop("reviewed", None)
            gated_sha = str(gate_result.get("head_sha", ""))
            approved = False
            if gate_result.get("needs_review"):
                raise RhError(f"Run {run_id} needs explicit review after re-gating; re-run merge with --reviewed")
            branch_tip = run_git(repo, "rev-parse", f"refs/heads/{expected_branch}", check=False)
            if not gated_sha or branch_tip.returncode or branch_tip.stdout.strip() != gated_sha:
                raise RhError(f"Run {run_id} branch changed during re-gate")
            current_tip = run_git(repo, "rev-parse", f"refs/heads/{default_branch}").stdout.strip()
            if gate_result.get("base_commit") != current_tip:
                raise RhError(f"Default branch moved again during re-gate for {run_id}; gate it again")

        origin_ahead = run_git(repo, "rev-list", "--count", f"origin/{default_branch}..refs/heads/{default_branch}", check=False)
        if origin_ahead.returncode == 0:
            # Compatibility diagnostic only; push authorization is re-evaluated from fetched refs below.
            meta["local_ahead_of_origin"] = int(origin_ahead.stdout.strip() or "0")
    fast_forward_default(repo, default_branch, gated_sha)
    meta["status"] = "merged_local"
    meta["merge_commit"] = gated_sha
    meta["reviewed_sha"] = gated_sha if approved else None
    meta.setdefault("pushed", False)
    write_json(run_dir / "meta.json", meta)
    return finish_merge(root, config, run_dir, meta, no_push)


def finish_merge(root: Path, config: dict[str, Any], run_dir: Path, meta: dict[str, Any], no_push: bool) -> int:
    project = get_project(config, str(meta.get("project", "")))
    repo = ensure_repo(project, str(meta.get("project", "")))
    default_branch = str(project["default_branch"])
    commit = str(meta.get("merge_commit", ""))
    if not commit:
        raise RhError("Merged-local run has no gated commit SHA")
    pushed = bool(meta.get("pushed"))
    if project.get("push") and not no_push and not pushed:
        try:
            gate_result = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RhError(f"Cannot re-check push eligibility without gate metadata: {exc}") from exc
        base_commit = str(gate_result.get("base_commit", ""))
        fetch = run_git(repo, "fetch", "origin", default_branch, check=False)
        if fetch.returncode:
            reason = f"origin fetch failed: {(fetch.stderr or fetch.stdout).strip()}"
            meta["status"] = "merged_local"
            meta["pushed"] = False
            meta["push_skip_reason"] = reason
            write_json(run_dir / "meta.json", meta)
            print(f"Push skipped: {reason}")
            return 0
        origin_ref = f"refs/remotes/origin/{default_branch}"
        origin_tip = run_git(repo, "rev-parse", origin_ref, check=False)
        if origin_tip.returncode:
            reason = "origin moved or its default branch is unavailable"
            meta["status"] = "merged_local"
            meta["pushed"] = False
            meta["push_skip_reason"] = reason
            write_json(run_dir / "meta.json", meta)
            print(f"Push skipped: {reason}")
            return 0
        origin_sha = origin_tip.stdout.strip()
        gated_sha = str(gate_result.get("head_sha") or "")
        already_published = bool(gated_sha) and run_git(
            repo, "merge-base", "--is-ancestor", gated_sha, origin_ref, check=False
        ).returncode == 0
        if already_published:
            # Origin already contains exactly the gated commit (e.g. pushed by hand); nothing to push, finish cleanup.
            print("Origin already contains the gated commit; no push needed.")
            pass  # push below is skipped via already_published
        elif origin_sha != base_commit:
            if gate_result.get("origin_commit") and origin_sha == gate_result["origin_commit"]:
                reason = "local default had commits not on origin before this run — Ethan's work, push by hand"
            else:
                reason = "origin moved since gate"
            meta["status"] = "merged_local"
            meta["pushed"] = False
            meta["push_skip_reason"] = reason
            write_json(run_dir / "meta.json", meta)
            print(f"Push skipped: {reason}")
            return 0
        push = subprocess.CompletedProcess([], 0, "", "") if already_published else run_git(
            repo, "push", "origin", f"{commit}:refs/heads/{default_branch}", check=False
        )
        if push.returncode:
            reason = f"push failed: {(push.stderr or push.stdout).strip()}"
            meta["status"] = "merged_local"
            meta["pushed"] = False
            meta["push_skip_reason"] = reason
            write_json(run_dir / "meta.json", meta)
            raise RhError(f"Local merge completed, but {reason}")
        pushed = True
        meta["pushed"] = True
        meta.pop("push_skip_reason", None)
        write_json(run_dir / "meta.json", meta)

    wt_value = meta.get("worktree")
    branch = meta.get("branch")
    if wt_value and (Path(str(wt_value)).exists() or Path(str(wt_value)).is_symlink()):
        worktree = Path(str(wt_value))
        unlink_project_dirs(worktree, project.get("link_dirs", []))
        unlink_reparse_points(worktree)
        remove = run_git(repo, "worktree", "remove", str(worktree), check=False)
        if remove.returncode:
            raise RhError(f"Merged locally, but cannot remove worktree: {(remove.stderr or remove.stdout).strip()}")
    if branch:
        # `branch -d` judges "fully merged" against the checked-out HEAD, which may be Ethan's feature branch.
        # Check against the default branch ourselves, then force-delete only if it is really contained there.
        contained = run_git(repo, "merge-base", "--is-ancestor", str(branch), f"refs/heads/{default_branch}", check=False)
        if contained.returncode:
            raise RhError(f"Merged locally, but {branch} is not contained in {default_branch}; left in place")
        delete = run_git(repo, "branch", "-D", str(branch), check=False)
        if delete.returncode:
            raise RhError(f"Merged locally, but cannot delete branch {branch}: {(delete.stderr or delete.stdout).strip()}")
    meta["status"] = "merged"
    meta["pushed"] = pushed
    meta["merge_commit"] = commit
    write_json(run_dir / "meta.json", meta)
    append_log(root, f"merge {meta['run_id']} commit={commit} pushed={str(pushed).lower()}")
    print(f"Merged {meta['run_id']} commit={commit[:12]} pushed={str(pushed).lower()}")
    return 0


def discard(root: Path, config: dict[str, Any], run_id: str, force: bool = False) -> int:
    run_dir, meta = read_run(root, run_id)
    if meta.get("status") in ("merged", "discarded"):
        raise RhError(f"Run {run_id} is already {meta['status']}")
    state = run_state(run_dir, meta)
    identity = process_identity_state(meta.get("pid"), meta.get("pid_created"))
    if identity is None:
        raise RhError(f"Cannot confirm worker process identity for run {run_id}; refusing cleanup")
    if identity is True:
        if not force:
            raise RhError(f"Run {run_id} has a live worker (state={state}); use --force to terminate its process tree")
        terminate_worker_tree(meta.get("pid"), meta.get("pid_created"))
        if process_identity_state(meta.get("pid"), meta.get("pid_created")) is not False:
            raise RhError(f"Cannot confirm worker process for run {run_id} is quiescent; refusing cleanup")
    project = get_project(config, str(meta.get("project", "")))
    repo = ensure_repo(project, str(meta.get("project", "")))
    worktree = meta.get("worktree")
    branch = meta.get("branch")
    if worktree and Path(str(worktree)).exists():
        worktree_path = Path(str(worktree))
        unlink_project_dirs(worktree_path, project.get("link_dirs", []))
        unlink_reparse_points(worktree_path)
        remove = run_git(repo, "worktree", "remove", "--force", str(worktree_path), check=False)
        if remove.returncode:
            raise RhError(f"Cannot remove worktree: {(remove.stderr or remove.stdout).strip()}")
    if branch:
        delete = run_git(repo, "branch", "-D", str(branch), check=False)
        if delete.returncode and "not found" not in delete.stderr.lower():
            raise RhError(f"Cannot delete branch {branch}: {(delete.stderr or delete.stdout).strip()}")
    meta["status"] = "discarded"
    write_json(run_dir / "meta.json", meta)
    append_log(root, f"discard {run_id}")
    print(f"Discarded {run_id}")
    return 0


def sync(root: Path, config: dict[str, Any]) -> int:
    """Fast-forward each default branch that is strictly behind origin. Never touches local-only commits or dirty trees."""
    problems = 0
    for name, project in sorted(config["projects"].items()):
        repo = project["repo_path"]
        branch = str(project["default_branch"])
        if not repo.is_dir():
            print(f"{name}: MISSING {repo}")
            problems += 1
            continue
        fetched = run_git(repo, "fetch", "-q", "origin", branch, check=False)
        if fetched.returncode:
            print(f"{name}: fetch failed: {(fetched.stderr or fetched.stdout).strip()[:200]}")
            problems += 1
            continue
        counts = run_git(repo, "rev-list", "--left-right", "--count", f"refs/heads/{branch}...origin/{branch}", check=False).stdout.split()
        ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (-1, -1)
        if behind == 0:
            print(f"{name}: up to date (ahead {ahead})")
            continue
        if ahead > 0:
            print(f"{name}: DIVERGED ahead {ahead} / behind {behind} — needs Ethan")
            problems += 1
            continue
        if run_git(repo, "branch", "--show-current").stdout.strip() == branch:
            if git_status(repo):
                print(f"{name}: behind {behind} but {branch} is checked out and dirty — left alone")
                problems += 1
                continue
            result = run_git(repo, "merge", "--ff-only", "-q", f"origin/{branch}", check=False)
        else:
            result = run_git(repo, "fetch", "-q", "origin", f"{branch}:{branch}", check=False)
        if result.returncode:
            print(f"{name}: fast-forward failed: {(result.stderr or result.stdout).strip()[:200]}")
            problems += 1
        else:
            print(f"{name}: fast-forwarded {behind} commit(s)")
            append_log(root, f"sync {name} ff {behind}")
    return 1 if problems else 0


def status(root: Path) -> int:
    runs_dir = root / "runs"
    count = 0
    if runs_dir.is_dir():
        for meta_path in sorted(runs_dir.glob("*/meta.json"), key=lambda p: p.parent.name):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("status") in ("merged", "discarded"):
                    continue
                count += 1
                run_dir = meta_path.parent
                print(
                    f"{meta.get('run_id', run_dir.name)} {meta.get('project', '?')} "
                    f"{run_state(run_dir, meta)} gate={gate_state(run_dir, meta)}"
                )
            except (OSError, json.JSONDecodeError):
                count += 1
                print(f"{meta_path.parent.name} invalid-meta")
    queues = queue_counts(root)
    if queues:
        for name, total in queues.items():
            print(f"queue {name}: {total}")
    else:
        print("queues: none")
    if count == 0:
        print("runs: none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic right-hand worker coordinator")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="write the current digest")
    dispatch_parser = commands.add_parser("dispatch", help="launch queued worker briefs")
    dispatch_parser.add_argument("queue")
    dispatch_parser.add_argument("--dry-run", action="store_true")
    dispatch_parser.add_argument("--max", type=int)
    gate_parser = commands.add_parser("gate", help="check a finished write-mode run")
    gate_parser.add_argument("run_id")
    gate_parser.add_argument("--run-checks", action="store_true", help="reviewer has read the diff: run checks even when needs_review")
    merge_parser = commands.add_parser("merge", help="fast-forward a passing run")
    merge_parser.add_argument("run_id")
    merge_parser.add_argument("--no-push", action="store_true")
    merge_parser.add_argument("--reviewed", action="store_true")
    discard_parser = commands.add_parser("discard", help="remove a run worktree and branch")
    discard_parser.add_argument("run_id")
    discard_parser.add_argument("--force", action="store_true")
    commands.add_parser("status", help="show active runs and queue counts")
    commands.add_parser("sync", help="fast-forward default branches that are strictly behind origin")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = ops_dir()
        if args.command == "status":
            return status(root)
        if args.command == "collect":
            root, config = load_config()
            return collect(root, config)
        root, config = load_config()
        with mutation_lock(root):
            if args.command == "sync":
                return sync(root, config)
            if args.command == "dispatch":
                return dispatch(root, config, args.queue, args.dry_run, args.max)
            if args.command == "gate":
                return 0 if gate(root, config, args.run_id, run_checks_after_review=args.run_checks)["pass"] else 1
            if args.command == "merge":
                return merge(root, config, args.run_id, args.no_push, args.reviewed)
            if args.command == "discard":
                return discard(root, config, args.run_id, args.force)
    except BusyError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (RhError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
