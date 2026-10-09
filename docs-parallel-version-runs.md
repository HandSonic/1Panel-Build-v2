# Parallel version runs

Read-only build and validation runs have unique run/attempt concurrency groups.
Canonical repair writers share a repository-and-release-version lock across all
refs, while draft candidates use their unique version/run tag identity. Writer
queues use `queue: max` with cancellation disabled, so up to 100 pending writers
can wait without the default single-pending replacement. Queue ordering follows
when jobs reach the lock, not workflow dispatch order. Do not exceed that queue
capacity. See the [GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Start with a bounded three-version build/validation cohort and coordinate hosted
runner/disk use with downstream native tests. Each build still limits compile
jobs to three; that is not a global cap across arbitrary workflow dispatches.
Wait for old workflow revisions with the former branch-wide lock to finish
before overlapping writers under this new policy. Promotion still requires the
same exact producer/artifact and receipt binding, recoverable backups and public
readback. These jobs do not alter repository-global latest-release metadata.
