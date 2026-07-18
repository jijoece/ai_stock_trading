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
| Latest completed integrity implementation | [`milestone12-1-1-provider-health-closure.md`](milestone12-1-1-provider-health-closure.md) |
| Prior completed integrity implementation | [`milestone12-1-provider-health-ci-integrity.md`](milestone12-1-provider-health-ci-integrity.md) |
| CI job requirements and branch-protection configuration | [`ci-branch-protection.md`](ci-branch-protection.md) |
| Prior completed integrity implementation | [`milestone11-3-2-operational-integrity.md`](milestone11-3-2-operational-integrity.md) |
| Prior integrity implementation | [`milestone11-3-1-safety-closure.md`](milestone11-3-1-safety-closure.md) |
| Milestone 11.3.1 specification | [`milestones/milestone-11.3.1.md`](milestones/milestone-11.3.1.md) |
| Prior completed integrity implementation | [`milestones/milestone11-3-integrity-closure.md`](milestones/milestone11-3-integrity-closure.md) |
| Milestone 11.3 specification | [`milestones/milestone11-3-remaining-integrity-closure.md`](milestones/milestone11-3-remaining-integrity-closure.md) |
| Latest full integrity-closure specification | [`milestones/milestone11-2-full-integrity-closure.md`](milestones/milestone11-2-full-integrity-closure.md) |
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
| Claude Code production research | [`claude-code-production-provider.md`](claude-code-production-provider.md) |
| Codex production research | [`codex-production-provider.md`](codex-production-provider.md) |
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
| 1 | [`milestones/milestone1-foundation.md`](milestones/milestone1-foundation.md) | Foundation developer guide |
| 2 | [`milestones/milestone-2.md`](milestones/milestone-2.md) | [`milestones/milestone2-analysis-layer.md`](milestones/milestone2-analysis-layer.md) |
| 3 | [`milestones/milestone-3.md`](milestones/milestone-3.md) | [`milestones/milestone3-lumibot-paper-integration.md`](milestones/milestone3-lumibot-paper-integration.md) |
| 4 | [`milestones/milestone-4.md`](milestones/milestone-4.md) | [`milestones/milestone4-isolated-paper-broker.md`](milestones/milestone4-isolated-paper-broker.md) |
| 5 | [`milestones/milestone-5.md`](milestones/milestone-5.md) | [`milestones/milestone5-evidence-backed-claude-research.md`](milestones/milestone5-evidence-backed-claude-research.md) |
| 6 | [`milestones/milestone-6.md`](milestones/milestone-6.md) | [`milestones/milestone6-real-evidence-continuous-evaluation.md`](milestones/milestone6-real-evidence-continuous-evaluation.md), [`milestones/milestone-6.1.md`](milestones/milestone-6.1.md) |
| 7 | [`milestones/milestone-7.md`](milestones/milestone-7.md) | [`milestones/milestone7-production-shadow-operations.md`](milestones/milestone7-production-shadow-operations.md), [`milestones/milestone-7.1.md`](milestones/milestone-7.1.md), [`milestones/milestone7-1-shadow-integration-closure.md`](milestones/milestone7-1-shadow-integration-closure.md), [`milestones/milestone-7.2.md`](milestones/milestone-7.2.md), [`milestones/milestone7-2-shadow-health-diagnostics.md`](milestones/milestone7-2-shadow-health-diagnostics.md) |
| 8 | [`milestones/milestone-8.md`](milestones/milestone-8.md) | [`milestones/milestone8-isolated-paper-portfolios.md`](milestones/milestone8-isolated-paper-portfolios.md), [`milestones/milestone-8.1.md`](milestones/milestone-8.1.md), [`milestones/milestone8-1-scheduled-paper-book-integration.md`](milestones/milestone8-1-scheduled-paper-book-integration.md) |
| 9 | [`milestones/milestone-9.md`](milestones/milestone-9.md) | [`milestones/milestone9-manual-paper-soak-and-lifecycle.md`](milestones/milestone9-manual-paper-soak-and-lifecycle.md), [`milestones/milestone-9.1.md`](milestones/milestone-9.1.md), [`milestones/milestone9-1-controlled-soak-readiness.md`](milestones/milestone9-1-controlled-soak-readiness.md), [`milestones/milestone-9.2.md`](milestones/milestone-9.2.md), [`milestones/milestone9-2-soak-evidence-integrity.md`](milestones/milestone9-2-soak-evidence-integrity.md), [`milestones/milestone-9-3-soak-campaign.md`](milestones/milestone-9-3-soak-campaign.md), [`milestones/milestone9-3-evidence-integrity-and-soak-campaign.md`](milestones/milestone9-3-evidence-integrity-and-soak-campaign.md), [`milestones/milestone9-3-1-campaign-integrity.md`](milestones/milestone9-3-1-campaign-integrity.md) |
| 10 | [`milestones/milestone10-controlled-recurring-local-paper.md`](milestones/milestone10-controlled-recurring-local-paper.md) | Current scheduler behavior remains defined by code and its runbook |
| 11 | [`milestones/milestone-11-alpaca-paper-boundary.md`](milestones/milestone-11-alpaca-paper-boundary.md) | [`milestone11-3-2-operational-integrity.md`](milestone11-3-2-operational-integrity.md), [`milestone11-3-1-safety-closure.md`](milestone11-3-1-safety-closure.md), [`milestones/milestone11-isolated-alpaca-paper-broker.md`](milestones/milestone11-isolated-alpaca-paper-broker.md), [`milestones/milestone11-1-external-paper-safety-closure.md`](milestones/milestone11-1-external-paper-safety-closure.md), [`milestones/milestone11-2-full-integrity-closure.md`](milestones/milestone11-2-full-integrity-closure.md), [`milestones/milestone11-3-integrity-closure.md`](milestones/milestone11-3-integrity-closure.md), [`milestones/milestone11-3-remaining-integrity-closure.md`](milestones/milestone11-3-remaining-integrity-closure.md) |

## Superseded, duplicate, and pending notes

These are retained for history and should not be used for current behavior:

- [`milestones/milestone-7 pending.md`](milestones/milestone-7%20pending.md)
- [`milestones/milestone-7 pending copy.md`](milestones/milestone-7%20pending%20copy.md)
- [`milestones/milestone11-2-integrity-closure.md`](milestones/milestone11-2-integrity-closure.md) — use the full specification above
- [`milestones/milestone9-3-1-campaign-resumability-and-point-in-time-integrity.md`](milestones/milestone9-3-1-campaign-resumability-and-point-in-time-integrity.md) — superseded by the campaign-integrity document above
- [`pitfalls_and_improvements.md`](pitfalls_and_improvements.md) — exact duplicate of `codebase-analysis-pitfalls.md`
