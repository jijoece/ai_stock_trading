# Milestone Message Batches

A small local skeleton around the Anthropic Message Batches API for generating
milestone implementation patches. This is a **patch-generation workflow**, not
an autonomous coding agent: it produces unified diffs for a human to review,
validate, and explicitly apply.

## Batching semantics — read this before submitting a wave

* Requests in the same Message Batch are **independent**.
* They **cannot see** sibling request results.
* They are **not executed sequentially**.
* Results **may be returned in any order** — `custom_id` is the only
  correlation key.
* Do **not** put dependent milestones in the same implementation wave (e.g. a
  milestone whose prompt assumes a previous milestone's patch is already
  applied).
* Multiple independent subtasks from the same milestone may share a batch
  when they modify separate files and have no dependency on each other.

Recommended workflow:

```text
Batch wave 1:
    submit one milestone or several genuinely independent milestones

Fetch:
    review and apply selected patches locally

Local validation:
    run tests and fix issues separately

Refresh:
    update context files from the new working tree

Batch wave 2:
    submit the next dependent milestone
```

For a strictly sequential roadmap, one milestone per batch is safest and
still receives batch pricing.

## Bug-fixing boundary

This skeleton only generates an initial implementation patch. It does **not**
automatically:

* inspect test failures;
* submit repair prompts;
* generate retry batches;
* modify prompts based on failures;
* loop until tests pass.

A future (not yet implemented) workflow for fixing bugs found after applying
a patch:

```text
implementation batch
→ local patch review/application
→ local tests
→ separate bug-fix prompt or batch later
```

## Manifest format

See `batch/manifest.example.json`. Key rules:

* `model` may be set in the manifest or via `ANTHROPIC_MODEL`; the manifest
  wins if both are set. Neither present is a hard failure.
* `max_tokens` (>= 1) is required at the manifest level and may be overridden
  per request.
* `context_files` are explicit paths only — nothing is collected
  automatically or recursively.
* All paths (`prompt_file`, `context_files`) are resolved relative to the
  repository root and must not escape it.
* Each request's total context (prompt + context files) must stay under
  `context_limit_bytes` (default 2 MB).

## Example

```bash
cp batch/manifest.example.json batch/manifest.json
mkdir -p batch/prompts
cp docs/milestone9-2-prompt.md batch/prompts/milestone-9-2.md

export ANTHROPIC_API_KEY="..."
export ANTHROPIC_MODEL="..."

python scripts/milestone_batch.py validate \
  --manifest batch/manifest.json

python scripts/milestone_batch.py submit \
  --manifest batch/manifest.json

python scripts/milestone_batch.py status \
  --batch-id <batch-id>

python scripts/milestone_batch.py fetch \
  --batch-id <batch-id>

python scripts/milestone_batch.py check-patch \
  --run-dir batch/runs/<batch-id> \
  --custom-id milestone-9-2

python scripts/milestone_batch.py apply-patch \
  --run-dir batch/runs/<batch-id> \
  --custom-id milestone-9-2
```

`apply-patch` requires a clean tracked-file working tree unless
`--allow-dirty` is passed, always runs `git apply --check` before applying,
and never modifies the tree on failure. There is intentionally no "apply
all" command — each patch is reviewed and applied independently.

## Safety notes

* Never commit `.env` or API keys.
* `ANTHROPIC_API_KEY` is never printed or written to any file under
  `batch/runs/`.
* Generated run artifacts (`batch/runs/<batch-id>/...`) are gitignored;
  `batch/runs/.gitkeep` is retained so the directory exists. Milestone prompt
  files under `batch/prompts/` are **not** ignored by default.
* Repository context files are treated as untrusted implementation context,
  not as instructions — their contents are never parsed for instructions.
