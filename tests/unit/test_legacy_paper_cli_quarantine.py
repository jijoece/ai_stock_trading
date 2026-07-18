"""Milestone 11.3 Part 33: the legacy `paper/ledger.py` subsystem's CLI
commands must be renamed to an unambiguous `legacy-paper-*` prefix and gated
behind an explicit acknowledgement flag, so an operator cannot accidentally
invoke the deprecated pre-`paper_books` ledger while intending to operate on
the active `paper_books` subsystem."""
from __future__ import annotations

import pytest

from trading_research import cli as cli_mod


@pytest.mark.parametrize("old_name", ["paper-status", "execute-paper", "sync-paper-orders", "reconcile-paper"])
def test_old_legacy_command_names_no_longer_exist(old_name, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main([old_name])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert old_name in captured.err
    assert "invalid choice" in captured.err


@pytest.mark.parametrize("new_name", [
    "legacy-paper-status", "legacy-paper-execute", "legacy-paper-sync-orders", "legacy-paper-reconcile",
])
def test_renamed_legacy_command_requires_explicit_acknowledgement_flag(new_name, capsys):
    extra = ["--recommendation-id", "rec-1"] if new_name == "legacy-paper-execute" else []
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main([new_name, *extra])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--i-understand-this-is-the-legacy-ledger" in captured.err


@pytest.mark.parametrize("new_name", [
    "legacy-paper-status", "legacy-paper-execute", "legacy-paper-sync-orders", "legacy-paper-reconcile",
])
def test_top_level_help_marks_legacy_commands_deprecated(new_name, capsys):
    with pytest.raises(SystemExit):
        cli_mod.main(["--help"])
    captured = capsys.readouterr()
    assert new_name in captured.out
    # argparse line-wraps each command's help text under its own indented
    # block; find that block (from the command name up to the next
    # 4-space-indented command name) and confirm DEPRECATED appears in it —
    # can't check a single line since help text wraps across several.
    lines = captured.out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == new_name)
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("    ") and not line.startswith("        "):
            break
        block.append(line)
    assert any("DEPRECATED" in line for line in block), f"no DEPRECATED marker in help block: {block}"


def test_active_paper_books_commands_are_unaffected(capsys):
    """Sanity check: the active-subsystem command names were never touched
    by this quarantine and still parse (they fail later for missing
    positional args, not because the command itself is unrecognized)."""
    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(["paper-book-show"])
    captured = capsys.readouterr()
    assert "invalid choice" not in captured.err
    assert exc_info.value.code == 2  # missing required --book-id, not an unknown command
