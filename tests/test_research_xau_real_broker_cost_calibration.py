from __future__ import annotations

import ast
from hashlib import sha256
import json
from pathlib import Path

import pytest

import quasartrend.research.xau_real_broker_cost_calibration as calibration


def test_protocol_is_deterministic_and_matches_the_frozen_artifact() -> None:
    protocol = calibration.build_xau_real_broker_cost_calibration_protocol()
    payload = calibration.xau_real_broker_cost_calibration_protocol_json(protocol)
    assert payload == Path(calibration.PROTOCOL_PATH).read_bytes()
    assert calibration.protocol_sha256(protocol) == calibration.expected_protocol_sha256()
    assert sha256(payload).hexdigest() == calibration.EXPECTED_PROTOCOL_SHA256
    assert payload == calibration.xau_real_broker_cost_calibration_protocol_json(calibration.build_xau_real_broker_cost_calibration_protocol())


def test_protocol_contains_exact_frozen_identity_and_unknowns() -> None:
    protocol = calibration.build_xau_real_broker_cost_calibration_protocol()
    assert protocol["canonical_starting_state"]["sha"] == "af02995705506ab1629f558e6fbdabe13d2d0785"
    assert protocol["canonical_starting_state"]["waiting_tag_object"] == "a95c26c7cb5458df48bb49bad5791b4e22cba972"
    assert protocol["directional_confirmation_freeze"]["protocol_sha256"] == "c201170afb974b4299e06608556ee49acfd2b11c950e0076a841e8d4412629ef"
    assert protocol["frozen_historical_validation"]["canonical_starting_sha"] == "446f93cfbad601a7517caac54fb2f2791fc2e5fe"
    assert protocol["frozen_historical_validation"]["population"] == {"observed_setups": 2162, "eligible_setups": 1072, "opened_trades": 820, "closed_trades": 820, "censored": 0}
    assert protocol["broker_identity"]["account_type"] == "UNKNOWN / UNVERIFIED"
    assert protocol["broker_identity"]["account_currency"] == "UNKNOWN"


def test_predeclared_semantics_formulas_and_decisions_are_unchanged() -> None:
    protocol = calibration.build_xau_real_broker_cost_calibration_protocol()
    assert protocol["spread"]["raw_column_semantics"] == "MqlRates minimum spread per M1 bar in integer MT5 points."
    assert protocol["spread"]["classification"] == "OBSERVED — M1 BAR-MINIMUM SPREAD, NOT POINT-IN-TIME EXECUTABLE SPREAD"
    assert protocol["spread"]["points_to_price_formula"] == "spread_price = spread_points * symbol_point (0.01)"
    assert "same-timestamp M1 row is the following interval and prohibited" in protocol["spread"]["lookup"]
    assert "cannot complete S1" in protocol["scenarios"]["S1_observed_core"]
    assert protocol["conversion"]["component_cost_r_formula"] == "component_cost_account / initial_risk_account"
    assert protocol["conversion"]["frozen_trade_risk_binding"] == "initial_risk_price_i = abs(entry_price_i - stop_price_i) from the frozen ledger."
    assert "missing quantity blocks account-unit trade values" in protocol["conversion"]["monetary_risk_requirement"]
    assert protocol["conversion"]["net_r"] == "gross_R - total_cost_R"
    assert protocol["scenarios"]["S2_realistic_execution"].startswith("S1 plus modelled one verified tick adverse")
    assert "cannot overcome missing essential S1" in protocol["scenarios"]["missing_rule"]
    assert protocol["decision_framework"]["precedence"] == ["WAITING", "FAIL", "PASS", "CONDITIONAL"]
    assert "utilization < 0.75" in protocol["decision_framework"]["PASS"]
    assert protocol["hard_restrictions"]["long_only"] == "NO"
    assert protocol["hard_restrictions"]["btc_phase_7_4"] == "DEFERRED"


def test_lock_is_immutable_and_blocks_evaluator_before_verification(tmp_path: Path) -> None:
    path = tmp_path / "protocol.json"
    protocol = calibration.build_xau_real_broker_cost_calibration_protocol()
    digest = calibration.write_xau_real_broker_cost_calibration_protocol(protocol, path)
    assert digest == calibration.expected_protocol_sha256()
    with pytest.raises(FileExistsError, match="immutable protocol lock"):
        calibration.write_xau_real_broker_cost_calibration_protocol(protocol, path)
    called = False
    def evaluator(locked: dict[str, object]) -> str:
        nonlocal called
        called = True
        return str(locked["schema_version"])
    assert calibration.protocol_before_economics(path, digest, evaluator) == calibration.SCHEMA_VERSION
    assert called is True
    modified = json.loads(path.read_bytes())
    modified["stage"] = "changed"
    path.write_bytes(calibration.xau_real_broker_cost_calibration_protocol_json(modified))
    called = False
    with pytest.raises(ValueError, match="hash mismatch|bytes differ"):
        calibration.protocol_before_economics(path, digest, evaluator)
    assert called is False


def test_protocol_module_does_not_import_or_open_economic_implementations() -> None:
    module_path = Path(calibration.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"quasartrend.backtest", "quasartrend.execution", "quasartrend.replay", "quasartrend.strategy", "xm_gold_historical_validation"}
    assert not imports.intersection(forbidden)
    builder = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_xau_real_broker_cost_calibration_protocol")
    builder_source = ast.get_source_segment(module_path.read_text(encoding="utf-8"), builder)
    assert builder_source is not None
    assert "open(" not in builder_source and "read_bytes(" not in builder_source and "read_text(" not in builder_source
