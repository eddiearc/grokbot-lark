#!/usr/bin/env python3
"""Shell tests for supervisor.sh, status.sh, start.sh and stop.sh.

Each test copies the scripts next to a FAKE relay.py into a temp relay dir with a
temp HOME, so no real lark-cli, network or user state is touched. The tests run on
macOS (no flock, bash 3.2) too: the supervisor test gets a stub flock there and the
start/stop tests are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

RELAY_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = ("load-env.sh", "supervisor.sh", "status.sh", "start.sh", "stop.sh")
HAS_FLOCK = shutil.which("flock") is not None

FAKE_RELAY = textwrap.dedent(
    """\
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    base = Path(__file__).resolve().parent
    (base / "env_dump.json").write_text(json.dumps(dict(os.environ)))
    # A child whose args look like the real event consumer.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", "event", "consume", "im.message.receive_v1"]
    )
    (base / "consumer.pid").write_text(str(child.pid))
    (base / "logs").mkdir(exist_ok=True)
    with (base / "logs" / "relay.jsonl").open("a") as f:
        f.write(json.dumps({"msg": "consumer_ready"}) + "\\n")

    def stop(*_):
        child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(0.1)
    """
)

# The supervisor of the first release: TERM ran cleanup and the loop carried on,
# so the relay came back. stop.sh must still be able to end it during an upgrade.
LEGACY_SUPERVISOR = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -eu
    BASE="$(cd "$(dirname "$0")" && pwd)"
    mkdir -p "${BASE}/logs" "${BASE}/state"
    exec 9>"${BASE}/state/supervisor.lock"
    flock -n 9 || exit 1
    echo "$$" > "${BASE}/state/supervisor.pid"
    cleanup() {
      if [[ -n "${RELAY_PID:-}" ]] && kill -0 "${RELAY_PID}" 2>/dev/null; then
        kill -TERM "${RELAY_PID}" 2>/dev/null || true
        wait "${RELAY_PID}" 2>/dev/null || true
      fi
      rm -f "${BASE}/state/supervisor.pid" "${BASE}/state/relay.pid"
    }
    trap cleanup EXIT INT TERM
    while true; do
      python3 "${BASE}/relay.py" >> "${BASE}/logs/relay.out" 2>&1 &
      RELAY_PID=$!
      echo "${RELAY_PID}" > "${BASE}/state/relay.pid"
      set +e
      wait "${RELAY_PID}"
      set -e
      rm -f "${BASE}/state/relay.pid"
      RELAY_PID=""
      sleep 2
    done
    """
)

