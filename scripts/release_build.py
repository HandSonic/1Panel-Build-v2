#!/usr/bin/env python3
"""Shared, resumable release checks for GitHub Actions and CNB preparation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHES = "amd64 arm64 armv7 ppc64le s390x loong64 riscv64"


def run(*args, check=True, timeout=180):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check, timeout=timeout)


def latest_version():
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                "https://api.github.com/repos/1Panel-dev/1Panel/releases/latest", headers=headers)
            with urllib.request.urlopen(request, timeout=25) as response:
                version = json.load(response).get("tag_name", "")
            if re.fullmatch(r"v2\.\d+\.\d+(?:-lts)?", version):
                return version
        except (OSError, ValueError):
            pass
        if attempt < 2:
            time.sleep(attempt + 1)
    # An API outage/rate limit must not silently downgrade to a hard-coded version.
    result = run("git", "ls-remote", "--tags", "https://github.com/1Panel-dev/1Panel.git", check=False)
    tags = re.findall(r"refs/tags/(v2\.\d+\.\d+(?:-lts)?)$", result.stdout, re.M)
    if tags:
        return max(tags, key=lambda value: (tuple(map(int, value[1:].split("-")[0].split("."))), value.endswith("-lts")))
    raise ValueError("Cannot resolve the latest v2 release; retry later or supply VERSION explicitly.")


def resolve_ref(repo, ref):
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return ref.lower()
    try:
        result = run("git", "ls-remote", repo, "refs/heads/" + ref,
                     "refs/tags/" + ref, "refs/tags/" + ref + "^{}", check=False)
        refs = dict(line.split()[::-1] for line in result.stdout.splitlines() if len(line.split()) == 2)
        for name in ("refs/tags/" + ref + "^{}", "refs/tags/" + ref, "refs/heads/" + ref):
            if name in refs:
                return refs[name]
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"Warning: cannot resolve {repo} ref {ref}; retaining the named ref for this attempt.", file=sys.stderr)
    return "unresolved:" + ref


def resolve_go(version):
    if os.environ.get("GO_VERSION"):
        return os.environ["GO_VERSION"]
    try:
        result = run("bash", str(ROOT / "scripts/resolve_go_version.sh"), version)
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        if result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    fallback = os.environ.get("DEFAULT_GO_VERSION", "1.25.7")
    print(f"Warning: Go requirement lookup unavailable; using {fallback} with GOTOOLCHAIN=auto", file=sys.stderr)
    return fallback


def prepare():
    version = os.environ.get("VERSION_INPUT") or os.environ.get("VERSION", "")
    if not version and os.environ.get("GITHUB_REF", "").startswith("refs/tags/"):
        version = os.environ["GITHUB_REF"][len("refs/tags/"):]
    if not version and re.fullmatch(r"v2\.[\w.-]+", os.environ.get("CNB_BRANCH", "")):
        version = os.environ["CNB_BRANCH"]
    version = version or latest_version()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("VERSION must be a single safe tag/ref name (letters, digits, '.', '_' or '-'; no '/').")
    arches = list(dict.fromkeys(re.split(r"[\s,;]+", os.environ.get("ARCH_INPUT") or
                                        os.environ.get("TARGET_ARCHES") or DEFAULT_ARCHES)))
    arches = [arch for arch in arches if arch]
    if not arches or any(not re.fullmatch(r"[a-z0-9]+", arch) for arch in arches):
        raise ValueError("Invalid architecture list")
    from configure_runtime import resolve_channel
    channel = resolve_channel(version, os.environ.get("CHANNEL"))
    if channel not in {"stable", "beta", "dev"}:
        raise ValueError("CHANNEL must be stable, beta, or dev")
    values = dict(VERSION=version, ARCH_LIST=" ".join(arches), ARCH_LABEL="_".join(arches),
                  GO_VERSION=resolve_go(version),
                  NODE_VERSION=os.environ.get("NODE_VERSION", "20"), CHANNEL=channel,
                  INSTALLER_REF=os.environ.get("INSTALLER_REF") or "v2")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", values["INSTALLER_REF"]) or ".." in values["INSTALLER_REF"]:
        raise ValueError("Invalid INSTALLER_REF; supply a branch, tag or commit ID")
    source_sha = resolve_ref("https://github.com/1Panel-dev/1Panel.git", version)
    installer_sha = resolve_ref("https://github.com/1Panel-dev/installer.git", values["INSTALLER_REF"])
    fingerprint = hashlib.sha256()
    # Architectures are checked individually so disjoint retries can be merged.
    fingerprint.update(json.dumps({key: val for key, val in values.items() if key not in {"ARCH_LIST", "ARCH_LABEL"}}, sort_keys=True).encode())
    fingerprint.update((source_sha + ":" + installer_sha).encode())
    files = [ROOT / "Dockerfile", ROOT / ".github/workflows/build.yml", ROOT / ".cnb.yml"]
    files += [path for path in (ROOT / "scripts").rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    for path in sorted(files):
        fingerprint.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    values["BUILD_FINGERPRINT"] = fingerprint.hexdigest()
    # A floating ref whose revision cannot be checked should never suppress a retry.
    values["REFS_RESOLVED"] = "0" if source_sha.startswith("unresolved:") or installer_sha.startswith("unresolved:") else "1"
    output = ROOT / ".ci/build.env"
    output.parent.mkdir(exist_ok=True)
    output.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))
    for key, value in values.items():
        if os.environ.get("CI_PROVIDER") == "cnb":
            print(f"##[set-output {key}={value}]")
        elif os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write(f"{key}={value}\n")
        else:
            print(f"{key}={value}")


def load_manifest(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("packages"), list):
        raise ValueError("Unsupported or malformed build manifest")
    return value


def built_packages(manifest):
    packages = {}
    for package in manifest["packages"]:
        if not isinstance(package, dict):
            raise ValueError("Malformed manifest package")
        arch = package.get("arch")
        if not isinstance(arch, str) or arch in packages:
            raise ValueError("Duplicate or missing manifest architecture")
        packages[arch] = package
        if package.get("status") == "built":
            name = package.get("file", "")
            if not name or Path(name).name != name or not name.endswith(".tar.gz"):
                raise ValueError("Unsafe package filename")
            if not re.fullmatch(r"[0-9a-f]{64}", package.get("sha256", "")):
                raise ValueError("Missing package SHA-256")
        elif package.get("status") != "skipped":
            raise ValueError("Unknown package status")
    return packages


def local_verify(directory):
    manifest = load_manifest(directory / "build-manifest.json")
    packages = built_packages(manifest)
    requested = manifest.get("requested_arches")
    if not isinstance(requested, list) or not requested or any(not isinstance(arch, str) or arch not in packages for arch in requested):
        raise ValueError("Missing requested architecture results")
    if manifest.get("complete") is not all(packages[arch]["status"] == "built" for arch in requested):
        raise ValueError("Manifest completion flag disagrees with architecture results")
    count = 0
    for package in packages.values():
        if package["status"] != "built":
            continue
        path = directory / package["file"]
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing built package: {path.name}")
        if package.get("size") is not None and package["size"] != path.stat().st_size:
            raise ValueError(f"Package size mismatch: {path.name}")
        digest = hash_file(path)
        if digest != package["sha256"]:
            raise ValueError(f"Package hash mismatch: {path.name}")
        checksum = directory / (path.name + ".sha256")
        if not checksum.is_file() or checksum.read_text().strip().split() != [digest, path.name]:
            raise ValueError(f"Invalid downloadable checksum: {checksum.name}")
        count += 1
    if not count:
        raise ValueError("No usable architecture was built; refusing to publish an empty release")
    return manifest


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gh(*args, check=True):
    command = ("gh", *args, "--repo", os.environ["GITHUB_REPOSITORY"])
    retryable = args[:2] in {("release", "view"), ("release", "download"),
                            ("release", "upload"), ("release", "edit")}
    for attempt in range(3 if retryable else 1):
        try:
            result = run(*command, check=False, timeout=900 if args[:2] == ("release", "upload") else 180)
        except subprocess.TimeoutExpired:
            if not retryable or attempt == 2:
                raise
        else:
            transient = re.search(r"(?:429|50[234]|timeout|timed out|connection|eof|temporar|tls handshake)", result.stderr, re.I)
            if not result.returncode or not retryable or not transient or attempt == 2:
                if check:
                    result.check_returncode()
                return result
        time.sleep(attempt + 1)


def remote_release(version):
    result = gh("release", "view", version, "--json", "isDraft,assets", check=False)
    return json.loads(result.stdout) if result.returncode == 0 else None


def remote_manifest(version):
    with tempfile.TemporaryDirectory() as tmp:
        result = gh("release", "download", version, "--pattern", "build-manifest.json", "--dir", tmp, "--clobber", check=False)
        if result.returncode:
            return None
        try:
            return load_manifest(Path(tmp) / "build-manifest.json")
        except (ValueError, OSError):
            return None


def remote_assets_match(manifest, release, required):
    packages = built_packages(manifest)
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    for arch in required:
        package = packages.get(arch, {})
        if package.get("status") != "built":
            return False
        name = package["file"]
        if any(assets.get(filename, {}).get("size", 0) <= 0 for filename in (name, name + ".sha256")):
            return False
        if package.get("size") is not None and assets[name]["size"] != package["size"]:
            return False
        digest = assets[name].get("digest")
        if digest and digest != "sha256:" + package["sha256"]:
            return False
    return True


def check_release():
    version = os.environ["VERSION"]
    build = True
    if os.environ.get("FORCE_BUILD", "0").lower() not in {"1", "true"} and os.environ.get("REFS_RESOLVED", "1") == "1":
        release = remote_release(version)
        manifest = remote_manifest(version) if release and not release["isDraft"] else None
        if manifest and manifest.get("version") == version and manifest.get("fingerprint") == os.environ["BUILD_FINGERPRINT"]:
            try:
                build = not remote_assets_match(manifest, release, os.environ["ARCH_LIST"].split())
            except ValueError:
                pass
    result = "1" if build else "0"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("build=" + result + "\n")
    print("Build/retry required" if build else "Matching manifest and every requested architecture are already published")


def publish(directory):
    manifest = local_verify(directory)
    local_packages = list(manifest["packages"])
    version = os.environ["VERSION"]
    if manifest.get("version") != version or manifest.get("fingerprint") != os.environ["BUILD_FINGERPRINT"]:
        raise ValueError("Build manifest does not match this pipeline's version/fingerprint")
    release = remote_release(version)
    previous = remote_manifest(version) if release else None
    current = built_packages(manifest)
    if previous and previous.get("version") == version and previous.get("fingerprint") == manifest["fingerprint"]:
        for arch, package in built_packages(previous).items():
            if current.get(arch, {}).get("status") != "built" and remote_assets_match(previous, release, [arch]):
                current[arch] = package
            elif arch not in current:
                current[arch] = dict(arch=arch, status="skipped", reason="Previously requested; awaiting a successful retry")
        manifest["requested_arches"] = sorted(set(manifest["requested_arches"]) | set(previous.get("requested_arches", [])))
    manifest["packages"] = list(current.values())
    manifest["complete"] = all(current.get(arch, {}).get("status") == "built" for arch in manifest["requested_arches"])
    usable = [arch for arch, package in current.items() if package["status"] == "built"]
    missing = [arch for arch in manifest["requested_arches"] if arch not in usable]
    notes = f"Automated source build for {version}. Available architectures: {', '.join(usable)}.\n\n"
    notes += ("All requested architectures are available." if not missing else
              "Partial architecture coverage; scheduled runs will retry: " + ", ".join(missing) + ".")
    notes += "\n\nBuild fingerprint: `" + manifest["fingerprint"] + "`\nSee build-manifest.json for individual results.\n"
    with tempfile.TemporaryDirectory() as tmp:
        # Keep local input unchanged: retained remote architectures may not exist
        # in dist, and a publication retry must still be locally verifiable.
        manifest_path = Path(tmp) / "build-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        notes_path = Path(tmp) / "notes.md"
        notes_path.write_text(notes)
        if not release:
            gh("release", "create", version, "--verify-tag", "--draft", "--title", version, "--notes-file", str(notes_path))
        # Upload only files verified locally. A manifest is the completion record,
        # and is removed during updates so an interrupted overwrite cannot skip retries.
        if release and any(asset["name"] == "build-manifest.json" for asset in release["assets"]):
            gh("release", "delete-asset", version, "build-manifest.json", "--yes")
        for package in local_packages:
            if package["status"] != "built":
                continue
            for name in (package["file"], package["file"] + ".sha256"):
                gh("release", "upload", version, str(directory / name), "--clobber")
        uploaded = remote_release(version)
        if not uploaded or not remote_assets_match(manifest, uploaded, usable):
            raise ValueError("Uploaded release is missing or has mismatched assets; publication remains pending")
        # A previous build can have the same filenames but different inputs.
        # After replacement packages are safe, remove old packages which this
        # manifest does not endorse; legacy consumers may ignore the manifest.
        approved = {name for package in current.values() if package["status"] == "built"
                    for name in (package["file"], package["file"] + ".sha256")}
        old_package = re.compile(r"1panel-" + re.escape(version) + r"-linux-[A-Za-z0-9_]+\.tar\.gz(?:\.sha256)?$")
        for asset in uploaded["assets"]:
            if old_package.fullmatch(asset["name"]) and asset["name"] not in approved:
                gh("release", "delete-asset", version, asset["name"], "--yes")
        gh("release", "upload", version, str(manifest_path), "--clobber")
        published_manifest = remote_manifest(version)
        if published_manifest != manifest:
            raise ValueError("Uploaded manifest verification failed; retry publication")
        gh("release", "edit", version, "--draft=false", "--notes-file", str(notes_path),
           "--prerelease=" + ("false" if manifest.get("channel") == "stable" else "true"))
    print("Published complete build" if manifest["complete"] else "Published usable architectures; missing architectures will be retried")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "check", "verify", "publish"])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "check":
        check_release()
    elif args.command == "verify":
        manifest = local_verify(args.dist)
        print(json.dumps({"complete": manifest.get("complete"), "packages": manifest["packages"]}, indent=2))
    else:
        publish(args.dist)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
