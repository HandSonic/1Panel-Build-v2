# Existing-version rebuild and publication

Use the repositories' **existing version-based Actions workflows**. No new login,
token, tag rewrite, push-triggered publication route or duplicate workflow entry
is required. An authenticated GitHub Actions UI is needed to start a new manual
run when the connected tool lacks workflow_dispatch.

## Default behavior

Normal builds keep their existing triggers and version inputs. A public release
is a successful no-op only when its exact canonical matrix, server content hashes,
checksum bytes and `release-validation.json` identify a completed successful
validation run under the current version-specific policy. Missing, stale or
unverifiable receipts report repair-needed without modifying the release.
Known `.backup-*`/`.staged-*` recovery files are noncanonical. Unknown canonical
assets block repair before any upload; concurrent additions also fail closed.

## Upstream first

In `HandSonic/1Panel-Build-v2`, use existing `build-offline` / `build.yml`:

- Normal compilation: operation=build; reuse version and arch inputs.
- Read-only promotion validation: operation=validate-promotion.
- Authorized same-tag promotion: operation=promote-existing.

Promotion requires an explicit version and exact successful artifact provenance.
It validates all seven upstream packages with the authoritative upstream validator,
including ELF, all member hashes, pinned installer/GeoIP, exact normalized embedded
Core/Agent YAML and Core version. It does not execute the binaries.

The current reviewed v2.3.2 input is:

- Run ID: 37803686376
- Artifact ID: 11562143708
- Artifact ZIP SHA-256: 93b4ad31f6c8bc6f33a320a2b5ecb98b31e2abba23d175fdc99a5f9d654ac954
- Built commit: 93474bc2959340b9880495002bb5645ee5c354ad

The built commit is the actual synthetic PR merge commit, not the branch head or
later master merge. Never relabel its provenance. Keep publish_candidate=false
for existing-tag promotion; that older option remains a separate normal-build
new-draft path. The arch input applies to normal builds; promotion requires the
complete verified seven-architecture contract.

## Downstream next

In `HandSonic/1Panel-offline-installer-V2`, use existing
`Build 1Panel v2 Offline` / `build-offline-v2.yml`:

- operation=build keeps normal builds.
- operation=validate-repair prepares and validates without release writes.
- operation=repair-existing performs the reviewed same-release repair.

Reuse version=v2.3.2 and release_tag=v2.3.2. Prefer upstream_source=release after
upstream promotion. If explicitly using verified-ci, supply the exact four
provenance values above. Do not create credentials if cross-repository artifact
access is blocked; use upstream promotion and the public verified release path.

v2.3.2 requires 17 packages: official6, custom7, enterprise original2 and enterprise
Docker-enhanced2. Original enterprise archives stay byte-identical. Each generated
expanded community directory is discarded only after its archive passes isolated
validation; all final archives remain for the complete release gate.

## Write boundaries and recovery

Preparation is read-only. Only an explicitly selected workflow_dispatch repair or
promotion mode can enter the separate write-enabled job. The exact preparation
receipt hash is bound through job outputs and checked again after download.
The write job revalidates the entire appropriate contract before any release write.

Every changed/new asset is staged under a unique name and checked by server digest
and complete byte download. Existing bytes are also read back and hashed. Before
each switch, asset IDs, names, sizes and hashes are checked again. GitHub does not
provide atomic compare-and-swap across assets; a brief naming window is possible.
Originals are retained under backup names, checksums switch last, and no asset is
deleted. A concurrent edit or failure triggers observed-state rollback; an
incomplete rollback requires journal review, not a blind retry. Journals are saved
on success or failure. Concurrent human release-note edits are preserved.

## Historical versions

Version inputs do not authorize using the latest installer for an older package.
Each historical cohort needs its source commit, installer/resource pins, exact
configuration schema, install/upgrade compatibility tests, architecture matrix
and actual rebuild evidence. Unknown contracts fail closed. Old interactive-only
installers must not receive unsupported modern CLI flags, and absent legacy
configuration fields must not be injected merely to match today's schema.

Historical binaries with dev/debug YAML or Core v2.0.0 require upstream production
rebuilds first. Docker-only repacking is not a valid repair. Fixtures/configuration
normalizer tests are separate from real builds and native install/upgrade tests;
untested runtime conditions are not completion claims.

## Compatibility policy and next-version discovery

Authenticity hashes describe immutable inputs; they are not a claim that harmless
formatting changes are incompatible. The current embedded-byte check proves that
the configuration derived from that exact input reached both binaries. For a new
source revision, discovery should compare parsed field meaning and installer
function behavior, preserving comments, unrelated added fields and legacy absent
optional fields. Formatting-only differences should pass or produce a warning,
not require a new adapter. Reviewed contracts remain the publication provenance
until this semantic discovery and its regression fixtures are implemented.

