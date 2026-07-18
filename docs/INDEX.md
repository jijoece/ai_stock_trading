# Documentation Index

Use this index before opening milestone documents. It identifies the smallest
authoritative document set for a task and prevents older implementation plans
from being mistaken for current behavior.

## Authority order

When sources disagree, use this order:

1. Current code, tests, schemas, and configuration
2. Accepted architecture decision records (ADRs)
3. Current operational runbooks
4. `README.md` and the architecture overview
5. Milestone implementation and developer guides
6. Audits, pending-work notes, and research material

Milestone documents explain how and why a capability was introduced. They are
historical context, not an override of current code or accepted ADRs. Do not
read multiple milestone variants unless the task requires comparing them.

## Start here

| Need | Canonical source |
|---|---|
| Project overview, setup, CLI, and package map | [`../README.md`](../README.md) |
| System boundaries and target architecture | [`AI-Driven-Stock-Trading-Architecture.md`](AI-Driven-Stock-Trading-Architecture.md) |
| Original roadmap and acceptance criteria | [`AI-Stock-Trading-Implementation-Plan.md`](AI-Stock-Trading-Implementation-Plan.md) |
| External research-source policy | [`AI-Stock-Trading-Research-Sources.md`](AI-Stock-Trading-Research-Sources.md) |
| Current remaining integrity work | [`milestone11-3-remaining-integrity-closure.md`](milestone11-3-remaining-integrity-closure.md) |
| Latest full integrity-closure specification | [`milestone11-2-full-integrity-closure.md`](milestone11-2-full-integrity-closure.md) |
| Latest repository audit | [`full-codebase-audit.md`](full-codebase-audit.md) |

## Architecture decisions

ADRs are canonical for the boundary they cover:

| Area | ADR |
|---|---|
| LumiBot paper runtime | [`adr/0001-lumibot-paper-runtime.md`](adr/0001-lumibot-paper-runtime.md) |
| Credentialed runtime process isolation | [`adr/0002-isolated-lumibot-runtime.md`](adr/0002-isolated-lumibot-runtime.md) |
| Claude research versus execution authority | [`adr/0003-claude-research-boundary.md`](adr/0003-claude-research-boundary.md) |
| Real evidence-provider boundary | [`adr/0004-real-evidence-provider-boundary.md`](adr/0004-real-evidence-provider-boundary.md) |
| Production shadow-operations boundary | [`adr/0005-production-shadow-operations-boundary.md`](adr/0005-production-shadow-operations-boundary.md) |
| Isolated paper books and evaluation | [`adr/0006-isolated-paper-books-and-portfolio-evaluation.md`](adr/0006-isolated-paper-books-and-portfolio-evaluation.md) |
| External paper-account isolation | [`adr/0007-external-paper-account-isolation.md`](adr/0007-external-paper-account-isolation.md) |

## Operational runbooks

Use runbooks for operator procedures; use code and tests for exact behavior.

| Operation | Runbook |
|---|---|
| Alpaca paper operations | [`runbooks/alpaca-paper-operations.md`](runbooks/alpaca-paper-operations.md) |
| Recurring local paper trading | [`runbooks/recurring-local-paper-trading.md`](runbooks/recurring-local-paper-trading.md) |
| Paper-book operations | [`runbooks/paper-book-operations.md`](runbooks/paper-book-operations.md) |
| Paper-book reconciliation | [`runbooks/paper-book-reconciliation.md`](runbooks/paper-book-reconciliation.md) |
| Manual paper-trading soak | [`runbooks/manual-paper-trading-soak.md`](runbooks/manual-paper-trading-soak.md) |
| Controlled paper soak | [`runbooks/controlled-paper-soak.md`](runbooks/controlled-paper-soak.md) |
| Paper-soak campaign | [`runbooks/paper-soak-campaign.md`](runbooks/paper-soak-campaign.md) |
| Soak evidence and alerts | [`runbooks/soak-evidence-and-alert-operations.md`](runbooks/soak-evidence-and-alert-operations.md) |
| Shadow operations | [`runbooks/shadow-operations.md`](runbooks/shadow-operations.md) |
| Shadow incident response | [`runbooks/shadow-incident-response.md`](runbooks/shadow-incident-response.md) |

## Supporting references

| Topic | Source | Status |
|---|---|---|
| Batch request construction | [`batch_creation.md`](batch_creation.md) | Supporting procedure |
| Batch result processing | [`batch_processing.md`](batch_processing.md) | Supporting procedure |
| Trading-desk requirements | [`trading_desk_requirement.md`](trading_desk_requirement.md) | Original requirements; verify against current code |
| Design and safety pitfalls | [`codebase-analysis-pitfalls.md`](codebase-analysis-pitfalls.md) | Canonical copy of the audit notes |
| Duplicate audit notes | [`pitfalls_and_improvements.md`](pitfalls_and_improvements.md) | Exact duplicate; do not read |

