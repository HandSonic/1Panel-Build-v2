# CI design audit — 2026-10-22

The audit started from [build-offline #652](https://github.com/HandSonic/1Panel-Build-v2/actions/runs/39786190908).
All seven architecture jobs succeeded and all shards downloaded, but aggregation
failed with `Nonterminal architecture job`. Hosted validation also showed that
live API status can remain queued while the actual job has finished. A bounded
status retry alone therefore does not provide a reliable scheduling barrier.

## Findings and repairs

| Area | Defect | Repair |
| --- | --- | --- |
| Matrix aggregation | Live jobs API state was used as a second scheduling barrier. | Keep `needs` as the barrier and collect explicit per-architecture outcome artifacts. Live REST status no longer controls aggregation. |
| Matrix identity | Stale records could hide a newer failed execution; a manifest could omit an architecture. | Require every record to match the run, build commit, prepared generation and effective job attempt. Bind immutable job IDs, reject extra compile branches, and preserve inherited successful executions. |
| Publication provenance | Running workflow metadata could lag behind successful native gates. | Same-run publication uses the native dependency chain and its bound preparation receipt. Cross-run promotion retains terminal API verification and bounded incomplete-evidence refresh. |
| Upload verification | A successful upload was immediately rejected if the asset or digest was not yet visible. | Bind the upload response's asset ID and retry only GET readback, at most seven times. Wrong bytes, changed IDs and changed existing inventory still fail immediately. Never repeat an upload POST. |
| Mixed scheduled repair | A failed new-package publication suppressed the independent checksum repair through the implicit step success condition. | Run the checksum repair unless cancelled, under the existing version writer lock. |
| Architecture inputs | Commas, tabs and newlines were not normalized before writing job outputs. | Use the shared architecture parser and emit a single normalized line. Invalid and duplicate architectures still stop before input discovery. |
| Archive validation | Generated-package readers lacked the resource limits already used for official packages. | Share the existing limits: 512 MiB compressed, 256 MiB per member, 1 GiB total member bytes and 10,000 members; reject sparse members. |

## Design boundaries

`needs` controls execution order. Each architecture records the original package
step outcome. Only that controlled step uses `continue-on-error`; result creation,
artifact upload, input checks and post-job cleanup must succeed natively. The
aggregate requires the native compile matrix to succeed, so an infrastructure
failure cannot hide behind another branch's recorded package failure.

A successful record accompanies a complete validated package. A failed record
replaces that architecture's old shard with a record-only artifact. Records bind
the run, actual execution attempt, architecture, build commit and prepared-source
generation. The API supplies only immutable job identities during aggregation;
queued live statuses are harmless. Missing records, stale attempts, unexpected
branches and infrastructure failures block publication.

The schema-2 manifest explicitly marks `outcome_source: package-step`; historical
manifests without that marker retain the old terminal-job contract. Same-run
writers authenticate the exact preparation receipt and all file hashes through
native job outputs. Explicit cross-run promotion additionally checks all terminal
compile jobs and the final `matrix_status` result against the recorded outcomes.

After publication, `matrix_status` turns a partial package build red. To retry a
recorded package failure, rerun its specific compile job or start a build selecting
that architecture. GitHub's **Re-run failed jobs** targets native infrastructure
or publication failures; rerunning only the final status job does not recompile
an architecture. Reruns retain the original prepared generation and replace only
their own result artifact.

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

Regressions cover permanently queued live API status, recorded partial failures,
stale rerun records, infrastructure and post-job failure boundaries, same-run
publication identity, terminal cross-run promotion, delayed upload metadata
without duplicate writes, mixed checksum recovery, input normalization and archive
bounds. Local simulations do
not exercise live Release mutations or replace hosted architecture builds.