Hard failures are reserved for unverifiable/corrupt artifacts, wrong architecture
or version, missing required payloads, development behavior, ambiguous required
keys/types, or failed install/upgrade safety tests. Renamed required keys or a
rewritten control flow must report the specific unsupported behavior and diff.
Discovery may propose a compatible contract; it must not silently bless changed
source or count untested runtime behavior as safe. This future-version adapter
work is pending and is not included in the current v2.3.2 completion claim.


## Bounded upstream architecture matrix

GitHub normal builds now use `prepare -> compile[arch] -> build` before the
existing optional draft-candidate stage. `prepare` uses the same Dockerfile's
`prepared-builder` target: the reviewed source commit, compatibility patches,
locked Node/npm frontend build, exact per-version embedded configuration, and
verified installer/GeoIP resources are prepared once. A SHA-256-bound source
archive is downloaded by each compile job; it is not a release artifact.

The compile matrix is limited to three concurrent runners and fails each shard
independently. Each runner checks the shared archive digest, source commit,
resolved input lock and exact Go version before using `GOTOOLCHAIN=local` to
cross-compile the two binaries. Its cache key includes version, Go version,
architecture, scripts, config and Go dependency locks; no broad restore prefix
is allowed. Intermediate source/shard artifacts expire after three days.

The aggregate job requires all requested shards from the same immutable preparation artifact,
validates each shard's packages and build commit, and then reconstructs the
original `verified-1panel-VERSION-COMMIT` contract. It validates the combined
bytes again. A full seven-architecture run therefore checks all fourteen
binaries twice. Subset development builds remain available, but promotion
still requires the unchanged complete seven-architecture contract. Missing,
extra, corrupt, mismatched-version or mismatched-commit shards fail closed.

CNB and local Docker full builds retain their serial behavior. Existing-release
promotion, its reviewed receipt, readback, backups, and single publication
writer are unchanged. Neither push nor scheduled builds trigger repair.

Performance must be measured on a new unpublished full build before adopting
an expected speedup: frontend work is shared, but source download/extraction,
runner queueing, seven Go environments and cold caches add overhead. Compare
prepare, compile critical path and aggregate durations against a serial run
with the same version/inputs. A first cold-cache run is not representative of
warm-cache performance. This change has local unit/shell contract coverage;
it does not itself establish that hosted CI builds or all historical versions
have completed successfully.

Failed-only and aggregate-only reruns retain successful dependency outputs.
Compile downloads the exact preparation artifact ID; shard names are keyed by
run ID plus that preparation ID, not the consumer's current run attempt. Each
architecture may replace only its own intermediate shard on a failed-job rerun.
A full preparation rerun gets a new artifact ID and isolated shard namespace.
The final verified artifact remains immutable and is never overwritten; use a
new workflow run when intentionally rebuilding an already successful aggregate.


## Historical configuration compatibility and input readiness

`semantic_configuration.py` uses pinned PyYAML 6.0.3 in an isolated Python
environment. It rejects duplicate keys, aliases/anchors, explicit tags, malformed
critical values and non-boolean known flags. It edits `base.mode`, Core
`base.version`, `log.level`, and known boolean flags only when present. Other
sections, unknown fields, comments and layout survive standalone normalization.
An absent optional flag is never inserted.

Build configuration must still match a SHA-pinned per-version source fixture
semantically, or its already normalized form. Benign YAML spacing, key order,
comments and line-ending differences are accepted. A typed comparison prevents
Python's `false == 0` behavior from hiding a semantic change. The build emits the
reviewed canonical normalized bytes and verifies their SHA, preserving exact
binary-embedding checks. An unreviewed field/value change is a source-integrity
failure, not an optional-field compatibility shortcut. Both components are
validated before either is written.

Fourteen configuration profiles are covered using cached immutable source
snapshots, including older schemas without enterprise/FXPlay flags. Newly added
profiles use current-tag snapshots for future reproducible rebuilds; they do not
claim those commits produced an original historical release. Existing reviewed
profiles retain their original source and normalized hashes.

`config/historical-input-readiness.json` enumerates all 40 historical/current
versions. It is planning metadata, never an input fallback. Only v2.3.2 currently
has a complete enabled source lock. Older versions still need their frontend
package-lock bytes, exact Node/npm compatibility, a reviewed exact Go compiler
satisfying BOTH Core and Agent module requirements, and complete reviewed
installer/GeoIP pins. Available source and installer candidates are not silently
promoted to trusted build inputs. Every newly enabled version still requires an
unpublished full seven-architecture build and downstream install/upgrade checks.
No historical rebuild or runtime result is implied by parser fixture coverage.
