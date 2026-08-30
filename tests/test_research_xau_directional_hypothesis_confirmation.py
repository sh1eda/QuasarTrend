from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

import quasartrend.research.xau_directional_hypothesis_confirmation as confirmation


def _candidate(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "sha256": "a" * 64, "row_count": 10,
        "normalized_first_epoch_ms": confirmation.CONFIRMATION_START_MS,
        "normalized_last_epoch_ms": confirmation.LAST_REQUIRED_CONFIRMATION_BAR_MS,
        "provider": "XM", "server": "XMGlobal-MT5 18", "instrument": "GOLD",
        "digits": 2, "point": 0.01,
        "previously_inspected_directional_or_persistence_economics": False,
        "previously_used_for_discovery": False,
        "all_rows_strictly_after_discovery_boundary": True,
        "timestamp_provider_semantics_verified": True,
        "warmup_verified": True,
        "fixed_confirmation_window_complete": True,
        "provider_gaps_verified": True,
        "acquired_after_protocol_commit": True,
    }
    value.update(changes)
    return value


def _trade(identifier: str, setup_id: str, direction: str, r: float, *, exit_timestamp: int | None = None, bucket: str = "LOW", stop: bool = False) -> dict[str, object]:
    return {"trade_id": identifier, "setup_id": setup_id, "direction": direction, "r": r,
            "exit_timestamp": confirmation.CONFIRMATION_START_MS + 1 if exit_timestamp is None else exit_timestamp,
            "persistence_bucket": bucket, "stop_hit": stop}


def test_discovery_and_committed_protocol_hashes_are_exact() -> None:
    assert confirmation.verify_discovery_hashes(Path(".")) == {
        confirmation.DISCOVERY_PROTOCOL_PATH: confirmation.DISCOVERY_PROTOCOL_SHA256,
        confirmation.DISCOVERY_RESULT_PATH: confirmation.DISCOVERY_RESULT_SHA256,
    }
    protocol = confirmation.verify_committed_protocol(Path(confirmation.PROTOCOL_PATH))
    assert confirmation.protocol_sha256(protocol) == confirmation.EXPECTED_PROTOCOL_SHA256
    assert confirmation.verify_frozen_xm_source_identity(Path(confirmation.FROZEN_XM_SOURCE_PATH)) == {
        "sha256": confirmation.FROZEN_XM_SOURCE_SHA256,
        "row_count": 1_000_000,
        "normalized_last_epoch_ms": confirmation.FROZEN_XM_LAST_NORMALIZED_MS,
    }


def test_untouched_source_admission_and_known_data_rejection() -> None:
    assert confirmation.admit_untouched_source(_candidate())["admitted"] is True
    frozen = confirmation.admit_untouched_source(_candidate(sha256=confirmation.FROZEN_XM_SOURCE_SHA256))
    assert frozen["admitted"] is False and "known discovery artifact" in frozen["reasons"][0]
    inspected = confirmation.admit_untouched_source(_candidate(previously_inspected_directional_or_persistence_economics=True))
    assert inspected["admitted"] is False
    incomplete = confirmation.admit_untouched_source(_candidate(normalized_last_epoch_ms=confirmation.CONFIRMATION_START_MS + 60_000))
    assert incomplete["admitted"] is False and "window end" in incomplete["reasons"][0]
    session_gap_start = confirmation.admit_untouched_source(_candidate(normalized_first_epoch_ms=confirmation.CONFIRMATION_START_MS + 60_000))
    assert session_gap_start["admitted"] is True
    invalid_hash = confirmation.admit_untouched_source(_candidate(sha256="g" * 64))
    assert invalid_hash["admitted"] is False and invalid_hash["reasons"][0] == "invalid raw SHA-256"


