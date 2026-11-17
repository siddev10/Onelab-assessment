"""
reconcile.py — Payments Reconciliation Engine

A pure library with no UI code. Implements layered reconciliation:
  1. Input validation and normalisation
  2. Duplicate detection (within each dataset)
  3. Exact matching on shared key (txn_id <-> bank_reference)
  4. Fuzzy fallback for unmatched rows
  5. Classification of every gap
  6. Aggregate reconciliation with waterfall
  7. Findings with explanations

Money is INTEGER PAISE throughout. See ASSUMPTIONS.md.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_TOLERANCE_PAISE = 1  # Per-row amount tolerance

PLATFORM_SCHEMA = {
    "txn_id": str,
    "created_at": str,
    "customer_id": str,
    "type": str,
    "gross_amount": int,
    "fee": int,
    "net_amount": int,
    "currency": str,
    "original_txn_id": str,
    "status": str,
}

BANK_SCHEMA = {
    "settlement_id": str,
    "bank_reference": str,
    "settled_at": str,
    "batch_id": str,
    "amount": int,
    "currency": str,
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class GapCategory(str, Enum):
    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
    DUPLICATE_PLATFORM = "DUPLICATE_PLATFORM"
    DUPLICATE_BANK = "DUPLICATE_BANK"
    ORPHAN_REFUND = "ORPHAN_REFUND"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_SETTLEMENT = "MISSING_SETTLEMENT"
    MISSING_TRANSACTION = "MISSING_TRANSACTION"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    ROUNDING_DRIFT = "ROUNDING_DRIFT"
    UNEXPLAINED = "UNEXPLAINED"


@dataclass
class Finding:
    category: GapCategory
    ids: list[str]
    amount_impact_paise: int
    confidence: float  # 0.0 to 1.0
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        return d


@dataclass
class WaterfallItem:
    label: str
    amount_paise: int
    row_count: int
    description: str


@dataclass
class ReconciliationResult:
    # Totals
    platform_total_paise: int
    bank_total_paise: int
    total_variance_paise: int

    # Counts
    platform_row_count: int
    bank_row_count: int
    matched_count: int
    unmatched_platform_count: int
    unmatched_bank_count: int

    # Findings
    findings: list[Finding]

    # Waterfall
    waterfall: list[WaterfallItem]
    explained_paise: int
    unexplained_paise: int
    pct_explained: float

    # Duplicates found
    platform_duplicates: list[dict]
    bank_duplicates: list[dict]

    # Validation errors
    validation_errors: list[str]

    # Rounding drift info
    rounding_drift_paise: int
    rounding_drift_row_count: int

    # Matched pairs for drill-down
    matched_pairs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform_total_paise": self.platform_total_paise,
            "bank_total_paise": self.bank_total_paise,
            "total_variance_paise": self.total_variance_paise,
            "platform_row_count": self.platform_row_count,
            "bank_row_count": self.bank_row_count,
            "matched_count": self.matched_count,
            "unmatched_platform_count": self.unmatched_platform_count,
            "unmatched_bank_count": self.unmatched_bank_count,
            "findings": [f.to_dict() for f in self.findings],
            "waterfall": [asdict(w) for w in self.waterfall],
            "explained_paise": self.explained_paise,
            "unexplained_paise": self.unexplained_paise,
            "pct_explained": self.pct_explained,
            "platform_duplicates": self.platform_duplicates,
            "bank_duplicates": self.bank_duplicates,
            "validation_errors": self.validation_errors,
            "rounding_drift_paise": self.rounding_drift_paise,
            "rounding_drift_row_count": self.rounding_drift_row_count,
        }


# ---------------------------------------------------------------------------
# Layer 1: Validation and Normalisation
# ---------------------------------------------------------------------------

def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp with timezone offset."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _coerce_int(value: Any, field_name: str) -> tuple[Optional[int], Optional[str]]:
    """Coerce a value to int, returning (value, error_or_None)."""
    if isinstance(value, int):
        return value, None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None, f"Empty value for {field_name}"
        try:
            return int(value), None
        except ValueError:
            try:
                f = float(value)
                return int(f), f"Float coerced to int for {field_name}: {value}"
            except ValueError:
                return None, f"Cannot convert '{value}' to int for {field_name}"
    if isinstance(value, float):
        return int(value), f"Float coerced to int for {field_name}: {value}"
    return None, f"Unexpected type {type(value)} for {field_name}"


def validate_platform_data(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate and normalise platform transaction rows.

    Returns (clean_rows, errors).
    """
    errors: list[str] = []
    clean: list[dict] = []

    if not rows:
        errors.append("Platform data is empty")
        return clean, errors

    for i, row in enumerate(rows):
        row_errors: list[str] = []
        row_num = i + 1

        for col in PLATFORM_SCHEMA:
            if col not in row:
                row_errors.append(f"Row {row_num}: missing column '{col}'")

        if row_errors:
            errors.extend(row_errors)
            continue

        clean_row = {}
        clean_row["txn_id"] = str(row["txn_id"]).strip()
        if not clean_row["txn_id"]:
            row_errors.append(f"Row {row_num}: empty txn_id")

        ts = _parse_timestamp(str(row["created_at"]))
        if ts is None:
            row_errors.append(f"Row {row_num}: invalid timestamp '{row['created_at']}'")
        else:
            clean_row["created_at"] = ts.astimezone(IST)

        clean_row["customer_id"] = str(row["customer_id"]).strip()
        clean_row["type"] = str(row["type"]).strip().upper()
        if clean_row["type"] not in ("PAYMENT", "REFUND"):
            row_errors.append(f"Row {row_num}: invalid type '{row['type']}'")

        for int_col in ("gross_amount", "fee", "net_amount"):
            val, err = _coerce_int(row[int_col], int_col)
            if err and val is None:
                row_errors.append(f"Row {row_num}: {err}")
            elif err:
                errors.append(f"Row {row_num}: {err}")
            clean_row[int_col] = val

        clean_row["currency"] = str(row["currency"]).strip().upper()
        clean_row["original_txn_id"] = str(row.get("original_txn_id", "")).strip()
        clean_row["status"] = str(row["status"]).strip().upper()

        if row_errors:
            errors.extend(row_errors)
            continue

        clean.append(clean_row)

    return clean, errors


