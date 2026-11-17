# ASSUMPTIONS.md — Payments Reconciliation System

> This document records **every** design assumption. Silent assumptions are failures.
> Updated as the build progresses. Each assumption is numbered for traceability.

---

## A1. Currency and Minor-Unit Precision

**Choice:** Indian Rupee (INR), stored as **integer paise** (1 INR = 100 paise).

**Justification:**
- INR has exactly 2 decimal places, so 1 minor unit = 1 paisa.
- Integer arithmetic eliminates floating-point representation errors entirely.
  `Decimal` would also work but adds serialisation complexity and is slower for
  aggregations. Since INR has a fixed 2-decimal structure (unlike currencies with
  3-decimal minor units, e.g. KWD), integer paise is the simplest correct choice.
- All CSVs store amounts in **paise** (integer columns). The UI converts to
  INR for display (divide by 100, format to 2 decimals).

**Rounding rule:** When a percentage fee produces a fractional paisa, the
platform truncates (`math.floor`), while the bank rounds half-up
(`ROUND_HALF_UP`). This is realistic (different systems, different rounding) and
is the mechanism that creates Gap Type B (rounding drift). See A7.

---

## A2. Reconciliation Month and Timezone

**Reconciliation month:** October 2026 (2026-10-01 to 2026-10-31 inclusive).

**Platform timezone:** Asia/Kolkata (IST, UTC+05:30). Timestamps are stored as
ISO 8601 with timezone: `2026-10-15T14:23:07+05:30`.

**Bank timezone:** Also Asia/Kolkata. Indian payment processors (NPCI, bank
SFTP files) report in IST.

**Why this matters for cut-off (Gap Type A):**
The reconciliation window is `2026-10-01 00:00:00 IST` to
`2026-10-31 23:59:59.999999 IST`. A transaction created at `2026-10-30 22:00 IST`
with a 2-business-day settlement lag lands on `2026-11-02` (Monday after the
weekend), which is **outside** the window. This is a timing difference, not an error.

**Edge case:** Both systems use the same timezone, so there is no cross-timezone
midnight ambiguity. In production this would not hold (see WHAT THIS WOULD GET
WRONG IN PRODUCTION in README.md).

---

## A3. Settlement Lag Distribution

**Model:** Settlement happens T+1 or T+2 **business days** after the transaction.

- ~60% settle T+1, ~40% settle T+2 (weighted random, seeded).
- **Weekends (Saturday, Sunday) are not business days.** A Friday T+1 settles
  Monday; a Friday T+2 settles Tuesday.
- **Public holidays:** Not modelled. In production, Indian bank holidays (RBI
  calendar) would add further delays. This is noted as a simplification.
- Consequence: Transactions on the last 1–2 business days of October may settle
  in November — these become Gap Type A (TIMING_DIFFERENCE).

---

## A4. Platform Fee Handling — Net vs Gross

**Platform records:** `gross_amount` (what the customer paid), `fee` (platform
commission), and `net_amount = gross_amount − fee`.

**Bank settles the net amount.** The bank pays the merchant, so the settlement
amount equals the platform's `net_amount`.

**Why net:** In India's payment aggregator model (RBI PA/PG guidelines), the
aggregator collects gross from the customer, deducts its fee, and settles net
to the merchant. The bank file therefore shows the net figure.

**Matching implication:** The reconciler compares
`platform.net_amount` ↔ `bank.amount`. A mismatch here means either the fee
was computed differently or the settlement amount is wrong.

---

## A5. Settlement Granularity — Per-Transaction (1:1)

**Choice:** Each bank settlement row corresponds to exactly one platform
transaction (1:1 mapping via `bank_reference = txn_id`).

**Justification:** This simplifies matching and isolates gap types. In
production, banks often batch-settle (one row for N transactions). The
`batch_id` column in the bank file groups settlements that were wire-transferred
together, but each line within the batch is still per-transaction.

**batch_id:** Settlements on the same day share a `batch_id`
(format: `BATCH-YYYYMMDD-NNN`). This is metadata for grouping, not a match key.

---

## A6. Match Key

**Primary key:** `platform.txn_id` ↔ `bank.bank_reference`.

Both are the same string (format: `TXN-XXXXXXXX`, 8-char hex). The platform
generates it; the bank echoes it back in the settlement file.

**Fallback (fuzzy matching):** When the primary key fails (e.g. a settlement
with a corrupted or missing `bank_reference`), the reconciler falls back to:
- Same `amount` (within tolerance, see A8)
- `settled_at` within [created_at + 0 days, created_at + 5 days]
- Optionally same `batch_id` date ≈ expected settlement date

Fuzzy matches are flagged with a confidence score (0.0–1.0) and category
`PROBABLE_MATCH`. They are never silently merged.

