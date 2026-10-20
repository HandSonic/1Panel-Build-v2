# Build and publication

Use the existing `build-offline` workflow in `.github/workflows/build.yml`.

## Daily updates

At 07:00 UTC, GitHub checks the latest official stable Release and this
repository's matching public Release attachment names. An architecture is
present when its archive and `.sha256` attachment both exist. Existing pairs
are trusted without downloading or validating their contents.

- All seven architectures present: stop before tests and source preparation.
- Missing architectures: build only that subset and append its new output.
- No matching public Release: build all seven architectures.
- Authentication, network or API errors: fail the check.

The selected version is fixed for the rest of the run. Successful new packages
are validated before publication and read back after upload. Existing files are
preserved. A half-present pair can be completed only if its existing member's
server digest and size match the new candidate; a conflict stops publication.

CNB retains tag and manual builds. It has no second daily build schedule.

## Manual operations

Use **Run workflow** with an explicit version, or leave it empty to discover the
selected `stable`, `beta` or `dev` channel. The `arch` field selects normal build
architectures; promotion takes its matrix from the selected producer artifact.

| Operation | Result |
| --- | --- |
| `build` | Build and publish verified successful architectures. |
| `build-only` | Build verified CI artifacts without publishing. |
| `validate-automatic` | Build and prepare the automatic publication plan without writing a Release. |
| `validate-promotion` | Validate an explicitly selected existing CI artifact without publishing. |
| `promote-existing` | Publish missing files from that exact verified CI artifact. |

`publish_candidate=true` with `build` creates a separate draft candidate instead
of the canonical version Release. Its tag includes the build commit and run ID.

To reuse an existing build, supply all four provenance inputs:

- `upstream_run_id`
- `upstream_artifact_id`
- `upstream_artifact_sha256` (the exact artifact ZIP hash)
- `upstream_build_commit` (the actual built commit, including a PR merge commit)

The source/preparation/aggregate gates must have passed. Only independently
successful architecture outputs are accepted. A known publication-writer failure
does not invalidate already verified build output; it does not require a rebuild.
Manual existing-release validation retains full package verification. Unlike the
daily metadata check, it can reject corrupt bytes or incompatible source inputs.

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

Downloaded shards and the final aggregate are validated at their transfer
boundaries. Shared preparation or validation failures block publication. A failed
architecture is omitted while independently successful output can be published.
Failure details stay in Actions, not Release notes.

Intermediate source/shard artifacts expire after three days. Failed-only retries
reuse the original preparation artifact and replace only their own intermediate
shard. A new full preparation has a new artifact ID. Final verified build artifacts
remain immutable.

## Publication and recovery

Preparation is read-only. One writer per canonical version checks the bound
preparation receipt and artifact before writing. Release operations bind to the
observed Release ID and retain its asset snapshot. Only missing files are
uploaded; this code does not rename, replace or delete existing assets.

A new canonical Release is staged as an owned draft. Its new attachments are
verified before it becomes public. If finalization has an uncertain result, the
writer attempts to return that same Release ID to draft and saves a journal.
Public notes are preserved.

- An empty draft created by this workflow can be resumed by a descendant build.
- A nonempty owned draft needs `promote-existing` with its exact original build
  artifact. The draft target and all existing attachment bytes must match that
  verified artifact before finalization.
- Conflicting assets or changed Release identity stop the transaction. Review
  the preserved journal and choose an explicit recovery action.

For historical versions, resolve that version's immutable source, installer,
configuration and dependency inputs. Compatibility changes need checks for the
affected behavior and architectures; unrelated historical or current-version
rebuilds are not a routine prerequisite. Packaging checks establish archive
integrity and metadata, not installed-service runtime behavior.
