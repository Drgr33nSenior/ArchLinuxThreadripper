"""Local model cache (infrastructure/ai-stack/models/cache.py): verified, atomic, no relics."""
import contextlib
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infrastructure/ai-stack/models"))
import cache  # noqa: E402


def blob_sha1(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.nas, self.cache = base / "nas", base / "cache"
        self.cache.mkdir()
        weights, config = b"w" * 4096, b'{"model": "tiny"}\n'
        self.entry = {"model": {"repository": "org/model", "revision": "a" * 40, "files": [
                          {"path": "model.safetensors", "size": len(weights), "sha256": hashlib.sha256(weights).hexdigest()},
                          {"path": "config.json", "size": len(config), "git_sha1": blob_sha1(config)}]},
                      "draft": {"repository": "org/draft", "revision": "b" * 40, "files": [
                          {"path": "draft.safetensors", "size": 8, "sha256": hashlib.sha256(b"draftwts").hexdigest()}]}}
        for spec, files in ((self.entry["model"], {"model.safetensors": weights, "config.json": config}),
                            (self.entry["draft"], {"draft.safetensors": b"draftwts"})):
            target = self.nas / spec["repository"] / spec["revision"]
            target.mkdir(parents=True)
            for name, data in files.items():
                (target / name).write_bytes(data)
            (target / "VERIFIED.json").write_text(json.dumps(spec))
        self.entry_file = base / "entry.json"
        self.entry_file.write_text(json.dumps(self.entry))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cache(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cache.main([str(self.entry_file), str(self.nas), str(self.cache), *extra])
        return out.getvalue()

    def cached(self, spec):
        return self.cache / spec["repository"] / spec["revision"]

    def test_stages_verified_copies_at_the_same_paths_then_reuses_them(self):
        out = self.run_cache()
        self.assertIn("STAGED=org/model/" + "a" * 40, out)
        self.assertIn("STAGED=org/draft/" + "b" * 40, out)
        for spec in (self.entry["model"], self.entry["draft"]):
            self.assertEqual(spec, json.loads((self.cached(spec) / "VERIFIED.json").read_text()))
        self.assertEqual(b"w" * 4096, (self.cached(self.entry["model"]) / "model.safetensors").read_bytes())
        self.assertFalse((self.cache / cache.STAGING).exists())
        # Second start: nothing is read from the NAS (it may even be gone).
        for f in (self.nas / "org/model" / ("a" * 40)).iterdir():
            f.unlink()
        out = self.run_cache()
        self.assertIn("CACHED=org/model/" + "a" * 40, out)
        self.assertNotIn("COPY", out)

    def test_old_revisions_and_staging_leftovers_are_pruned(self):
        relic = self.cache / "org/model" / ("c" * 40)
        relic.mkdir(parents=True)
        (relic / "model.safetensors").write_bytes(b"old")
        other = self.cache / "retired/model" / ("d" * 40)
        other.mkdir(parents=True)
        (self.cache / cache.STAGING / "org/model/x").mkdir(parents=True)
        out = self.run_cache()
        self.assertIn("PRUNE org/model/" + "c" * 40, out)
        self.assertFalse(relic.exists())
        self.assertFalse((self.cache / "retired").exists())
        self.assertEqual({"org"}, {p.name for p in self.cache.iterdir()})

    def test_tampered_nas_copy_is_refused_without_publishing(self):
        (self.nas / "org/model" / ("a" * 40) / "model.safetensors").write_bytes(b"x" * 4096)  # same size, wrong bytes
        with self.assertRaisesRegex(SystemExit, "hash mismatch"):
            self.run_cache()
        self.assertFalse(self.cached(self.entry["model"]).exists())

    def test_unverified_nas_copy_is_refused(self):
        (self.nas / "org/draft" / ("b" * 40) / "VERIFIED.json").unlink()
        with self.assertRaisesRegex(SystemExit, "not verified"):
            self.run_cache()
        self.assertFalse(self.cached(self.entry["draft"]).exists())

    def test_damaged_cached_file_is_restaged(self):
        self.run_cache()
        (self.cached(self.entry["model"]) / "model.safetensors").write_bytes(b"short")
        out = self.run_cache()
        self.assertIn("STAGED=org/model/" + "a" * 40, out)
        self.assertIn("CACHED=org/draft/" + "b" * 40, out)

    def test_size_cap_and_unsafe_paths(self):
        self.entry["model"]["files"][0]["size"] = 70 << 30
        self.entry_file.write_text(json.dumps(self.entry))
        with self.assertRaisesRegex(SystemExit, "cache limit"):
            self.run_cache()
        self.entry["model"]["files"][0]["path"] = "../escape"
        self.entry_file.write_text(json.dumps(self.entry))
        with self.assertRaisesRegex(SystemExit, "unsafe"):
            self.run_cache()


if __name__ == "__main__":
    unittest.main()
