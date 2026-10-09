# 1Panel v2 community builder

Builds the public Core, Agent and frontend sources for Linux amd64, arm64,
armv7, ppc64le, s390x, loong64 and riscv64. Missing proprietary XPack modules
retain the existing community compatibility fallback. This is not an enterprise
build and does not enable enterprise capabilities.

## Reviewed inputs

Historical input contracts remain in `config/sources.json`. Scheduled builds discover
the official stable release; manual runs can select stable, beta or dev, or name
an exact version. A compatible version absent from the historical registry is
resolved into a per-run `resolved-source.json` artifact, without adding a version
entry to the repository. The contract pins source/installer commits, exact
Go/Node/npm versions, resources, dependency inputs and embedded configuration.

Discovery compares installer resources against the matching official archive.
It checks the resolved installer tip, existing immutable inputs and up to 300
commits of official history anchored to that tip. Unmatched or unavailable
history fails explicitly. GeoIP is bound to the same version archive, with full
compressed size/hash and member size/hash checks, so later updates to the
standalone GeoIP URL do not change the resolved build. Discovery also
selects toolchains satisfying both Go modules and every declared npm engine,
and retains `npm ci --engine-strict`, archive integrity and all architecture gates.
A source without a lock can reuse only an exact-manifest-bound verified repair
lock. An unsupported workspace, package manager, dependency source, missing new
lock or incompatible installer fails clearly before publication. Such failures
need a compatibility repair; they are never silently accepted. The source
contract digest follows every shard, package, aggregate and publication receipt.

The custom source build remains community-only. Enterprise packages use the
separate downstream official-package path and installation gates. Builds alone
do not imply a published or installation-verified release.

The v2.3.2 source is `65243c68c463cc055ab044093f641ea5d2e9e28b`.
The installer is `aa4a6bbf24ae0fd938b32294672f5086f940e483`: its install.sh
SHA256 matches the official v2.3.2 installer (`3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f`).
GeoIP comes from the official resource host with a locked hash. A host update
requires an explicit reviewed lock change; a changed response fails closed.

This historical source uses Go 1.26.1. Node 22.22.1 and npm 10.9.4 are pinned and checked
at build time. Frontend dependencies use `npm ci` and the source lockfile.
Release config is embedded before compiling: stable releases use `stable` and
`info`; beta/dev versions use their matching channel and the same discovery gates. Demo, enterprise and fxplay remain false. `is_offline` remains false:
shipping offline installation resources is not the application's separate
offline-feature/license mode. Upstream's stable mode selects Gin release mode
and avoids dev-only external app.yaml override behavior.

## Build and verify

Requires Docker, Git and Python 3. Run from a clean committed checkout so the
recorded build-repository commit identifies the build scripts actually used.

```bash
VERSION=v2.3.2
TARGET_ARCHES='amd64 arm64 armv7 ppc64le s390x loong64 riscv64'
python3 -m unittest discover -s tests -v
python3 scripts/resolve_inputs.py "$VERSION" > build-inputs.env
source build-inputs.env
docker build --build-arg VERSION="$VERSION" \
  --build-arg GO_VERSION="$GO_VERSION" --build-arg NODE_VERSION="$NODE_VERSION" \
  --build-arg SOURCE_COMMIT="$SOURCE_COMMIT" --build-arg INSTALLER_REF="$INSTALLER_REF" \
  --build-arg TARGET_ARCHES="$TARGET_ARCHES" \
  --build-arg BUILD_REPOSITORY_COMMIT="$(git rev-parse HEAD)" \
  -t 1panel-verified-builder .
mkdir -p dist
docker run --rm -v "$PWD/dist:/dist" 1panel-verified-builder
python3 scripts/validate_artifacts.py dist "$VERSION" "$TARGET_ARCHES"
(cd dist && sha256sum -c checksums.txt)
```

Use an empty output directory. Required downloads are fetched to temporary files,
checked for nonzero size and pinned SHA256, then promoted only when the whole
resource set passes. Existing files are never accepted merely because they exist.
The build checks frontend embed output, compiles each architecture, then validates
ELF class/endianness/machine without executing target binaries.

Each `1panel-VERSION-linux-ARCH.tar.gz` has one matching root directory and contains
Core/Agent, install.sh, 1pctl, GeoIP, all eight init scripts, five language files,
root systemd services, and `manifest.json`. The manifest records community edition,
version/architecture, source/installer/build commits, toolchains, mode, and SHA256
and size for every other regular file. Each archive has a relative-basename
`.sha256` sidecar. `checksums.txt` and `build-manifest.json` cover the selected
matrix. Archive traversal, symlinks, duplicate/missing/empty members, wrong ELF,
wrong resource digests, stale extra outputs and checksum mismatches are rejected.

Pinned inputs improve repeatability, but this does not promise bit-identical
archives: base-image tags and OS package repositories are not digest/snapshot
locked, and archive metadata is not normalized. ELF inspection and fixture tests
do not replace real builds or installed-service runtime tests on each target.

## CI and publication

GitHub Actions runs regression tests, builds the requested matrix and preserves
verified pipeline artifacts. Existing tags/releases do not suppress validation.
An explicit `publish_candidate` dispatch creates a **new draft** tag/release,
downloads its assets again and verifies exact byte identity. It never overwrites
public release assets. Promotion requires separate review after downstream
regression. Scheduled builds use the reviewed default version, never floating
latest. CNB honors the pushed tag, installs Bash/Python, runs the same validation,
and preserves pipeline artifacts. CNB automatic publication is intentionally
removed until a provider-specific verified staging/promote flow is reviewed.

The legacy direct GoReleaser entry point is disabled because it bypasses the
frontend, production configuration and manifest gates. Use the Docker workflow.
