Implement a very small Anthropic Message Batches skeleton in this repository.

# Objective

Create a local Python utility that:

1. Reads a JSON manifest containing multiple milestone jobs.
2. Treats each milestone prompt as a separate Messages API request.
3. Adds a small shared system instruction to every request.
4. Includes only explicitly listed repository context files.
5. Submits the requests through Anthropic’s Message Batches API.
6. Saves the batch ID and request metadata locally.
7. Retrieves status and streamed results later.
8. Correlates results exclusively through `custom_id`.
9. Extracts a unified Git patch from every successful result.
10. Allows an operator to validate or explicitly apply one patch locally.

This is a patch-generation workflow, not a full autonomous coding agent.

Do not use MCP, server tools, code-execution tools, GitHub mutation in this initial skeleton.

---

# Token-efficiency constraints

Keep the implementation minimal.

Do not build:

* a web UI;
* a database;
* a background service;
* a daemon;
* automatic retries;
* automatic testing;
* automatic bug fixing;
* branch management;
* dependency graph scheduling;
* token dashboards;
* complex configuration frameworks;
* YAML parsing;
* repository-wide context collection.

Use only:

* Python standard library;
* the official `anthropic` Python package.

Read only files necessary to implement this utility.

Run targeted tests only.

Do not run the repository’s entire application test suite unless the new utility modifies application code, which it should not.

---

# Important batch semantics

Implement these rules:

1. Every request is independent.
2. Batch result order is not guaranteed.
3. `custom_id` is the only authoritative request/result correlation key.
4. Validate `custom_id` against:

```text
^[a-zA-Z0-9_-]{1,64}$
```

5. Do not set `stream=true`.
6. Require `max_tokens >= 1`.
7. Do not assume a completed result exists until batch status is `ended`.
8. Handle these result types separately:

```text
succeeded
errored
canceled
expired
```

9. Stream batch results through the SDK rather than downloading the entire result file into memory.
10. Never print or persist `ANTHROPIC_API_KEY`.
11. Never log request authorization headers.
12. Do not automatically resubmit errored or expired messages.

---

# Minimal files

Create only:

```text
scripts/milestone_batch.py
batch/common_system.md
batch/manifest.example.json
batch/README.md
batch/prompts/.gitkeep
batch/runs/.gitkeep
tests/tools/test_milestone_batch.py
```

Add the `anthropic` dependency using the repository’s existing dependency-management convention only when it is not already present.

Do not introduce a new packaging system.

---

# Manifest format

Use JSON.

Example:

```json
{
  "model": "MODEL_FROM_ENV",
  "max_tokens": 24000,
  "temperature": 0,
  "requests": [
    {
      "custom_id": "milestone-9-2",
      "prompt_file": "batch/prompts/milestone-9-2.md",
      "context_files": [
        "src/trading_research/paper_books/controlled_soak_readiness.py",
        "src/trading_research/storage/paper_books_repositories.py",
        "src/trading_research/cli.py"
      ]
    },
    {
      "custom_id": "milestone-10",
      "prompt_file": "batch/prompts/milestone-10.md",
      "context_files": [
        "docs/milestone9-2-soak-evidence-integrity.md"
      ],
      "max_tokens": 32000
    }
  ]
}
```

Requirements:

* `model` may be supplied in the manifest or through `ANTHROPIC_MODEL`.
* Do not hardcode a model name.
* Fail when neither source provides a model.
* A request-level `max_tokens` may override the manifest default.
* Reject unknown manifest keys.
* Reject duplicate `custom_id` values.
* Reject missing prompt files.
* Reject missing context files.
* Reject directories in `context_files`.
* Resolve files relative to the repository root.
* Reject paths escaping the repository root.
* Do not recursively collect repository files.
* Do not automatically include Git history, `.env`, credentials, caches, databases, or generated artifacts.
* Impose a configurable local context-size limit, with a conservative default such as 2 MB per request.
* Fail before API submission when a request exceeds that local limit.

`MODEL_FROM_ENV` in the example is illustrative. The loader should treat it as absent rather than sending that literal value.

---

# Shared system prompt