STUB_FLOCK = "#!/bin/sh\nexit 0\n"
STUB_LARK_CLI = textwrap.dedent(
    """\
    #!/bin/sh
    echo '{"apps": [{"app_id": "cli_x", "running": true, "status": "ok", "consumers": [{"event_key": "im.message.receive_v1", "received": 12, "dropped": 0}]}]}'
    """
)


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-shell-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.relay = self.tmp / "relay"
        self.relay.mkdir()
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name in SCRIPTS:
            shutil.copy2(RELAY_DIR / name, self.relay / name)
        (self.relay / "relay.py").write_text(FAKE_RELAY)
        self._stub("lark-cli", STUB_LARK_CLI)
        if not HAS_FLOCK:
            self._stub("flock", STUB_FLOCK)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("LARK", "LARKSUITE"))}
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(self.stubs) + os.pathsep + os.environ.get("PATH", "")
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._kill_everything)

    def _stub(self, name: str, body: str) -> None:
        path = self.stubs / name
        path.write_text(body)
        path.chmod(0o755)

    def _pid_from(self, name: str) -> int | None:
        try:
            return int((self.relay / "state" / name).read_text().strip())
        except (OSError, ValueError):
            return None

    def _kill_everything(self) -> None:
        for name in ("supervisor.pid", "relay.pid"):
            pid = self._pid_from(name)
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # The fake consumer child may outlive a SIGKILLed relay.
        try:
            os.kill(int((self.relay / "consumer.pid").read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def run_script(self, name: str, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.relay / name)],
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    def wait_for(self, predicate, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_gone(pid: int) -> bool:
    """True when the process no longer runs (an unreaped zombie counts as gone)."""
    proc = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    state = proc.stdout.strip()
    return not state or state.startswith("Z")


class SupervisorTests(ShellCase):
    def test_relay_env_reaches_relay_and_term_stops_everything(self):
        (self.relay / "relay.env").write_text(
            "\n".join(
                [
                    "LARK_BOT_OPEN_ID=ou_test",
                    'LARK_BOT_NAME="Grok Bot"',
                    "LARK_BOT_WEBHOOK_URL=https://example.invalid/hook",
                    "LARK_BOT_WEBHOOK_KEY=k_test",
                ]
            )
            + "\n"
        )
        sup = subprocess.Popen(
            [str(self.relay / "supervisor.sh")],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.procs.append(sup)
        dump = self.relay / "env_dump.json"
        ready = self.relay / "logs" / "relay.jsonl"
        self.assertTrue(self.wait_for(lambda: dump.exists() and ready.exists() and self._pid_from("relay.pid")))
        env = json.loads(dump.read_text())
        self.assertEqual(env.get("LARK_BOT_OPEN_ID"), "ou_test")
        self.assertEqual(env.get("LARK_BOT_NAME"), "Grok Bot")
        self.assertTrue(env["LARK_BOT_ENV"].endswith("/.lark-bots/lark-bot/env.sh"))

        # status.sh reads the same settings: a healthy report with the webhook from the environment.
        out = self.run_script("status.sh")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("webhook: configured (environment)", out.stdout)
        self.assertIn("event consume processes: 1 (expected 1)", out.stdout)
        self.assertIn("consumer key=im.message.receive_v1 received=12 dropped=0", out.stdout)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "status: healthy")
        self.assertNotIn("k_test", out.stdout)

        sup_pid = self._pid_from("supervisor.pid")
        relay_pid = self._pid_from("relay.pid")
        self.assertEqual(sup_pid, sup.pid)
        os.kill(sup_pid, signal.SIGTERM)
        try:
            sup.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("supervisor did not exit within 10 s of SIGTERM")
        self.assertTrue(self.wait_for(lambda: not pid_alive(relay_pid), timeout=5), "relay.py still running")
        self.assertFalse((self.relay / "state" / "supervisor.pid").exists())
        self.assertFalse((self.relay / "state" / "relay.pid").exists())
        # It must not have restarted a relay after TERM.
        time.sleep(0.5)
        self.assertFalse((self.relay / "state" / "relay.pid").exists())


class StatusTests(ShellCase):
    def test_empty_spool_and_nothing_running(self):
        spool = self.relay / "spool"
        spool.mkdir()
        (spool / "pending.jsonl").write_text("")
        out = self.run_script("status.sh")
        lines = out.stdout.splitlines()
        self.assertEqual(lines.count("spool depth: 0"), 1, out.stdout)
        self.assertNotIn("0", [ln.strip() for ln in lines])
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: unhealthy", lines)
        self.assertTrue(any(ln.startswith("problem: listener_down - ") for ln in lines), out.stdout)
        self.assertTrue(any(ln.startswith("problem: webhook_unconfigured - ") for ln in lines), out.stdout)
        self.assertIn("webhook: not configured (events are spooled until it is)", lines)
        self.assertIn("(missing)", [ln for ln in lines if ln.startswith("lark-cli env file:")][0])

    def test_stalled_spool_is_reported(self):
        spool = self.relay / "spool"
        spool.mkdir()
        path = spool / "pending.jsonl"
        path.write_text('{"message_id": "m1"}\n')
        old = time.time() - 300
        os.utime(path, (old, old))
        out = self.run_script("status.sh")
        self.assertIn("spool depth: 1", out.stdout)
        self.assertIn("problem: forwarding_stalled - 1 message(s) have waited more than a minute", out.stdout)

    def test_missing_lark_cli(self):
        # A PATH with python3 (via a private symlink) and the system basics, but no lark-cli.
        pybin = self.tmp / "pybin"
        pybin.mkdir()
        (pybin / "python3").symlink_to(shutil.which("python3"))
        path = os.pathsep.join([str(pybin), "/usr/bin", "/bin"])
        if shutil.which("lark-cli", path=path):
            self.skipTest("a real lark-cli is installed in /usr/bin or /bin")
        out = self.run_script("status.sh", env={"PATH": path})
        self.assertIn("problem: lark_cli_missing - reinstall it: npm install -g @larksuite/cli", out.stdout)


class BotOpenIdStatusTests(ShellCase):
    def test_missing_open_id_is_a_problem(self):
        proc = self.run_script("status.sh")
        self.assertIn("bot open_id: not set", proc.stdout)
        self.assertIn("problem: bot_open_id_missing - ", proc.stdout)

    def test_open_id_from_relay_env_is_enough(self):
        (self.relay / "relay.env").write_text("LARK_BOT_OPEN_ID=ou_test\n")
        proc = self.run_script("status.sh")
        self.assertIn("bot open_id: set", proc.stdout)
        self.assertNotIn("bot_open_id_missing", proc.stdout)
        self.assertNotIn("ou_test", proc.stdout)


@unittest.skipUnless(HAS_FLOCK, "start.sh and stop.sh need flock")
class StartStopTests(ShellCase):
    def test_start_twice_then_stop_twice(self):
        env = {"LARK_RELAY_READY_TIMEOUT": "30"}
        first = self.run_script("start.sh", env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("long connection ready", first.stdout)
        second = self.run_script("start.sh", env=env)
        self.assertEqual(second.returncode, 0)
        self.assertIn("relay already running", second.stdout)
        stopped = self.run_script("stop.sh")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("relay stopped", stopped.stdout)
        again = self.run_script("stop.sh")
        self.assertEqual(again.returncode, 0)
        self.assertIn("relay is not running", again.stdout)

    def lock_is_free(self) -> bool:
        lock = self.relay / "state" / "supervisor.lock"
        return subprocess.run(["flock", "-n", str(lock), "true"], env=self.env).returncode == 0

    def relay_processes(self) -> str:
        listing = subprocess.run(["ps", "-eo", "stat=,args="], capture_output=True, text=True).stdout
        return "\n".join(
            line for line in listing.splitlines()
            if str(self.relay / "relay.py") in line and not line.lstrip().startswith("Z")
        )

    def test_stop_ends_a_first_release_supervisor_that_survives_term(self):
        (self.relay / "supervisor.sh").write_text(LEGACY_SUPERVISOR)
        sup = subprocess.Popen(
            [str(self.relay / "supervisor.sh")],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.procs.append(sup)
        self.assertTrue(self.wait_for(lambda: self._pid_from("relay.pid") is not None))
        self.assertFalse(self.lock_is_free())
        stopped = self.run_script("stop.sh", env={"LARK_RELAY_STOP_GRACE": "4"}, timeout=90)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("relay stopped", stopped.stdout)
        self.assertIsNotNone(sup.wait(timeout=5))
        self.assertTrue(self.lock_is_free())
        self.assertEqual(self.relay_processes(), "")
        self.assertIsNone(self._pid_from("supervisor.pid"))

    def test_stop_finds_a_first_release_supervisor_that_deleted_its_pid_file(self):
        # After one TERM the first release removed supervisor.pid and kept running.
        (self.relay / "supervisor.sh").write_text(LEGACY_SUPERVISOR)
        sup = subprocess.Popen(
            [str(self.relay / "supervisor.sh")],
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.procs.append(sup)
        self.assertTrue(self.wait_for(lambda: self._pid_from("relay.pid") is not None))
        first_relay = self._pid_from("relay.pid")
        os.kill(sup.pid, signal.SIGTERM)
        self.assertTrue(
            self.wait_for(lambda: self._pid_from("relay.pid") not in (None, first_relay), timeout=20)
        )
        self.assertIsNone(self._pid_from("supervisor.pid"))
        self.assertIsNone(sup.poll())  # still running, still holding the lock
        stopped = self.run_script("stop.sh", env={"LARK_RELAY_STOP_GRACE": "4"}, timeout=120)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("relay stopped", stopped.stdout)
        self.assertIsNotNone(sup.wait(timeout=5))
        self.assertTrue(self.lock_is_free())
        self.assertEqual(self.relay_processes(), "")

    def test_stop_ends_a_relay_whose_supervisor_died(self):
        started = self.run_script("start.sh", env={"LARK_RELAY_READY_TIMEOUT": "30"})
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        sup, relay = self._pid_from("supervisor.pid"), self._pid_from("relay.pid")
        os.kill(sup, signal.SIGKILL)
        self.assertTrue(self.wait_for(lambda: process_gone(sup)))
        self.assertFalse(process_gone(relay))
        self.assertFalse(self.lock_is_free())  # relay.py inherited the lock
        stopped = self.run_script("stop.sh", env={"LARK_RELAY_STOP_GRACE": "3"}, timeout=90)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("relay stopped", stopped.stdout)
        self.assertTrue(self.wait_for(lambda: process_gone(relay)))
        self.assertTrue(self.lock_is_free())
        restarted = self.run_script("start.sh", env={"LARK_RELAY_READY_TIMEOUT": "30"})
        self.assertEqual(restarted.returncode, 0, restarted.stdout + restarted.stderr)
        self.assertIn("long connection ready", restarted.stdout)


if __name__ == "__main__":
    unittest.main()
