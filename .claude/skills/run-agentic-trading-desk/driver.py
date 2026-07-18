#!/usr/bin/env python3
"""
Smoke-test driver for agentic-trading-desk's deterministic scripts.

Exercises the three CLI entry points (scripts/indicators.py, scripts/score.py,
scripts/macro_pillar.py) the same way the agent invokes them: as subprocesses
consuming a JSON file and emitting either a human-readable table or --json.
Also exercises the direct-import path (score_symbol / compute / score_macro),
which is what a Python-side test or a PR touching internals should use
instead of shelling out.

Usage:
    python3 .claude/skills/run-agentic-trading-desk/driver.py
"""
from __future__ import annotations
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


def synthetic_close(seed: int, n: int = 291, start: float = 100.0, drift: tuple[float, float] = (-0.02, 0.025)) -> list[float]:
    rng = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(*drift)))
    return [round(c, 4) for c in closes]


def synthetic_macro_series(seed: int, n: int = 260) -> dict:
    rng = random.Random(seed)

    def series(start: float) -> list[float]:
        s = [start]
        for _ in range(n - 1):
            s.append(s[-1] * (1 + rng.uniform(-0.015, 0.017)))
        return [round(x, 2) for x in s]

    return {
        "as_of": "2026-07-09",
        "yield_spread": -0.15,
        "series": {
            "SPY": series(450), "RSP": series(160), "IWM": series(198),
            "HYG": series(78), "LQD": series(108), "TLT": series(92),
            "XLY": series(180), "XLP": series(75),
        },
    }


def run_cli(args: list[str], tmp_path: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, *args], cwd=ROOT, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout, proc.stderr


def check(label: str, cond: bool, detail: str = ""):
    status = "OK  " if cond else "FAIL"
    print(f"[{status}] {label} {detail}".rstrip())
    if not cond:
        raise SystemExit(f"driver check failed: {label} {detail}")


def main() -> int:
    tmp = Path("/tmp/agentic_trading_desk_smoke")
    tmp.mkdir(exist_ok=True)

    # --- indicators.py: self-test, file input, short-series warning ---
    rc, out, err = run_cli(["scripts/indicators.py"], tmp)
    check("indicators.py self-test exits 0", rc == 0, err)
    check("indicators.py self-test emits JSON", '"n_bars"' in out)

    ticker_path = tmp / "ticker.json"
    ticker_path.write_text(json.dumps({"close": synthetic_close(42)}))
    rc, out, err = run_cli(["scripts/indicators.py", str(ticker_path)], tmp)
    ind = json.loads(out)
    check("indicators.py file input exits 0", rc == 0, err)
    check("indicators.py computes ema200", ind["ema200"] is not None)
    check("indicators.py no warning at 291 bars", ind["warning"] is None, str(ind["warning"]))

    short_path = tmp / "short.json"
    short_path.write_text(json.dumps({"close": [100, 101, 102, 101, 103, 104, 102, 105]}))
    rc, out, err = run_cli(["scripts/indicators.py", str(short_path)], tmp)
    short_ind = json.loads(out)
    check("indicators.py flags short series", short_ind["warning"] is not None)
    check("indicators.py nulls ema200 when too short", short_ind["ema200"] is None)

    # --- macro_pillar.py: self-test, file input, --json ---
    rc, out, err = run_cli(["scripts/macro_pillar.py"], tmp)
    check("macro_pillar.py self-test exits 0", rc == 0, err)
    check("macro_pillar.py self-test prints regime", "Regime" in out)

    macro_path = tmp / "macro.json"
    macro_path.write_text(json.dumps(synthetic_macro_series(7)))
    rc, out, err = run_cli(["scripts/macro_pillar.py", str(macro_path), "--json"], tmp)
    macro = json.loads(out)
    check("macro_pillar.py --json exits 0", rc == 0, err)
    check("macro_pillar.py pillar_score in range", -2 <= macro["pillar_score"] <= 2, str(macro["pillar_score"]))
    check("macro_pillar.py has 6 components", len(macro["components"]) == 6, str(len(macro["components"])))

    # --- score.py: self-test, file input, --json, missing-file error ---
    rc, out, err = run_cli(["scripts/score.py"], tmp)
    check("score.py self-test exits 0", rc == 0, err)
    check("score.py self-test prints decision", "HOLD" in out or "EXIT" in out or "WAIT" in out or "RE-ENTRY" in out or "OBSERVE" in out or "STAY OUT" in out)

    score_path = tmp / "score_input.json"
    score_path.write_text(json.dumps({
        "symbol": "SMOKETEST", "close": synthetic_close(42),
        "macro_score": macro["pillar_score"], "holding": True,
    }))
    rc, out, err = run_cli(["scripts/score.py", str(score_path), "--json"], tmp)
    card = json.loads(out)
    check("score.py --json exits 0", rc == 0, err)
    check("score.py pillar_total in range", -6 <= card["pillar_total"] <= 6, str(card["pillar_total"]))
    check("score.py has decision.action", bool(card["decision"]["action"]))

    rc, out, err = run_cli(["scripts/score.py", "/tmp/does_not_exist_smoketest.json"], tmp)
    check("score.py exits nonzero on missing file", rc != 0)

    # --- direct-import path (no subprocess) ---
    sys.path.insert(0, str(SCRIPTS))
    from score import score_symbol  # noqa: E402
    from indicators import compute  # noqa: E402
    from macro_pillar import score_macro  # noqa: E402

    ind2 = compute(synthetic_close(1))
    check("indicators.compute() callable directly", ind2["ema200"] is not None)

    macro_result = score_macro(synthetic_macro_series(1))
    check("macro_pillar.score_macro() callable directly", -2 <= macro_result.pillar_score <= 2)

    card2 = score_symbol(synthetic_close(1), macro_score=macro_result.pillar_score, holding=False)
    check("score.score_symbol() callable directly", bool(card2["decision"]["action"]))

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
