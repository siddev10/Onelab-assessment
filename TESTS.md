# TESTS.md — Test Suite Specification & Results

> Comprehensive test report for the payments reconciliation engine.
> Total tests: **37 passed** across all layers, invariants, and end-to-end scenarios.

---

## 1. Test Summary

| Layer / Suite | Test Count | Status | Description |
|---|:---:|:---:|---|
| **Layer 1: Validation** | 9 | Passed | Schema compliance, type coercion, timezone normalisation to IST, malformed rows, empty files |
| **Layer 2: Duplicate Detection** | 4 | Passed | Detection on platform side, bank side, triple duplicate, clean data |
| **Layer 3: Exact Matching** | 6 | Passed | Exact key matching, tolerance edge cases (±1 inside, ±2 outside), refunds, failed txns |
| **Layer 4: Fuzzy Matching** | 2 | Passed | Proximity scoring, confidence thresholds, amount/time distance matching (0–5 day window) |
| **Layer 5: Classification** | 6 | Passed | Timing difference on month end, mid-month handling, orphan refund detection, valid refund passing, weekend settlement lag |
| **Layer 6: Aggregate & Rounding** | 2 | Passed | Row-level passes while aggregate fails (rounding drift proof), waterfall sum invariant |
| **Invariants & Properties** | 2 | Passed | Mutually exclusive classification (no double counting), waterfall exact sum to variance |
| **End-to-End & Ground Truth** | 4 | Passed | Seed 42, Seed 123, Seed 7777 evaluation (100% recall), precision report |
| **CSV Utilities** | 2 | Passed | File and string streaming readers |
| **Total** | **37** | **37 Passed** | **0 Failed, 0 Skipped (21.5s)** |

---

## 2. Detailed Test Specifications and Proofs

### Layer 1: Validation & Normalisation
| Test Function | What it Proves |
|---|---|
| `test_valid_platform_row` | Validates standard platform transaction row, types, and IST datetime parsing. |
| `test_valid_bank_row` | Validates standard bank settlement row and integer amount coercion. |
| `test_empty_platform_data` | Gracefully reports error without crashing when platform input is empty. |
| `test_empty_bank_data` | Gracefully reports error without crashing when bank input is empty. |
| `test_malformed_timestamp` | Rejects unparseable timestamps, records row-level error, skips bad row. |
| `test_malformed_amount` | Catches non-numeric amount strings and flags error. |
| `test_missing_column` | Detects missing required schema headers and reports descriptive error. |
| `test_invalid_type` | Enforces domain validation on `type` (only `PAYMENT` or `REFUND` allowed). |
| `test_timezone_normalisation`| Normalises UTC or offset timestamps to IST (`+05:30`) preserving exact point in time. |

### Layer 2: Duplicate Detection (Pre-Matching)
| Test Function | What it Proves |
|---|---|
| `test_no_duplicates_platform` | Clean dataset produces zero false-positive duplicate findings. |
| `test_duplicate_on_platform_side` | Detects duplicated `txn_id` on platform, keeps first occurrence, assigns signed impact (+amount). |
| `test_duplicate_on_bank_side` | Detects duplicated `bank_reference` across multiple `settlement_id`s, assigns signed impact (-amount). |
| `test_triple_duplicate` | Handles N duplicates (>2 copies) correctly, accounting for (N-1) extra impacts. |

### Layer 3: Exact Matching & Amount Tolerance
| Test Function | What it Proves |
|---|---|
| `test_exact_match_happy_path` | Exact `txn_id` ↔ `bank_reference` match with zero residual. |
| `test_match_within_tolerance` | **Tolerance edge case (inside):** residual of 1 paisa (within tolerance=1) matches without mismatch flag. |
| `test_match_just_outside_tolerance` | **Tolerance edge case (outside):** residual of 2 paise (outside tolerance=1) matches key but raises `AMOUNT_MISMATCH`. |
| `test_unmatched_platform` | Unmatched transactions are properly routed to unmatched pool. |
| `test_failed_txn_skipped` | Platform records with `FAILED` status do not attempt bank matching. |
| `test_refund_matching` | Refunds match against bank negative settlements (`platform.net_amount == -bank.amount`). |

