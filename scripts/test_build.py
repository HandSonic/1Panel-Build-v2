#!/usr/bin/env python3
"""Offline regressions for channel selection and isolated architecture failures."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from configure_runtime import configure, resolve_channel

SCRIPTS = Path(__file__).resolve().parent


class RuntimeTests(unittest.TestCase):
    def test_channels(self):
        for version, channel in {"v2.2.5": "stable", "v2.2.5-lts": "stable",
                                 "v2.3.0-beta.1": "beta", "v2.3.0-rc1": "beta",
                                 "main": "dev", "v2": "dev"}.items():
            self.assertEqual(resolve_channel(version), channel)
        self.assertEqual(resolve_channel("main", "stable"), "stable")
        with self.assertRaises(ValueError):
            resolve_channel("v2.2.5", "production")

    def test_reordered_renamed_configs_and_idempotency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for module in ("core", "agent"):
                config = root / module / "cmd" / "renamed" / "config" / "defaults.yml"
                config.parent.mkdir(parents=True)
                config.write_text("log:\n  mode: debug\nbase: # defaults\n    version: old\n    mode: 'dev' # selected channel\n    nested:\n      mode: preserve\n")
            (root / "1pctl").write_text("#!/bin/bash\nexport ORIGINAL_VERSION = old\n".replace("VERSION =", "VERSION="))
            configure(root, "v2.2.5")
            first = config.read_text()
            configure(root, "v2.2.5")
            self.assertEqual(first, config.read_text())
            self.assertIn("    mode: stable # selected channel", first)
            self.assertIn("  mode: debug", first)
            self.assertIn("      mode: preserve", first)
            self.assertIn("    version: v2.2.5", first)
            self.assertIn("ORIGINAL_VERSION=v2.2.5", (root / "1pctl").read_text())


class PackagingTests(unittest.TestCase):
    def run_fixture(self, arches):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for directory in ("core", "agent", "bin", "initscript", "lang"):
            (root / directory).mkdir()
        for filename in ("1pctl", "install.sh", "GeoIP.mmdb", "initscript/1panel-core.service",
                         "initscript/1panel-agent.service", "lang/zh.sh"):
            (root / filename).write_text("fixture\n")
        go = root / "bin" / "go"
        go.write_text("""#!/usr/bin/env bash
set -eu
if [[ "$GOARCH" == s390x && "$PWD" == */agent ]]; then
  echo 'simulated unsupported dependency' >&2
  exit 42
fi
while [[ "$#" -gt 0 ]]; do
  if [[ "$1" == -o ]]; then shift; printf '%s/%s\\n' "$GOARCH" "${PWD##*/}" > "$1"; exit 0; fi
  shift
done
exit 1
""")
        go.chmod(0o755)
        env = dict(os.environ, SOURCE_ROOT=str(root), VERSION="v2.2.5",
                   TARGET_ARCHES=arches, BUILD_FINGERPRINT="fixture-inputs",
                   PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["bash", str(SCRIPTS / "build_packages.sh")], env=env,
                                capture_output=True, text=True)
        return root, result

    def test_failed_middle_arch_preserves_healthy_packages(self):
        root, result = self.run_fixture("amd64 s390x arm64")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        dist = root / "dist"
        manifest = json.loads((dist / "build-manifest.json").read_text())
        self.assertFalse(manifest["complete"])
        self.assertEqual([p["status"] for p in manifest["packages"]], ["built", "skipped", "built"])
        self.assertEqual(manifest["channel"], "stable")
        for arch in ("amd64", "arm64"):
            name = f"1panel-v2.2.5-linux-{arch}"
            check = subprocess.run(["sha256sum", "-c", name + ".tar.gz.sha256"], cwd=dist,
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            with tarfile.open(dist / (name + ".tar.gz")) as archive:
                self.assertEqual(archive.extractfile(name + "/1panel-agent").read(), f"{arch}/agent\n".encode())
        self.assertFalse((dist / "1panel-v2.2.5-linux-s390x.tar.gz").exists())

    def test_all_architectures_fail(self):
        root, result = self.run_fixture("s390x")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads((root / "dist/build-manifest.json").read_text())["complete"])
        self.assertEqual(list((root / "dist").glob("*.tar.gz")), [])


if __name__ == "__main__":
    unittest.main()
