# CI design audit — 2026-10-22

The audit started from [build-offline #652](https://github.com/HandSonic/1Panel-Build-v2/actions/runs/39786190908).
All seven architecture jobs succeeded and all shards downloaded, but aggregation
failed with `Nonterminal architecture job`. The failing API response was not
logged, so the exact delayed job cannot be reconstructed.

## Findings and repairs

| Area | Defect | Repair |
| --- | --- | --- |
| Matrix aggregation | One jobs API snapshot was treated as immediately final. | Keep `needs` as the scheduling barrier; refresh incomplete API evidence at most 13 times, with five seconds between reads. Report the architecture, job ID, attempt, status and conclusion. |
| Matrix identity | An older rerun snapshot could disagree with the scheduler; a manifest could omit a successful architecture. | Require the current aggregate job, compare branch outcomes with `needs.compile.result`, and reject extra compile branches. Preserve inherited successful jobs and prefer the latest execution of every branch. |
| Publication provenance | Shared gates and run status could still be pending in a single API read. | Reuse the same bounded read-only refresh in automatic publication and explicit artifact promotion. Reject changed identities, cancellation, unsupported states and failed shared gates. |
| Upload verification | A successful upload was immediately rejected if the asset or digest was not yet visible. | Bind the upload response's asset ID and retry only GET readback, at most seven times. Wrong bytes, changed IDs and changed existing inventory still fail immediately. Never repeat an upload POST. |
| Mixed scheduled repair | A failed new-package publication suppressed the independent checksum repair through the implicit step success condition. | Run the checksum repair unless cancelled, under the existing version writer lock. |
| Architecture inputs | Commas, tabs and newlines were not normalized before writing job outputs. | Use the shared architecture parser and emit a single normalized line. Invalid and duplicate architectures still stop before input discovery. |
| Archive validation | Generated-package readers lacked the resource limits already used for official packages. | Share the existing limits: 512 MiB compressed, 256 MiB per member, 1 GiB total member bytes and 10,000 members; reject sparse members. |

## Design boundaries

`needs` controls execution order. Separate architecture artifacts carry package
bytes and immutable input metadata. The jobs API authenticates per-architecture
results and rerun identities, including the reason an architecture is omitted.
Missing artifacts never imply a legitimate compilation failure.

Incomplete or contradictory evidence cannot authorize publication. A successful
branch must have a valid shard; failed or timed-out branches are recorded and
omitted. Cancellation, malformed identities, unexpected branches and shared-gate
failures block publication. If API evidence does not settle, aggregation fails
with diagnostics instead of publishing an assumed result.

Scheduler-result and current-job checks reject stale aggregate snapshots; they
do not promise a transactionally consistent API view of every branch. Publication
rechecks the exact selected producer attempt and recorded job identities.

The review also covered PR permissions and scope, cancellation, source/toolchain
and resource pinning, shared frontend preparation, shard generations, failed-only
reruns, writer-only retries, version writer concurrency, draft recovery and CNB
artifact retention. Existing behavior in those areas is retained where no defect
was established.

## Validation

Run the complete local regression suite:

```sh
python3 -m unittest discover -s tests -q
git diff --check
bash -n scripts/build_release.sh scripts/build_cnb_matrix.sh scripts/download_resources.sh
```

Regressions cover delayed and permanently pending job snapshots, scheduler/API
disagreement, rerun inheritance, cancellation and invalid states, extra branches,
publication-gate delay, delayed upload metadata without duplicate writes, mixed
checksum recovery, input normalization and archive bounds. Local simulations do
not exercise live Release mutations or replace hosted architecture builds.