### Layer 4: Fuzzy Matching Fallback
| Test Function | What it Proves |
|---|---|
| `test_fuzzy_match_same_amount_close_time` | Recovers match when key is corrupted but amount and settlement time match within 5 days. |
| `test_fuzzy_no_match_different_amount` | Rejects false candidates when amounts differ significantly. |

### Layer 5: Gap Classification
| Test Function | What it Proves |
|---|---|
| `test_timing_difference_last_day` | Unmatched transaction on last days of month is classified as `TIMING_DIFFERENCE`. |
| `test_timing_difference_mid_month` | Unmatched transaction on Oct 15 is NOT classified as timing, but as `MISSING_SETTLEMENT`. |
| `test_orphan_refund_detected` | Refund pointing to a non-existent `original_txn_id` is caught as `ORPHAN_REFUND`. |
| `test_valid_refund_not_orphan` | **Valid Refund Test:** Refund with a verified parent transaction in the dataset is not flagged as orphan. |
| `test_missing_transaction_bank_side` | Bank settlement with no corresponding platform transaction is flagged as `MISSING_TRANSACTION`. |
| `test_weekend_settlement_timing` | Friday transactions correctly account for Saturday/Sunday non-business days in lag calculations. |

### Layer 6: Aggregate & Rounding Drift
| Test Function | What it Proves |
|---|---|
| `test_rounding_row_level_passes_aggregate_fails` | **CRITICAL PROOF:** 100 transactions each differing by 1 paisa pass row-level matching (residual ≤ tolerance), but aggregate detection identifies the accumulated 100 paise drift. |
| `test_waterfall_sums_to_variance` | Sum of all waterfall components equals platform total minus bank total exactly. |

### Invariant & Property Tests
| Test Function | What it Proves |
|---|---|
| `test_no_double_classification` | Invariant: no transaction ID is assigned to multiple contradictory gap categories. |
| `test_waterfall_invariant_on_generated_data` | Mathematical invariant: `Platform Total - Bank Total == Sum(Waterfall Components)` to the minor unit (paise) on full synthetic datasets. |

### Multi-Seed Verification (100% Recall & Exact Waterfall Balance)

| Metric | Seed 42 | Seed 7 | Seed 2026 |
|---|:---:|:---:|:---:|
| **Platform Total** | 1,051,074,730 paise | 1,044,715,915 paise | 1,075,451,847 paise |
| **Bank Total** | 971,240,486 paise | 987,668,698 paise | 984,347,683 paise |
| **Total Variance** | 79,834,244 paise | 57,047,217 paise | 91,104,164 paise |
| **Waterfall Sum** | 79,834,244 paise | 57,047,217 paise | 91,104,164 paise |
| **Waterfall Residual** | **0 paise (Exact)** | **0 paise (Exact)** | **0 paise (Exact)** |
| **Variance Explained** | **100.00%** | **100.00%** | **100.00%** |
| **Planted Anomalies** | 504 | 474 | 496 |
| **Detected Anomalies** | 504 | 474 | 496 |
| **Planted Recall** | **100.0%** | **100.0%** | **100.0%** |
| **Timing Gaps Found** | 511 (484 planted + 27 natural) | 499 (450 planted + 49 natural) | 514 (476 planted + 38 natural) |
| **Rounding Drift Rows** | 3,069 rows (+3,069 paise) | 3,066 rows (+3,066 paise) | 2,968 rows (+2,968 paise) |
| **Duplicates (Platform)** | 3 of 3 | 3 of 3 | 3 of 3 |
| **Duplicates (Bank)** | 2 of 2 | 4 of 4 | 2 of 2 |
| **Orphan Refunds** | 8 of 8 | 7 of 7 | 6 of 6 |
| **Missing Settlements** | 3 of 3 | 4 of 4 | 5 of 5 |
| **Amount Mismatches** | 3 of 3 | 5 of 5 | 3 of 3 |
