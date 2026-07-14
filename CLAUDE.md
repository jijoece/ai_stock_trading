# Claude Code Project Instructions

This project uses the trading skill in SKILL.md for all analysis and decision support.

## Always follow these rules
- Use the instructions in SKILL.md whenever the user asks to analyze a ticker, review positions, decide entries/exits/rebuys, calculate indicators, score with the three-pillar framework, or manage the Agentic account.
- Never calculate indicators by reasoning over price bars; fetch the data and run the deterministic scripts in the scripts/ directory.
- Respect the guardrails in SKILL.md: protected positions, account roles, T+1 cash rules, and the requirement for explicit user confirmation before executing orders.
- Use the local scripts for computation:
  - scripts/indicators.py
  - scripts/score.py
  - scripts/macro_pillar.py

## Python code intelligence

Prefer the Pyright LSP for Python navigation and diagnostics:

- Use definitions, references, symbols, implementations, hover types, and call hierarchy before broad grep or full-file reads.
- Read only the relevant symbol body when sufficient.
- Treat Pyright diagnostics as guidance; verify behavior with the project's tests.
- Do not perform broad type-error cleanup unless the task explicitly requests it.
