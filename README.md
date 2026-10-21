# 1Panel v2 community builder

Builds the public Core, Agent and frontend sources for Linux amd64, arm64,
armv7, ppc64le, s390x, loong64 and riscv64. Missing proprietary XPack modules
retain the existing community compatibility fallback. This is not an enterprise
build and does not enable enterprise capabilities.

## Reviewed inputs

Every version follows the same discovery path. There is no version allowlist or
checked-in per-version source/configuration map. Scheduled builds select the
official stable release; manual runs may select a channel or an explicit version.
The generated `resolved-source.json` artifact pins source/installer commits,
Go/Node/npm versions, resources, dependency inputs and embedded configuration.
Subsequent jobs authenticate that exact contract rather than rediscovering inputs.

Discovery compares installer resources against the matching official archive.
It checks the resolved installer tip and up to 300
commits of official history anchored to that tip. Unmatched or unavailable
history fails explicitly. GeoIP is bound to the same version archive, with full
compressed size/hash and member size/hash checks, so later updates to the
standalone GeoIP URL do not change the resolved build. Discovery also
retains its authenticated GeoIP bytes as an optional local cache so resource
preparation does not download the same official package twice. Older inputs
without this cache keep the version-bound download path. Discovery
selects toolchains satisfying both Go modules and every declared npm engine,
and retains `npm ci --engine-strict`, archive integrity and all architecture gates.
A source without a lock first reuses an exact-manifest-bound reviewed repair.
Otherwise discovery generates a per-run lock using pinned Node 22.22.1/npm 10.9.4
and the official npm registry. Unsupported workspaces, package managers,
dependency sources or incompatible installers fail clearly before publication. Such failures
need a compatibility repair; they are never silently accepted. The source
contract digest follows every shard, package, aggregate and publication receipt.

The custom source build remains community-only. Enterprise packages use the
separate downstream official-package path and installation gates. Builds alone
do not imply a published or installation-verified release.

Dependency repairs are selected by exact package-manifest and original-lock
hashes in `config/repair-locks.json`, including an explicit absent-lock selector.
The three stored lock payloads are content-addressed and shared across matching
source manifests. Repairs never upgrade arbitrary existing dependencies or
resolve floating versions during replay. New missing-lock inputs are resolved
only during preparation. The generated lock, exact source manifest, generator
identity and lock digest travel in the authenticated contract and
`resolved-frontend-package-lock.json` sidecar. Every later job replays the same
bytes offline. A present but invalid upstream lock is not silently replaced.

Generation rejects non-registry direct sources before npm, disables lifecycle
scripts, and runs the pinned npm process without Node child-process, worker or
native-addon permission. A real spawn-denial preflight is required. This restricts
trusted npm preparation paths; it is not a sandbox for arbitrary malicious code.
The generated graph is checked for registry origins, integrity and compatible
engines. Bundled dependencies inherit only an explicitly declared pinned parent;
its entire compressed archive and actual child manifests are verified. The real
frontend build still runs `npm ci --engine-strict` against that fixed lock.

Release config is embedded before compiling: stable releases use `stable` and
`info`; beta/dev versions use their matching channel and the same discovery gates. Demo, enterprise and fxplay remain false. `is_offline` remains false:
shipping offline installation resources is not the application's separate
offline-feature/license mode. Upstream's stable mode selects Gin release mode
and avoids dev-only external app.yaml override behavior.

## Build and verify

Requires Docker, Git, Python 3 and PyYAML 6.0.3, plus Node/npm with npm's
`npm-package-arg` and `semver` modules for validation. Missing-lock discovery
requires the exact host resolver versions Node 22.22.1 and npm 10.9.4.
The hosted workflows install these prerequisites before discovery.
Run from a clean committed checkout so the
recorded build-repository commit identifies the build scripts actually used.

