# Paper soak campaign runbook

Use a persistent evaluation database. Enable both isolated books, lifecycle processing, and
`paper_books.soak_campaign` only through `config/paper_books.yaml`. Enabling the campaign does not
enable a scheduler.

Prepare a bounded JSON manifest:

```json
{
  "campaign_id": "manual-soak-july-2026",
  "dates": [
    {"as_of": "2026-07-15T20:00:00Z", "cycle_ids": ["cycle-a"]},
    {"as_of": "2026-07-16T20:00:00Z", "cycle_ids": []},
    {"as_of": "2026-07-18T20:00:00Z", "cycle_ids": [], "lifecycle_only": true}
  ]
}
```

Validate and run manually:

```bash
python -m trading_research.cli paper-soak-campaign-validate --manifest campaign.json
python -m trading_research.cli paper-soak-campaign-run --manifest campaign.json
```

A hard blocker records the blocking date and later dates as `SKIPPED_AFTER_BLOCKER`. Continue only
after explicit operator review:

```bash
python -m trading_research.cli paper-soak-campaign-run \
  --manifest campaign.json --continue-on-blocker \
  --operator alice --reason "pause cause remediated"
```

Continuation creates attempt 2 and never rewrites attempt 1. Completed dates are not rerun. Resume
an interrupted `RUNNING` attempt, or continue without the original manifest, with:

```bash
python -m trading_research.cli paper-soak-campaign-resume \
  --campaign-id manual-soak-july-2026 \
  --operator alice --reason "recover interrupted attempt"
```

Complete operator evidence is reconstructed without repeating lifecycle mutation. Uncertain stage
evidence becomes `RECOVERY_REQUIRES_REVIEW`. Neither command clears pause/kill state or resolves
alerts.

Inspect immutable evidence:

```bash
python -m trading_research.cli paper-soak-campaign-show --campaign-id manual-soak-july-2026
python -m trading_research.cli paper-soak-activation-review --campaign-id manual-soak-july-2026
```

Campaign display lists every attempt and identifies the latest attempt and review. Identical frozen
evidence returns the same review; remediation creates a later immutable review with a supersession
link. Reviews use only campaign-associated evidence at the campaign cutoff, including the final
snapshot for open positions.

All timestamps must be timezone-aware and canonicalize to UTC. Normal dates must be trading days
at or after regular New York close. Non-trading dates require `lifecycle_only: true` and empty cycle
IDs. The offline calendar does not model early closes; do not use its 4:00 p.m. assumption for a
half-day close valuation.

Readiness uses qualifying real-provider cycles: every observed real-provider category must succeed.
Partial, failed, unavailable, attempted, or unknown real outcomes disqualify the cycle. Historical
cross-book checks use cutoff-bounded immutable evidence and may report insufficient data when safe
reconstruction is unavailable.

`READY_FOR_RECURRING_ACTIVATION_REVIEW` is a human-review recommendation only. This runbook does
not install cron/launchd, enable recurring execution, call a provider, submit to an external broker,
or promote the enhanced arm.
