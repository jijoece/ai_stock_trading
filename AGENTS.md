# Codex Project Instructions

This is a research and paper-trading system. Python computes; Codex explains;
the user decides and explicitly approves any external paper-order action.

## Project navigation

- Start with `docs/INDEX.md` when documentation is needed. Prefer its canonical
  current documents over milestone history.
- Treat code, tests, configuration, ADRs, and current runbooks as authoritative
  when historical milestone documents disagree.
- Use the `run-agentic-trading-desk` skill for ticker scoring, deterministic
  indicator computation, or verification of the scripts in `scripts/`.
- Use the `deep-dive` skill only for an explicitly requested formal deep dive,
  end-to-end investigation, or evidence-backed audit.

## Trading and computation guardrails

- Never calculate indicators by reasoning over price bars. Fetch the data and
  run `scripts/indicators.py`, `scripts/score.py`, or
  `scripts/macro_pillar.py` as appropriate.
- Default all broker interaction to read-only. Never submit an external paper
  order without an explicit request and the repository's confirmation gates.
- Do not describe research output as financial advice.

## Python code intelligence

Prefer the Pyright LSP for Python navigation and diagnostics:

- Use definitions, references, symbols, implementations, hover types, and call hierarchy before broad grep or full-file reads.
- Read only the relevant symbol body when sufficient.
- Treat Pyright diagnostics as guidance; verify behavior with the project's tests.
- Do not perform broad type-error cleanup unless the task explicitly requests it.

## Compact instructions

When compacting, preserve the current goal, decisions, changed files and
symbols, test failures and passes, unresolved risks, and remaining work.
Discard raw search results, successful command output, historical-document
excerpts, repeated explanations, and superseded hypotheses.
