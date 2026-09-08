#!/usr/bin/env python3
"""Offline regression cases for release interruption, partial builds and retries."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import release_build as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "VERSION": "v2.2.5", "BUILD_FINGERPRINT": "a" * 64,
            "ARCH_LIST": "amd64 arm64", "GITHUB_REPOSITORY": "test/repo",
            "FORCE_BUILD": "0", "REFS_RESOLVED": "1",
            "GITHUB_OUTPUT": str(self.dist / "output"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state = {"exists": False, "draft": True, "files": {}, "calls": []}
        self.fail_file = None

    def manifest(self, built=("amd64",), skipped=("arm64",), fingerprint=None):
        packages = []
        for arch in built:
            name = f"1panel-v2.2.5-linux-{arch}.tar.gz"
            content = ("package " + arch).encode()
            digest = hashlib.sha256(content).hexdigest()
            (self.dist / name).write_bytes(content)
            (self.dist / (name + ".sha256")).write_text(digest + "  " + name + "\n")
            packages.append(dict(arch=arch, status="built", file=name, sha256=digest, size=len(content)))
        packages += [dict(arch=arch, status="skipped", reason="unsupported fixture") for arch in skipped]
        manifest = dict(schema=1, version="v2.2.5", channel="stable", fingerprint=fingerprint or "a" * 64,
                        requested_arches=list(built + skipped), packages=packages, complete=not skipped)
        (self.dist / "build-manifest.json").write_text(json.dumps(manifest))
        return manifest

    def remote_release(self, version):
        if not self.state["exists"]:
            return None
        return dict(isDraft=self.state["draft"], assets=[dict(name=name, size=len(content),
            digest="sha256:" + hashlib.sha256(content).hexdigest()) for name, content in self.state["files"].items()])

    def remote_manifest(self, version):
        content = self.state["files"].get("build-manifest.json")
        return json.loads(content) if content else None

    def gh(self, *args, check=True):
        self.state["calls"].append(args)
        operation = args[1]
        if operation == "create":
            self.assertIn("--draft", args)
            self.state["exists"] = True
        elif operation == "upload":
            file = Path(args[3])
            if file.name == self.fail_file:
                raise subprocess.CalledProcessError(1, args, stderr="simulated interrupted upload")
            self.state["files"][file.name] = file.read_bytes()
        elif operation == "delete-asset":
            del self.state["files"][args[3]]
        elif operation == "edit":
            self.assertIn("build-manifest.json", self.state["files"])
            self.state["draft"] = False
        else:
            self.fail("Unexpected remote mutation: " + repr(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def mocked_remote(self):
        return patch.multiple(release, gh=self.gh, remote_release=self.remote_release,
                              remote_manifest=self.remote_manifest)

    def check_output(self):
        (self.dist / "output").write_text("")
        release.check_release()
        return (self.dist / "output").read_text().strip()

    def test_partial_build_is_usable_and_keeps_retrying(self):
        self.manifest()
        with self.mocked_remote():
            release.publish(self.dist)
            self.assertFalse(self.state["draft"])
            self.assertFalse(self.remote_manifest("v2.2.5")["complete"])
            self.assertEqual(self.check_output(), "build=1")
            with patch.dict(os.environ, {"ARCH_LIST": "amd64"}):
                self.assertEqual(self.check_output(), "build=0")

    def test_missing_checksum_or_changed_fingerprint_retries(self):
        self.manifest(built=("amd64", "arm64"), skipped=())
        with self.mocked_remote():
            release.publish(self.dist)
            self.assertEqual(self.check_output(), "build=0")
            with patch.dict(os.environ, {"BUILD_FINGERPRINT": "b" * 64}):
                self.assertEqual(self.check_output(), "build=1")
            with patch.dict(os.environ, {"REFS_RESOLVED": "0"}):
                self.assertEqual(self.check_output(), "build=1")
            del self.state["files"]["1panel-v2.2.5-linux-arm64.tar.gz.sha256"]
            self.assertEqual(self.check_output(), "build=1")

    def test_interrupted_upload_leaves_draft_and_can_resume(self):
        self.manifest(built=("amd64", "arm64"), skipped=())
        self.fail_file = "1panel-v2.2.5-linux-arm64.tar.gz"
        with self.mocked_remote():
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.dist)
            self.assertTrue(self.state["draft"])
            self.assertNotIn("build-manifest.json", self.state["files"])
            self.assertEqual(self.check_output(), "build=1")
            self.fail_file = None
            release.publish(self.dist)
            self.assertFalse(self.state["draft"])
            self.assertEqual(self.check_output(), "build=0")

    def test_separate_architecture_retries_merge_verified_remote_packages(self):
        self.manifest()
        with self.mocked_remote():
            release.publish(self.dist)
            for file in self.dist.glob("*.tar.gz*"):
                file.unlink()
            local = self.manifest(built=("arm64",), skipped=("amd64",))
            release.publish(self.dist)
            merged = self.remote_manifest("v2.2.5")
            self.assertTrue(merged["complete"])
            self.assertEqual({p["arch"] for p in merged["packages"] if p["status"] == "built"}, {"amd64", "arm64"})
            self.assertEqual(json.loads((self.dist / "build-manifest.json").read_text()), local)
            # Publication is idempotent even though amd64 is only on the server.
            release.publish(self.dist)
            self.assertEqual(self.check_output(), "build=0")

    def test_new_fingerprint_removes_stale_unbuilt_architecture(self):
        self.manifest(built=("amd64", "arm64"), skipped=())
        with self.mocked_remote():
            release.publish(self.dist)
            self.manifest(fingerprint="b" * 64)
            with patch.dict(os.environ, {"BUILD_FINGERPRINT": "b" * 64}):
                release.publish(self.dist)
            self.assertNotIn("1panel-v2.2.5-linux-arm64.tar.gz", self.state["files"])
            self.assertNotIn("1panel-v2.2.5-linux-arm64.tar.gz.sha256", self.state["files"])
            self.assertFalse(self.remote_manifest("v2.2.5")["complete"])

    def test_corrupt_package_and_absolute_checksum_are_rejected(self):
        manifest = self.manifest()
        package = manifest["packages"][0]
        path = self.dist / package["file"]
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "(?:hash|size) mismatch"):
            release.local_verify(self.dist)
        self.manifest()
        (self.dist / (package["file"] + ".sha256")).write_text(package["sha256"] + "  /opt/" + package["file"])
        with self.assertRaisesRegex(ValueError, "checksum"):
            release.local_verify(self.dist)

    def test_old_release_with_no_manifest_is_not_complete(self):
        self.state["exists"] = True
        self.state["draft"] = False
        with self.mocked_remote():
            self.assertEqual(self.check_output(), "build=1")

    def test_latest_resolution_falls_back_to_newest_upstream_tag(self):
        tags = "a refs/tags/v2.0.13\nb refs/tags/v2.2.5-lts\nc refs/tags/v2.2.4\nd refs/tags/v2.3.0-beta.1\n"
        with patch.object(release.urllib.request, "urlopen", side_effect=OSError("offline")), \
                patch.object(release, "run", return_value=subprocess.CompletedProcess([], 0, tags, "")), \
                patch.object(release.time, "sleep"):
            self.assertEqual(release.latest_version(), "v2.2.5-lts")
        with patch.object(release.urllib.request, "urlopen", side_effect=OSError("offline")), \
                patch.object(release, "run", return_value=subprocess.CompletedProcess([], 1, "", "offline")), \
                patch.object(release.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "supply VERSION"):
                release.latest_version()

    def test_transient_upload_is_retried(self):
        responses = [subprocess.CompletedProcess([], 1, "", "HTTP 503"), subprocess.CompletedProcess([], 0, "ok", "")]
        with patch.object(release, "run", side_effect=responses) as mocked, patch.object(release.time, "sleep"):
            result = release.gh("release", "upload", "v2.2.5", "fixture", "--clobber")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(mocked.call_args.kwargs["timeout"], 900)

    def test_go_lookup_timeout_keeps_auto_toolchain_fallback(self):
        with patch.dict(os.environ, {"GO_VERSION": "", "DEFAULT_GO_VERSION": "1.25.7"}), \
                patch.object(release, "run", side_effect=subprocess.TimeoutExpired("resolver", 180)):
            self.assertEqual(release.resolve_go("v2.2.5"), "1.25.7")

    def test_fingerprint_tracks_scripts_and_refs_but_merges_architectures(self):
        root = self.dist / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / ".github/workflows").mkdir(parents=True)
        for name in ("Dockerfile", ".cnb.yml", ".github/workflows/build.yml", "scripts/fixture.sh"):
            (root / name).write_text("initial")

        def fingerprint():
            (self.dist / "output").write_text("")
            release.prepare()
            values = dict(line.split("=", 1) for line in (self.dist / "output").read_text().splitlines())
            return values["BUILD_FINGERPRINT"]

        with patch.object(release, "ROOT", root), patch.object(release, "resolve_ref", return_value="b" * 40), \
                patch.dict(os.environ, {"GO_VERSION": "1.25.7", "ARCH_INPUT": "amd64"}):
            first = fingerprint()
            with patch.dict(os.environ, {"ARCH_INPUT": "arm64;loong64"}):
                self.assertEqual(fingerprint(), first)
            (root / "scripts/fixture.sh").write_text("fixed")
            second = fingerprint()
            self.assertNotEqual(first, second)
            with patch.object(release, "resolve_ref", return_value="c" * 40):
                self.assertNotEqual(fingerprint(), second)


if __name__ == "__main__":
    unittest.main()
