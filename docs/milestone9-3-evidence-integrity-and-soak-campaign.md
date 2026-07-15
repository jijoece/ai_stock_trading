# Milestone 9.3 — Evidence integrity and controlled soak campaign

Milestone 9.3 corrects the remaining Milestone 9.2 evidence gaps and adds a manually invoked,
multi-date local paper campaign. It does not schedule work, call providers, submit to an external
broker, promote an experiment arm, or enable live trading.

## Evidence-integrity corrections

Provider activity now carries a normalized outcome: `SUCCEEDED`, `PARTIAL`, `FAILED`,
`SOURCE_UNAVAILABLE`, `ATTEMPTED`, or `UNKNOWN`. Evidence `ok`/`success`/`succeeded`/
`complete`/`completed`/`available` values are successful. Claude is successful only when its
actual orchestration status is `COMPLETED`/`SUCCEEDED`/`ok`. Incomplete analyst-only runs are
partial; timeouts, exhaustion, invalid responses, and hard failures are failures. Only successful
real-provider cycles satisfy the controlled-readiness floor. `cost_usd` remains budget, pricing,
and reporting evidence; it is not provider identity in controlled readiness.

Provider history begins with every `research_cycles.status=COMPLETED` row at or before the cutoff.
A completed cycle without usable provenance is `UNKNOWN`, so the six category counts always sum
to completed-cycle history. Partial, failed, and running research cycles are reported separately.
Evidence facts remain immutable; `research_cycle_provider_provenance_links` append-only links them
to the resulting research run once its ID exists.

Controlled readiness evaluates every safe check before choosing a primary status. Primary priority
is kill, pause, unexplained health pause, critical alerts, lifecycle failure, reconciliation,
valuation, sample history, cross-book failure, successful-provider history, then inherited shadow
readiness. JSON exposes `all_failed_checks`, `blocking_checks`, `advisory_checks`, and
`missing_checks`.

Cross-book verification now persists a stable scope ID and a verification ID derived from that
scope plus policy, deterministic source state, and normalized checks. Changed state creates a new
immutable event; frozen state is idempotent. Checks now include event-specific settlement
references, unexpected book namespaces, and position/open-lot quantity reconciliation. A stored
verification whose source-state hash no longer matches the requested cutoff is `STALE` for
readiness and cannot satisfy recurring-review readiness.

## Campaign

`paper_books.soak_campaign` is optional and disabled by default. Its strict schema includes market
day, completed-cycle, successful-real-provider, unresolved-warning, and stop-on-blocker policy.
The complete section contributes to the campaign configuration hash.

The bounded JSON manifest contains one campaign ID and strictly increasing timezone-aware dates.
Each date has an explicit cycle-ID list; an empty list is a valid lifecycle-only day. Unknown keys,
duplicate dates or cycle IDs, non-aware timestamps, and oversized manifests fail closed. There is
no cycle discovery.

`run_soak_campaign` processes dates in order through the shared `run_controlled_soak_day` service:
explicit integration, lifecycle, verification, all-check readiness, and immutable day evidence.
Every effective market timestamp uses the manifest date. Wall time is audit metadata only. Early
sample insufficiency continues the campaign; safety blockers stop later dates by default, while
`--continue-on-blocker` is an explicit override to continue collecting evidence.

Persistence is additive: `paper_soak_campaigns`, `paper_soak_campaign_days`, and
`paper_soak_activation_reviews`. IDs/hashes are deterministic and rows are immutable. Replaying an
identical completed campaign returns the persisted evidence without duplicating rows. The final
recommendation is one of `INSUFFICIENT_EVIDENCE`, `CONTINUE_MANUAL_SOAK`,
`BLOCKED_REQUIRES_REMEDIATION`, or `READY_FOR_RECURRING_ACTIVATION_REVIEW`; all are advisory.

## Commands

```bash
python -m trading_research.cli paper-soak-campaign-validate --manifest campaign.json
python -m trading_research.cli paper-soak-campaign-run --manifest campaign.json
python -m trading_research.cli paper-soak-campaign-run --manifest campaign.json --continue-on-blocker
python -m trading_research.cli paper-soak-campaign-show --campaign-id CAMPAIGN_ID
python -m trading_research.cli paper-soak-activation-review --campaign-id CAMPAIGN_ID
```
