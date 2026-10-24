# Build and publication

Use the existing `build-offline` workflow in `.github/workflows/build.yml`.

## Daily updates

The check is scheduled for 08:00 UTC (16:00 Beijing time); GitHub may delay its
actual start. It reads the official Latest stable Release and this repository's
matching public Release attachment names. An architecture is
present when its archive and `.sha256` attachment are both uploaded and nonempty. Existing pairs
are trusted without downloading or validating their contents.

- All seven architectures present: stop before tests and source preparation.
- Missing architectures: build only that subset and append its new output.
- Archive present but `.sha256` absent: generate only the sidecar from GitHub's
  SHA-256 metadata, under the same version writer lock. No build or package download.
- No matching public Release: build all seven architectures.
- Authentication, network or API errors: fail the check.
- An unfinished or empty canonical asset, including a failed upload's `starter`
  placeholder: fail with its name and ID for explicit repair; never treat it as complete or delete it automatically.
- A checksum without its archive, or an archive-only asset without a server
  digest: stop before building and use the exact original artifact for repair.

The selected version is fixed for the rest of the run. Successful new packages
are validated before publication; uploaded SHA-256 and size must match GitHub's
metadata. Existing files are preserved. A checksum-only repair skips tests and
source preparation and uses the existing publication writer, not a second CI.

CNB retains tag and manual builds. It has no second daily build schedule.

## Manual operations

Use **Run workflow** with an explicit version, or leave it empty to discover the
selected `stable`, `beta` or `dev` channel. The `arch` field selects normal build
architectures; promotion takes its matrix from the selected producer artifact.

| Operation | Result |
| --- | --- |
| `build` | Build and publish verified successful architectures. |
| `build-only` | Build verified CI artifacts without publishing. |
| `validate-automatic` | Build and validate the local automatic publication payload and receipt without writing a Release. |
| `validate-promotion` | Validate an explicitly selected existing CI artifact without publishing. |
| `promote-existing` | Publish missing files from that exact verified CI artifact. |

`publish_candidate=true` with `build` creates a separate draft candidate instead
of the canonical version Release. Its tag includes the build commit and run ID.
Retrying the same verified output resumes that owned candidate draft and fills
only missing attachments.

To reuse an existing build, supply all four provenance inputs:

- `upstream_run_id`
- `upstream_artifact_id`
- `upstream_artifact_sha256` (the exact artifact ZIP hash)
- `upstream_build_commit` (the actual built commit, including a PR merge commit)

The source/preparation/aggregate gates must have passed. Only independently
successful architecture outputs are accepted. A known publication-writer failure
does not invalidate already verified build output; it does not require a rebuild.
Explicit promotion validates the selected CI package content once after any
legacy metadata migration. Normal builds trust completed, nonempty public pairs by name
and append only missing output, without downloading those old packages.

## Builds and PR checks

PRs run unit tests. Documentation, Python unit tests and the explicit publication
script list do not require an architecture build. Build inputs and unknown paths
retain integration checks. A documentation branch does not inherit unrelated
changes made later on the base branch.

Source inputs, installer resources and toolchains are resolved once for the
selected version. Source and frontend preparation is shared by the requested
architecture matrix, with at most three compile runners active. The Go cache is
separate by version, Go version and architecture; module inputs and the Go
compatibility adapter invalidate it.

Discovery retains the GeoIP bytes it already authenticated. Resource preparation
checks that optional cache against the same contract instead of downloading the
official archive again. It is not a required public artifact or a new input schema.

Downloaded shards and the final aggregate are validated at their transfer
boundaries. Shared preparation or validation failures block publication. A failed
architecture is omitted while independently successful output can be published.
Failure details stay in Actions, not Release notes.

The scheduler dependency waits for the matrix. Only the package build step may
continue after failure; each branch then uploads a result record, with validated
packages on success or no package files on failure. All surrounding native jobs,
including result transport and post-job cleanup, must succeed before aggregation.
The jobs API binds immutable job IDs and attempts; its live status is not used as
a second completion gate. Missing identities are refreshed at most 13 times,
five seconds apart. Missing records and infrastructure failures block publication.
The manifest marks this contract with `outcome_source: package-step`.

Same-run publication authenticates its native workflow identity and the exact
preparation receipt supplied by job outputs, without polling live job status.
Cross-run promotion still verifies the terminal producer and exact job identities.
After publication, `matrix_status` makes a partially successful run red so failed
package branches remain visible. See [the CI design audit](docs-ci-audit.md).

Intermediate source/shard artifacts expire after three days. Rerunning a specific
compile job reuses the original preparation artifact and replaces only its own
intermediate shard and result record. Inherited successful jobs remain
authenticated by their original job IDs. For a recorded package failure, rerun
that architecture's compile job or start a build selecting the missing architectures.
**Re-run failed jobs** repairs failed infrastructure/publication jobs, but cannot
recompile a package failure whose native compile job succeeded; rerunning only
`matrix_status` repeats its red result.
A new full preparation has a new artifact ID. Final verified build artifacts
include their producer attempt in the name for exact selection. GitHub controls
retention: [a full workflow rerun can delete prior artifacts](https://github.com/orgs/community/discussions/45415),
while a failed-only retry retains inherited artifacts. Save any artifact needed
for later recovery before a full rerun. Retrying only a publication job consumes
its available original preparation receipt and verified artifact, without compiling again. Reruns use the original
workflow commit; code fixes require a new run or explicit artifact promotion.

## Publication and recovery

Preparation validates local files and records a receipt; it does not query or
freeze remote Release state. One writer per canonical version authenticates that
receipt and all transferred file hashes without unpacking the same archives again,
then discovers the current Release state. Release operations bind
to the observed Release ID and retain its asset snapshot. Only missing files are
uploaded; this code does not rename, replace or delete existing assets.

After a successful upload, the returned asset ID binds readback verification.
Only a newly uploaded asset or digest that is not yet visible is re-read, at most
seven times with five seconds between reads. The upload is never repeated by this
wait. Conflicting bytes, changed asset IDs or changes to the existing inventory
still stop the transaction immediately.

A new canonical Release is staged as an owned draft. Its new attachments are
verified before it becomes public. If finalization has an uncertain result, the
writer attempts to return that same Release ID to draft and saves a journal.
Public notes are preserved.

Legacy metadata migration fixes the gzip header so preparing the same original
CI artifact again produces identical package bytes. A partial upload from an
older migration still needs its original prepared bytes; a conflicting existing
asset is never overwritten.

- An empty draft created by this workflow can be resumed by a descendant build.
- A nonempty owned draft can resume from the same verified build commit, or via
  `promote-existing` with its exact original build artifact. Every existing
  attachment must match before any missing file is uploaded. Only older assets
  without a server digest need a readback.
- Conflicting assets or changed Release identity stop the transaction. Review
  the preserved journal and choose an explicit recovery action.

For historical versions, resolve that version's immutable source, installer,
configuration and dependency inputs. Compatibility changes need checks for the
affected behavior and architectures; unrelated historical or current-version
rebuilds are not a routine prerequisite. Packaging checks establish archive
integrity and metadata, not installed-service runtime behavior.
