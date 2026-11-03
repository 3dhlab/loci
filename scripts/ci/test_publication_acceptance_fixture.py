"""Pure target-guard and fixture-assertion checks; no DB, service or encoder."""
from copy import deepcopy
import builtins
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publication_acceptance_fixture as fixture


class FixtureTargetGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="loci-fixture-guard-", dir=os.environ.get("LOCI_TEST_FILE_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source, self.target = self.root / "source-media", self.root / "target-media"
        self.source.mkdir(mode=0o700); self.target.mkdir(mode=0o700)
        self.env = {"SEMANTIC_ENV": "development", "LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE": "true",
            "CI_PUBLICATION_SOURCE_DATABASE_URL": "postgresql+psycopg://generated:synthetic@127.0.0.1:15432/semantic_ci_publication_source",
            "CI_PUBLICATION_TARGET_DATABASE_URL": "postgresql+psycopg://generated:synthetic@127.0.0.1:25432/semantic_ci_publication_target",
            "CI_PUBLICATION_SOURCE_MEDIA_ROOT": str(self.source), "CI_PUBLICATION_TARGET_MEDIA_ROOT": str(self.target),
            "MEDIA_ROOT": str(self.source), "EMBEDDING_ENABLED": "false", "EMBEDDING_WARMUP_ENABLED": "false",
            "OPENAI_API_KEY": "", "LOCI_PUBLICATION_TEST_PASSWORD": "synthetic-test-password-32-bytes-only"}
        self.env["DATABASE_URL"] = self.env["CI_PUBLICATION_SOURCE_DATABASE_URL"]

    def test_named_separate_loopback_targets_allow_random_controller_verified_ports(self):
        selected = fixture.require_fixture_environment(self.env)
        self.assertEqual(selected.source_media, self.source)
        self.assertNotIn(self.env["LOCI_PUBLICATION_TEST_PASSWORD"], repr(selected))
        self.assertNotIn("synthetic@", repr(selected))

    def test_explicit_development_and_opt_in_are_required(self):
        for name, value in (("SEMANTIC_ENV", "production"), ("SEMANTIC_ENV", "prod"),
                            ("SEMANTIC_ENV", ""), ("LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE", "false")):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                fixture.require_fixture_environment({**self.env, name: value})

    def test_external_wrong_named_or_option_overridden_database_is_refused(self):
        for url in ("postgresql://role:private-password@remote.example/semantic_ci_publication_source",
                "postgresql://role@127.0.0.1/semantic", "postgresql://role@127.0.0.1/semantic_ci_publication_source?host=remote.example",
                "postgresql://role@localhost:bad/semantic_ci_publication_source", "postgresql://localhost/semantic_ci_publication_source",
                "postgresql://role@127.0.0.1:/semantic_ci_publication_source"):
            env = {**self.env, "CI_PUBLICATION_SOURCE_DATABASE_URL": url, "DATABASE_URL": url}
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                fixture.require_fixture_environment(env)
            self.assertNotIn("private-password", str(error.exception))

    def test_docker_service_requires_explicit_disposable_opt_in(self):
        env = {**self.env, "CI_PUBLICATION_SOURCE_DATABASE_URL": "postgresql://role@postgres/semantic_ci_publication_source"}
        env["DATABASE_URL"] = env["CI_PUBLICATION_SOURCE_DATABASE_URL"]
        with self.assertRaises(ValueError): fixture.require_fixture_environment(env)
        fixture.require_fixture_environment({**env, "CI_DISPOSABLE_SERVICES": "true"})

    def test_effective_database_and_media_settings_must_match_explicit_source(self):
        for name, value in (("DATABASE_URL", self.env["CI_PUBLICATION_TARGET_DATABASE_URL"]),
                ("MEDIA_ROOT", str(self.target)), ("CI_PUBLICATION_TARGET_MEDIA_ROOT", str(self.source))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fixture.require_fixture_environment({**self.env, name: value})

    def test_media_roots_must_be_private_siblings_owned_by_current_user(self):
        self.source.chmod(0o755)
        with self.assertRaises(ValueError): fixture.require_fixture_environment(self.env)
        self.source.chmod(0o700)
        with patch.object(fixture.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError): fixture.require_fixture_environment(self.env)
        alias = self.root / "alias"; alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError): fixture.require_fixture_environment({**self.env,
            "CI_PUBLICATION_SOURCE_MEDIA_ROOT": str(alias), "MEDIA_ROOT": str(alias)})

    def test_embedding_network_inputs_and_bcrypt_password_limits_are_closed(self):
        for name, value in (("EMBEDDING_ENABLED", "true"), ("EMBEDDING_WARMUP_ENABLED", "true"),
                ("OPENAI_API_KEY", "synthetic-not-real"), ("LOCI_PUBLICATION_TEST_PASSWORD", "short"),
                ("LOCI_PUBLICATION_TEST_PASSWORD", "x" * 73), ("LOCI_PUBLICATION_TEST_PASSWORD", "x" * 31 + "\n")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fixture.require_fixture_environment({**self.env, name: value})

    def test_failed_preflight_precedes_all_database_application_or_encoder_imports(self):
        real_import = builtins.__import__; seen = []
        def guarded_import(name, *args, **kwargs):
            if name == "sqlalchemy" or name == "app" or name.startswith("app."):
                seen.append(name); raise AssertionError("Guard allowed application import.")
            return real_import(name, *args, **kwargs)
        with patch.dict(os.environ, {**self.env, "SEMANTIC_ENV": "production"}), patch("builtins.__import__", guarded_import):
            with self.assertRaises(ValueError): fixture.seed_fixture()
        self.assertEqual(seen, [])


class SeedEligibilityTests(unittest.TestCase):
    def eligible(self):
        return {"SEMANTIC_ENV": "development", "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted", "CI_DISPOSABLE_SERVICES": "true",
            "LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE": "true"}

    def test_injected_linux_github_hosted_explicit_disposable_markers_are_required(self):
        env = self.eligible()
        fixture.require_seed_eligibility(env, platform="linux")
        for platform in ("darwin", "win32", "freebsd", "linux2", ""):
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                fixture.require_seed_eligibility(env, platform=platform)
        for name, value in (("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                ("CI_DISPOSABLE_SERVICES", "false"), ("LOCI_PUBLICATION_ACCEPTANCE_DISPOSABLE", "false"),
                ("SEMANTIC_ENV", "production")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fixture.require_seed_eligibility({**env, name: value}, platform="linux")
            missing = dict(env); missing.pop(name)
            with self.assertRaises(ValueError): fixture.require_seed_eligibility(missing, platform="linux")

    def test_mac_non_ci_and_self_hosted_seed_refusals_precede_app_import_target_checks_and_writes(self):
        actual_gate = fixture.require_seed_eligibility
        real_import = builtins.__import__; seen = []
        def guarded_import(name, *args, **kwargs):
            if name == "sqlalchemy" or name == "app" or name.startswith("app."):
                seen.append(name); raise AssertionError("Unsafe runner reached application import.")
            return real_import(name, *args, **kwargs)
        for platform, env in (("darwin", self.eligible()), ("linux", {}),
                ("linux", {**self.eligible(), "RUNNER_ENVIRONMENT": "self-hosted"})):
            with self.subTest(platform=platform, env=env):
                with patch.object(fixture, "require_seed_eligibility", side_effect=lambda: actual_gate(env, platform=platform)), \
                     patch.object(fixture, "require_fixture_environment", side_effect=AssertionError("Unsafe runner reached target checks.")) as targets, \
                     patch("builtins.__import__", guarded_import), \
                     patch.object(fixture.Path, "mkdir", side_effect=AssertionError("Unsafe runner wrote files.")) as mkdir, \
                     patch.object(fixture.subprocess, "run", side_effect=AssertionError("Unsafe runner started an encoder.")) as encoder:
                    with self.assertRaises(ValueError): fixture.seed_fixture()
                    targets.assert_not_called(); mkdir.assert_not_called(); encoder.assert_not_called()
        self.assertEqual(seen, [])

    def test_expectations_and_pure_assertions_remain_platform_independent(self):
        with patch.object(fixture, "require_seed_eligibility", side_effect=AssertionError("Portable helper required seed eligibility.")):
            self.assertEqual(fixture.fixture_expectations()["version"], 1)
            fixture.assert_projection(*selected_projection())


def selected_projection():
    ids = fixture.IDS
    manifest = {"projects": [{"id": ids["project"]}], "objects": [{"id": ids["object"]}],
        "transcripts": [{"id": ids["transcript"]}], "segments": [{"id": ids[f"segment{i}"]} for i in (1, 2, 3)],
        "videos": [{"id": ids["video"], "public_media_relpath": f"transcoded/{ids['video']}.{'a'*64}.{'1'*32}.mp4",
            "publication_attribution_json": [{"start_ms": 0, "end_ms": None, "payload": dict(fixture.PUBLIC_ATTRIBUTION)}]}],
        "object_models": [{"id": ids["model"], "public_media_relpath": "fixture/cube.glb", "public_poster_relpath": "fixture/poster.png"}],
        "clips": [{"id": ids[f"clip{i}"], "public_media_relpath": f"fixture/clip-{i}.mp4",
            "public_poster_relpath": f"fixture/clip-{i}.jpg", "public_transcript_relpath": f"fixture/clip-{i}.transcript.txt"} for i in (1, 2)],
        "object_model_annotations": [{"id": ids["annotation"], "playlist_json": [{"label": "first"}, {"label": "last"}]}]}
    paths = [manifest["videos"][0]["public_media_relpath"], "fixture/cube.glb", "fixture/poster.png"]
    for clip in manifest["clips"]:
        paths.extend(clip[name] for name in ("public_media_relpath", "public_poster_relpath", "public_transcript_relpath"))
    manifest["media_paths"] = paths
    return manifest, paths


class FixtureAssertionsTests(unittest.TestCase):
    def test_safe_expectations_have_real_query_names_without_credentials_or_raw_title_cards(self):
        expected = fixture.fixture_expectations(); encoded = json.dumps(expected)
        self.assertIn("annotation_id=", expected["public_paths"]["annotation"])
        self.assertIn("clip_id=", expected["public_paths"]["clip"])
        for value in ("password", "token", "raw_text_observed", "raw_payload_json", *fixture.PRIVATE_MARKERS):
            self.assertNotIn(value, encoded)
        expected["public_attribution"]["speaker_label"] = "changed copy"
        self.assertEqual(fixture.PUBLIC_ATTRIBUTION["speaker_label"], "Synthetic Public Speaker")

    def test_exact_public_fixture_contract_accepts_all_nine_media_files(self):
        manifest, paths = selected_projection()
        fixture.assert_projection(manifest, paths)

    def test_projection_rejects_private_selection_raw_titlecard_and_missing_media(self):
        original, paths = selected_projection()
        mutations = [lambda value: value["objects"].append({"id": fixture.IDS["private_object"]}),
            lambda value: value.update(description=fixture.PRIVATE_MARKERS[3]),
            lambda value: value.update(title_card_observations=[]),
            lambda value: value["videos"][0].update(publication_attribution_json=[]),
            lambda value: value["object_model_annotations"][0].update(playlist_json=[])]
        for mutate in mutations:
            value = deepcopy(original); mutate(value)
            with self.subTest(mutate=mutate), self.assertRaises(AssertionError): fixture.assert_projection(value, paths)
        with self.assertRaises(AssertionError): fixture.assert_projection(original, paths[:-1])

    def test_public_snapshot_requires_private_404s_and_exact_approved_fields(self):
        objects = [{"id": fixture.IDS["object"]}]
        evidence = {"object": {"id": fixture.SLUG}, "playback": {"video_id": fixture.VIDEO_SLUG},
            "annotations": [{"id": fixture.ANNOTATION_SLUG}], "clips": [{"id": value} for value in fixture.CLIP_SLUGS],
            "transcript": {"segments": [{"text": text} for text in fixture.TEXTS]},
            "citation_attribution": dict(fixture.PUBLIC_ATTRIBUTION)}
        statuses = {path: 404 for path in fixture.fixture_expectations()["private_paths"]}
        fixture.assert_public_snapshot(objects, evidence, statuses)
        with self.assertRaises(AssertionError): fixture.assert_public_snapshot(objects, evidence, {})
        statuses[next(iter(statuses))] = 200
        with self.assertRaises(AssertionError): fixture.assert_public_snapshot(objects, evidence, statuses)
        statuses = {path: 404 for path in fixture.fixture_expectations()["private_paths"]}
        evidence["citation_attribution"]["speaker_label"] = fixture.PRIVATE_MARKERS[2]
        with self.assertRaises(AssertionError): fixture.assert_public_snapshot(objects, evidence, statuses)


if __name__ == "__main__":
    unittest.main()
