"""
tests/test_reconcile.py — Comprehensive test suite for the reconciliation engine.

Structured by layer, with hand-built fixtures (no generator), plus
property tests and an end-to-end seed-based integration test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from collections import Counter

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconcile import (
    validate_platform_data,
    validate_bank_data,
    detect_duplicates_platform,
    detect_duplicates_bank,
    exact_match,
    fuzzy_match,
    classify_unmatched,
    detect_orphan_refunds,
    compute_aggregate,
    reconcile,
    load_csv,
    GapCategory,
    IST,
    DEFAULT_TOLERANCE_PAISE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def _make_platform_row(
    txn_id: str = "TXN-00000001",
    day: int = 15,
    customer_id: str = "CUST-00001",
    txn_type: str = "PAYMENT",
    gross: int = 10000,
    fee: int = 235,
    net: int | None = None,
    original_txn_id: str = "",
    status: str = "SUCCESS",
    month: int = 10,
    year: int = 2026,
) -> dict:
    """Build a normalised platform row (what validate_platform_data outputs)."""
    if net is None:
        net = gross - fee
    return {
        "txn_id": txn_id,
        "created_at": datetime(year, month, day, 12, 0, 0, tzinfo=IST),
        "customer_id": customer_id,
        "type": txn_type,
        "gross_amount": gross,
        "fee": fee,
        "net_amount": net,
        "currency": "INR",
        "original_txn_id": original_txn_id,
        "status": status,
    }


def _make_bank_row(
    settlement_id: str = "STL-00000001",
    bank_reference: str = "TXN-00000001",
    day: int = 16,
    amount: int = 9765,
    batch_id: str = "BATCH-20261016-001",
    month: int = 10,
    year: int = 2026,
) -> dict:
    return {
        "settlement_id": settlement_id,
        "bank_reference": bank_reference,
        "settled_at": datetime(year, month, day, 10, 0, 0, tzinfo=IST),
        "batch_id": batch_id,
        "amount": amount,
        "currency": "INR",
    }


# ===================================================================
# LAYER 1: Validation
# ===================================================================

class TestValidation:
    """Tests for input validation and normalisation."""

    def test_valid_platform_row(self):
        """A well-formed platform row passes validation."""
        rows = [{
            "txn_id": "TXN-001",
            "created_at": "2026-10-15T12:00:00+05:30",
            "customer_id": "CUST-001",
            "type": "PAYMENT",
            "gross_amount": "10000",
            "fee": "235",
            "net_amount": "9765",
            "currency": "INR",
            "original_txn_id": "",
            "status": "SUCCESS",
        }]
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 1
        assert clean[0]["gross_amount"] == 10000
        assert isinstance(clean[0]["created_at"], datetime)

    def test_valid_bank_row(self):
        """A well-formed bank row passes validation."""
        rows = [{
            "settlement_id": "STL-001",
            "bank_reference": "TXN-001",
            "settled_at": "2026-10-16T10:00:00+05:30",
            "batch_id": "BATCH-001",
            "amount": "9765",
            "currency": "INR",
        }]
        clean, errors = validate_bank_data(rows)
        assert len(clean) == 1
        assert clean[0]["amount"] == 9765

    def test_empty_platform_data(self):
        """Empty platform data produces an error, not a crash."""
        clean, errors = validate_platform_data([])
        assert len(clean) == 0
        assert any("empty" in e.lower() for e in errors)

    def test_empty_bank_data(self):
        """Empty bank data produces an error, not a crash."""
        clean, errors = validate_bank_data([])
        assert len(clean) == 0
        assert any("empty" in e.lower() for e in errors)

    def test_malformed_timestamp(self):
        """A malformed timestamp is reported, row is skipped."""
        rows = [{
            "txn_id": "TXN-001",
            "created_at": "not-a-date",
            "customer_id": "CUST-001",
            "type": "PAYMENT",
            "gross_amount": "10000",
            "fee": "235",
            "net_amount": "9765",
            "currency": "INR",
            "original_txn_id": "",
            "status": "SUCCESS",
        }]
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 0
        assert any("timestamp" in e.lower() or "invalid" in e.lower() for e in errors)

    def test_malformed_amount(self):
        """A non-numeric amount is reported."""
        rows = [{
            "txn_id": "TXN-001",
            "created_at": "2026-10-15T12:00:00+05:30",
            "customer_id": "CUST-001",
            "type": "PAYMENT",
            "gross_amount": "abc",
            "fee": "235",
            "net_amount": "9765",
            "currency": "INR",
            "original_txn_id": "",
            "status": "SUCCESS",
        }]
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 0
        assert any("convert" in e.lower() for e in errors)

    def test_missing_column(self):
        """A missing required column is reported."""
        rows = [{"txn_id": "TXN-001"}]  # Missing almost everything
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 0
        assert len(errors) > 0

    def test_invalid_type(self):
        """An invalid transaction type is caught."""
        rows = [{
            "txn_id": "TXN-001",
            "created_at": "2026-10-15T12:00:00+05:30",
            "customer_id": "CUST-001",
            "type": "CHARGEBACK",  # Invalid
            "gross_amount": "10000",
            "fee": "235",
            "net_amount": "9765",
            "currency": "INR",
            "original_txn_id": "",
            "status": "SUCCESS",
        }]
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 0

    def test_timezone_normalisation(self):
        """UTC timestamps are normalised to IST."""
        rows = [{
            "txn_id": "TXN-001",
            "created_at": "2026-10-15T06:30:00+00:00",  # UTC = 12:00 IST
            "customer_id": "CUST-001",
            "type": "PAYMENT",
            "gross_amount": "10000",
            "fee": "235",
            "net_amount": "9765",
            "currency": "INR",
            "original_txn_id": "",
            "status": "SUCCESS",
        }]
        clean, errors = validate_platform_data(rows)
        assert len(clean) == 1
        assert clean[0]["created_at"].hour == 12  # IST


# ===================================================================
# LAYER 2: Duplicate Detection
# ===================================================================

class TestDuplicateDetection:
    """Tests for duplicate detection within each dataset."""

    def test_no_duplicates_platform(self):
        """Clean data produces no duplicate findings."""
        rows = [
            _make_platform_row(txn_id="TXN-001"),
            _make_platform_row(txn_id="TXN-002"),
        ]
        deduped, dups, findings = detect_duplicates_platform(rows)
        assert len(deduped) == 2
        assert len(dups) == 0
        assert len(findings) == 0

    def test_duplicate_on_platform_side(self):
        """A duplicate txn_id on platform is detected with correct impact."""
        rows = [
            _make_platform_row(txn_id="TXN-DUP", net=5000),
            _make_platform_row(txn_id="TXN-DUP", net=5000),
            _make_platform_row(txn_id="TXN-OK", net=3000),
        ]
        deduped, dups, findings = detect_duplicates_platform(rows)
        assert len(deduped) == 2  # TXN-DUP (once) + TXN-OK
        assert len(dups) == 1
        assert len(findings) == 1
        assert findings[0].category == GapCategory.DUPLICATE_PLATFORM
        assert findings[0].amount_impact_paise == 5000  # One extra duplicate

    def test_duplicate_on_bank_side(self):
        """A duplicate bank_reference on bank is detected."""
        rows = [
            _make_bank_row(settlement_id="STL-001", bank_reference="TXN-DUP", amount=5000),
            _make_bank_row(settlement_id="STL-002", bank_reference="TXN-DUP", amount=5000),
            _make_bank_row(settlement_id="STL-003", bank_reference="TXN-OK", amount=3000),
        ]
        deduped, dups, findings = detect_duplicates_bank(rows)
        assert len(deduped) == 2
        assert len(dups) == 1
        assert len(findings) == 1
        assert findings[0].category == GapCategory.DUPLICATE_BANK
        # Bank dup: negative impact (bank overpays)
        assert findings[0].amount_impact_paise == -5000

    def test_triple_duplicate(self):
        """Three copies of the same ID: keep one, flag two extras."""
        rows = [
            _make_platform_row(txn_id="TXN-TRIP", net=1000),
            _make_platform_row(txn_id="TXN-TRIP", net=1000),
            _make_platform_row(txn_id="TXN-TRIP", net=1000),
        ]
        deduped, dups, findings = detect_duplicates_platform(rows)
        assert len(deduped) == 1
        assert len(dups) == 2
        assert findings[0].amount_impact_paise == 2000  # 2 extras × 1000


# ===================================================================
# LAYER 3: Exact Matching
# ===================================================================

class TestExactMatch:
    """Tests for exact key matching with amount tolerance."""

    def test_exact_match_happy_path(self):
        """Matching txn_id and amount within tolerance."""
        platform = [_make_platform_row(txn_id="TXN-001", net=10000)]
        bank = [_make_bank_row(bank_reference="TXN-001", amount=10000)]
        matched, up, ub, findings = exact_match(platform, bank)
        assert len(matched) == 1
        assert len(up) == 0
        assert len(ub) == 0
        assert matched[0]["residual"] == 0

    def test_match_within_tolerance(self):
        """Amount differs by exactly 1 paise — within tolerance."""
        platform = [_make_platform_row(txn_id="TXN-001", net=10000)]
        bank = [_make_bank_row(bank_reference="TXN-001", amount=10001)]
        matched, up, ub, findings = exact_match(platform, bank, tolerance_paise=1)
        assert len(matched) == 1
        assert matched[0]["residual"] == 1
        assert len(findings) == 0  # Within tolerance: no AMOUNT_MISMATCH finding

    def test_match_just_outside_tolerance(self):
        """Amount differs by 2 paise with tolerance=1 — AMOUNT_MISMATCH."""
        platform = [_make_platform_row(txn_id="TXN-001", net=10000)]
        bank = [_make_bank_row(bank_reference="TXN-001", amount=10002)]
        matched, up, ub, findings = exact_match(platform, bank, tolerance_paise=1)
        assert len(matched) == 1  # Still matched on key
        assert len(findings) == 1
        assert findings[0].category == GapCategory.AMOUNT_MISMATCH

    def test_unmatched_platform(self):
        """Platform txn with no bank settlement."""
        platform = [_make_platform_row(txn_id="TXN-001")]
        bank = [_make_bank_row(bank_reference="TXN-002")]
        matched, up, ub, findings = exact_match(platform, bank)
        assert len(matched) == 0
        assert len(up) == 1
        assert len(ub) == 1

    def test_failed_txn_skipped(self):
        """FAILED transactions are not matched."""
        platform = [_make_platform_row(txn_id="TXN-001", status="FAILED")]
        bank = [_make_bank_row(bank_reference="TXN-001")]
        matched, up, ub, findings = exact_match(platform, bank)
        assert len(matched) == 0

    def test_refund_matching(self):
        """Refunds match when bank amount is negative of net_amount."""
        platform = [_make_platform_row(
            txn_id="TXN-R001", txn_type="REFUND",
            gross=5000, fee=0, net=5000,
            original_txn_id="TXN-001"
        )]
        bank = [_make_bank_row(bank_reference="TXN-R001", amount=-5000)]
        matched, up, ub, findings = exact_match(platform, bank)
        assert len(matched) == 1
        assert matched[0]["residual"] == 0


# ===================================================================
# LAYER 4: Fuzzy Matching
# ===================================================================

class TestFuzzyMatch:
    """Tests for fuzzy matching fallback."""

    def test_fuzzy_match_same_amount_close_time(self):
        """Same amount and close timestamp should fuzzy match."""
        platform = [_make_platform_row(txn_id="TXN-001", net=10000, day=15)]
        bank = [_make_bank_row(bank_reference="TXN-XXX", amount=10000, day=16)]
        fuzzy, up, ub, findings = fuzzy_match(platform, bank)
        assert len(fuzzy) == 1
        assert findings[0].category == GapCategory.PROBABLE_MATCH
        assert findings[0].confidence >= 0.5

    def test_fuzzy_no_match_different_amount(self):
        """Very different amounts should not fuzzy match."""
        platform = [_make_platform_row(txn_id="TXN-001", net=10000, day=15)]
        bank = [_make_bank_row(bank_reference="TXN-XXX", amount=50000, day=16)]
        fuzzy, up, ub, findings = fuzzy_match(platform, bank)
        assert len(fuzzy) == 0
        assert len(up) == 1


# ===================================================================
# LAYER 5: Classification
# ===================================================================

class TestClassification:
    """Tests for gap classification."""

    def test_timing_difference_last_day(self):
        """Transaction on Oct 31 (Friday) with T+1 settling Nov 3 (Monday)."""
        # Oct 31, 2026 is a Saturday actually. Let's use Oct 30 (Friday).
        ptxn = _make_platform_row(txn_id="TXN-TIMING", day=30)
        # Oct 30 is a Friday in 2026
        findings = classify_unmatched([ptxn], [], set(), recon_month=10, recon_year=2026)
        assert len(findings) == 1
        assert findings[0].category == GapCategory.TIMING_DIFFERENCE

    def test_timing_difference_mid_month(self):
        """Transaction on Oct 15 should NOT be a timing difference."""
        ptxn = _make_platform_row(txn_id="TXN-MID", day=15)
        findings = classify_unmatched([ptxn], [], set(), recon_month=10, recon_year=2026)
        assert len(findings) == 1
        assert findings[0].category == GapCategory.MISSING_SETTLEMENT

    def test_orphan_refund_detected(self):
        """Refund with non-existent original_txn_id is detected."""
        refund = _make_platform_row(
            txn_id="TXN-R001", txn_type="REFUND",
            gross=5000, fee=0, net=5000,
            original_txn_id="TXN-GHOST"
        )
        all_ids = {"TXN-R001"}  # Only the refund exists
        findings = detect_orphan_refunds([refund], all_ids)
        assert len(findings) == 1
        assert findings[0].category == GapCategory.ORPHAN_REFUND

    def test_valid_refund_not_orphan(self):
        """Refund with a valid original_txn_id is NOT an orphan."""
        refund = _make_platform_row(
            txn_id="TXN-R001", txn_type="REFUND",
            gross=5000, fee=0, net=5000,
            original_txn_id="TXN-ORIG"
        )
        all_ids = {"TXN-R001", "TXN-ORIG"}
        findings = detect_orphan_refunds([refund], all_ids)
        assert len(findings) == 0

    def test_missing_transaction_bank_side(self):
        """Bank settlement with no platform txn."""
        brow = _make_bank_row(bank_reference="TXN-UNKNOWN")
        findings = classify_unmatched([], [brow], set())
        assert len(findings) == 1
        assert findings[0].category == GapCategory.MISSING_TRANSACTION

    def test_weekend_settlement_timing(self):
        """Friday transaction (Oct 30) should be classified as timing difference."""
        # Oct 30, 2026 is a Friday. T+1 = Monday Nov 2.
        ptxn = _make_platform_row(txn_id="TXN-FRI", day=30)
        findings = classify_unmatched([ptxn], [], set(), recon_month=10, recon_year=2026)
        assert len(findings) == 1
        assert findings[0].category == GapCategory.TIMING_DIFFERENCE
        assert findings[0].details["days_over_cutoff"] >= 1


# ===================================================================
# LAYER 6: Aggregate / Rounding
# ===================================================================

class TestAggregate:
    """Tests for aggregate reconciliation and rounding drift."""

    def test_rounding_row_level_passes_aggregate_fails(self):
        """CRITICAL TEST: Row-level matching passes (within tolerance) but
        aggregate shows drift. This proves the rounding gap type works."""
        # Create 100 rows where bank is 1 paise more than platform each time
        platform = []
        bank = []
        for i in range(100):
            tid = f"TXN-R{i:04d}"
            platform.append(_make_platform_row(txn_id=tid, net=10000))
            bank.append(_make_bank_row(bank_reference=tid, amount=10001))

        matched, up, ub, mismatch_findings = exact_match(platform, bank, tolerance_paise=1)
        assert len(matched) == 100  # All match row-by-row
        assert len(mismatch_findings) == 0  # No per-row mismatches

        # But aggregate drift is 100 paise
        (pt, bt, var, waterfall, explained, unexplained,
         drift, drift_count) = compute_aggregate(
            platform, bank, matched, [], tolerance_paise=1
        )
        # drift = -(sum of residuals) = -(100 × 1) = -100
        assert drift == -100
        assert drift_count == 100

    def test_waterfall_sums_to_variance(self):
        """Waterfall components must sum exactly to total variance."""
        platform = [
            _make_platform_row(txn_id="TXN-001", net=10000),
            _make_platform_row(txn_id="TXN-002", net=20000),
        ]
        bank = [
            _make_bank_row(bank_reference="TXN-001", amount=10000),
            # TXN-002 missing from bank
        ]

        matched, up, ub, mf = exact_match(platform, bank)
        classify_f = classify_unmatched(up, ub, {"TXN-001", "TXN-002"})
        all_findings = mf + classify_f

        (pt, bt, var, waterfall, explained, unexplained,
         drift, drift_count) = compute_aggregate(
            platform, bank, matched, all_findings
        )

        waterfall_sum = sum(w.amount_paise for w in waterfall)
        assert waterfall_sum == var, (
            f"Waterfall sum {waterfall_sum} != variance {var}"
        )


# ===================================================================
# PROPERTY / INVARIANT TESTS
# ===================================================================

class TestInvariants:
    """Property-based invariant tests."""

    def test_no_double_classification(self):
        """No transaction should be classified in more than one category."""
        result = reconcile('./data/platform_transactions.csv',
                          './data/bank_settlements.csv')

        # Count how many times each txn_id appears across findings
        id_cats: dict[str, list[str]] = {}
        for f in result.findings:
            # Skip aggregate-level findings (no specific IDs)
            if not f.ids:
                continue
            # Skip rounding drift (aggregate finding)
            if f.category == GapCategory.ROUNDING_DRIFT:
                continue
            primary_id = f.ids[0]
            if primary_id not in id_cats:
                id_cats[primary_id] = []
            id_cats[primary_id].append(f.category.value)

        # Allow ORPHAN_REFUND to coexist with other categories
        # (a refund can be orphaned AND have a timing issue)
        for tid, cats in id_cats.items():
            non_orphan = [c for c in cats if c != "ORPHAN_REFUND"]
            assert len(non_orphan) <= 1, (
                f"{tid} classified multiple times: {cats}"
            )

    def test_waterfall_invariant_on_generated_data(self):
        """Waterfall components sum exactly to total variance on generated data."""
        result = reconcile('./data/platform_transactions.csv',
                          './data/bank_settlements.csv')
        waterfall_sum = sum(w.amount_paise for w in result.waterfall)
        assert waterfall_sum == result.total_variance_paise, (
            f"Waterfall {waterfall_sum} != variance {result.total_variance_paise}"
        )


# ===================================================================
# END-TO-END TESTS
# ===================================================================

class TestEndToEnd:
    """Full pipeline tests with generated data and ground-truth validation."""

    def test_e2e_seed_42(self):
        """Generate with seed 42, reconcile, compare against ground truth."""
        self._run_e2e(42)

    def test_e2e_seed_123(self):
        """Generate with seed 123, reconcile, compare against ground truth."""
        self._run_e2e(123)

    def test_e2e_seed_7777(self):
        """Generate with seed 7777, reconcile, compare against ground truth."""
        self._run_e2e(7777)

    def _run_e2e(self, seed: int):
        """Core e2e test: generate, reconcile, check against ground truth."""
        from generate_data import DataGenerator

        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate
            gen = DataGenerator(seed=seed)
            gen.generate()
            gen.write(tmpdir)

            # Reconcile
            plat_path = os.path.join(tmpdir, "platform_transactions.csv")
            bank_path = os.path.join(tmpdir, "bank_settlements.csv")
            result = reconcile(plat_path, bank_path)

            # Load ground truth
            gt_path = os.path.join(tmpdir, "_ground_truth.json")
            with open(gt_path) as f:
                gt = json.load(f)

            # --- Check recall: every planted anomaly must be found ---
            planted_anomalies = gt["anomalies"]
            found_ids = set()
            for f in result.findings:
                for id_ in f.ids:
                    found_ids.add(id_)

            found_categories = Counter(f.category.value for f in result.findings)

            # Check each anomaly type
            missed = []
            for anomaly in planted_anomalies:
                atype = anomaly["type"]
                aids = anomaly["ids"]

                if atype == "TIMING_DIFFERENCE":
                    # Check the primary txn_id is found as TIMING_DIFFERENCE
                    primary = aids[0]
                    timing_ids = {f.ids[0] for f in result.findings
                                  if f.category == GapCategory.TIMING_DIFFERENCE}
                    if primary not in timing_ids:
                        missed.append(anomaly)

                elif atype == "DUPLICATE_PLATFORM":
                    dup_ids = set()
                    for f in result.findings:
                        if f.category == GapCategory.DUPLICATE_PLATFORM:
                            dup_ids.update(f.ids)
                    if aids[0] not in dup_ids:
                        missed.append(anomaly)

                elif atype == "DUPLICATE_BANK":
                    dup_ids = set()
                    for f in result.findings:
                        if f.category == GapCategory.DUPLICATE_BANK:
                            dup_ids.update(f.ids)
                    if aids[0] not in dup_ids:
                        missed.append(anomaly)

                elif atype == "ORPHAN_REFUND":
                    orphan_ids = {f.ids[0] for f in result.findings
                                  if f.category == GapCategory.ORPHAN_REFUND}
                    if aids[0] not in orphan_ids:
                        missed.append(anomaly)

                elif atype == "ROUNDING_DRIFT":
                    # Rounding drift is aggregate-level
                    if result.rounding_drift_row_count == 0:
                        missed.append(anomaly)

                elif atype == "MISSING_SETTLEMENT":
                    missing_ids = {f.ids[0] for f in result.findings
                                   if f.category == GapCategory.MISSING_SETTLEMENT}
                    if aids[0] not in missing_ids:
                        missed.append(anomaly)

                elif atype == "AMOUNT_MISMATCH":
                    mismatch_ids = set()
                    for f in result.findings:
                        if f.category == GapCategory.AMOUNT_MISMATCH:
                            mismatch_ids.update(f.ids)
                    if aids[0] not in mismatch_ids:
                        missed.append(anomaly)

            # Report
            total_planted = len(planted_anomalies)
            total_found = total_planted - len(missed)
            recall = total_found / total_planted if total_planted > 0 else 1.0

            print(f"\n{'='*60}")
            print(f"E2E Test — Seed {seed}")
            print(f"{'='*60}")
            print(f"Planted anomalies:  {total_planted}")
            print(f"Detected:           {total_found}")
            print(f"Missed:             {len(missed)}")
            print(f"Recall:             {recall:.1%}")
            print(f"Total findings:     {len(result.findings)}")
            print(f"Variance explained: {result.pct_explained:.1f}%")
            print(f"Waterfall balance:  "
                  f"{sum(w.amount_paise for w in result.waterfall)} == "
                  f"{result.total_variance_paise}")

            if missed:
                print(f"\nMissed anomalies:")
                for m in missed[:5]:
                    print(f"  {m['type']}: {m['ids'][:2]} — {m['description'][:80]}")

            # Assert 100% recall
            assert recall == 1.0, (
                f"Seed {seed}: Recall {recall:.1%} — missed {len(missed)} of "
                f"{total_planted}: {[m['type'] for m in missed]}"
            )

            # Waterfall invariant
            waterfall_sum = sum(w.amount_paise for w in result.waterfall)
            assert waterfall_sum == result.total_variance_paise

    def test_e2e_precision_report(self):
        """Report precision (false positive rate) but don't fail on it.

        A "false positive" is a finding that isn't a planted anomaly.
        Some are legitimate (natural timing gaps), so we just report.
        """
        from generate_data import DataGenerator

        with tempfile.TemporaryDirectory() as tmpdir:
            gen = DataGenerator(seed=42)
            gen.generate()
            gen.write(tmpdir)

            result = reconcile(
                os.path.join(tmpdir, "platform_transactions.csv"),
                os.path.join(tmpdir, "bank_settlements.csv"),
            )

            with open(os.path.join(tmpdir, "_ground_truth.json")) as f:
                gt = json.load(f)

            planted_ids = set()
            for a in gt["anomalies"]:
                for id_ in a["ids"]:
                    planted_ids.add(id_)

            # Count findings that reference at least one planted ID
            true_positives = 0
            false_positives = 0
            for f in result.findings:
                if f.category == GapCategory.ROUNDING_DRIFT:
                    true_positives += 1  # Aggregate-level, always correct
                    continue
                if any(id_ in planted_ids for id_ in f.ids):
                    true_positives += 1
                else:
                    false_positives += 1

            total = true_positives + false_positives
            precision = true_positives / total if total > 0 else 1.0

            print(f"\n{'='*60}")
            print(f"Precision Report — Seed 42")
            print(f"{'='*60}")
            print(f"True positives:  {true_positives}")
            print(f"False positives: {false_positives}")
            print(f"Precision:       {precision:.1%}")
            print(f"Note: 'false positives' include natural timing gaps, which are "
                  f"legitimate findings — they're just not planted anomalies.")

            # Precision should be reasonable (planted represent a fraction)
            # Natural timing gaps are real findings, not errors
            assert precision > 0.3, f"Precision too low: {precision:.1%}"


# ===================================================================
# CSV LOADING
# ===================================================================

class TestCSVLoading:
    """Tests for CSV file loading."""

    def test_load_csv_file(self):
        """Load a CSV file from disk."""
        rows = load_csv('./data/platform_transactions.csv')
        assert len(rows) > 0
        assert "txn_id" in rows[0]

    def test_load_csv_string(self):
        """Load CSV from a string."""
        csv_text = "txn_id,amount\nTXN-001,100\n"
        rows = load_csv(csv_text, is_file=False)
        assert len(rows) == 1
        assert rows[0]["txn_id"] == "TXN-001"