def validate_bank_data(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate and normalise bank settlement rows."""
    errors: list[str] = []
    clean: list[dict] = []

    if not rows:
        errors.append("Bank data is empty")
        return clean, errors

    for i, row in enumerate(rows):
        row_errors: list[str] = []
        row_num = i + 1

        for col in BANK_SCHEMA:
            if col not in row:
                row_errors.append(f"Row {row_num}: missing column '{col}'")

        if row_errors:
            errors.extend(row_errors)
            continue

        clean_row = {}
        clean_row["settlement_id"] = str(row["settlement_id"]).strip()
        clean_row["bank_reference"] = str(row["bank_reference"]).strip()

        ts = _parse_timestamp(str(row["settled_at"]))
        if ts is None:
            row_errors.append(f"Row {row_num}: invalid timestamp '{row['settled_at']}'")
        else:
            clean_row["settled_at"] = ts.astimezone(IST)

        clean_row["batch_id"] = str(row["batch_id"]).strip()

        val, err = _coerce_int(row["amount"], "amount")
        if err and val is None:
            row_errors.append(f"Row {row_num}: {err}")
        elif err:
            errors.append(f"Row {row_num}: {err}")
        clean_row["amount"] = val

        clean_row["currency"] = str(row["currency"]).strip().upper()

        if row_errors:
            errors.extend(row_errors)
            continue

        clean.append(clean_row)

    return clean, errors


# ---------------------------------------------------------------------------
# Layer 2: Duplicate Detection
# ---------------------------------------------------------------------------

def detect_duplicates_platform(rows: list[dict]) -> tuple[list[dict], list[dict], list[Finding]]:
    """Find duplicate txn_ids in platform data.

    Returns (deduplicated_rows, duplicate_rows, findings).
    Keeps the first occurrence, marks extras as duplicates.
    """
    seen: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        tid = row["txn_id"]
        if tid not in seen:
            seen[tid] = []
        seen[tid].append(i)

    deduped: list[dict] = []
    duplicates: list[dict] = []
    findings: list[Finding] = []

    for tid, indices in seen.items():
        if len(indices) > 1:
            deduped.append(rows[indices[0]])
            for idx in indices[1:]:
                duplicates.append(rows[idx])

            # Impact: extra rows inflate the platform total.
            # Each extra duplicate contributes its net_amount to the variance.
            extra_count = len(indices) - 1
            per_row_amount = rows[indices[0]]["net_amount"]
            total_impact = per_row_amount * extra_count

            findings.append(Finding(
                category=GapCategory.DUPLICATE_PLATFORM,
                ids=[tid],
                amount_impact_paise=total_impact,
                confidence=1.0,
                explanation=(
                    f"Platform duplicate: txn_id {tid} appears {len(indices)} times. "
                    f"Kept first occurrence, flagged {extra_count} extra(s). "
                    f"Extra duplicates inflate platform total by "
                    f"{total_impact} paise (₹{total_impact / 100:.2f})."
                ),
                details={
                    "occurrences": len(indices),
                    "extra_count": extra_count,
                    "per_row_net_amount": per_row_amount,
                    "timestamps": [rows[i]["created_at"].isoformat()
                                   for i in indices if isinstance(rows[i].get("created_at"), datetime)],
                },
            ))
        else:
            deduped.append(rows[indices[0]])

    return deduped, duplicates, findings


def detect_duplicates_bank(rows: list[dict]) -> tuple[list[dict], list[dict], list[Finding]]:
    """Find duplicate bank_references in bank data.

    Returns (deduplicated_rows, duplicate_rows, findings).
    """
    seen: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        ref = row["bank_reference"]
        if ref not in seen:
            seen[ref] = []
        seen[ref].append(i)

    deduped: list[dict] = []
    duplicates: list[dict] = []
    findings: list[Finding] = []

    for ref, indices in seen.items():
        if len(indices) > 1:
            deduped.append(rows[indices[0]])
            for idx in indices[1:]:
                duplicates.append(rows[idx])

            # Bank duplicates inflate bank total → reduce the platform-bank variance.
            # From the platform perspective (variance = platform - bank), bank dupes
            # make variance more negative (bank overpays).
            extra_count = len(indices) - 1
            per_row_amount = rows[indices[0]]["amount"]
            total_impact = -(per_row_amount * extra_count)  # Negative: bank has extra

            findings.append(Finding(
                category=GapCategory.DUPLICATE_BANK,
                ids=[ref] + [rows[i]["settlement_id"] for i in indices],
                amount_impact_paise=total_impact,
                confidence=1.0,
                explanation=(
                    f"Bank duplicate: bank_reference {ref} appears {len(indices)} times "
                    f"across settlement IDs "
                    f"{', '.join(rows[i]['settlement_id'] for i in indices)}. "
                    f"Kept first, flagged {extra_count} extra(s). "
                    f"Extra duplicates inflate bank total by "
                    f"{abs(total_impact)} paise."
                ),
                details={
                    "occurrences": len(indices),
                    "extra_count": extra_count,
                    "settlement_ids": [rows[i]["settlement_id"] for i in indices],
                },
            ))
        else:
            deduped.append(rows[indices[0]])

    return deduped, duplicates, findings


# ---------------------------------------------------------------------------
# Layer 3: Exact Matching
# ---------------------------------------------------------------------------

def exact_match(
    platform: list[dict],
    bank: list[dict],
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
) -> tuple[list[dict], list[dict], list[dict], list[Finding]]:
    """Match platform txns to bank settlements on txn_id <-> bank_reference.

    Returns (matched_pairs, unmatched_platform, unmatched_bank, findings).
    matched_pairs: list of {"platform": row, "bank": row, "residual": int}
    """
    bank_by_ref: dict[str, dict] = {}
    for row in bank:
        bank_by_ref[row["bank_reference"]] = row

    matched: list[dict] = []
    unmatched_platform: list[dict] = []
    amount_mismatch_findings: list[Finding] = []
    matched_refs: set[str] = set()

    for ptxn in platform:
        if ptxn["status"] != "SUCCESS":
            continue

        ref = ptxn["txn_id"]
        if ref in bank_by_ref:
            brow = bank_by_ref[ref]
            matched_refs.add(ref)

            # Compare amounts
            if ptxn["type"] == "REFUND":
                expected_bank = -ptxn["net_amount"]
            else:
                expected_bank = ptxn["net_amount"]

            residual = brow["amount"] - expected_bank
            abs_residual = abs(residual)

            if abs_residual > tolerance_paise:
                amount_mismatch_findings.append(Finding(
                    category=GapCategory.AMOUNT_MISMATCH,
                    ids=[ref, brow["settlement_id"]],
                    amount_impact_paise=-residual,  # From platform perspective: platform - bank
                    confidence=1.0,
                    explanation=(
                        f"Amount mismatch: txn {ref} — platform expects "
                        f"{expected_bank} paise, bank has {brow['amount']} paise "
                        f"(diff = {residual} paise, ₹{residual / 100:.2f})."
                    ),
                    details={
                        "platform_net_amount": expected_bank,
                        "bank_amount": brow["amount"],
                        "difference_paise": residual,
                        "settlement_id": brow["settlement_id"],
                    },
                ))

            matched.append({
                "platform": ptxn,
                "bank": brow,
                "residual": residual,
            })
        else:
            unmatched_platform.append(ptxn)

    unmatched_bank = [row for row in bank if row["bank_reference"] not in matched_refs]

    return matched, unmatched_platform, unmatched_bank, amount_mismatch_findings


# ---------------------------------------------------------------------------
# Layer 4: Fuzzy Matching
# ---------------------------------------------------------------------------

def _fuzzy_score(ptxn: dict, brow: dict, tolerance_paise: int) -> float:
    """Score a potential fuzzy match (0.0 = no match, 1.0 = perfect)."""
    score = 0.0

    if ptxn["type"] == "REFUND":
        expected = -ptxn["net_amount"]
    else:
        expected = ptxn["net_amount"]

    amount_diff = abs(brow["amount"] - expected)
    if amount_diff == 0:
        score += 0.5
    elif amount_diff <= tolerance_paise:
        score += 0.4
    elif amount_diff <= tolerance_paise * 10:
        score += 0.1
    else:
        return 0.0

    p_ts = ptxn.get("created_at")
    b_ts = brow.get("settled_at")
    if isinstance(p_ts, datetime) and isinstance(b_ts, datetime):
        days_diff = (b_ts - p_ts).total_seconds() / 86400
        if 0 <= days_diff <= 5:
            score += 0.3 * (1 - days_diff / 5)
        elif -1 <= days_diff < 0:
            score += 0.1

    if ptxn.get("currency") == brow.get("currency"):
        score += 0.2

    return min(score, 1.0)


def fuzzy_match(
    unmatched_platform: list[dict],
    unmatched_bank: list[dict],
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
    min_confidence: float = 0.5,
) -> tuple[list[dict], list[dict], list[dict], list[Finding]]:
    """Attempt fuzzy matching for remaining unmatched rows.

    Returns (fuzzy_matched, still_unmatched_platform, still_unmatched_bank, findings).
    """
    findings: list[Finding] = []
    fuzzy_matched: list[dict] = []
    used_bank: set[int] = set()
    still_unmatched_p: list[dict] = []

    for ptxn in unmatched_platform:
        best_score = 0.0
        best_idx = -1

        for j, brow in enumerate(unmatched_bank):
            if j in used_bank:
                continue
            score = _fuzzy_score(ptxn, brow, tolerance_paise)
            if score > best_score:
                best_score = score
                best_idx = j

        if best_score >= min_confidence and best_idx >= 0:
            brow = unmatched_bank[best_idx]
            used_bank.add(best_idx)

            if ptxn["type"] == "REFUND":
                expected = -ptxn["net_amount"]
            else:
                expected = ptxn["net_amount"]
            residual = brow["amount"] - expected

            fuzzy_matched.append({
                "platform": ptxn,
                "bank": brow,
                "residual": residual,
                "confidence": best_score,
            })
            findings.append(Finding(
                category=GapCategory.PROBABLE_MATCH,
                ids=[ptxn["txn_id"], brow["settlement_id"]],
                amount_impact_paise=-residual,  # Platform perspective
                confidence=best_score,
                explanation=(
                    f"Probable match: txn {ptxn['txn_id']} ↔ settlement "
                    f"{brow['settlement_id']} (confidence {best_score:.0%}). "
                    f"Key mismatch prevented exact match."
                ),
                details={
                    "platform_txn_id": ptxn["txn_id"],
                    "bank_reference": brow["bank_reference"],
                    "settlement_id": brow["settlement_id"],
                    "residual_paise": residual,
                },
            ))
        else:
            still_unmatched_p.append(ptxn)

    still_unmatched_b = [b for j, b in enumerate(unmatched_bank) if j not in used_bank]

    return fuzzy_matched, still_unmatched_p, still_unmatched_b, findings


# ---------------------------------------------------------------------------
# Layer 5: Classification
# ---------------------------------------------------------------------------

def _is_month_end_timing(ptxn: dict, recon_month: int, recon_year: int) -> tuple[bool, int]:
    """Check if unmatched platform txn is a timing difference.

    Returns (is_timing, days_over_cutoff).
    """
    ts = ptxn.get("created_at")
    if not isinstance(ts, datetime):
        return False, 0

    if ts.month != recon_month or ts.year != recon_year:
        return False, 0

    # Last 4 days of month — settlement lag + weekends can push further
    if ts.day < 28:
        return False, 0

    # Determine month-end date
    if recon_month == 12:
        month_end = datetime(recon_year, 12, 31, tzinfo=IST)
    else:
        month_end = datetime(recon_year, recon_month + 1, 1, tzinfo=IST) - timedelta(days=1)

    # Check if T+1 or T+2 settlement would fall in next month
    for lag in (1, 2):
        settle_date = ts
        added = 0
        while added < lag:
            settle_date += timedelta(days=1)
            if settle_date.weekday() < 5:
                added += 1
        if settle_date.month != recon_month or settle_date.year != recon_year:
            days_over = (settle_date.date() - month_end.date()).days
            return True, max(days_over, 1)

    return False, 0


def detect_orphan_refunds(
    platform_rows: list[dict],
    all_platform_txn_ids: set[str],
) -> list[Finding]:
    """Detect orphan refunds: refunds whose original_txn_id doesn't exist.

    This runs independently of matching — a refund can be matched (settled)
    but still be an orphan if its original payment is missing.
    """
    findings: list[Finding] = []
    for row in platform_rows:
        if row["type"] != "REFUND":
            continue
        orig = row.get("original_txn_id", "")
        if orig and orig not in all_platform_txn_ids:
            findings.append(Finding(
                category=GapCategory.ORPHAN_REFUND,
                ids=[row["txn_id"]],
                amount_impact_paise=0,  # No direct variance impact (it's matched/settled)
                confidence=1.0,
                explanation=(
                    f"Orphan refund: {row['txn_id']} references original "
                    f"{orig} which does not exist in the platform dataset."
                ),
                details={
                    "refund_txn_id": row["txn_id"],
                    "original_txn_id": orig,
                    "refund_amount_paise": row["net_amount"],
                },
            ))
    return findings


def classify_unmatched(
    unmatched_platform: list[dict],
    unmatched_bank: list[dict],
    all_platform_txn_ids: set[str],
    recon_month: int = 10,
    recon_year: int = 2026,
) -> list[Finding]:
    """Classify every unmatched row into a gap category."""
    findings: list[Finding] = []

    for ptxn in unmatched_platform:
        txn_id = ptxn["txn_id"]

        # Check timing difference
        is_timing, days_over = _is_month_end_timing(ptxn, recon_month, recon_year)
        if is_timing:
            # Amount impact: platform has it, bank doesn't
            # For payments: positive (platform ahead)
            # For refunds: negative (platform records a refund, bank hasn't deducted it)
            if ptxn["type"] == "REFUND":
                impact = -ptxn["net_amount"]
            else:
                impact = ptxn["net_amount"]

            findings.append(Finding(
                category=GapCategory.TIMING_DIFFERENCE,
                ids=[txn_id],
                amount_impact_paise=impact,
                confidence=0.9,
                explanation=(
                    f"Timing difference: {txn_id} created on "
                    f"{ptxn['created_at'].strftime('%Y-%m-%d')} (day {ptxn['created_at'].day}) "
                    f"— settlement expected ~{days_over} day(s) after month-end."
                ),
                details={
                    "created_at": ptxn["created_at"].isoformat(),
                    "days_over_cutoff": days_over,
                    "txn_day": ptxn["created_at"].day,
                    "txn_type": ptxn["type"],
                },
            ))
            continue

        # Fallback: missing settlement
        if ptxn["type"] == "REFUND":
            impact = -ptxn["net_amount"]
        else:
            impact = ptxn["net_amount"]

        findings.append(Finding(
            category=GapCategory.MISSING_SETTLEMENT,
            ids=[txn_id],
            amount_impact_paise=impact,
            confidence=0.8,
            explanation=(
                f"Missing settlement: {txn_id} ({ptxn['type']}, created "
                f"{ptxn['created_at'].strftime('%Y-%m-%d')}) has no matching "
                f"bank settlement and doesn't qualify as a timing difference."
            ),
            details={
                "txn_type": ptxn["type"],
                "net_amount_paise": ptxn["net_amount"],
                "created_at": ptxn["created_at"].isoformat(),
            },
        ))

    # Unmatched bank rows
    for brow in unmatched_bank:
        ref = brow["bank_reference"]
        findings.append(Finding(
            category=GapCategory.MISSING_TRANSACTION,
            ids=[ref, brow["settlement_id"]],
            amount_impact_paise=-brow["amount"],  # Bank has money but no platform record
            confidence=0.8,
            explanation=(
                f"Missing transaction: bank settlement {brow['settlement_id']} "
                f"references {ref} which is not in the platform data."
            ),
            details={
                "bank_reference": ref,
                "settlement_id": brow["settlement_id"],
                "bank_amount_paise": brow["amount"],
                "settled_at": brow["settled_at"].isoformat() if isinstance(brow.get("settled_at"), datetime) else str(brow.get("settled_at")),
            },
        ))

    return findings


# ---------------------------------------------------------------------------
# Layer 6: Aggregate Reconciliation & Waterfall
# ---------------------------------------------------------------------------

def compute_aggregate(
    platform_before_dedup: list[dict],
    bank_before_dedup: list[dict],
    matched: list[dict],
    findings: list[Finding],
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
) -> tuple[int, int, int, list[WaterfallItem], int, int, int, int]:
    """Compute totals and build waterfall.

    IMPORTANT: Totals use the RAW (pre-dedup) data because that's what the
    books actually show. The variance then includes the duplicate impact.

    Returns (platform_total, bank_total, variance,
             waterfall_items, explained, unexplained,
             rounding_drift_paise, rounding_drift_count).
    """
    # Totals from RAW data (before dedup) — this is what the books show
    platform_total = 0
    for row in platform_before_dedup:
        if row["status"] != "SUCCESS":
            continue
        if row["type"] == "REFUND":
            platform_total -= row["net_amount"]
        else:
            platform_total += row["net_amount"]

    bank_total = sum(row["amount"] for row in bank_before_dedup)

    # Variance = platform - bank (positive means platform shows more)
    variance = platform_total - bank_total

    # --- Rounding drift detection ---
    # Sum per-row residuals from matched pairs that are within tolerance
    rounding_residuals = [m["residual"] for m in matched if abs(m["residual"]) <= tolerance_paise]
    total_rounding = sum(rounding_residuals)
    rounding_rows = sum(1 for r in rounding_residuals if r != 0)

    # From platform perspective: if bank pays more (positive residual),
    # that reduces variance. So drift impact on variance = -total_rounding
    rounding_drift_impact = -total_rounding

    # --- Build waterfall ---
    # The waterfall explains: variance = sum of all component impacts
    # Each component's impact is signed from the platform's perspective:
    #   positive = platform shows more than bank (inflates variance)
    #   negative = bank shows more than platform (reduces variance)

    category_impacts: dict[str, tuple[int, int]] = {}  # category -> (total_paise, count)

    for f in findings:
        cat = f.category.value
        # Skip rounding drift findings (we compute it separately at aggregate level)
        if cat == "ROUNDING_DRIFT":
            continue
        if cat not in category_impacts:
            category_impacts[cat] = (0, 0)
        old_total, old_count = category_impacts[cat]
        category_impacts[cat] = (old_total + f.amount_impact_paise, old_count + 1)

    waterfall: list[WaterfallItem] = []

    # Order matters for the visual: largest components first
    waterfall_order = [
        ("TIMING_DIFFERENCE", "Timing / Cut-off"),
        ("DUPLICATE_PLATFORM", "Duplicate (Platform)"),
        ("DUPLICATE_BANK", "Duplicate (Bank)"),
        ("ORPHAN_REFUND", "Orphan Refunds"),
        ("AMOUNT_MISMATCH", "Amount Mismatches"),
        ("MISSING_SETTLEMENT", "Missing Settlements"),
        ("MISSING_TRANSACTION", "Missing Transactions"),
        ("PROBABLE_MATCH", "Probable Matches (Fuzzy)"),
    ]

    for cat_key, label in waterfall_order:
        if cat_key in category_impacts:
            amt, cnt = category_impacts[cat_key]
            if cat_key == "TIMING_DIFFERENCE":
                desc = f"{cnt} transaction(s) created near month-end, settling next month"
            elif cat_key == "DUPLICATE_PLATFORM":
                desc = f"{cnt} duplicate entries on platform side inflate platform total"
            elif cat_key == "DUPLICATE_BANK":
                desc = f"{cnt} duplicate entries on bank side inflate bank total"
            elif cat_key == "ORPHAN_REFUND":
                desc = f"{cnt} refund(s) with no matching original transaction"
            elif cat_key == "AMOUNT_MISMATCH":
                desc = f"{cnt} settlement(s) with amount differing by more than tolerance"
            elif cat_key == "MISSING_SETTLEMENT":
                desc = f"{cnt} platform transaction(s) with no bank settlement"
            elif cat_key == "MISSING_TRANSACTION":
                desc = f"{cnt} bank settlement(s) with no platform transaction"
            elif cat_key == "PROBABLE_MATCH":
                desc = f"{cnt} row(s) matched by fuzzy logic with residual"
            else:
                desc = f"{cnt} items"

            waterfall.append(WaterfallItem(
                label=label,
                amount_paise=amt,
                row_count=cnt,
                description=desc,
            ))

    # Rounding drift (aggregate-level, not from individual findings)
    if rounding_drift_impact != 0 or rounding_rows > 0:
        waterfall.append(WaterfallItem(
            label="Rounding Drift",
            amount_paise=rounding_drift_impact,
            row_count=rounding_rows,
            description=(
                f"{rounding_rows} rows contributed ±1 paise rounding differences; "
                f"aggregate drift = {rounding_drift_impact} paise "
                f"(₹{rounding_drift_impact / 100:.2f})"
            ),
        ))

    # Calculate explained
    explained = sum(w.amount_paise for w in waterfall)
    unexplained = variance - explained

    # Unexplained remainder
    if unexplained != 0:
        waterfall.append(WaterfallItem(
            label="Unexplained",
            amount_paise=unexplained,
            row_count=0,
            description="Residual variance not attributed to any identified cause",
        ))

    return (platform_total, bank_total, variance, waterfall,
            explained, unexplained, rounding_drift_impact, rounding_rows)


# ---------------------------------------------------------------------------
# Main Reconciler
# ---------------------------------------------------------------------------

def load_csv(filepath_or_text: str, is_file: bool = True) -> list[dict]:
    """Load a CSV file or string into a list of dicts."""
    if is_file:
        with open(filepath_or_text, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)
    else:
        reader = csv.DictReader(io.StringIO(filepath_or_text))
        return list(reader)


def reconcile(
    platform_data: list[dict] | str,
    bank_data: list[dict] | str,
    tolerance_paise: int = DEFAULT_TOLERANCE_PAISE,
    recon_month: int = 10,
    recon_year: int = 2026,
) -> ReconciliationResult:
    """Run the full reconciliation pipeline.

    Args:
        platform_data: list of dicts or file path
        bank_data: list of dicts or file path
        tolerance_paise: per-row amount match tolerance
        recon_month: month to reconcile (1-12)
        recon_year: year to reconcile

    Returns:
        ReconciliationResult with all findings and waterfall
    """
    # Load data if file paths
    if isinstance(platform_data, str):
        platform_raw = load_csv(platform_data)
    else:
        platform_raw = platform_data

    if isinstance(bank_data, str):
        bank_raw = load_csv(bank_data)
    else:
        bank_raw = bank_data

    all_findings: list[Finding] = []
    all_errors: list[str] = []

    # --- Layer 1: Validate ---
    platform_clean, p_errors = validate_platform_data(platform_raw)
    bank_clean, b_errors = validate_bank_data(bank_raw)
    all_errors.extend(p_errors)
    all_errors.extend(b_errors)

    # Filter to SUCCESS only for matching
    platform_success = [r for r in platform_clean if r["status"] == "SUCCESS"]

    # --- Layer 2: Duplicate Detection (BEFORE matching) ---
    platform_deduped, platform_dups, dup_p_findings = detect_duplicates_platform(platform_success)
    bank_deduped, bank_dups, dup_b_findings = detect_duplicates_bank(bank_clean)
    all_findings.extend(dup_p_findings)
    all_findings.extend(dup_b_findings)

    # --- Orphan Refund Detection (independent of matching) ---
    all_platform_ids = {r["txn_id"] for r in platform_clean}
    orphan_findings = detect_orphan_refunds(platform_deduped, all_platform_ids)
    all_findings.extend(orphan_findings)

    # --- Layer 3: Exact Matching ---
    matched, unmatched_p, unmatched_b, mismatch_findings = exact_match(
        platform_deduped, bank_deduped, tolerance_paise
    )
    all_findings.extend(mismatch_findings)

    # --- Layer 4: Fuzzy Matching ---
    fuzzy_matched, still_unmatched_p, still_unmatched_b, fuzzy_findings = fuzzy_match(
        unmatched_p, unmatched_b, tolerance_paise
    )
    all_findings.extend(fuzzy_findings)
    all_matched = matched + fuzzy_matched

    # --- Layer 5: Classification of remaining unmatched ---
    classify_findings = classify_unmatched(
        still_unmatched_p, still_unmatched_b,
        all_platform_ids, recon_month, recon_year
    )
    all_findings.extend(classify_findings)

    # --- Layer 6: Aggregate (uses pre-dedup data for totals) ---
    (platform_total, bank_total, variance, waterfall,
     explained, unexplained, rounding_drift, rounding_count) = compute_aggregate(
        platform_success, bank_clean, all_matched, all_findings, tolerance_paise
    )

    # Add rounding drift finding if significant
    if rounding_count > 0:
        all_findings.append(Finding(
            category=GapCategory.ROUNDING_DRIFT,
            ids=[],
            amount_impact_paise=rounding_drift,
            confidence=0.95,
            explanation=(
                f"Aggregate rounding drift: {rounding_count} rows with per-row "
                f"residuals within ±{tolerance_paise} paise summed to "
                f"{rounding_drift} paise (₹{rounding_drift / 100:.2f}). "
                f"Caused by platform floor() vs bank round_half_up() on fees."
            ),
            details={
                "rounding_rows": rounding_count,
                "total_drift_paise": rounding_drift,
                "detection_method": "aggregate_check",
            },
        ))

    pct = (abs(explained) / abs(variance) * 100) if variance != 0 else 100.0

    # Serialise matched pairs for drill-down (convert datetimes)
    serialised_matched = []
    for m in all_matched:
        sm = {
            "platform": {k: (v.isoformat() if isinstance(v, datetime) else v)
                         for k, v in m["platform"].items()},
            "bank": {k: (v.isoformat() if isinstance(v, datetime) else v)
                     for k, v in m["bank"].items()},
            "residual": m["residual"],
        }
        if "confidence" in m:
            sm["confidence"] = m["confidence"]
        serialised_matched.append(sm)

    return ReconciliationResult(
        platform_total_paise=platform_total,
        bank_total_paise=bank_total,
        total_variance_paise=variance,
        platform_row_count=len(platform_clean),
        bank_row_count=len(bank_clean),
        matched_count=len(all_matched),
        unmatched_platform_count=len(still_unmatched_p),
        unmatched_bank_count=len(still_unmatched_b),
        findings=all_findings,
        waterfall=waterfall,
        explained_paise=explained,
        unexplained_paise=unexplained,
        pct_explained=min(pct, 100.0),
        platform_duplicates=[{k: (v.isoformat() if isinstance(v, datetime) else v)
                              for k, v in d.items()} for d in platform_dups],
        bank_duplicates=[{k: (v.isoformat() if isinstance(v, datetime) else v)
                          for k, v in d.items()} for d in bank_dups],
        validation_errors=all_errors,
        rounding_drift_paise=rounding_drift,
        rounding_drift_row_count=rounding_count,
        matched_pairs=serialised_matched,
    )
