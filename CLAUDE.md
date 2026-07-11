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
