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
    {"as_of": "2026-07-16T20:00:00Z", "cycle_ids": []}
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
  --manifest campaign.json --continue-on-blocker
```

The override does not clear pause/kill state or resolve alerts; affected dates remain `BLOCKED`.

Inspect immutable evidence:

```bash
python -m trading_research.cli paper-soak-campaign-show --campaign-id manual-soak-july-2026
python -m trading_research.cli paper-soak-activation-review --campaign-id manual-soak-july-2026
```

`READY_FOR_RECURRING_ACTIVATION_REVIEW` is a human-review recommendation only. This runbook does
not install cron/launchd, enable recurring execution, call a provider, submit to an external broker,
or promote the enhanced arm.
