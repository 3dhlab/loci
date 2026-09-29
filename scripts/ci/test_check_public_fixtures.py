"""Negative tests for the scoped public-fixture CI guard."""

from pathlib import Path
import runpy
import tempfile
import unittest

check = runpy.run_path(str(Path(__file__).with_name("check-public-fixtures.py")))["check"]


class PublicFixtureGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="loci-fixture-guard-")
        self.root = Path(self.temporary.name)
        self.paths = [
            "README.md",
            "apps/web/src/lib/citationAttribution.test.js",
            "apps/api/tests/test_authoring_api.py",
        ]
        self.write("README.md", "Public project documentation.\n")
        self.write(
            "apps/web/src/lib/citationAttribution.test.js",
            "collectionName: 'Synthetic Demonstration Collection'\n"
            "speakerLabel: 'Avery Example'\n"
            "speakerAuthorName: 'Example, Avery'\n",
        )
        self.write("apps/api/tests/test_authoring_api.py", 'stable_video_id="video-test-sample"\n')

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        if relative not in self.paths:
            self.paths.append(relative)

    def test_accepts_synthetic_test_data(self):
        self.assertEqual(check(self.root, self.paths), [])

    def test_rejects_unreviewed_browser_media(self):
        self.write("apps/web/tests/fixtures/captured-interview.mp4", b"synthetic media bytes")
        findings = check(self.root, self.paths)
        self.assertTrue(any("new browser media fixture needs a generation recipe and review" in item for item in findings))

    def test_rejects_runtime_media_path(self):
        self.write("apps/api/.media/local-recording.mov", b"local media bytes")
        findings = check(self.root, self.paths)
        self.assertTrue(any("tracked runtime or private file" in item for item in findings))

    def test_rejects_non_synthetic_citation_identity(self):
        self.write(
            "apps/web/src/lib/citationAttribution.test.js",
            "collectionName: 'Community Interview Archive'\n",
        )
        findings = check(self.root, self.paths)
        self.assertTrue(any("collectionName in citation test is not plainly synthetic" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
