"""CI-only production-worker process boundary for the publication proof.

The caller owns freshly created PostgreSQL/Redis containers and supplies their
creation receipt plus fresh Docker inspections. This module creates no service,
changes no production setting and never substitutes a queue, worker or transport.
Its only executable command is the actual ``python -m app.worker.main`` entry.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


QUEUES = {"publication", "default", "transcode", "transcription", "index", "clip", "corpus_phrases"}
ID = re.compile(r"[a-f0-9]{64}")
RUN = re.compile(r"[a-f0-9]{32}")
LABEL_PREFIX = "org.3dhlab.loci.acceptance"
MAX_LOG_BYTES = 16 * 1024 * 1024


class WorkerProofRefused(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise WorkerProofRefused(message)


def _target(raw, *, database):
    try:
        value = urlsplit(raw)
        allowed = {"postgresql", "postgresql+psycopg"} if database else {"redis"}
        path = "/semantic_ci_publication_source" if database else "/15"
        need(value.scheme in allowed and value.hostname in {"127.0.0.1", "localhost", "::1"}
             and value.port is not None and 1 <= value.port <= 65535 and value.path == path
             and not value.query and not value.fragment and (not database or bool(value.username)),
             "Only the explicit loopback source PostgreSQL and Redis DB15 targets are permitted.")
        return value
    except (TypeError, ValueError):
        raise WorkerProofRefused("Invalid disposable source target.") from None


def require_worker_cli_environment(environ, receipt, inspections, *, platform=None):
    """Pure guard: no subprocess, socket, Docker call or mutation."""
    platform = sys.platform if platform is None else platform
    need(platform == "linux" and environ.get("GITHUB_ACTIONS") == "true"
         and environ.get("CI") == "true" and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
         and environ.get("LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE") == "true",
         "The worker proof requires explicit disposable GitHub-hosted Linux CI.")
    need(environ.get("SEMANTIC_ENV", "").lower() not in {"production", "prod", "public"},
         "Production/public mode is forbidden.")
    need(environ.get("DOCKER_HOST", "") in {"", "unix:///var/run/docker.sock"}
         and environ.get("DOCKER_CONTEXT", "") in {"", "default"}, "Remote Docker contexts are forbidden.")
    need(environ.get("EMBEDDING_ENABLED", "").lower() == "false"
         and environ.get("EMBEDDING_WARMUP_ENABLED", "").lower() == "false"
         and not environ.get("OPENAI_API_KEY") and not environ.get("SENTRY_DSN"),
         "External provider and monitoring credentials must be absent in the proof.")
    need(isinstance(receipt, dict) and receipt.get("version") == 1
         and isinstance(receipt.get("run_id"), str) and RUN.fullmatch(receipt["run_id"])
         and receipt.get("redis_initially_empty") is True,
         "A fresh owned-service creation and initially empty Redis receipt is required.")
    for role, variable, field, port in (
        ("postgres", "DATABASE_URL", "source_database_url", "5432/tcp"),
        ("redis", "REDIS_URL", "source_redis_url", "6379/tcp"),
    ):
        need(environ.get(variable) == receipt.get(field) and isinstance(receipt.get(field), str),
             "The actual application environment differs from the owned source receipt.")
        target = _target(receipt[field], database=role == "postgres")
        expected = receipt.get("containers", {}).get(role, {})
        observed = inspections.get(role, {})
        need(expected.get("created_by_run") is True and expected.get("initial_absence_verified") is True
             and isinstance(expected.get("id"), str) and ID.fullmatch(expected["id"])
             and isinstance(expected.get("image_id"), str) and re.fullmatch(r"sha256:[a-f0-9]{64}", expected["image_id"])
             and isinstance(expected.get("name"), str) and re.fullmatch(r"loci-[A-Za-z0-9_.-]{1,120}", expected["name"]),
             "Only positively created, named proof containers may be used.")
        label = expected.get("label_key", "")
        need(isinstance(label, str) and (label == LABEL_PREFIX or label.startswith(LABEL_PREFIX + "."))
             and expected.get("label_value") == receipt["run_id"], "Owned container labels differ.")
        need(observed.get("Id") == expected["id"] and observed.get("Name") == "/" + expected["name"]
             and observed.get("Image") == expected["image_id"]
             and observed.get("Config", {}).get("Labels", {}).get(label) == receipt["run_id"]
             and observed.get("State", {}).get("Running") is True,
             "Fresh Docker inspection does not identify the owned running source container.")
        host = observed.get("HostConfig", {})
        need(host.get("Privileged") is False and host.get("NetworkMode") != "host"
             and host.get("PidMode", "") != "host" and not host.get("Binds"),
             "Privileged, host-network/PID or host-bind service scopes are forbidden.")
        for mounts in (host.get("Mounts", []), observed.get("Mounts", [])):
            need(isinstance(mounts, list) and all(isinstance(mount, dict)
                 and str(mount.get("Type", "")).lower() != "bind" for mount in mounts),
                 "Host-bind service mounts are forbidden in both Docker mount representations.")
        ports = observed.get("NetworkSettings", {}).get("Ports", {})
        published = {key: values for key, values in ports.items() if values}
        bindings = published.get(port)
        allowed_ip = "::1" if target.hostname == "::1" else "127.0.0.1"
        need(set(published) == {port} and isinstance(bindings, list) and len(bindings) == 1
             and bindings[0].get("HostIp") == allowed_ip and bindings[0].get("HostPort") == str(target.port),
             "The owned source port is not its exact loopback-only publication.")
    return {"run_id": receipt["run_id"], "queues": sorted(QUEUES), "scope": "owned-source-worker-CLI-only"}


def require_worker_registration(registration, pid):
    """Check the actual RQ hash from the fresh dedicated Redis container."""
    values = {(key.decode() if isinstance(key, bytes) else key):
              (value.decode() if isinstance(value, bytes) else value) for key, value in registration.items()}
    queues = str(values.get("queues", "")).split(",")
    need(type(pid) is int and pid > 0 and values.get("pid") == str(pid)
         and set(queues) == QUEUES and len(queues) == len(QUEUES) and not values.get("death"),
         "Production worker PID/queue registration is absent or differs.")
    return True


def _linux_rows():
    rows = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            rest = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            command = (directory / "cmdline").read_bytes()
            rows[int(directory.name)] = {"ppid": int(rest[1]), "birth": int(rest[19]),
                "uid": directory.stat().st_uid, "state": rest[0],
                "command_sha256": hashlib.sha256(command).hexdigest(),
                "spawn_main": b"multiprocessing.spawn" in command and b"spawn_main" in command}
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
            continue
    return rows


def _private_directory(path):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path, "Literal private output directory required.")
    info = path.stat()
    need(path.is_dir() and info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700,
         "Private owned output directory required.")
    return path


class ProductionWorkerCLI:
    """One directly returned CLI process and only its observed descendants."""
    def __init__(self, api_dir, environ, receipt, inspections, logs_dir):
        self.guard = require_worker_cli_environment(environ, receipt, inspections)
        need(sys.version_info[:2] == (3, 12), "The supported Python3.12 ABI is required.")
        self.api = Path(api_dir)
        need(self.api.is_absolute() and self.api.resolve() == self.api, "Literal API source directory required.")
        need(not (self.api / ".env").exists() and not (self.api / ".env").is_symlink(),
             "The worker proof must use the explicit environment, not an API .env file.")
        for name, field in (("app/worker/main.py", "worker_main_sha256"),
                            ("app/services/publication_jobs.py", "publication_jobs_sha256")):
            path = self.api / name
            need(path.is_file() and not path.is_symlink() and isinstance(receipt.get(field), str)
                 and hashlib.sha256(path.read_bytes()).hexdigest() == receipt[field],
                 "The actual worker source ABI differs from its candidate receipt.")
        need(not environ.get("PYTHONHOME") and all(Path(item).resolve() == self.api
             for item in environ.get("PYTHONPATH", "").split(os.pathsep) if item),
             "Ambient Python source overrides are forbidden.")
        self.env = dict(environ)
        self.logs = Path(logs_dir)
        _private_directory(self.logs.parent)
        need(not self.logs.exists() and not self.logs.is_symlink(), "Fresh worker logs required.")
        self.logs.mkdir(mode=0o700)
        self.process = None
        self.known = {}
        self.initial = _linux_rows()
        need(self.initial.get(os.getpid(), {}).get("uid") == os.getuid(),
             "Native Linux process ownership inspection is unavailable.")
        self.forced = False
        self.cleanup = False
        self.violations = []
        self.stop_record = None
        self.argv = [sys.executable, "-m", "app.worker.main"]

    def start(self):
        need(self.process is None, "This CLI handle cannot be reused or adopted.")
        out = self.logs / "stdout.log"
        err = self.logs / "stderr.log"
        with out.open("xb") as stdout, err.open("xb") as stderr:
            os.chmod(out, 0o600); os.chmod(err, 0o600)
            self.process = subprocess.Popen(self.argv, cwd=self.api, env=self.env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True)
        # The direct returned handle is retained before any fallible write or
        # observation; every such failure enters exact-owned cleanup below.
        try:
            self._save("created")
            rows = _linux_rows()
            row = rows.get(self.process.pid)
            need(row is not None and row["uid"] == os.getuid()
                 and self.initial.get(self.process.pid, {}).get("birth") != row["birth"],
                 "The returned CLI process could not be independently identified.")
            self.known[self.process.pid] = row
            self.observe()
            self._save("started")
            return self
        except BaseException:
            # This fallback owns only the direct Popen child. It grants no
            # descendant adoption and cannot count as accepted CLI startup.
            try:
                if self.known:
                    self.stop()
                else:
                    if self.process.poll() is None:
                        self.process.kill()
                    self.process.wait(timeout=3)
            finally:
                self._save("startup-refused")
            raise

    def observe(self):
        need(self.process is not None, "No owned CLI process was started.")
        rows = _linux_rows()
        active = {pid: row for pid, row in rows.items() if pid in self.known
                  and row["birth"] == self.known[pid]["birth"] and row["uid"] == os.getuid()}
        changed = True
        while changed:
            changed = False
            for pid, row in rows.items():
                if pid in active or row["ppid"] not in active:
                    continue
                need(row["uid"] == os.getuid() and self.initial.get(pid, {}).get("birth") != row["birth"],
                     "An uncertain or pre-existing process cannot be adopted.")
                self.known[pid] = row; active[pid] = row; changed = True
        breaches = []
        if len(active) > 64:
            breaches.append("Owned worker process count exceeded64.")
        if sum(p.stat().st_size for p in self.logs.iterdir() if p.is_file()) > MAX_LOG_BYTES:
            breaches.append("Combined worker output exceeded16MiB.")
        if breaches:
            if not self.cleanup:
                raise WorkerProofRefused(" ".join(breaches))
            self.violations.extend(item for item in breaches if item not in self.violations)
        return {pid: row for pid, row in active.items() if row["state"] != "Z"}

    def dispatcher_observed(self):
        return any(row["spawn_main"] for pid, row in self.observe().items() if pid != self.process.pid)

    def _signal(self, pid, which):
        current = _linux_rows().get(pid)
        known = self.known.get(pid)
        need(type(pid) is int and pid > 0 and known is not None,
             "Refusing a signal to an unknown process.")
        if current is None or current["state"] == "Z":
            return
        need(current["birth"] == known["birth"] and current["uid"] == os.getuid(),
             "Refusing a signal to a foreign or reused process.")
        try:
            os.kill(pid, which)
        except ProcessLookupError:
            pass

    def _save(self, status):
        record = {"status": status, "run_id": self.guard["run_id"], "argv": self.argv,
            "pid": self.process.pid, "returncode": self.process.poll(), "forced_cleanup": self.forced,
            "environment_sha256": hashlib.sha256(json.dumps(self.env, sort_keys=True).encode()).hexdigest(),
            "observed_owned_processes": self.known, "raw_environment_saved": False,
            "violations": self.violations,
            "clean_shutdown": status == "stopped" and self.process.returncode == 0
                              and not self.forced and not self.violations}
        path = self.logs / (status + ".json")
        with path.open("x") as stream:
            os.chmod(path, 0o600)
            json.dump(record, stream, indent=2); stream.write("\n")
        return record

    def stop(self):
        need(self.process is not None, "No owned CLI process to stop.")
        self.cleanup = True
        if self.stop_record is not None:
            need(not self.observe(), "Owned CLI process reappeared after shutdown.")
            return self.stop_record
        self.observe()
        if self.process.poll() is None:
            self._signal(self.process.pid, signal.SIGTERM)
        end = time.monotonic() + 12
        while time.monotonic() < end:
            active = self.observe()
            if self.process.poll() is not None and not active:
                self.process.wait(timeout=1)
                self.stop_record = self._save("stopped")
                return self.stop_record
            time.sleep(0.1)
        self.forced = True
        # Only exact observed positive PIDs, never a process-group/ambient kill.
        for which in (signal.SIGTERM, signal.SIGKILL):
            for pid in sorted(self.observe(), reverse=True):
                self._signal(pid, which)
            end = time.monotonic() + 3
            while time.monotonic() < end and self.observe():
                self.process.poll(); time.sleep(0.1)
        self.process.wait(timeout=1)
        need(not self.observe(), "Owned CLI descendants remain; cleanup is incomplete.")
        self.stop_record = self._save("stopped")
        return self.stop_record
