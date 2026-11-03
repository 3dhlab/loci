"""Pure guards only: these tests never create services or start the worker."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import publication_worker_cli as proof


def fixture():
    run = "e" * 32
    database = "postgresql+psycopg://generated:generated@127.0.0.1:25432/semantic_ci_publication_source"
    redis = "redis://127.0.0.1:26379/15"
    env = {"GITHUB_ACTIONS": "true", "CI": "true", "RUNNER_ENVIRONMENT": "github-hosted",
        "LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE": "true", "SEMANTIC_ENV": "test",
        "DATABASE_URL": database, "REDIS_URL": redis, "EMBEDDING_ENABLED": "false", "EMBEDDING_WARMUP_ENABLED": "false"}
    receipt = {"version": 1, "run_id": run, "redis_initially_empty": True,
        "source_database_url": database, "source_redis_url": redis, "containers": {}}
    inspections = {}
    for role, digit, port, published in (("postgres", "a", "5432/tcp", "25432"), ("redis", "b", "6379/tcp", "26379")):
        item = {"id": digit * 64, "image_id": "sha256:" + digit * 64, "name": "loci-source-" + role,
            "label_key": "org.3dhlab.loci.acceptance.run", "label_value": run,
            "created_by_run": True, "initial_absence_verified": True}
        receipt["containers"][role] = item
        inspections[role] = {"Id": item["id"], "Image": item["image_id"], "Name": "/" + item["name"],
            "Config": {"Labels": {item["label_key"]: run}}, "State": {"Running": True},
            "HostConfig": {"Privileged": False, "NetworkMode": "loci-private-proof", "Binds": None},
            "NetworkSettings": {"Ports": {port: [{"HostIp": "127.0.0.1", "HostPort": published}]}}}
    return env, receipt, inspections


class WorkerCLIEnvironmentGuardTests(unittest.TestCase):
    def check(self, env, receipt, inspections, **kwargs):
        return proof.require_worker_cli_environment(env, receipt, inspections, platform="linux", **kwargs)

    def test_exact_owned_fresh_loopback_services_are_accepted_by_pure_guard(self):
        env, receipt, inspections = fixture()
        result = self.check(env, receipt, inspections)
        self.assertEqual(result["run_id"], receipt["run_id"])
        self.assertEqual(set(result["queues"]), proof.QUEUES)

    def test_mac_and_non_ci_are_refused_before_any_runtime_action(self):
        env, receipt, inspections = fixture()
        with self.assertRaises(proof.WorkerProofRefused):
            proof.require_worker_cli_environment(env, receipt, inspections, platform="darwin")
        for key in ("GITHUB_ACTIONS", "CI", "LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE", "RUNNER_ENVIRONMENT"):
            other = dict(env); other.pop(key)
            with self.subTest(key=key), self.assertRaises(proof.WorkerProofRefused):
                self.check(other, receipt, inspections)

    def test_production_modes_are_refused(self):
        env, receipt, inspections = fixture()
        for value in ("production", "prod", "public"):
            with self.subTest(value=value), self.assertRaises(proof.WorkerProofRefused):
                self.check({**env, "SEMANTIC_ENV": value}, receipt, inspections)

    def test_remote_docker_and_external_provider_credentials_are_refused(self):
        env, receipt, inspections = fixture()
        for key, value in (("DOCKER_HOST", "tcp://example.invalid:2375"), ("DOCKER_CONTEXT", "remote"),
                           ("OPENAI_API_KEY", "generated-forbidden"), ("SENTRY_DSN", "https://example.invalid"),
                           ("EMBEDDING_WARMUP_ENABLED", "true"), ("EMBEDDING_ENABLED", "true")):
            with self.subTest(key=key), self.assertRaises(proof.WorkerProofRefused):
                self.check({**env, key: value}, receipt, inspections)

    def test_actual_app_targets_must_match_the_receipt(self):
        env, receipt, inspections = fixture()
        for key in ("DATABASE_URL", "REDIS_URL"):
            with self.subTest(key=key), self.assertRaises(proof.WorkerProofRefused):
                self.check({**env, key: env[key].replace("127.0.0.1", "example.invalid")}, receipt, inspections)

    def test_wrong_database_redis_db_remote_query_and_fragment_are_refused(self):
        env, receipt, inspections = fixture()
        for variable, field, change in (
            ("DATABASE_URL", "source_database_url", lambda x: x.replace("semantic_ci_publication_source", "production")),
            ("DATABASE_URL", "source_database_url", lambda x: x.replace("127.0.0.1", "example.invalid")),
            ("REDIS_URL", "source_redis_url", lambda x: x.replace("/15", "/0")),
            ("REDIS_URL", "source_redis_url", lambda x: x + "?db=0"),
            ("REDIS_URL", "source_redis_url", lambda x: x + "#fragment"),
            ("REDIS_URL", "source_redis_url", lambda x: x.replace(":26379", ":70000")),
        ):
            altered = copy.deepcopy(receipt); target = change(env[variable]); altered[field] = target
            with self.subTest(target=field), self.assertRaises(proof.WorkerProofRefused):
                self.check({**env, variable: target}, altered, inspections)

    def test_unproved_freshness_or_nonempty_redis_receipt_is_refused(self):
        env, receipt, inspections = fixture()
        for role, field in (("redis", "created_by_run"), ("postgres", "initial_absence_verified")):
            altered = copy.deepcopy(receipt); altered["containers"][role][field] = False
            with self.subTest(field=field), self.assertRaises(proof.WorkerProofRefused):
                self.check(env, altered, inspections)
        altered = {**receipt, "redis_initially_empty": False}
        with self.assertRaises(proof.WorkerProofRefused): self.check(env, altered, inspections)

    def test_changed_id_image_label_name_or_stopped_container_is_refused(self):
        env, receipt, inspections = fixture()
        alterations = [
            lambda x: x["redis"].update(Id="f" * 64),
            lambda x: x["redis"].update(Image="sha256:" + "f" * 64),
            lambda x: x["redis"].update(Name="/ambient-redis"),
            lambda x: x["redis"]["Config"]["Labels"].clear(),
            lambda x: x["redis"]["State"].update(Running=False),
        ]
        for modify in alterations:
            changed = copy.deepcopy(inspections); modify(changed)
            with self.assertRaises(proof.WorkerProofRefused): self.check(env, receipt, changed)

    def test_wildcard_port_wrong_port_or_extra_publication_is_refused(self):
        env, receipt, inspections = fixture()
        for binding in ({"HostIp": "0.0.0.0", "HostPort": "26379"},
                        {"HostIp": "127.0.0.1", "HostPort": "6379"}):
            changed = copy.deepcopy(inspections)
            changed["redis"]["NetworkSettings"]["Ports"]["6379/tcp"] = [binding]
            with self.assertRaises(proof.WorkerProofRefused): self.check(env, receipt, changed)
        changed = copy.deepcopy(inspections)
        changed["redis"]["NetworkSettings"]["Ports"]["8080/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "22222"}]
        with self.assertRaises(proof.WorkerProofRefused): self.check(env, receipt, changed)

    def test_privilege_host_network_pid_and_bind_mount_are_refused(self):
        env, receipt, inspections = fixture()
        for key, value in (("Privileged", True), ("NetworkMode", "host"), ("PidMode", "host"),
                           ("Binds", ["/generated-private:/data"])):
            changed = copy.deepcopy(inspections); changed["redis"]["HostConfig"][key] = value
            with self.subTest(key=key), self.assertRaises(proof.WorkerProofRefused):
                self.check(env, receipt, changed)

    def test_hostconfig_mount_and_inspected_mount_bind_forms_are_refused(self):
        env, receipt, inspections = fixture()
        for location in ("HostConfig", "inspection"):
            changed = copy.deepcopy(inspections)
            target = changed["redis"]["HostConfig"] if location == "HostConfig" else changed["redis"]
            target["Mounts"] = [{"Type": "bind", "Source": "/var/run/docker.sock", "Target": "/socket"}]
            with self.subTest(location=location), self.assertRaises(proof.WorkerProofRefused):
                self.check(env, receipt, changed)

    def test_embedding_switch_is_required_even_without_provider_credentials(self):
        env, receipt, inspections = fixture()
        for value in (None, "true", "1"):
            changed = dict(env)
            if value is None:
                changed.pop("EMBEDDING_ENABLED")
            else:
                changed["EMBEDDING_ENABLED"] = value
            with self.subTest(value=value), self.assertRaises(proof.WorkerProofRefused):
                self.check(changed, receipt, inspections)

    def test_only_the_owner_label_namespace_is_accepted(self):
        env, receipt, inspections = fixture()
        changed = copy.deepcopy(receipt)
        changed["containers"]["redis"]["label_key"] = "ambient.label"
        with self.assertRaises(proof.WorkerProofRefused): self.check(env, changed, inspections)

    def test_constructor_cannot_start_or_create_logs_on_mac(self):
        env, receipt, inspections = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(proof.sys, "platform", "darwin"), \
                patch.object(proof.subprocess, "Popen", side_effect=AssertionError("No process may start")):
            output = Path(directory) / "never-created"
            with self.assertRaises(proof.WorkerProofRefused):
                proof.ProductionWorkerCLI(Path(directory), env, receipt, inspections, output)
            self.assertFalse(output.exists())


class ReturnedCLIHandleCleanupTests(unittest.TestCase):
    def test_created_receipt_write_failure_still_reaps_only_the_returned_handle(self):
        from types import SimpleNamespace

        calls = []
        child = SimpleNamespace(pid=12345, returncode=None)
        child.poll = lambda: child.returncode
        def kill():
            calls.append("kill-returned-child")
            child.returncode = -9
        def wait(**kwargs):
            calls.append("wait-returned-child")
            return child.returncode
        child.kill, child.wait = kill, wait
        with tempfile.TemporaryDirectory() as directory:
            handle = object.__new__(proof.ProductionWorkerCLI)
            handle.process = None
            handle.known = {}
            handle.logs = Path(directory)
            handle.api, handle.env = Path(directory), {}
            handle.argv = [proof.sys.executable, "-m", "app.worker.main"]
            def save(status):
                calls.append(status)
                if status == "created":
                    raise OSError("generated receipt-write failure")
            handle._save = save
            with patch.object(proof.subprocess, "Popen", return_value=child) as popen, \
                    patch.object(proof, "_linux_rows", side_effect=AssertionError("No observation after injected write failure")):
                with self.assertRaisesRegex(OSError, "receipt-write failure"):
                    handle.start()
                self.assertEqual(popen.call_args.args[0], handle.argv)
            self.assertEqual(calls, ["created", "kill-returned-child", "wait-returned-child", "startup-refused"])
            self.assertEqual(handle.known, {}, "No guessed descendant ownership may be created.")


class ProductionRegistrationGuardTests(unittest.TestCase):
    def test_real_queue_set_and_exact_cli_pid_are_required(self):
        registration = {b"pid": b"12345", b"queues": ",".join(sorted(proof.QUEUES)).encode()}
        self.assertTrue(proof.require_worker_registration(registration, 12345))
        with self.assertRaises(proof.WorkerProofRefused): proof.require_worker_registration(registration, 999)

    def test_missing_queue_duplicate_queue_or_death_record_is_not_live_acceptance(self):
        for queues, extra in (("publication", {}), (",".join(sorted(proof.QUEUES)) + ",publication", {}),
                              (",".join(sorted(proof.QUEUES)), {"death": "generated-time"})):
            with self.assertRaises(proof.WorkerProofRefused):
                proof.require_worker_registration({"pid": "12345", "queues": queues, **extra}, 12345)


if __name__ == "__main__":
    unittest.main()
