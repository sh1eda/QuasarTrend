#!/usr/bin/env python3
"""Compare two timestamp-aligned indicator diagnostic CSV files."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Any

from quasartrend.indicators.golden import cold_start_convergence, compare_seeded_export


NA_TEXT = {"", "na", "nan", "null", "none"}


def _number(value: str) -> float | None:
    try:
        return float("nan") if value.strip().lower() in NA_TEXT else float(value)
    except ValueError:
        return None


def _equal(left: str, right: str, *, rel_tol: float, abs_tol: float) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return left.strip().lower() == right.strip().lower()
    if math.isnan(left_number) or math.isnan(right_number):
        return math.isnan(left_number) and math.isnan(right_number)
    return math.isclose(left_number, right_number, rel_tol=rel_tol, abs_tol=abs_tol)


def _read(path: Path, key: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or key not in reader.fieldnames:
            raise ValueError(f"{path}: missing key column {key!r}")
        order: list[str] = []
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            row_key = row[key]
            if row_key in rows:
                raise ValueError(f"{path}: duplicate key {row_key!r}")
            order.append(row_key)
            rows[row_key] = row
    return order, rows


def compare(
    expected_path: Path,
    actual_path: Path,
    *,
    key: str,
    rel_tol: float,
    abs_tol: float,
    context: int,
) -> int:
    expected_order, expected = _read(expected_path, key)
    actual_order, actual = _read(actual_path, key)
    if expected_order != actual_order:
        print("row-key sequence mismatch")
        print(f"expected-only: {sorted(set(expected) - set(actual))[:10]}")
        print(f"actual-only: {sorted(set(actual) - set(expected))[:10]}")
        return 1

    common_columns = sorted(set(next(iter(expected.values()))) & set(next(iter(actual.values()))) - {key})
    if not common_columns:
        print("no common comparison columns")
        return 2

    for index, row_key in enumerate(expected_order):
        mismatches = [
            column
            for column in common_columns
            if not _equal(expected[row_key][column], actual[row_key][column], rel_tol=rel_tol, abs_tol=abs_tol)
        ]
        if mismatches:
            print(f"first mismatch at row {index}, {key}={row_key!r}")
            for column in mismatches:
                print(
                    f"  {column}: expected={expected[row_key][column]!r} "
                    f"actual={actual[row_key][column]!r}"
                )
            start = max(0, index - context)
            end = min(len(expected_order), index + context + 1)
            print(f"context keys: {expected_order[start:end]}")
            return 1
    print(f"matched {len(expected_order)} rows across {len(common_columns)} columns")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expected", type=Path, nargs="?", help="TradingView/exported expected CSV")
    parser.add_argument("actual", type=Path, nargs="?", help="Python diagnostic CSV")
    parser.add_argument(
        "--golden",
        type=Path,
        action="append",
        default=[],
        help="audit and verify a TradingView parity export using its first complete recursive checkpoint",
    )
    parser.add_argument("--key", default="timestamp", help="unique row-alignment column")
    parser.add_argument("--rel-tol", type=float, default=1e-12)
    parser.add_argument("--abs-tol", type=float, default=1e-9)
    parser.add_argument("--context", type=int, default=2)
    parser.add_argument(
        "--min-converged-candles",
        type=int,
        default=40,
        help="minimum exact suffix required to accept cold-start convergence",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.golden:
            failed = False
            for path in args.golden:
                comparison = compare_seeded_export(
                    path, rel_tol=args.rel_tol, abs_tol=args.abs_tol
                )
                convergence = cold_start_convergence(
                    path, rel_tol=args.rel_tol, abs_tol=args.abs_tol
                )
                audit = comparison.audit
                print(f"{path}: {audit.timeframe_label}")
                print(
                    f"  rows={audit.row_count} source_parity={'PASS' if audit.source_parity_passes else 'FAIL'} "
                    f"gaps={audit.continuity_gap_count} ohlc_mismatches={audit.source_ohlc_mismatch_count}"
                )
                print(
                    f"  checkpoint_seed_rows={comparison.excluded_seed_candles} "
                    f"compared={comparison.compared_candles} mismatched_candles={comparison.mismatch_count} "
                    f"state_seeded_recurrence_parity={'PASS' if comparison.passes else 'FAIL'}"
                )
                convergence_passes = (
                    convergence.acceptance_candles >= max(1, args.min_converged_candles)
                )
                print(
                    f"  cold_start_acceptance_index={convergence.overall_acceptance_start} "
                    f"cold_start_acceptance_candles={convergence.acceptance_candles} "
                    f"cold_start_convergence={'PASS' if convergence_passes else 'INSUFFICIENT'}"
                )
                external_passes = comparison.passes and convergence_passes
                print(f"  external_acceptance={'PASS' if external_passes else 'FAIL'}")
                failed = failed or not external_passes
            return 1 if failed else 0
        if args.expected is None or args.actual is None:
            raise ValueError("provide EXPECTED ACTUAL, or at least one --golden export")
        return compare(
            args.expected,
            args.actual,
            key=args.key,
            rel_tol=args.rel_tol,
            abs_tol=args.abs_tol,
            context=max(0, args.context),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