@pytest.mark.parametrize("source_hash", [
    confirmation.DISCOVERY_PROTOCOL_SHA256,
    confirmation.DISCOVERY_RESULT_SHA256,
    confirmation.FROZEN_XM_SOURCE_SHA256,
])
def test_all_known_discovery_hashes_and_periods_are_excluded(source_hash: str) -> None:
    assert confirmation.is_known_discovery_observation(source_sha256=source_hash, normalized_timestamp_ms=confirmation.CONFIRMATION_START_MS)
    assert confirmation.is_known_discovery_observation(source_sha256="b" * 64, normalized_timestamp_ms=confirmation.FROZEN_XM_LAST_NORMALIZED_MS)
    rejected = confirmation.admit_untouched_source(_candidate(normalized_first_epoch_ms=confirmation.FROZEN_XM_LAST_NORMALIZED_MS))
    assert rejected["admitted"] is False


def test_frozen_persistence_definition_and_boundary_buckets() -> None:
    assert confirmation.frozen_bias_persistence_hours(7_200_000, 0) == 2.0
    assert confirmation.frozen_bias_persistence_hours(1, None) is None
    with pytest.raises(ValueError):
        confirmation.frozen_bias_persistence_hours(0, 1)
    assert [confirmation.frozen_persistence_bucket(value) for value in (None, -1, 39.75, 39.750001, 116.5, 116.500001)] == ["MISSING", "MISSING", "LOW", "MEDIUM", "MEDIUM", "HIGH"]


def test_population_construction_and_exact_cutoff_enforcement() -> None:
    start = confirmation.CONFIRMATION_START_MS
    end = confirmation.CONFIRMATION_END_EXCLUSIVE_MS
    setups = [
        {"setup_id": "before", "setup_origin_timestamp": start - 1},
        {"setup_id": "included", "setup_origin_timestamp": start},
        {"setup_id": "censored", "setup_origin_timestamp": start + 1},
        {"setup_id": "at-end", "setup_origin_timestamp": end},
        {"setup_id": "ineligible", "setup_origin_timestamp": start + 1, "eligible": False},
    ]
    population = confirmation.construct_confirmation_population(setups, [
        _trade("a", "before", "LONG", 9.0), _trade("b", "included", "LONG", 2.0, bucket="HIGH"),
        _trade("c", "censored", "SHORT", -1.0, exit_timestamp=end, bucket="LOW"),
        _trade("d", "at-end", "SHORT", 4.0),
    ])
    assert population["included_setup_ids"] == ["censored", "included"]
    assert population["overall"]["observed_setups"] == 3
    assert population["overall"]["eligible_setups"] == population["overall"]["opened"] == 2
    assert population["overall"]["closed"] == 1 and population["overall"]["censored"] == 1
    assert population["closed_trade_ledger"][0]["trade_id"] == "b"


def test_direction_and_persistence_aggregation_and_minimum_cell_label() -> None:
    rows = [
        _trade("l", "s", "LONG", 2.0, bucket="HIGH"),
        _trade("s1", "s", "SHORT", -1.0, bucket="LOW", stop=True),
        _trade("s2", "s", "SHORT", 3.0, bucket="LOW"),
    ]
    directions = confirmation.aggregate_direction_metrics(rows)
    assert directions["LONG"]["expectancy_r"] == 2.0
    assert directions["SHORT"]["total_r"] == 2.0 and directions["SHORT"]["stop_rate"] == .5
    persistence = confirmation.aggregate_persistence_metrics(rows)
    assert persistence["LOW"]["SHORT"]["expectancy_r"] == 1.0
    assert persistence["HIGH"]["LONG"]["count"] == 1
    assert persistence["MISSING"]["SHORT"]["closed_trades"] == 0
    assert confirmation.minimum_cell_label({"closed_trades": 29}) == confirmation.SMALL_SAMPLE_LABEL
    assert confirmation.minimum_cell_label({"closed_trades": 30}) is None