## Milestone history

Open these only when the task needs implementation history, original acceptance
criteria, or the rationale not captured in an ADR.

| Milestone | Primary specification | Developer or closure detail |
|---|---|---|
| 1 | [`milestone1-foundation.md`](milestone1-foundation.md) | Foundation developer guide |
| 2 | [`milestone-2.md`](milestone-2.md) | [`milestone2-analysis-layer.md`](milestone2-analysis-layer.md) |
| 3 | [`milestone-3.md`](milestone-3.md) | [`milestone3-lumibot-paper-integration.md`](milestone3-lumibot-paper-integration.md) |
| 4 | [`milestone-4.md`](milestone-4.md) | [`milestone4-isolated-paper-broker.md`](milestone4-isolated-paper-broker.md) |
| 5 | [`milestone-5.md`](milestone-5.md) | [`milestone5-evidence-backed-claude-research.md`](milestone5-evidence-backed-claude-research.md) |
| 6 | [`milestone-6.md`](milestone-6.md) | [`milestone6-real-evidence-continuous-evaluation.md`](milestone6-real-evidence-continuous-evaluation.md), [`milestone-6.1.md`](milestone-6.1.md) |
| 7 | [`milestone-7.md`](milestone-7.md) | [`milestone7-production-shadow-operations.md`](milestone7-production-shadow-operations.md), [`milestone-7.1.md`](milestone-7.1.md), [`milestone7-1-shadow-integration-closure.md`](milestone7-1-shadow-integration-closure.md), [`milestone-7.2.md`](milestone-7.2.md), [`milestone7-2-shadow-health-diagnostics.md`](milestone7-2-shadow-health-diagnostics.md) |
| 8 | [`milestone-8.md`](milestone-8.md) | [`milestone8-isolated-paper-portfolios.md`](milestone8-isolated-paper-portfolios.md), [`milestone-8.1.md`](milestone-8.1.md), [`milestone8-1-scheduled-paper-book-integration.md`](milestone8-1-scheduled-paper-book-integration.md) |
| 9 | [`milestone-9.md`](milestone-9.md) | [`milestone9-manual-paper-soak-and-lifecycle.md`](milestone9-manual-paper-soak-and-lifecycle.md), [`milestone-9.1.md`](milestone-9.1.md), [`milestone9-1-controlled-soak-readiness.md`](milestone9-1-controlled-soak-readiness.md), [`milestone-9.2.md`](milestone-9.2.md), [`milestone9-2-soak-evidence-integrity.md`](milestone9-2-soak-evidence-integrity.md), [`milestone-9-3-soak-campaign.md`](milestone-9-3-soak-campaign.md), [`milestone9-3-evidence-integrity-and-soak-campaign.md`](milestone9-3-evidence-integrity-and-soak-campaign.md), [`milestone9-3-1-campaign-integrity.md`](milestone9-3-1-campaign-integrity.md) |
| 10 | [`milestone10-controlled-recurring-local-paper.md`](milestone10-controlled-recurring-local-paper.md) | Current scheduler behavior remains defined by code and its runbook |
| 11 | [`milestone-11-alpaca-paper-boundary.md`](milestone-11-alpaca-paper-boundary.md) | [`milestone11-isolated-alpaca-paper-broker.md`](milestone11-isolated-alpaca-paper-broker.md), [`milestone11-1-external-paper-safety-closure.md`](milestone11-1-external-paper-safety-closure.md), [`milestone11-2-full-integrity-closure.md`](milestone11-2-full-integrity-closure.md), [`milestone11-3-remaining-integrity-closure.md`](milestone11-3-remaining-integrity-closure.md) |

## Superseded, duplicate, and pending notes

These are retained for history and should not be used for current behavior:

- [`milestone-7 pending.md`](milestone-7%20pending.md)
- [`milestone-7 pending copy.md`](milestone-7%20pending%20copy.md)
- [`milestone11-2-integrity-closure.md`](milestone11-2-integrity-closure.md) — use the full specification above
- [`milestone9-3-1-campaign-resumability-and-point-in-time-integrity.md`](milestone9-3-1-campaign-resumability-and-point-in-time-integrity.md) — alternate Milestone 9.3.1 detail
- [`pitfalls_and_improvements.md`](pitfalls_and_improvements.md) — exact duplicate
