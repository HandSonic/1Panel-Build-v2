# Parallel version runs

Read-only build and validation runs have unique run/attempt concurrency groups.
Canonical repair writers share a repository-and-release-version lock across all
refs, while draft candidates use their unique version/run tag identity. Writer
queues use `queue: max` with cancellation disabled, so up to 100 pending writers
can wait without the default single-pending replacement. Queue ordering follows
when jobs reach the lock, not workflow dispatch order. Do not exceed that queue
capacity. See the [GitHub concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

Each build limits compile jobs to three; this is a per-run limit. Build only the
versions needed for the change. Existing artifacts can be promoted without
compiling another version or repeating a successful architecture.

Publication retains the selected producer/artifact and receipt binding. It
uploads only absent files and checks GitHub's returned digest and size. Existing
attachments are preserved; only historical assets without a server digest need
one download for verification. Publishing a canonical version updates release
visibility and lets GitHub determine the Latest release from its normal policy.