```bash
VERSION=v2.3.2
TARGET_ARCHES='amd64 arm64 armv7 ppc64le s390x loong64 riscv64'
python3 -m unittest discover -s tests -v
contract_sha=$(python3 scripts/discover_inputs.py --version "$VERSION" --output resolved-source.json)
python3 scripts/resolved_contract.py apply resolved-source.json "$VERSION" --sha256 "$contract_sha"
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
Core/Agent, install.sh, 1pctl, GeoIP, the source installer’s service layout,
language files, root systemd services, and `manifest.json`. The manifest records community edition,
version/architecture, source/installer/build commits, toolchains, mode, and SHA256
and size for every other regular file. Each archive has a relative-basename
`.sha256` sidecar. These packages and sidecars are the only new Release assets.
The ordinary tar member directory `build-metadata/` carries `resolved-source.json`
and a generated frontend lock only when needed, covered by the same manifest.
`checksums.txt`, aggregate manifests and validation receipts stay inside CI artifacts. Archive traversal, symlinks, duplicate/missing/empty members, wrong ELF,
wrong resource digests, stale extra outputs and checksum mismatches are rejected.

Pinned inputs improve repeatability, but this does not promise bit-identical
archives: base-image tags and OS package repositories are not digest/snapshot
locked, and archive metadata is not normalized. ELF inspection and fixture tests
do not replace real builds or installed-service runtime tests on each target.

## CI and publication

GitHub Actions runs regression tests and selects integration work from the PR
diff. Documentation, Python unit tests and the explicit publication-only script
list do not trigger seven architecture builds; unknown, build, input or workflow
changes do. Manual `build-only` remains available for a full integration run.
Version-tag pushes and non-candidate manual `build` runs build the requested
matrix, preserve verified artifacts and publish a canonical release.
Daily runs check the latest official stable version and the names of this
repository's public release attachments before running tests. Existing archive
and SHA-256 pairs are trusted without downloading them. A complete seven-architecture
release ends the run immediately; otherwise only missing architectures enter the
build matrix. An existing archive missing only its SHA-256 sidecar is completed
from GitHub's digest metadata without a build or package download. A sidecar
without its archive stops before building and needs the original artifact.
New packages are validated before upload and checked against GitHub's returned
SHA-256 and size; existing packages are preserved. API errors stop the check
instead of triggering a rebuild.
`validate-automatic` validates the same local payload and preparation receipt without
enabling a writer. It is an optional validation mode, not a required step for new
versions. Existing tags/releases do not suppress validation.
`build-only` creates verified CI source artifacts without publication preparation
or release writes, for example when repairing an old release without a trusted
receipt. It uses the same discovery, build and aggregate gates. Normal `build`
remains the default and publishes automatically; new releases need no extra step.
Matrix jobs use `fail-fast: false`. A failed architecture is omitted while successful
branches are independently validated and aggregated. The schema-2 manifest records
every requested architecture, its exact producer attempt/job identity, and its
terminal result. Failed-only retries authenticate the latest job for each branch
through the selected producer attempt, retaining inherited successful jobs by ID.
Final verified artifact names include the producer attempt for exact selection.
GitHub controls artifact retention; a full workflow rerun can delete earlier
artifacts. Retrying only publication reuses its available preparation and does not
recompile. Shared source/preparation/validation failures block all dependent
artifacts; cancelled producer attempts cannot publish. Failure details remain in Actions jobs,
warnings and summaries, never Release notes. Existing canonical assets are not
renamed, deleted or replaced. Conflicting bytes stop for specific review before
any upload; other existing attachments stay outside the repair payload.
An explicit `publish_candidate` dispatch creates a separate draft tag/release
and verifies uploaded SHA-256 and size. A retry resumes that same owned draft
after checking existing attachments against the verified output. It never overwrites
public release assets. `promote-existing` consumes an explicitly selected verified
CI artifact and fills missing assets without replacing existing bytes.
Normal new releases are staged as owned drafts, verified against their server
digests, then made public. A failed finalization leaves recovery evidence and
attempts to return that exact release to draft. No publication failure warning is
added to the Release body.

Draft publication binds all reads and uploads to the created
Release ID. Preparation validates local files and records a receipt; the serialized
writer authenticates the receipt and all file hashes without unpacking the same
archives again, then discovers the current Release state. A retry can reuse a
single empty draft created by GitHub Actions for the same version on this build's
commit ancestry; its target is updated before upload. A nonempty owned draft can
resume on the exact build commit when every existing attachment matches the
verified output. Multiple drafts, changed notes, another author or conflicting
bytes stop publication. `promote-existing` can also recover that draft using the
exact original CI artifact, ZIP digest and built commit.
Only older assets without server digests need downloaded-byte verification.
A run with successful build gates and only a
known publication-writer failure can supply that artifact without another build.

Manual existing-release validation still checks actual package bytes, source
inputs and architecture. It does not require old CI logs to remain online.
Existing controls and recovery files are preserved; publication never deletes them.

GitHub Actions owns the daily update check. Its one maintained workflow shares
source/frontend preparation across the requested architecture subset and runs at
most three compile jobs concurrently. Historical one-off lock probes and receipt
diagnostics have been removed. CNB runs only on tags or manual requests.
CNB honors the pushed tag, installs Bash/Python and the pinned
resolver toolchain with its runtime libraries, runs the same validation,
and runs each architecture independently from a shared prepared image. Its failure
stage preserves only the final validated subset while retaining a failed pipeline
status. CNB automatic publication is intentionally
removed until a provider-specific verified staging/promote flow is reviewed.

The legacy direct GoReleaser entry point is disabled because it bypasses the
frontend, production configuration and manifest gates. Use the Docker workflow.