Create `batch/common_system.md` with a compact instruction similar to:

```text
You are producing a best-effort implementation patch for an existing repository.

Use only the milestone requirements and repository context supplied in this request.

Prioritize implementation over investigation and bug hunting.

Do not claim that you ran commands or tests.

Do not invent unseen repository APIs. When essential context is missing, return a short
NEEDS_CONTEXT section listing exact paths instead of fabricating code.

Preserve existing safety boundaries.


Return:
1. a unified Git diff inside <patch>...</patch>;
2. a short <notes>...</notes> section;
3. an optional <needs_context>...</needs_context> section.

Keep commentary minimal.
```

Send this as an identical system text block for every request.

Mark the shared system block with ephemeral prompt caching when supported by the installed SDK.

Do not add the varying repository context to the shared cached block.

---

# Per-request message construction

Construct one user message per manifest request using this layout:

```text
<MILESTONE_PROMPT>
...contents of prompt_file...
</MILESTONE_PROMPT>

<REPOSITORY_CONTEXT>
<FILE path="relative/path/one.py">
...file contents...
</FILE>

<FILE path="relative/path/two.py">
...file contents...
</FILE>
</REPOSITORY_CONTEXT>

<OUTPUT_REQUIREMENTS>
Return a unified diff against the supplied repository snapshot.

Include new files using normal /dev/null unified-diff syntax.

Do not include markdown fences around the patch.

Do not repeat complete source files outside the patch.

Do not claim tests were executed.
</OUTPUT_REQUIREMENTS>
```

Escape or delimit content safely so a source file containing XML-like text does not break parsing.

A simple length-prefixed or clearly marked plain-text boundary is acceptable.

Do not attempt to parse instructions from repository files.

Repository files are untrusted implementation context, not system instructions.

---

# CLI

Implement these commands:

