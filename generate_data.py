"""
generate_data.py — Synthetic Payments Data Generator

Produces:
  1. platform_transactions.csv  — the platform's view
  2. bank_settlements.csv       — the bank's view
  3. _ground_truth.json         — planted anomaly manifest (test-only, never read by app)

Usage:
  python generate_data.py --seed 42 --output-dir ./data

See ASSUMPTIONS.md for every design decision.
Money is stored as INTEGER PAISE (1 INR = 100 paise). No floats for money.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from random import Random
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

RECON_YEAR = 2026
RECON_MONTH = 10  # October

MONTH_START = datetime(RECON_YEAR, RECON_MONTH, 1, tzinfo=IST)
MONTH_END = datetime(RECON_YEAR, RECON_MONTH, 31, 23, 59, 59, 999999, tzinfo=IST)

# Next month window (for timing-difference settlements)
NEXT_MONTH_START = datetime(RECON_YEAR, 11, 1, tzinfo=IST)
NEXT_MONTH_END = datetime(RECON_YEAR, 11, 10, tzinfo=IST)  # enough to cover lag

DEFAULT_SEED = 42
DEFAULT_VOLUME = 7000
FEE_RATE = Decimal("0.0235")  # 2.35% — computed at 4-decimal precision

# Customer pool
NUM_CUSTOMERS = 2000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_business_day(dt: datetime) -> bool:
    """Monday=0 … Friday=4 are business days."""
    return dt.weekday() < 5


def _next_business_day(dt: datetime, n: int = 1) -> datetime:
    """Advance dt by n business days."""
    result = dt
    added = 0
    while added < n:
        result += timedelta(days=1)
        if _is_business_day(result):
            added += 1
    return result


def _random_time_in_day(rng: Random, day: datetime) -> datetime:
    """Return a random IST timestamp on the given day (business hours biased)."""
    hour = rng.choices(range(24), weights=[
        1, 1, 1, 1, 1, 1,    # 0-5: low
        3, 5, 8, 10, 12, 12, # 6-11: ramp up
        10, 10, 12, 12, 10, 8, # 12-17: peak
        6, 5, 4, 3, 2, 1     # 18-23: taper
    ])[0]
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    microsecond = rng.randint(0, 999999)
    return day.replace(hour=hour, minute=minute, second=second,
                       microsecond=microsecond)


def _generate_txn_id(rng: Random) -> str:
    return f"TXN-{rng.getrandbits(32):08X}"


def _generate_settlement_id(rng: Random) -> str:
    return f"STL-{rng.getrandbits(32):08X}"


def _compute_fee_platform(gross_paise: int) -> int:
    """Platform fee: floor(gross * rate). See ASSUMPTIONS.md A7."""
    # Use Decimal for the multiplication to avoid float
    gross_d = Decimal(str(gross_paise))
    fee_d = gross_d * FEE_RATE
    return int(fee_d.to_integral_value(rounding='ROUND_FLOOR'))


def _compute_net_bank(gross_paise: int) -> int:
    """Bank net: round_half_up(gross * (1 - rate)). See ASSUMPTIONS.md A7."""
    gross_d = Decimal(str(gross_paise))
    net_d = gross_d * (Decimal("1") - FEE_RATE)
    return int(net_d.to_integral_value(rounding=ROUND_HALF_UP))


def _fmt_ts(dt: datetime) -> str:
    """ISO 8601 with IST offset."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+05:30"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlatformTxn:
    txn_id: str
    created_at: datetime
    customer_id: str
    type: str           # PAYMENT or REFUND
    gross_amount: int   # paise
    fee: int            # paise
    net_amount: int     # paise
    currency: str
    original_txn_id: str  # for refunds, else ""
    status: str         # SUCCESS, FAILED, PENDING


@dataclass
class BankSettlement:
    settlement_id: str
    bank_reference: str   # = txn_id
    settled_at: datetime
    batch_id: str
    amount: int           # paise (negative for refunds)
    currency: str


