# Repository Setup

This document records the manual GitHub UI configuration required for this
repository. Steps here cannot be automated via workflows; they must be applied
once by the repository owner.

## Branch Protection — `main`

Navigate to **Settings → Branches → Add rule** (or edit the existing rule for
`main`).

Apply the following settings:

| Setting | Value |
|---|---|
| Branch name pattern | `main` |
| Require a pull request before merging | ✅ Enabled |
| Required approving reviews | 0 (single-user repo) |
| Require status checks to pass before merging | ✅ Enabled |
| Require branches to be up to date before merging | ✅ Enabled |
| Required status checks | `backend`, `frontend`, `crg-build`, `Analyze (python)`, `Analyze (javascript)` |
| Allow force pushes | ❌ Disabled |
| Allow deletions | ❌ Disabled |
| Require signed commits | ❌ Not required (homelab project; tighten in future if needed) |
| Require linear history | ❌ Not required (merge commits allowed) |

> **Note on CodeQL check names:** GitHub registers CodeQL results under the
> job matrix names `Analyze (python)` and `Analyze (javascript)`. Add both as
> required status checks after the first CodeQL run completes (the check names
> only appear in the UI after they have run at least once).

## Repository Secrets

No secrets are required for v0. The `release.yml` workflow uses the automatic
`GITHUB_TOKEN` for GHCR pushes, which works for public repositories without
additional configuration.

When the release workflow is activated (STAGE-001-015), verify:
- Repository → Settings → Actions → General → Workflow permissions: set to
  **Read and write permissions** (needed for `softprops/action-gh-release`).

## GHCR Package Visibility

After the first container image is pushed (STAGE-001-015), navigate to the
package settings and set visibility to **Public** to match the public
repository.

## Issue and PR Templates

Out of scope for this stage. Deferred to EPIC-019.