def test_deterministic_serialization_and_protocol_before_results_guard(tmp_path: Path) -> None:
    value = {"z": [1, 2], "a": {"b": True}}
    assert confirmation.confirmation_json(value) == b'{"a":{"b":true},"z":[1,2]}\n'
    protocol_path = Path(confirmation.PROTOCOL_PATH)
    called = False
    def evaluator(_protocol: dict[str, object]) -> str:
        nonlocal called
        called = True
        return "synthetic-only"
    assert confirmation.protocol_before_results(protocol_path, evaluator) == "synthetic-only"
    assert called is True
    tampered = tmp_path / "protocol.json"
    payload = json.loads(protocol_path.read_bytes())
    payload["schema_version"] = "tampered"
    tampered.write_bytes(confirmation.confirmation_json(payload))
    called = False
    with pytest.raises(ValueError, match="committed protocol path|hash mismatch"):
        confirmation.protocol_before_results(tampered, evaluator)
    assert called is False
    with pytest.raises(RuntimeError, match="WAITING FOR UNTOUCHED DATA"):
        confirmation.execute_confirmatory_economics()


def test_population_rejects_closed_exit_before_setup_origin_or_confirmation_start() -> None:
    setup = {"setup_id": "s", "setup_origin_timestamp": confirmation.CONFIRMATION_START_MS}
    with pytest.raises(ValueError, match="precedes"):
        confirmation.construct_confirmation_population([setup], [
            _trade("too-early", "s", "LONG", 1.0, exit_timestamp=confirmation.CONFIRMATION_START_MS - 1),
        ])


def test_inventory_selects_time_metadata_from_real_format_temporary_csv(tmp_path: Path) -> None:
    path = tmp_path / "tradingview.csv"
    path.write_text("time,open,high,low,close\n10,1,1,1,1\n20,1,1,1,1\n", encoding="utf-8")
    inventory = confirmation.inventory_source_identity(path)
    assert inventory["timestamp_column"] == "time"
    assert inventory["row_count"] == 2
    assert inventory["first_labeled_timestamp"] == 10 and inventory["last_labeled_timestamp"] == 20


def test_waiting_status_is_deterministic_and_only_inventories_xau_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def synthetic_inventory(path: Path) -> dict[str, object]:
        return {"path": str(path), "sha256": sha256(str(path).encode()).hexdigest(), "row_count": 1,
                "timestamp_column": "time_epoch", "first_labeled_timestamp": 1, "last_labeled_timestamp": 1}
    monkeypatch.setattr(confirmation, "inventory_source_identity", synthetic_inventory)
    monkeypatch.setattr(confirmation, "verify_frozen_xm_source_identity", lambda _path: {
        "sha256": confirmation.FROZEN_XM_SOURCE_SHA256,
        "row_count": confirmation.FROZEN_XM_SOURCE_ROWS,
        "normalized_last_epoch_ms": confirmation.FROZEN_XM_LAST_NORMALIZED_MS,
    })
    first = confirmation.build_waiting_status(Path("."))
    second = confirmation.build_waiting_status(Path("."))
    assert confirmation.confirmation_json(first) == confirmation.confirmation_json(second)
    assert first["phase_status"] == confirmation.WAITING_PHASE_STATUS
    assert first["protocol_commit_sha"] == confirmation.PROTOCOL_COMMIT_SHA
    assert len(first["available_xau_source_inventory"]) == 3
    assert [item["path"] for item in first["available_xau_source_inventory"]] == [
        confirmation.FROZEN_XM_SOURCE_PATH,
        "exports/xauusd_pending/XAUUSD_15m.csv",
        "exports/xauusd_pending/XAUUSD_4h.csv",
    ]
    assert all(not item["path"].startswith(str(Path(".").resolve())) for item in first["available_xau_source_inventory"])
    assert first["wrong_instrument_exclusions"][0]["category"] == "BTC golden CSVs"
    assert first["hypotheses"] == {"H1": "WAITING FOR UNTOUCHED DATA", "H2": "WAITING FOR UNTOUCHED DATA"}
    assert first["sample_adequacy"].startswith("UNAVAILABLE")
    assert first["production_strategy_change"] == first["production_market_change"] == "NO"