@dataclass
class Anomaly:
    type: str
    sub_type: str
    ids: list[str]
    description: str
    amount_impact_paise: int
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class DataGenerator:
    def __init__(self, seed: int = DEFAULT_SEED, volume: int = DEFAULT_VOLUME):
        self.rng = Random(seed)
        self.seed = seed
        self.volume = volume

        self.platform_txns: list[PlatformTxn] = []
        self.bank_settlements: list[BankSettlement] = []
        self.anomalies: list[Anomaly] = []

        # Tracking
        self._txn_ids: set[str] = set()
        self._stl_ids: set[str] = set()
        self._batch_cache: dict[str, str] = {}  # date_str -> batch_id
        self._batch_counter = 0

        # Customer pool
        self._customers = [f"CUST-{i:05d}" for i in range(1, NUM_CUSTOMERS + 1)]

        # Payment txns that can be refunded (populated during generation)
        self._refundable_txns: list[PlatformTxn] = []

    def _unique_txn_id(self) -> str:
        while True:
            tid = _generate_txn_id(self.rng)
            if tid not in self._txn_ids:
                self._txn_ids.add(tid)
                return tid

    def _unique_stl_id(self) -> str:
        while True:
            sid = _generate_settlement_id(self.rng)
            if sid not in self._stl_ids:
                self._stl_ids.add(sid)
                return sid

    def _get_batch_id(self, settle_date: datetime) -> str:
        key = settle_date.strftime("%Y%m%d")
        if key not in self._batch_cache:
            self._batch_counter += 1
            self._batch_cache[key] = f"BATCH-{key}-{self._batch_counter:03d}"
        return self._batch_cache[key]

    def _random_gross_amount(self) -> int:
        """Random payment in range ₹50 – ₹25,000 (5000–2500000 paise)."""
        # Log-normal-ish: most payments small, some large
        tier = self.rng.random()
        if tier < 0.50:
            return self.rng.randint(5000, 50000)        # ₹50–₹500
        elif tier < 0.80:
            return self.rng.randint(50000, 200000)       # ₹500–₹2,000
        elif tier < 0.95:
            return self.rng.randint(200000, 500000)      # ₹2,000–₹5,000
        else:
            return self.rng.randint(500000, 2500000)     # ₹5,000–₹25,000

    def _settlement_lag_days(self) -> int:
        """T+1 (60%) or T+2 (40%) business days."""
        return 1 if self.rng.random() < 0.60 else 2

    def _generate_days(self) -> list[datetime]:
        """All days in the reconciliation month."""
        days = []
        day = MONTH_START
        while day.month == RECON_MONTH:
            days.append(day)
            day += timedelta(days=1)
        return days

    # -------------------------------------------------------------------
    # Core generation
    # -------------------------------------------------------------------

    def generate(self) -> None:
        """Main entry: build clean data, then plant anomalies."""
        self._generate_clean_data()
        self._plant_timing_gaps()         # Gap A
        self._plant_rounding_drift()      # Gap B
        self._plant_duplicates()          # Gap C
        self._plant_orphan_refunds()      # Gap D
        self._plant_missing_settlements() # Extra E1
        self._plant_amount_mismatches()   # Extra E2

    # -------------------------------------------------------------------
    # Clean data generation
    # -------------------------------------------------------------------

    def _generate_clean_data(self) -> None:
        days = self._generate_days()

        # Distribute volume across days (more on business days)
        daily_weights = [3.0 if _is_business_day(d) else 1.0 for d in days]
        total_weight = sum(daily_weights)
        daily_counts = [max(1, round(self.volume * w / total_weight)) for w, d in zip(daily_weights, days)]

        # Adjust to match target volume
        diff = self.volume - sum(daily_counts)
        for i in range(abs(diff)):
            idx = self.rng.randint(0, len(daily_counts) - 1)
            daily_counts[idx] += 1 if diff > 0 else -1

        # Determine refund count (~7%)
        num_refunds = int(self.volume * 0.07)
        refund_budget = num_refunds

        for day, count in zip(days, daily_counts):
            for _ in range(max(0, count)):
                # Decide if this is a refund
                is_refund = (refund_budget > 0 and
                             len(self._refundable_txns) > 0 and
                             self.rng.random() < 0.07)

                if is_refund:
                    self._create_refund(day)
                    refund_budget -= 1
                else:
                    self._create_payment(day)

    def _create_payment(self, day: datetime) -> PlatformTxn:
        txn_id = self._unique_txn_id()
        created_at = _random_time_in_day(self.rng, day)
        customer_id = self.rng.choice(self._customers)
        gross = self._random_gross_amount()
        fee = _compute_fee_platform(gross)
        net = gross - fee

        txn = PlatformTxn(
            txn_id=txn_id,
            created_at=created_at,
            customer_id=customer_id,
            type="PAYMENT",
            gross_amount=gross,
            fee=fee,
            net_amount=net,
            currency="INR",
            original_txn_id="",
            status="SUCCESS",
        )
        self.platform_txns.append(txn)
        self._refundable_txns.append(txn)

        # Create matching settlement
        lag = self._settlement_lag_days()
        settle_date = _next_business_day(created_at, lag)

        # Only create settlement if it falls within recon month
        # (this is for clean data; timing gaps are planted separately)
        if settle_date <= MONTH_END:
            bank_net = net  # Clean data: bank matches platform exactly
            stl = BankSettlement(
                settlement_id=self._unique_stl_id(),
                bank_reference=txn_id,
                settled_at=_random_time_in_day(self.rng, settle_date),
                batch_id=self._get_batch_id(settle_date),
                amount=bank_net,
                currency="INR",
            )
            self.bank_settlements.append(stl)
        else:
            # This naturally creates a timing difference — but we track it as
            # part of clean data, not as a planted gap. We'll re-do this
            # properly in _plant_timing_gaps for controlled planting.
            # For now, still create the settlement in next month so we can
            # record it as a known timing difference.
            stl = BankSettlement(
                settlement_id=self._unique_stl_id(),
                bank_reference=txn_id,
                settled_at=_random_time_in_day(self.rng, settle_date),
                batch_id=self._get_batch_id(settle_date),
                amount=net,
                currency="INR",
            )
            # Don't add to bank_settlements (it's next month's file)
            # But record as a natural timing gap
            self.anomalies.append(Anomaly(
                type="TIMING_DIFFERENCE",
                sub_type="A_NATURAL",
                ids=[txn_id, stl.settlement_id],
                description=(
                    f"Natural timing: txn on {created_at.strftime('%Y-%m-%d')} "
                    f"settles {settle_date.strftime('%Y-%m-%d')} (next month)"
                ),
                amount_impact_paise=net,
                extra={
                    "created_at": _fmt_ts(created_at),
                    "settled_at": _fmt_ts(settle_date),
                    "days_over_cutoff": (settle_date.date() - MONTH_END.date()).days,
                },
            ))

        return txn

    def _create_refund(self, day: datetime) -> PlatformTxn:
        # Pick a random earlier payment to refund
        original = self.rng.choice(self._refundable_txns)
        # Remove from refundable to avoid double refunds
        self._refundable_txns.remove(original)

        txn_id = self._unique_txn_id()
        created_at = _random_time_in_day(self.rng, day)
        # Refund amount: full or partial (80% full, 20% partial)
        if self.rng.random() < 0.80:
            refund_gross = original.gross_amount
        else:
            refund_gross = self.rng.randint(
                original.gross_amount // 4, original.gross_amount
            )

        txn = PlatformTxn(
            txn_id=txn_id,
            created_at=created_at,
            customer_id=original.customer_id,
            type="REFUND",
            gross_amount=refund_gross,
            fee=0,  # Platform doesn't refund its fee
            net_amount=refund_gross,
            currency="INR",
            original_txn_id=original.txn_id,
            status="SUCCESS",
        )
        self.platform_txns.append(txn)

        # Settlement for refund (negative amount in bank)
        lag = self._settlement_lag_days()
        settle_date = _next_business_day(created_at, lag)

        if settle_date <= MONTH_END:
            stl = BankSettlement(
                settlement_id=self._unique_stl_id(),
                bank_reference=txn_id,
                settled_at=_random_time_in_day(self.rng, settle_date),
                batch_id=self._get_batch_id(settle_date),
                amount=-refund_gross,  # Negative: money flows back
                currency="INR",
            )
            self.bank_settlements.append(stl)

        return txn

    # -------------------------------------------------------------------
    # Gap Planting
    # -------------------------------------------------------------------

    def _plant_timing_gaps(self) -> None:
        """Gap Type A: Transactions at month-end that settle next month.

        We create additional transactions on Oct 29-31 whose settlement
        falls in November. These are planted on top of any natural timing
        gaps that arose from clean generation.
        """
        num_planted = self.rng.randint(15, 25)
        last_days = [
            datetime(RECON_YEAR, RECON_MONTH, 29, tzinfo=IST),
            datetime(RECON_YEAR, RECON_MONTH, 30, tzinfo=IST),
            datetime(RECON_YEAR, RECON_MONTH, 31, tzinfo=IST),
        ]

        for _ in range(num_planted):
            day = self.rng.choice(last_days)
            txn_id = self._unique_txn_id()
            created_at = _random_time_in_day(self.rng, day)
            customer_id = self.rng.choice(self._customers)
            gross = self._random_gross_amount()
            fee = _compute_fee_platform(gross)
            net = gross - fee

            txn = PlatformTxn(
                txn_id=txn_id,
                created_at=created_at,
                customer_id=customer_id,
                type="PAYMENT",
                gross_amount=gross,
                fee=fee,
                net_amount=net,
                currency="INR",
                original_txn_id="",
                status="SUCCESS",
            )
            self.platform_txns.append(txn)

            # Settlement in November
            lag = self._settlement_lag_days()
            settle_date = _next_business_day(created_at, lag)
            # Ensure it's actually in next month
            if settle_date <= MONTH_END:
                settle_date = _next_business_day(MONTH_END, 1)

            days_over = (settle_date.date() - MONTH_END.date()).days

            self.anomalies.append(Anomaly(
                type="TIMING_DIFFERENCE",
                sub_type="A_PLANTED",
                ids=[txn_id],
                description=(
                    f"Planted timing gap: txn on {day.strftime('%Y-%m-%d')} "
                    f"settles {settle_date.strftime('%Y-%m-%d')} "
                    f"({days_over} day(s) after month-end)"
                ),
                amount_impact_paise=net,
                extra={
                    "created_at": _fmt_ts(created_at),
                    "expected_settle_at": _fmt_ts(settle_date),
                    "days_over_cutoff": days_over,
                },
            ))

    def _plant_rounding_drift(self) -> None:
        """Gap Type B: Replace bank settlement amounts with bank-computed net.

        For every settlement, recompute net using the bank's rounding rule.
        The per-row difference is at most 1 paisa (within tolerance), but
        the aggregate drift is visible.
        """
        total_drift = 0
        affected_count = 0
        affected_ids: list[str] = []

        for stl in self.bank_settlements:
            # Find the matching platform txn
            matching_txns = [t for t in self.platform_txns
                             if t.txn_id == stl.bank_reference and t.type == "PAYMENT"]
            if not matching_txns:
                continue

            txn = matching_txns[0]
            bank_net = _compute_net_bank(txn.gross_amount)
            platform_net = txn.net_amount  # = gross - floor(gross * rate)

            if bank_net != platform_net:
                old_amount = stl.amount
                stl.amount = bank_net
                drift = bank_net - platform_net  # +1 or -1 paise typically
                total_drift += drift
                affected_count += 1
                affected_ids.append(stl.bank_reference)

        if affected_count > 0:
            self.anomalies.append(Anomaly(
                type="ROUNDING_DRIFT",
                sub_type="B",
                ids=affected_ids[:20],  # Sample for manifest readability
                description=(
                    f"Rounding drift: {affected_count} rows have ±1 paisa difference. "
                    f"Aggregate drift = {total_drift} paise "
                    f"(₹{total_drift / 100:.2f}). "
                    f"Platform uses floor(), bank uses round_half_up()."
                ),
                amount_impact_paise=total_drift,
                extra={
                    "affected_row_count": affected_count,
                    "total_drift_paise": total_drift,
                    "all_affected_ids": affected_ids,
                },
            ))

    def _plant_duplicates(self) -> None:
        """Gap Type C: Duplicate entries in one or both datasets.

        Plant at least 1 duplicate in each dataset (platform and bank).
        Duplicates have the same txn_id/amount but slightly different timestamps
        or a new surrogate settlement_id.
        """
        # --- Platform duplicates ---
        num_platform_dups = self.rng.randint(2, 4)
        payment_txns = [t for t in self.platform_txns
                        if t.type == "PAYMENT" and t.status == "SUCCESS"
                        and t.created_at.day < 25]  # Not too close to month-end
        self.rng.shuffle(payment_txns)

        for i in range(min(num_platform_dups, len(payment_txns))):
            original = payment_txns[i]
            # Create duplicate with slightly different timestamp
            dup = PlatformTxn(
                txn_id=original.txn_id,  # Same txn_id = duplicate
                created_at=original.created_at + timedelta(seconds=self.rng.randint(1, 300)),
                customer_id=original.customer_id,
                type=original.type,
                gross_amount=original.gross_amount,
                fee=original.fee,
                net_amount=original.net_amount,
                currency=original.currency,
                original_txn_id=original.original_txn_id,
                status=original.status,
            )
            self.platform_txns.append(dup)
            self.anomalies.append(Anomaly(
                type="DUPLICATE_PLATFORM",
                sub_type="C",
                ids=[original.txn_id],
                description=(
                    f"Duplicate platform entry: {original.txn_id} appears twice "
                    f"with timestamps {_fmt_ts(original.created_at)} and "
                    f"{_fmt_ts(dup.created_at)}"
                ),
                amount_impact_paise=original.net_amount,
                extra={
                    "original_timestamp": _fmt_ts(original.created_at),
                    "duplicate_timestamp": _fmt_ts(dup.created_at),
                },
            ))

        # --- Bank duplicates ---
        num_bank_dups = self.rng.randint(2, 4)
        eligible_stls = [s for s in self.bank_settlements
                         if s.amount > 0 and s.settled_at.day < 25]
        self.rng.shuffle(eligible_stls)

        for i in range(min(num_bank_dups, len(eligible_stls))):
            original = eligible_stls[i]
            dup = BankSettlement(
                settlement_id=self._unique_stl_id(),  # New surrogate ID
                bank_reference=original.bank_reference,  # Same bank_reference = dup
                settled_at=original.settled_at + timedelta(seconds=self.rng.randint(1, 600)),
                batch_id=original.batch_id,
                amount=original.amount,
                currency=original.currency,
            )
            self.bank_settlements.append(dup)
            self.anomalies.append(Anomaly(
                type="DUPLICATE_BANK",
                sub_type="C",
                ids=[original.bank_reference, original.settlement_id, dup.settlement_id],
                description=(
                    f"Duplicate bank settlement: bank_reference {original.bank_reference} "
                    f"appears in settlement IDs {original.settlement_id} and "
                    f"{dup.settlement_id}"
                ),
                amount_impact_paise=original.amount,
                extra={
                    "original_settlement_id": original.settlement_id,
                    "duplicate_settlement_id": dup.settlement_id,
                    "original_timestamp": _fmt_ts(original.settled_at),
                    "duplicate_timestamp": _fmt_ts(dup.settled_at),
                },
            ))

    def _plant_orphan_refunds(self) -> None:
        """Gap Type D: Refunds whose original_txn_id doesn't exist."""
        num_orphans = self.rng.randint(5, 8)
        orphan_days = self._generate_days()

        for _ in range(num_orphans):
            day = self.rng.choice(orphan_days[5:25])  # Mid-month
            txn_id = self._unique_txn_id()
            created_at = _random_time_in_day(self.rng, day)
            customer_id = self.rng.choice(self._customers)
            refund_gross = self.rng.randint(5000, 200000)

            # Generate a fake original_txn_id that doesn't exist
            fake_original = f"TXN-{self.rng.getrandbits(32):08X}"
            while fake_original in self._txn_ids:
                fake_original = f"TXN-{self.rng.getrandbits(32):08X}"

            txn = PlatformTxn(
                txn_id=txn_id,
                created_at=created_at,
                customer_id=customer_id,
                type="REFUND",
                gross_amount=refund_gross,
                fee=0,
                net_amount=refund_gross,
                currency="INR",
                original_txn_id=fake_original,
                status="SUCCESS",
            )
            self.platform_txns.append(txn)

            # The refund itself may still be settled
            lag = self._settlement_lag_days()
            settle_date = _next_business_day(created_at, lag)
            if settle_date <= MONTH_END:
                stl = BankSettlement(
                    settlement_id=self._unique_stl_id(),
                    bank_reference=txn_id,
                    settled_at=_random_time_in_day(self.rng, settle_date),
                    batch_id=self._get_batch_id(settle_date),
                    amount=-refund_gross,
                    currency="INR",
                )
                self.bank_settlements.append(stl)

            self.anomalies.append(Anomaly(
                type="ORPHAN_REFUND",
                sub_type="D",
                ids=[txn_id],
                description=(
                    f"Orphan refund: {txn_id} references non-existent "
                    f"original {fake_original}"
                ),
                amount_impact_paise=-refund_gross,  # Negative: it's a refund
                extra={
                    "refund_txn_id": txn_id,
                    "fake_original_txn_id": fake_original,
                    "refund_amount_paise": refund_gross,
                },
            ))

    def _plant_missing_settlements(self) -> None:
        """Extra E1: Transactions with no settlement at all."""
        num_missing = self.rng.randint(3, 5)

        # Find settlements to remove
        eligible = [s for s in self.bank_settlements
                    if s.amount > 0 and s.settled_at.day >= 5 and s.settled_at.day <= 20]
        self.rng.shuffle(eligible)

        for i in range(min(num_missing, len(eligible))):
            stl = eligible[i]
            self.bank_settlements.remove(stl)
            self.anomalies.append(Anomaly(
                type="MISSING_SETTLEMENT",
                sub_type="E1_EXTRA",
                ids=[stl.bank_reference],
                description=(
                    f"Missing settlement: platform txn {stl.bank_reference} "
                    f"has no bank settlement (removed settlement {stl.settlement_id})"
                ),
                amount_impact_paise=stl.amount,
                extra={
                    "removed_settlement_id": stl.settlement_id,
                    "original_amount_paise": stl.amount,
                },
            ))

    def _plant_amount_mismatches(self) -> None:
        """Extra E2: Settlements where amount differs by more than tolerance."""
        num_mismatches = self.rng.randint(3, 5)

        eligible = [s for s in self.bank_settlements
                    if s.amount > 0 and s.settled_at.day >= 5 and s.settled_at.day <= 20]
        self.rng.shuffle(eligible)

        for i in range(min(num_mismatches, len(eligible))):
            stl = eligible[i]
            # Introduce a noticeable mismatch: ₹1 to ₹50 (100–5000 paise)
            mismatch = self.rng.randint(100, 5000)
            if self.rng.random() < 0.5:
                mismatch = -mismatch
            original_amount = stl.amount
            stl.amount = stl.amount + mismatch
            self.anomalies.append(Anomaly(
                type="AMOUNT_MISMATCH",
                sub_type="E2_EXTRA",
                ids=[stl.bank_reference, stl.settlement_id],
                description=(
                    f"Amount mismatch: platform expects {original_amount} paise, "
                    f"bank has {stl.amount} paise (diff = {mismatch} paise)"
                ),
                amount_impact_paise=mismatch,
                extra={
                    "platform_amount_paise": original_amount,
                    "bank_amount_paise": stl.amount,
                    "difference_paise": mismatch,
                },
            ))

    # -------------------------------------------------------------------
    # Output
    # -------------------------------------------------------------------

    def write(self, output_dir: str, write_ground_truth: bool = True) -> None:
        os.makedirs(output_dir, exist_ok=True)

        # Sort for determinism
        self.platform_txns.sort(key=lambda t: t.created_at)
        self.bank_settlements.sort(key=lambda s: s.settled_at)

        # 1. Platform CSV
        plat_path = os.path.join(output_dir, "platform_transactions.csv")
        with open(plat_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "txn_id", "created_at", "customer_id", "type",
                "gross_amount", "fee", "net_amount", "currency",
                "original_txn_id", "status"
            ])
            for t in self.platform_txns:
                writer.writerow([
                    t.txn_id, _fmt_ts(t.created_at), t.customer_id, t.type,
                    t.gross_amount, t.fee, t.net_amount, t.currency,
                    t.original_txn_id, t.status
                ])

        # 2. Bank CSV
        bank_path = os.path.join(output_dir, "bank_settlements.csv")
        with open(bank_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "settlement_id", "bank_reference", "settled_at",
                "batch_id", "amount", "currency"
            ])
            for s in self.bank_settlements:
                writer.writerow([
                    s.settlement_id, s.bank_reference,
                    _fmt_ts(s.settled_at), s.batch_id,
                    s.amount, s.currency
                ])

        # 3. Ground truth manifest (test-only; skipped by web app)
        summary = {
            atype: len([a for a in self.anomalies if a.type == atype])
            for atype in sorted(set(a.type for a in self.anomalies))
        }

        if write_ground_truth:
            gt_path = os.path.join(output_dir, "_ground_truth.json")
            manifest = {
                "seed": self.seed,
                "volume": self.volume,
                "recon_month": f"{RECON_YEAR}-{RECON_MONTH:02d}",
                "platform_row_count": len(self.platform_txns),
                "bank_row_count": len(self.bank_settlements),
                "anomalies": [asdict(a) for a in self.anomalies],
                "summary": summary,
            }
            with open(gt_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, default=str)

        # Print summary
        print(f"Generated data with seed={self.seed}, volume={self.volume}")
        print(f"  Platform transactions: {len(self.platform_txns)} rows -> {plat_path}")
        print(f"  Bank settlements:      {len(self.bank_settlements)} rows -> {bank_path}")
        if write_ground_truth:
            print(f"  Ground truth:          {len(self.anomalies)} anomalies -> {gt_path}")
            print(f"  Anomaly breakdown:")
            for atype, count in summary.items():
                print(f"    {atype}: {count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic payments reconciliation data"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"Random seed (default: {DEFAULT_SEED})")
    parser.add_argument("--volume", type=int, default=DEFAULT_VOLUME,
                        help=f"Number of transactions (default: {DEFAULT_VOLUME})")
    parser.add_argument("--output-dir", type=str, default="./data",
                        help="Output directory (default: ./data)")
    args = parser.parse_args()

    gen = DataGenerator(seed=args.seed, volume=args.volume)
    gen.generate()
    gen.write(args.output_dir)


if __name__ == "__main__":
    main()