---

## A7. Rounding Policy per System

**Platform:** Fee = `floor(gross_amount * fee_rate)` in paise.
`fee_rate` is computed at 4-decimal precision (e.g., 0.0235 = 2.35%).
The `floor` truncates any fractional paisa.

**Bank:** The bank independently computes the net as
`round_half_up(gross_amount * (1 − fee_rate))` in paise.

**Drift mechanism:** For most transactions, `gross − floor(gross × rate)` and
`round_half_up(gross × (1 − rate))` agree. But for certain gross amounts, they
differ by exactly 1 paisa. Over thousands of rows, these ±1 paisa differences
accumulate to a visible aggregate drift (Gap Type B).

**Per-row tolerance absorbs this** (see A8), so row-level matching says "OK".
Only the aggregate check detects the drift.

---

## A8. Match Tolerance for Amounts

**Per-row tolerance:** ±1 paisa (i.e., `abs(platform.net_amount − bank.amount) <= 1`).

**Justification:** The rounding difference (A7) is at most 1 paisa per row.
Setting tolerance to 1 paisa means row-level matching passes even when rounding
differs, which is exactly the scenario we need: row-level OK but aggregate drift
visible.

**Aggregate tolerance:** 0 (zero). Any non-zero aggregate difference is reported
in the waterfall. The rounding drift will show up here as the sum of thousands
of ±1-paisa per-row residuals.

---

## A9. Refund Representation

**Platform:** Refunds are rows with `type = 'REFUND'`, a **positive**
`gross_amount` (the refund amount), and `original_txn_id` pointing to the
original `PAYMENT` row. `fee` on a refund is 0 (the platform does not refund
its fee — common in India). `net_amount` = `gross_amount` for refunds.

**Bank:** Refund settlements have a **negative** `amount` (money flows back
to the customer). The `bank_reference` links to the **refund** transaction's
`txn_id`, not the original payment's.

**Orphan refund (Gap Type D):** A refund whose `original_txn_id` either does
not exist in the platform dataset or points to a transaction outside the
reconciliation window. The refund itself may still be settled correctly by the
bank, but the reconciler cannot trace it to an originating payment.

---

## A10. Volume

**Target:** ~7,000 transactions per month (≈225/day on 31 days, ≈320/day on
~22 business days). This is realistic for a mid-size Indian payment aggregator.

**Breakdown:**
- ~93% PAYMENT, ~7% REFUND (industry norm: 5–10% refund rate).
- Refunds reference a random earlier payment from the same month.
- Customer IDs: ~2,000 unique customers (some are repeat buyers).

---

## A11. Planted Gap Volumes

| Gap Type | Code | Count | Notes |
|---|---|---|---|
| Timing / Cut-off | A | 15–25 | Transactions on Oct 29–31 settling in Nov |
| Rounding Drift | B | ~2,000–3,000 rows | Per-row ±1 paisa; aggregate drift ~₹10–30 |
| Duplicate | C | 3–5 per side | At least 1 in platform, at least 1 in bank |
| Orphan Refund | D | 5–8 | original_txn_id missing or out of window |
| **Extra: Missing Settlement** | E1 | 3–5 | Transaction exists, no settlement at all |
| **Extra: Amount Mismatch** | E2 | 3–5 | Settlement amount differs by >1 paisa |

"Extra" types are clearly labelled in the code and ground-truth manifest.

---

## A12. Status Field (Platform)

`status` ∈ {`SUCCESS`, `FAILED`, `PENDING`}.

- Only `SUCCESS` transactions are expected to have a bank settlement.
- `FAILED` transactions are ignored by the reconciler.
- `PENDING` transactions at month-end are flagged but not treated as gaps
  (they haven't been sent to the bank yet).
- Refunds have status `SUCCESS` when the refund has been processed.

---

## A13. Data Generator Determinism

The generator uses Python's `random.Random(seed)` instance (not the global
`random` module) so that:
- Same seed → same CSVs byte-for-byte.
- The `--seed` CLI argument is an integer.
- Default seed is `42` if not provided.

NumPy is **not** used for random number generation to avoid cross-platform
PRNG differences. Only stdlib `random`.

---

## A14. File Encoding and Format

- CSV files: UTF-8, no BOM, comma-delimited, double-quote escaping.
- Timestamps: ISO 8601 with timezone offset (`+05:30`).
- Amounts: plain integers (paise), no thousands separators.
- `_ground_truth.json`: UTF-8, pretty-printed (indent=2).

---

## A15. Business Day Definition

A business day is Monday–Friday. Weekends (Saturday and Sunday) are non-business
days. Indian public holidays are **not** modelled (see A3).

---

## Changelog

| Date | Change |
|---|---|
| 2026-11-17 | Initial version — Phase 1 |
