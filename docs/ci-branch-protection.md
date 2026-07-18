# CI and Branch-Protection Requirements

Milestone 12.1 Item 10. PRs #17 and #18 were merged into `main` while the
GitHub `main-tests` job was failing — the workflow (`.github/workflows/ci.yml`)
already ran the right jobs, but nothing in the repository's GitHub branch
protection configuration required them to pass before merge. That is a
repository *settings* gap, not a code gap, and cannot be fixed by a commit to
this repository — it requires a maintainer with admin access to the GitHub
repository to configure branch protection once, using the steps below.

## Required status checks for `main`

Configure these as required status checks on the `main` branch (GitHub →
Settings → Branches → Branch protection rules → `main` → "Require status
checks to pass before merging"):

- `main-tests`
- `paper-runtime-tests`
- `migration-smoke`
- `type-check-safety` (new in Milestone 12.1 — blocking safety-critical
  Pyright subset; the pre-existing `type-check` job stays non-blocking, see
  below)

Do **not** require the plain `type-check` job — it intentionally retains a
large pre-existing whole-project Pyright baseline (`continue-on-error: true`)
predating this milestone, and making it required would block every future PR
on unrelated, pre-existing type errors. `type-check-safety` is the actual
blocking gate, scoped to the production modules Milestone 12.1 touched
(`pyright-safety.json`).

## Additional branch-protection settings

- **Base branch for feature delivery is `main`.** Every feature/fix PR must
  target `main`, not another feature branch.
- **Require branches to be up to date before merging** — a required check
  that passed against a stale base does not prove anything about the merge
  commit that will actually land.
- **Require at least one approving review** before merge. This repository's
  prior workflow (research/implementation agent commits reviewed by the
  repository owner) satisfies this if enforced as a GitHub setting rather
  than only a convention.
- **Do not allow administrators to bypass** these requirements for ordinary
  feature PRs. An admin-bypass merge is exactly the failure mode that let
  PR #17/#18 land with `main-tests` red — if this setting is left enabled,
  branch protection provides no actual guarantee.
- **Label branch-sync PRs accurately** (e.g. `sync`, `no-op`) when a PR only
  merges `main` forward into a stale feature branch with no code changes of
  its own — this keeps required-check history and PR review expectations
  legible when scanning merged-PR history later.

## Verification status

This document records the required configuration. **It has not been applied
to the live GitHub repository as part of this milestone** — doing so requires
GitHub admin access and a change to shared repository settings outside this
codebase, which is out of scope for an automated code change and was not
independently verified against the live repository during this milestone.
A maintainer with admin access must apply the settings above manually and
confirm them under Settings → Branches before this gap is considered closed
operationally, not just documented.
