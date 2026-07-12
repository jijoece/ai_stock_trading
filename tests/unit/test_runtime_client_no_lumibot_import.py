"""Milestone 4 tightens the existing AST-walk boundary
(`test_lumibot_adapter.py::test_no_lumibot_import_outside_runtime_package`,
which excludes the entire `runtime/` package because `runtime/lumibot/` is
allowed to import LumiBot). That existing test is intentionally left
unmodified. This test additionally confirms the specific new Milestone 4
modules under `runtime/` other than `runtime/lumibot/` — the runtime
client, its protocol/models/errors, and the paper-runtime config loader —
never import LumiBot either, since they are unambiguously part of the main
trading-desk process (docs/milestone-4.md: "The main trading-desk process
must not directly import LumiBot")."""
from __future__ import annotations

import ast
import pathlib

SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "trading_research"

MUST_NOT_IMPORT_LUMIBOT = [
    SRC_ROOT / "runtime" / "client" / "__init__.py",
    SRC_ROOT / "runtime" / "client" / "errors.py",
    SRC_ROOT / "runtime" / "client" / "protocol.py",
    SRC_ROOT / "runtime" / "client" / "models.py",
    SRC_ROOT / "runtime" / "client" / "process_client.py",
    SRC_ROOT / "runtime" / "deterministic_adapter.py",
    SRC_ROOT / "runtime" / "paper_runtime_config.py",
    SRC_ROOT / "execution" / "broker_snapshots.py",
    SRC_ROOT / "execution" / "account_reconciliation.py",
    SRC_ROOT / "services" / "submit_credentialed_paper_order.py",
    SRC_ROOT / "services" / "sync_paper_orders.py",
    SRC_ROOT / "services" / "reconcile_paper.py",
    SRC_ROOT / "evaluation" / "market_calendar.py",
    SRC_ROOT / "evaluation" / "models.py",
    SRC_ROOT / "evaluation" / "price_provider.py",
    SRC_ROOT / "evaluation" / "evaluation_service.py",
    SRC_ROOT / "evaluation" / "metrics.py",
]


def _imports_lumibot(path: pathlib.Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "lumibot" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "lumibot":
            return True
    return False


def test_all_listed_files_exist():
    missing = [str(p) for p in MUST_NOT_IMPORT_LUMIBOT if not p.is_file()]
    assert missing == [], f"expected Milestone 4 files are missing: {missing}"


def test_none_of_these_milestone4_modules_import_lumibot():
    offenders = [str(p) for p in MUST_NOT_IMPORT_LUMIBOT if _imports_lumibot(p)]
    assert offenders == []
