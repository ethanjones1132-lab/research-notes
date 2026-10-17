import json
import os
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import uuid
from datetime import datetime, timezone


RH = Path(__file__).with_name("rh.py")


def find_git():
    if os.name == "nt":
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(entry) / "git.exe"
            if Path(entry).name.lower() == "cmd" and candidate.is_file():
                return str(candidate)
    return shutil.which("git") or "git"


GIT = find_git()
GIT_HTTP_BACKEND = str(Path(GIT).resolve().parent.parent / "mingw64" / "libexec" / "git-core" / "git-http-backend.exe")


def remove_test_tree(path):
    def retry_read_only(func, name, _exc):
        os.chmod(name, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        func(name)

    shutil.rmtree(path, onexc=retry_read_only)


def windows_pid_is_alive(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == 5
    exit_code = wintypes.DWORD()
    known = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return not known or exit_code.value == 259


class GitHttpBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    project_root = None

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self.handle_git_request()

    def do_POST(self):
        self.handle_git_request()

    def handle_git_request(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) if self.command == "POST" else b""
        parsed = urllib.parse.urlsplit(self.path)
        env = os.environ.copy()
        env.update({
            "GIT_PROJECT_ROOT": str(self.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": self.client_address[0],
            "SERVER_PROTOCOL": self.protocol_version,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": str(self.server.server_port),
            "SCRIPT_NAME": "",
        })
        result = subprocess.run(
            [GIT_HTTP_BACKEND], input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, check=False,
        )
        response = result.stdout
        separator = b"\r\n\r\n"
        split_at = response.find(separator)
        if split_at < 0:
            separator = b"\n\n"
            split_at = response.find(separator)
        if split_at < 0:
            self.send_error(500, result.stderr.decode("utf-8", errors="replace") or "Invalid Git HTTP backend response")
            return
        raw_headers = response[:split_at].decode("latin-1")
        payload = response[split_at + len(separator):]
        status = 200
        headers = []
        for line in raw_headers.splitlines():
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name.lower() != "content-length":
                headers.append((name.strip(), value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RightHandIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(__file__).parent / (".rh-test-" + uuid.uuid4().hex)
        self.temp.mkdir()
        self.addCleanup(remove_test_tree, self.temp)
        self.ops = self.temp / "ops"
        self.ops.mkdir()
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        self.env["RH_OPS_DIR"] = str(self.ops)
        self.env["RH_TEST_GIT"] = GIT
        self.env["RH_TEST_ESCAPE_REPO"] = str(self.repo)
        self.git("init", "-b", "main", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.name", "RH Test")
        self.git("-C", str(self.repo), "config", "user.email", "rh-test@example.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "base.py").write_text("BASE = True\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-m", "initial")
        self.worker = self.ops / "fake_worker.py"
        self.worker.write_text(self.fake_worker_source(), encoding="utf-8")
        config = {
            "projects": {
                "sample": {
                    "path": str(self.repo),
                    "default_branch": "main",
                    "push": False,
                    "link_dirs": [],
                    "verify": [],
                }
            },
            "worker_py": str(self.worker),
            "worktree_root": str(self.ops / "worktrees"),
        }
        (self.ops / "projects.json").write_text(json.dumps(config), encoding="utf-8")
        self.queue = self.ops / "queue" / "batch"
        self.queue.mkdir(parents=True)

    @staticmethod
    def fake_worker_source():
        return '''import json
import os
import subprocess
import sys
import time
from pathlib import Path

_, _worker, brief_name, out_name, *args = sys.argv
brief = Path(brief_name).read_text(encoding="utf-8")
if brief.lstrip().startswith("-"):
    print("RUNG=none")
    print("EXIT=2")
    sys.exit(2)
cwd =Path(args[args.index("--cwd") + 1])
commit_cwd = cwd
change = {"src/change.py": "CHANGED = True\\n"}
delete = []
sleep_seconds = 0
switch_branch = None
escape_to_repo = False
utf16 = False
for line in brief.splitlines():
    if line.startswith("FAKE_CHANGE_JSON="):
        change = json.loads(line.split("=", 1)[1])
    elif line.startswith("FAKE_DELETE_JSON="):
        delete = json.loads(line.split("=", 1)[1])
    elif line.startswith("FAKE_SLEEP="):
        sleep_seconds = int(line.split("=", 1)[1])
    elif line.startswith("FAKE_SWITCH_BRANCH="):
        switch_branch = line.split("=", 1)[1]
    elif line == "FAKE_ESCAPE=1":
        escape_to_repo = True
    elif line == "FAKE_UTF16=1":
        utf16 = True
if escape_to_repo:
    commit_cwd = Path(os.environ["RH_TEST_ESCAPE_REPO"])
print("RUNG=fake", flush=True)
if sleep_seconds:
    time.sleep(sleep_seconds)
for name, content in change.items():
    path = commit_cwd / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if utf16:
        path.write_bytes(content.encode("utf-16"))
    else:
        path.write_text(content, encoding="utf-8")
for name in delete:
    (commit_cwd / name).unlink()
git = os.environ["RH_TEST_GIT"]
subprocess.run([git, "add", "-A"], cwd=commit_cwd, check=True)
subprocess.run([git, "-c", "user.name=RH Fake Worker", "-c", "user.email=rh-worker@example.invalid", "commit", "-m", "feat: worker change"], cwd=commit_cwd, check=True)
if switch_branch:
    subprocess.run([git, "switch", "-c", switch_branch], cwd=cwd, check=True)
Path(out_name).write_text("worker complete\\n", encoding="utf-8")
print("EXIT=0")
'''

    @staticmethod
    def git(*args):
        result = subprocess.run([GIT, *args], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(f"git failed: {args}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def brief(self, *, title="Add change", scope="src/**", change=None, check=None, worker="deepseek",
              switch_branch=None, escape=False, utf16=False, delete=None):
        body = "Worker task."
        if change is not None:
            body += "\nFAKE_CHANGE_JSON=" + json.dumps(change)
        if switch_branch:
            body += "\nFAKE_SWITCH_BRANCH=" + switch_branch
        if escape:
            body += "\nFAKE_ESCAPE=1"
        if utf16:
            body += "\nFAKE_UTF16=1"
        if delete:
            body += "\nFAKE_DELETE_JSON=" + json.dumps(delete)
        text = (
            "---\nproject: sample\nworker: " + worker + "\n"
            f"title: {title}\nmode: write\nscope: {scope}\n"
        )
        if check is not None:
            text += f"check: {check}\n"
        text += f"---\n{body}\n"
        path = self.queue / "001-task.md"
        path.write_text(text, encoding="utf-8")
        return path

    def run_rh(self, *args):
        return subprocess.run(
            [sys.executable, "-I", str(RH), *args],
            cwd=self.ops,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def gate_review_then_run_checks(self, run_dir, expected_review):
        """A needs_review gate defers checks; a reviewer re-runs with --run-checks to pass."""
        deferred = self.run_rh("gate", run_dir.name)
        self.assertNotEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"], gate["reasons"])
        self.assertTrue(any("checks deferred until review" in reason for reason in gate["reasons"]), gate["reasons"])
        self.assertTrue(any(expected_review in reason for reason in gate["needs_review"]), gate["needs_review"])
        self.assertTrue(
            any(item.get("cmd") == "(deferred)" and item.get("exit") == -1 for item in gate["check"]),
            gate["check"],
        )
        checked = self.run_rh("gate", run_dir.name, "--run-checks")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(gate["pass"], gate["reasons"])
        self.assertTrue(gate["needs_review"], gate)
        return gate

    def dispatch_and_wait(self, *, title="Add change", scope="src/**", change=None, check=None, worker="deepseek",
                          switch_branch=None, escape=False, utf16=False, delete=None):
        self.brief(title=title, scope=scope, change=change, check=check, worker=worker,
                   switch_branch=switch_branch, escape=escape, utf16=utf16, delete=delete)
        dispatched = self.run_rh("dispatch", "batch")
        self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
        run_dirs = list((self.ops / "runs").glob("*"))
        self.assertEqual(len(run_dirs), 1, dispatched.stdout)
        run_dir = run_dirs[0]
        self.wait_for_worker(run_dir)
        return run_dir

    def wait_for_worker(self, run_dir, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            log = run_dir / "worker.log"
            text = log.read_text(encoding="utf-8", errors="replace").rstrip() if log.exists() else ""
            if text.endswith("EXIT=0"):
                break
            if "EXIT=" in text:
                self.fail(f"worker failed: {text}")
            time.sleep(0.05)
        else:
            try:
                meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
                pid = int(meta.get("pid") or 0)
                if pid > 0 and os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                elif pid > 0:
                    os.killpg(pid, signal.SIGKILL)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            self.fail(f"worker did not finish: {(run_dir / 'worker.log').read_text(encoding='utf-8', errors='replace')}")

    def set_push(self, push):
        config = json.loads((self.ops / "projects.json").read_text(encoding="utf-8"))
        config["projects"]["sample"]["push"] = push
        (self.ops / "projects.json").write_text(json.dumps(config), encoding="utf-8")

    def add_origin(self):
        origin = self.temp / "origin.git"
        self.git("init", "--bare", "-b", "main", str(origin))
        initial = self.git("-C", str(self.repo), "rev-parse", "refs/heads/main")
        shutil.copytree(self.repo / ".git" / "objects", origin / "objects", dirs_exist_ok=True)
        self.git("--git-dir", str(origin), "update-ref", "refs/heads/main", initial)
        if os.name == "nt" and Path(GIT_HTTP_BACKEND).is_file():
            self.git("--git-dir", str(origin), "config", "http.uploadpack", "true")
            self.git("--git-dir", str(origin), "config", "http.receivepack", "true")

            class Handler(GitHttpBackendHandler):
                pass

            Handler.project_root = self.temp
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(self.stop_git_http, server, thread)
            self.origin_url = f"http://127.0.0.1:{server.server_port}/origin.git"
        else:
            self.origin_url = str(origin)
        self.git("-C", str(self.repo), "remote", "add", "origin", self.origin_url)
        self.git("-C", str(self.repo), "fetch", "origin", "main")
        return origin

    @staticmethod
    def stop_git_http(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def test_dispatch_dry_run_changes_nothing(self):
        brief = self.brief()
        result = self.run_rh("dispatch", "batch", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY-RUN", result.stdout)
        self.assertTrue(brief.exists())
        self.assertFalse((self.ops / "runs").exists())
        self.assertFalse((self.ops / "LOG.md").exists())

    def test_dispatch_detaches_and_collect_reports_finished(self):
        run_dir = self.dispatch_and_wait()
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        self.assertEqual(digest["runs"][0]["status"], "finished")
        self.assertEqual(digest["runs"][0]["run_id"], run_dir.name)

    def test_gate_passes_and_merge_fast_forwards_without_push(self):
        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(gate["pass"], gate["reasons"])
        merged = self.run_rh("merge", run_dir.name)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertEqual((self.repo / "src" / "change.py").read_text(encoding="utf-8"), "CHANGED = True\n")
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "merged")
        self.assertFalse(meta["pushed"])

    def test_gate_fails_on_added_skip_marker(self):
        run_dir = self.dispatch_and_wait(change={"src/change.py": "@pytest.mark.skip(reason=\"disabled\")\ndef test_change():\n    pass\n"})
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"])
        self.assertTrue(any("skip marker" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_gate_fails_on_added_secret(self):
        run_dir = self.dispatch_and_wait(change={"src/change.py": 'api_key = "1234567890123456"\n'})
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"])
        self.assertTrue(any("secret pattern" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_preexisting_fixture_token_in_touched_file_passes_but_new_secret_fails(self):
        fixture = self.repo / "tests" / "fixture_test.py"
        fixture.write_text('TOKEN = {"token": "device-token-1"}\n', encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-m", "chore: add secret-like fixture")

        run_dir = self.dispatch_and_wait(
            scope="tests/**",
            change={"tests/fixture_test.py": 'TOKEN = {"token": "device-token-1"}\nX = 1\n'},
        )
        self.gate_review_then_run_checks(run_dir, "existing test file changed")

        discarded = self.run_rh("discard", run_dir.name)
        self.assertEqual(discarded.returncode, 0, discarded.stdout + discarded.stderr)
        remove_test_tree(run_dir)

        run_dir = self.dispatch_and_wait(
            scope="tests/**",
            change={"tests/fixture_test.py": 'TOKEN = {"token": "device-token-1"}\nNEW = {"api_key": "zzzzzzzzzzzzzzzz9"}\n'},
        )
        gated = self.run_rh("gate", run_dir.name, "--run-checks")
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"])
        self.assertTrue(any("secret" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_gate_fails_on_out_of_scope_file(self):
        run_dir = self.dispatch_and_wait(change={"docs/surprise.md": "Out of scope.\n"})
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"])
        self.assertTrue(any("out-of-scope file" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_merge_refuses_dirty_checked_out_default_branch(self):
        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        dirty_file = self.repo / "user-work.txt"
        dirty_file.write_text("keep this uncommitted\n", encoding="utf-8")
        merged = self.run_rh("merge", run_dir.name)
        self.assertNotEqual(merged.returncode, 0)
        self.assertIn("checked out and dirty", merged.stderr)
        self.assertEqual(dirty_file.read_text(encoding="utf-8"), "keep this uncommitted\n")
        self.assertEqual(self.git("-C", str(self.repo), "branch", "--show-current"), "main")

    def test_push_skipped_when_local_default_ahead_of_origin(self):
        origin = self.add_origin()
        self.set_push(True)
        extra = self.repo / "user-work.txt"
        extra.write_text("not for origin yet\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-m", "chore: local only")
        origin_tip_before = self.git("-C", str(origin), "rev-parse", "refs/heads/main")

        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        merged = self.run_rh("merge", run_dir.name)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["pushed"])
        self.assertEqual(meta["local_ahead_of_origin"], 1)
        self.assertEqual(self.git("-C", str(origin), "rev-parse", "refs/heads/main"), origin_tip_before)

    def test_push_happens_when_local_default_in_sync(self):
        origin = self.add_origin()
        self.set_push(True)

        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        merged = self.run_rh("merge", run_dir.name)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)

        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["pushed"])
        self.assertEqual(
            self.git("-C", str(origin), "rev-parse", "refs/heads/main"),
            self.git("-C", str(self.repo), "rev-parse", "refs/heads/main"),
        )

    def test_check_runs_under_bash(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"marker.txt": "hello\n"},
            check="test -f marker.txt && grep -q hello marker.txt",
        )
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(gate["pass"], gate["reasons"])
        self.assertEqual(gate["check"][0]["exit"], 0)

    def test_go_plan_cap_holds_extra_briefs(self):
        runs = self.ops / "runs"
        for index in range(3):
            run_dir = runs / f"20260101-00{index:02d}-sample-held"
            run_dir.mkdir(parents=True)
            (run_dir / "meta.json").write_text(
                json.dumps({
                    "run_id": run_dir.name,
                    "project": "sample",
                    "worker": "deepseek",
                    "status": "running",
                    "pid": os.getpid(),
                }),
                encoding="utf-8",
            )
        brief = self.brief()
        result = self.run_rh("dispatch", "batch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HELD", result.stdout)
        self.assertTrue(brief.exists())
        self.assertEqual(len(list(runs.glob("*/meta.json"))), 3)

    def test_gate_squashes_secret_added_then_removed_before_public_push(self):
        origin = self.add_origin()
        self.set_push(True)
        run_dir = self.dispatch_and_wait()
        worktree = Path(json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["worktree"])
        secret_file = worktree / "src" / "transient.py"
        secret_file.write_text('TOKEN = "transient-secret-987654321"\n', encoding="utf-8")
        self.git("-C", str(worktree), "add", "-A")
        self.git("-C", str(worktree), "-c", "user.name=RH Test", "-c", "user.email=rh-test@example.invalid", "commit", "-m", "temporary secret")
        secret_file.unlink()
        self.git("-C", str(worktree), "add", "-A")
        self.git("-C", str(worktree), "-c", "user.name=RH Test", "-c", "user.email=rh-test@example.invalid", "commit", "-m", "remove secret")

        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(gate["pass"], gate["reasons"])
        self.assertEqual(self.git("-C", str(worktree), "rev-list", "--count", f"{gate['base_commit']}..HEAD"), "1")
        merged = self.run_rh("merge", run_dir.name)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        published = self.git("--git-dir", str(origin), "log", "-p", "--all")
        self.assertNotIn("transient-secret-987654321", published)

    def test_gate_fails_on_quoted_json_secret_key(self):
        run_dir = self.dispatch_and_wait(scope="**", change={"src/config.json": '{"api_key": "1234567890123456"}\n'})
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(any("secret pattern" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_gate_fails_on_quoted_secret_with_spaces(self):
        run_dir = self.dispatch_and_wait(scope="**", change={"src/config.env": 'password = "correct horse battery staple"\n'})
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(any("secret pattern" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_gate_scans_utf16le_secret_blob(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"src/credentials.txt": 'api_key = "zzzzzzzzzzzzzzzz9"\n'},
            utf16=True,
        )
        gated = self.run_rh("gate", run_dir.name)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(not gate["pass"] or any("undecodable content" in item for item in gate["needs_review"]),
                        gated.stdout + str(gate))

    def test_gate_refuses_worktree_switched_to_another_branch_without_git_mutation(self):
        run_dir = self.dispatch_and_wait(scope="**", switch_branch="other")
        worktree = Path(json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["worktree"])
        refs_before = self.git("-C", str(self.repo), "show-ref")
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertFalse(gate["pass"], gate)
        self.assertTrue(any("branch" in reason and "worktree" in reason for reason in gate["reasons"]), gate["reasons"])
        self.assertEqual(self.git("-C", str(worktree), "branch", "--show-current"), "other")
        self.assertEqual(self.git("-C", str(self.repo), "show-ref"), refs_before)

    def test_default_branch_worker_escape_fails_gate_without_rh_mutation(self):
        run_dir = self.dispatch_and_wait(scope="**", escape=True)
        main_sha = self.git("-C", str(self.repo), "rev-parse", "refs/heads/main")
        refs_before = self.git("-C", str(self.repo), "show-ref")
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(any("default branch changed outside rh during this run (possible worker escape)" in reason and main_sha in reason
                            for reason in gate["reasons"]), gate["reasons"])
        self.assertEqual(self.git("-C", str(self.repo), "show-ref"), refs_before)

    def test_existing_test_file_any_change_requires_review(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"tests/test_base.py": "def test_base():\n    assert True\n# added line\n"},
        )
        self.gate_review_then_run_checks(run_dir, "existing test file changed")

    def test_new_test_file_addition_does_not_require_review(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"tests/test_new.py": "def test_new():\n    assert True\n"},
        )
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(gate["pass"], gate["reasons"])
        self.assertFalse(gate["needs_review"], gate["needs_review"])

    def test_deleting_existing_test_file_requires_review(self):
        run_dir = self.dispatch_and_wait(scope="**", delete=["tests/test_base.py"])
        self.gate_review_then_run_checks(run_dir, "existing test file changed")

    def test_coverage_ratchet_change_requires_review(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"scripts/coverage-ratchet.mts": "export const threshold = 0;\n"},
        )
        self.gate_review_then_run_checks(run_dir, "verification config changed: scripts/coverage-ratchet.mts")

    def test_review_approval_is_cleared_when_a_regate_produces_new_sha(self):
        self.set_push(True)
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"package.json": '{"scripts":{"verify":"exit 0"}}\n'},
        )
        gate_a = self.gate_review_then_run_checks(run_dir, "verification config changed")
        reviewed = self.run_rh("merge", run_dir.name, "--reviewed")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta.get("reviewed_sha"), gate_a["head_sha"])

        worktree = Path(meta["worktree"])
        (worktree / "package.json").write_text('{"scripts":{"verify":"exit 0"},"changed":true}\n', encoding="utf-8")
        self.git("-C", str(worktree), "add", "package.json")
        self.git("-C", str(worktree), "-c", "user.name=RH Test", "-c", "user.email=rh-test@example.invalid", "commit", "-m", "second gated revision")
        gate_b = self.gate_review_then_run_checks(run_dir, "verification config changed")
        self.assertNotEqual(gate_a["head_sha"], gate_b["head_sha"])
        plain_merge = self.run_rh("merge", run_dir.name)
        self.assertNotEqual(plain_merge.returncode, 0, plain_merge.stdout + plain_merge.stderr)
        self.assertIn("needs explicit review", plain_merge.stderr)

    def test_live_process_with_mismatched_creation_time_is_not_running(self):
        run_dir = self.ops / "runs" / "20200101-0000-sample-pid-identity"
        run_dir.mkdir(parents=True)
        (run_dir / "worker.log").write_text("RUNG=fake\n", encoding="utf-8")
        (run_dir / "meta.json").write_text(json.dumps({
            "run_id": run_dir.name,
            "project": "sample",
            "worker": "deepseek",
            "mode": "write",
            "status": "running",
            "pid": os.getpid(),
            "pid_created": "wrong-creation-time",
            "started": datetime.now(timezone.utc).isoformat(),
            "timeout": 3600,
        }), encoding="utf-8")
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        row = next(item for item in digest["runs"] if item["run_id"] == run_dir.name)
        self.assertNotEqual(row["status"], "running", row)

    def test_collect_flags_unexpected_local_commits_but_not_rh_merges(self):
        self.add_origin()
        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        merged = self.run_rh("merge", run_dir.name, "--no-push")
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        row = next(item for item in digest["projects"] if item["project"] == "sample")
        self.assertFalse(row["unexpected_local_commits"], row)
        direct_file = self.repo / "src" / "local-only.py"
        direct_file.write_text("LOCAL_ONLY = True\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "src/local-only.py")
        self.git("-C", str(self.repo), "-c", "user.name=RH Test", "-c", "user.email=rh-test@example.invalid", "commit", "-m", "direct local commit")
        direct_sha = self.git("-C", str(self.repo), "rev-parse", "HEAD")
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        row = next(item for item in digest["projects"] if item["project"] == "sample")
        self.assertIn("UNEXPECTED LOCAL COMMITS", collected.stdout)
        self.assertIn(direct_sha, collected.stdout)
        self.assertEqual(row["unexpected_local_commits"], [direct_sha], row)

    def test_gate_scans_added_content_that_starts_with_plus_plus(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"src/change.js": '++counter; const token = "1234567890123456";\n'},
        )
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(any("secret pattern" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_verification_config_requires_explicit_review(self):
        run_dir = self.dispatch_and_wait(scope="**", change={"package.json": '{"scripts":{"verify":"exit 0"}}\n'})
        self.gate_review_then_run_checks(run_dir, "verification config changed")
        status = self.run_rh("status")
        self.assertIn("gate=needs-review", status.stdout)
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        self.assertEqual(digest["runs"][0]["gate"], "needs-review")
        refused = self.run_rh("merge", run_dir.name)
        self.assertNotEqual(refused.returncode, 0)
        reviewed = self.run_rh("merge", run_dir.name, "--reviewed")
        self.assertEqual(reviewed.returncode, 0, reviewed.stdout + reviewed.stderr)

    def test_cargo_config_change_requires_review(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={".cargo/config.toml": "[build]\ntarget-dir = \"target\"\n"},
        )
        self.gate_review_then_run_checks(run_dir, "verification config changed: .cargo/config.toml")

    def test_removing_test_code_from_source_file_requires_review(self):
        source = self.repo / "src" / "lib.rs"
        source.write_text("#[test]\nfn existing() { assert!(true); }\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-m", "chore: add rust test")
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"src/lib.rs": "pub fn helper() {}\n"},
        )
        self.gate_review_then_run_checks(run_dir, "test code removed")

    def test_shrinking_existing_test_file_requires_review(self):
        run_dir = self.dispatch_and_wait(scope="**", change={"tests/test_base.py": "pass\n"})
        self.gate_review_then_run_checks(run_dir, "test file shrunk")

    def test_check_that_modifies_worktree_fails_gate(self):
        run_dir = self.dispatch_and_wait(
            scope="**",
            change={"src/change.py": "CHANGED = True\n"},
            check="echo CHECK_CHANGED >> src/base.py",
        )
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 1, gated.stdout + gated.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertTrue(any("check modified the worktree" in reason for reason in gate["reasons"]), gate["reasons"])

    def test_merge_refuses_worker_branch_moved_after_gate(self):
        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        worktree = Path(json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))["worktree"])
        self.git("-C", str(worktree), "-c", "user.name=RH Test", "-c", "user.email=rh-test@example.invalid", "commit", "--allow-empty", "-m", "move after gate")
        merged = self.run_rh("merge", run_dir.name)
        self.assertNotEqual(merged.returncode, 0)
        self.assertIn("changed since gate", merged.stderr)
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        self.assertEqual(self.git("-C", str(self.repo), "rev-parse", "refs/heads/main"), gate["base_commit"])

    def test_push_skips_when_origin_moves_after_gate(self):
        origin = self.add_origin()
        self.set_push(True)
        run_dir = self.dispatch_and_wait()
        gated = self.run_rh("gate", run_dir.name)
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        before = self.git("--git-dir", str(origin), "rev-parse", "refs/heads/main")
        clone = self.temp / "other-clone"
        self.git("clone", self.origin_url, str(clone))
        self.git("-C", str(clone), "config", "user.name", "Other Clone")
        self.git("-C", str(clone), "config", "user.email", "other@example.invalid")
        (clone / "other.txt").write_text("remote moved\n", encoding="utf-8")
        self.git("-C", str(clone), "add", "-A")
        self.git("-C", str(clone), "commit", "-m", "remote moved")
        self.git("-C", str(clone), "push", "origin", "main")
        moved_tip = self.git("--git-dir", str(origin), "rev-parse", "refs/heads/main")
        self.assertNotEqual(before, moved_tip)

        merged = self.run_rh("merge", run_dir.name)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        self.assertIn("origin moved", merged.stdout)
        self.assertEqual(self.git("--git-dir", str(origin), "rev-parse", "refs/heads/main"), moved_tip)
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "merged_local")
        self.assertFalse(meta["pushed"])

    def test_discard_requires_force_for_running_worker_and_kills_it(self):
        brief = self.brief()
        brief.write_text(brief.read_text(encoding="utf-8") + "FAKE_SLEEP=20\n", encoding="utf-8")
        dispatched = self.run_rh("dispatch", "batch")
        self.assertEqual(dispatched.returncode, 0, dispatched.stdout + dispatched.stderr)
        run_dir = next((self.ops / "runs").iterdir())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "RUNG=fake" not in (run_dir / "worker.log").read_text(encoding="utf-8", errors="replace"):
            time.sleep(0.05)
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        pid = int(meta["pid"])
        state = self.run_rh("status")
        self.assertIn(f"{run_dir.name} sample running", state.stdout, f"pid={pid} status={state.stdout} stderr={state.stderr}")
        refused = self.run_rh("discard", run_dir.name)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("--force", refused.stderr)
        removed = self.run_rh("discard", run_dir.name, "--force")
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        if os.name == "nt":
            self.assertFalse(windows_pid_is_alive(pid))
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertFalse(Path(meta["worktree"]).exists())

    @unittest.skipUnless(os.name == "nt", "junction creation uses Windows mklink /J")
    def test_discard_unlinks_worktree_junction_without_deleting_target(self):
        run_dir = self.dispatch_and_wait()
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        worktree = Path(meta["worktree"])
        target = self.temp / "junction-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        link = worktree / "worker-junction"
        linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)

        discarded = self.run_rh("discard", run_dir.name)
        self.assertEqual(discarded.returncode, 0, discarded.stdout + discarded.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_expired_deadline_overrides_live_pid_and_go_plan_capacity(self):
        runs = self.ops / "runs"
        for index in range(3):
            run_dir = runs / f"20200101-000{index}-sample-expired-{index}"
            run_dir.mkdir(parents=True)
            (run_dir / "meta.json").write_text(json.dumps({
                "run_id": run_dir.name,
                "project": "sample",
                "worker": "deepseek",
                "status": "running",
                "pid": os.getpid(),
                "started": "2000-01-01T00:00:00Z",
                "timeout": 1,
            }), encoding="utf-8")
        collected = self.run_rh("collect")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        digest = json.loads((self.ops / "state" / "digest.json").read_text(encoding="utf-8"))
        expired = [row for row in digest["runs"] if row["run_id"].endswith(tuple(f"expired-{i}" for i in range(3)))]
        self.assertEqual(len(expired), 3)
        self.assertTrue(all(row["status"] == "timeout" for row in expired), expired)
        self.brief(worker="deepseek")
        dispatched = self.run_rh("dispatch", "batch")
        self.assertEqual(dispatched.returncode, 0, dispatched.stdout + dispatched.stderr)
        self.assertNotIn("HELD", dispatched.stdout)
        launched = next(path for path in runs.glob("*") if (path / "worker.log").exists())
        self.wait_for_worker(launched)

    def test_mutation_exits_three_when_global_lock_is_held(self):
        lock = self.ops / "state" / "rh.lock"
        lock.parent.mkdir(parents=True)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(descriptor, f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
            result = self.run_rh("dispatch", "batch")
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("busy: another rh command is running", result.stderr)
        finally:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_os_lock_releases_after_holding_child_is_killed(self):
        self.brief()
        holder_script = self.temp / "hold_lock.py"
        holder_script.write_text(
            "import runpy, sys, time\n"
            "from pathlib import Path\n"
            "lock = runpy.run_path(sys.argv[1])['mutation_lock']\n"
            "with lock(Path(sys.argv[2])):\n"
            "    print('LOCKED', flush=True)\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        holder = subprocess.Popen(
            [sys.executable, "-I", str(holder_script), str(RH), str(self.ops)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            busy = self.run_rh("dispatch", "batch")
            self.assertEqual(busy.returncode, 3, busy.stdout + busy.stderr)
        finally:
            holder.kill()
            holder.communicate(timeout=10)
        dispatched = self.run_rh("dispatch", "batch")
        self.assertEqual(dispatched.returncode, 0, dispatched.stdout + dispatched.stderr)
        run_dir = next((self.ops / "runs").iterdir())
        self.wait_for_worker(run_dir)

    def test_duplicate_run_ids_in_batch_receive_numeric_suffixes(self):
        first = self.brief(title="Repeated title", worker="claude")
        second = self.queue / "002-task.md"
        second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        result = self.run_rh("dispatch", "batch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_dirs = sorted((self.ops / "runs").iterdir(), key=lambda path: path.name)
        self.assertEqual(len(run_dirs), 2)
        self.assertTrue(run_dirs[0].name.endswith("repeated-title"), [path.name for path in run_dirs])
        self.assertTrue(run_dirs[1].name.endswith("repeated-title-2"), [path.name for path in run_dirs])
        for run_dir in run_dirs:
            self.wait_for_worker(run_dir)


if __name__ == "__main__":
    unittest.main()
