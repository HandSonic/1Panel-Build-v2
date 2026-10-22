# Upstream/downstream CI audit, 2026-10-22

This follow-up checks the interface with `HandSonic/1Panel-offline-installer-V2`, including historical CI inputs and recovery after merging a PR.

## Confirmed upstream defect

GitHub removed the PR association from successful run #654 after PR #28 merged: both the run and attempt API returned `pull_requests: []`. Its artifact still contains the tested synthetic merge commit. The former promotion verifier required a live PR association and rejected this legitimate historical input.

The verifier now recovers the PR number and exact executed commit from the successful build job's checkout step and timestamped log. It authenticates the closed, merged PR's repository, frozen base/head commits and merge parents. Ambiguous, missing or later-step log evidence fails validation. This is a read-only fallback for an explicitly empty association; existing live PR validation remains strict.

## Validation

- All 249 unit tests pass, including absent associations, spoofed logs, ambiguous PRs and the manual input transport.
- Replaying the authenticated API responses and original build log of run #654 validates all seven architectures.
- Python compilation, shell syntax and `git diff --check` pass.

The downstream audit separately fixes consumption of the package-step outcome discriminator, attempt-qualified artifact names and inherited job copies. The default package-only public release route was checked against the real v2.4.1 amd64 package, including its complete archive and embedded source/installer contract. This static check does not claim native installation coverage for all architectures.