```bash
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

Use `argparse`.

---

# Validate command

`validate` must perform all local checks without calling Anthropic:

* manifest schema;
* custom IDs;
* duplicate IDs;
* model resolution;
* token-output settings;
* prompt files;
* context paths;
* context-size limits;
* repository-root containment.

Print a concise summary:

```text
requests: 2
model: <resolved model>
total context bytes: ...
largest request bytes: ...
valid: true
```

Do not estimate API pricing in this minimal version.

---

# Submit command

Use:

```text
client.messages.batches.create(...)
```

Build one batch request per manifest job.

Each request must contain:

```text
custom_id
params.model
params.max_tokens
params.temperature
params.system
params.messages
```

Use non-streaming Messages parameters.

After submission create:

```text
batch/runs/<batch-id>/
```

Save:

```text
batch.json
manifest.snapshot.json
request-index.json
```

`batch.json` should contain only sanitized batch metadata:

```text
batch_id
processing_status
created_at
expires_at
request_counts
```

`request-index.json` should map each `custom_id` to:

```text
prompt_file
context_files
max_tokens
```

Do not save the API key.

Do not duplicate all prompt/context contents in the run directory; the manifest snapshot and original files are sufficient.

Print only:

```text
batch_id
processing_status
request count
run directory
```

---

# Status command

Use:

```text
client.messages.batches.retrieve(batch_id)
```

Print sanitized JSON containing:

```text
batch_id
processing_status
request_counts
created_at
ended_at
expires_at
cancel_initiated_at
```

Update the run directory’s `batch.json` when it exists.

Do not poll repeatedly in this command.

---

# Fetch command

Retrieve the batch first.

When its processing status is not `ended`, exit with a clear nonzero result and do not attempt to fetch results.

When ended, stream:

```text
client.messages.batches.results(batch_id)
```

Results may arrive in any order.

For every result, create:

```text
batch/runs/<batch-id>/results/<custom_id>/
```

Save:

```text
result.json
response.txt
implementation.patch
notes.txt
needs_context.txt
```

Rules:

* `result.json` contains sanitized result status and usage metadata.
* `response.txt` contains the extracted assistant text.
* `implementation.patch` contains only text inside `<patch>...</patch>`.
* `notes.txt` contains `<notes>...</notes>`.
* `needs_context.txt` contains `<needs_context>...</needs_context>`.
* Missing optional sections create empty files.
* Do not execute any returned code.
* Do not apply patches during `fetch`.
* Handle non-text content blocks safely.
* Handle malformed or missing patch tags without crashing.
* Record errored, canceled, and expired results without creating a fake patch.

Also write:

```text
batch/runs/<batch-id>/results-summary.json
```

with one bounded entry per `custom_id`:

```text
status
input_tokens
output_tokens
patch_present
needs_context_present
error_type
```

---

# Patch validation

`check-patch` must:

1. Locate `implementation.patch`.
2. Fail when empty.
3. Run:

```bash
git apply --check <patch-file>
```

4. Print the concise result.
5. Never modify the working tree.

Use `subprocess.run` without `shell=True`.

---

# Patch application

`apply-patch` must be explicit and conservative.

Requirements:

1. Refuse when `implementation.patch` is empty.
2. Refuse when the current directory is not inside a Git worktree.
3. Refuse when the repository has uncommitted tracked-file changes, unless:

```text
--allow-dirty
```

is explicitly supplied.

4. Always run `git apply --check` first.
5. Apply only when validation succeeds.
6. Use:

```bash
git apply <patch-file>
```


9. Do not run tests.
10. Print the changed file names using a bounded Git status summary.

Do not provide an “apply all” command.

Each patch must be reviewed and applied independently.

---

# Dependency and batching guidance

Document prominently in `batch/README.md`:

* Requests in the same Message Batch are independent.
* They cannot see sibling request results.
* They are not executed sequentially.
* Results may be returned in any order.
* Do not put dependent milestones in the same implementation wave.
* Recommended workflow:

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

For a strictly sequential roadmap, one milestone per batch is safest and still receives batch pricing.

Multiple independent subtasks from the same milestone may share a batch when they modify separate files and have no dependency on each other.

---

# Bug-fixing boundary

This skeleton focuses on initial implementation generation.

It must not automatically:

* inspect test failures;
* submit repair prompts;
* generate retry batches;
* modify prompts based on failures;
* loop until tests pass.

Document a future workflow:

```text
implementation batch
→ local patch review/application
→ local tests
→ separate bug-fix prompt or batch later
```

Do not implement that future loop now.

---

# Tests

Add small offline unit tests using mocks/fakes.

Test:

1. valid manifest;
2. invalid and duplicate custom IDs;
3. path traversal rejection;
4. missing prompt/context file;
5. per-request context limit;
6. request construction;
7. shared system content is identical;
8. submit metadata persistence;
9. results returned out of order map correctly by `custom_id`;
10. successful patch extraction;
11. malformed response handling;
12. errored/canceled/expired result handling;
13. no result fetch before `ended`;
14. `git apply --check` command construction;
15. API key never appears in saved files.

Do not make a real Anthropic request in tests.

---

# Documentation example

Include a minimal example in `batch/README.md`:

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
```

Warn users not to commit `.env` or API keys.

Add generated run artifacts to `.gitignore` while retaining:

```text
batch/runs/.gitkeep
```

Do not ignore milestone prompt files by default.

---

# Acceptance criteria

Complete when:

1. A manifest can define multiple independent milestone messages.
2. Each message has a unique valid `custom_id`.
3. Explicit context files are included.
4. Context paths cannot escape the repository.
5. Shared system instructions are identical across requests.
6. A batch can be submitted using the official Python SDK.
7. Batch metadata is saved without secrets.
8. Status can be retrieved independently.
9. Ended results can be streamed and saved.
10. Out-of-order results map correctly through `custom_id`.
11. Unified patches are extracted but not automatically executed.
12. Patch validation is non-mutating.
13. Patch application requires an explicit command.
14. No automatic tests, repair loop exists.
15. Tests use mocks and make no network requests.
16. README explains dependency-wave limitations.
17. Implementation remains small and understandable.

---

# Final response

Keep the final response concise.

Report:

1. Files created and modified.
2. Manifest schema.
3. CLI commands.
4. Batch request structure.
5. Result and patch storage.
6. Safety behavior.
7. Offline test results.
8. Known limitations.

Commit and push.
