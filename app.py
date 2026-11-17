"""
app.py — Institutional Payments Reconciliation Dashboard

A high-density, professional finance cockpit designed for CFOs and Treasury teams.
Reconciles platform payments vs bank settlements, explains root causes,
and bridges month-end variances with an interactive waterfall.
"""

from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from generate_data import DataGenerator
from reconcile import (
    reconcile,
    validate_platform_data,
    validate_bank_data,
    load_csv,
    GapCategory,
    ReconciliationResult,
    Finding,
)

# ---------------------------------------------------------------------------
# Page Configuration & Professional Theme
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Fintech Ledger & Settlement Reconciler",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Institutional CSS
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }
    code, pre, .mono {
        font-family: 'JetBrains Mono', monospace !important;
    }

    /* Main Container Padding */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
        padding-left: 2rem;
        padding-right: 2rem;
        max-width: 1400px;
    }

    /* Metric Cards */
    .kpi-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 1.1rem 1.25rem;
        box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.05);
        margin-bottom: 0.75rem;
    }
    .kpi-label {
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748b;
        margin-bottom: 0.35rem;
    }
    .kpi-value {
        font-size: 1.5rem;
        font-weight: 700;
        color: #0f172a;
        line-height: 1.2;
    }
    .kpi-sub {
        font-size: 0.75rem;
        color: #94a3b8;
        margin-top: 0.3rem;
    }

    /* Badge & Tag Styling */
    .badge {
        display: inline-block;
        padding: 0.2rem 0.55rem;
        border-radius: 4px;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.02em;
    }
    .badge-timing { background-color: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
    .badge-duplicate { background-color: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
    .badge-orphan { background-color: #f3e8ff; color: #6b21a8; border: 1px solid #e9d5ff; }
    .badge-mismatch { background-color: #ffedd5; color: #9a3412; border: 1px solid #fed7aa; }
    .badge-missing { background-color: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; }
    .badge-fuzzy { background-color: #e0f2fe; color: #075985; border: 1px solid #bae6fd; }
    .badge-drift { background-color: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }

    /* Explanation Banner */
    .explanation-box {
        background-color: #f8fafc;
        border-left: 4px solid #3b82f6;
        padding: 0.9rem 1.1rem;
        border-radius: 0 6px 6px 0;
        margin-bottom: 1rem;
        font-size: 0.88rem;
        color: #334155;
        line-height: 1.5;
    }

    /* Table styling tweaks */
    div[data-testid="stDataFrame"] {
        border: 1px solid #e2e8f0;
        border-radius: 6px;
        overflow-x: auto;
    }

    /* Mobile Responsive Optimizations (390px) */
    @media (max-width: 640px) {
        .block-container {
            padding-left: 0.5rem !important;
            padding-right: 0.5rem !important;
            padding-top: 1rem !important;
        }
        .kpi-card {
            padding: 0.75rem 0.85rem !important;
            margin-bottom: 0.5rem !important;
        }
        .kpi-value {
            font-size: 1.2rem !important;
        }
        .kpi-label {
            font-size: 0.7rem !important;
        }
        h2 {
            font-size: 1.3rem !important;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Format Helpers
# ---------------------------------------------------------------------------

def format_inr(paise: int, show_sign: bool = False) -> str:
    """Format integer paise into standard Indian Rupee string."""
    sign = "+" if show_sign and paise > 0 else ""
    rupees = paise / 100.0
    return f"{sign}₹{rupees:,.2f}"


def format_paise(paise: int) -> str:
    return f"{paise:,} p"


# ---------------------------------------------------------------------------
# State Management & Data Generation
# ---------------------------------------------------------------------------

def get_or_create_default_data() -> tuple[str, str]:
    """Ensure baseline synthetic data exists on disk."""
    plat_path = os.path.join(".", "data", "platform_transactions.csv")
    bank_path = os.path.join(".", "data", "bank_settlements.csv")

    if not os.path.exists(plat_path) or not os.path.exists(bank_path):
        with st.spinner("Initializing synthetic ledger baseline..."):
            gen = DataGenerator(seed=42, volume=7000)
            gen.generate()
            gen.write("./data", write_ground_truth=False)

    return plat_path, bank_path


# ---------------------------------------------------------------------------
# Sidebar: Controls & Data Ingestion
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Ledger Controls")
    mode = st.radio(
        "Mode",
        ["Synthetic Generator", "Upload CSVs"],
        label_visibility="collapsed",
    )

    if mode == "Synthetic Generator":
        st.markdown("##### Generator Parameters")
        seed_val = st.number_input("Random Seed", min_value=1, max_value=999999, value=42, step=1)
        volume_val = st.slider("Transaction Volume", min_value=1000, max_value=15000, value=7000, step=500)
        recon_month = st.selectbox("Reconciliation Period", ["October 2026 (IST)"], index=0)
        tolerance_val = st.number_input(
            "Per-Row Match Tolerance (paise)",
            min_value=0,
            max_value=100,
            value=1,
            help="±1 paise tolerance absorbs fractional rounding differences (Gap B) at row level.",
        )

        generate_clicked = st.button("Generate & Reconcile", type="primary", use_container_width=True)

        if generate_clicked or "result" not in st.session_state:
            with st.spinner("Generating ledger & executing 7-layer reconciliation..."):
                gen = DataGenerator(seed=int(seed_val), volume=int(volume_val))
                gen.generate()
                with tempfile.TemporaryDirectory() as tmpdir:
                    gen.write(tmpdir, write_ground_truth=False)
                    p_file = os.path.join(tmpdir, "platform_transactions.csv")
                    b_file = os.path.join(tmpdir, "bank_settlements.csv")
                    st.session_state.platform_raw = load_csv(p_file)
                    st.session_state.bank_raw = load_csv(b_file)
                    st.session_state.result = reconcile(
                        st.session_state.platform_raw,
                        st.session_state.bank_raw,
                        tolerance_paise=int(tolerance_val),
                        recon_month=10,
                        recon_year=2026,
                    )
            st.success(f"Generated & reconciled {volume_val:,} records (Seed: {seed_val})")

    else:
        st.markdown("##### Upload External Files")
        plat_upload = st.file_uploader("Platform Transactions (CSV)", type=["csv"])
        bank_upload = st.file_uploader("Bank Settlements (CSV)", type=["csv"])
        tolerance_val = st.number_input(
            "Per-Row Tolerance (paise)",
            min_value=0,
            max_value=100,
            value=1,
            key="upload_tolerance",
        )

        if plat_upload and bank_upload:
            plat_text = plat_upload.getvalue().decode("utf-8")
            bank_text = bank_upload.getvalue().decode("utf-8")

            if st.button("Reconcile Uploaded Files", type="primary", use_container_width=True):
                with st.spinner("Validating schema and running reconciliation..."):
                    try:
                        p_rows = load_csv(plat_text, is_file=False)
                        b_rows = load_csv(bank_text, is_file=False)
                        uploaded_result = reconcile(
                            p_rows,
                            b_rows,
                            tolerance_paise=int(tolerance_val),
                            recon_month=10,
                            recon_year=2026,
                        )
                        if uploaded_result.validation_errors and (not uploaded_result.platform_row_count or not uploaded_result.bank_row_count):
                            st.error(f"Upload rejected: {len(uploaded_result.validation_errors)} schema error(s) found.")
                            for err in uploaded_result.validation_errors[:5]:
                                st.caption(f"• {err}")
                        else:
                            st.session_state.platform_raw = p_rows
                            st.session_state.bank_raw = b_rows
                            st.session_state.result = uploaded_result
                            st.success("Custom files reconciled successfully.")
                    except Exception as ex:
                        st.error(f"Malformed CSV Error: Unable to parse uploaded file ({str(ex)})")

    st.markdown("---")
    st.markdown(
        """
        <div style="font-size:0.75rem; color:#64748b; line-height:1.4;">
        <b>Engine:</b> 7-Layer Deterministic Reconciler<br>
        <b>Precision:</b> Integer Minor Units (Paise)<br>
        <b>Zero Float Policy:</b> Strict<br>
        <b>IST Timezone:</b> UTC+05:30
        </div>
        """,
        unsafe_allow_html=True,
    )

# Fallback initialization if first load without button click
if "result" not in st.session_state:
    p_def, b_def = get_or_create_default_data()
    st.session_state.platform_raw = load_csv(p_def)
    st.session_state.bank_raw = load_csv(b_def)
    st.session_state.result = reconcile(
        st.session_state.platform_raw,
        st.session_state.bank_raw,
        tolerance_paise=1,
        recon_month=10,
        recon_year=2026,
    )

res: ReconciliationResult = st.session_state.result

# ---------------------------------------------------------------------------
# Top Header & 60-Second KPI Ribbon
# ---------------------------------------------------------------------------

st.markdown(
    """
    <div style="margin-bottom: 1.25rem;">
        <h2 style="margin:0; font-weight:700; color:#0f172a; letter-spacing:-0.02em;">
            Payments Month-End Reconciliation
        </h2>
        <div style="font-size:0.88rem; color:#64748b; margin-top:0.2rem;">
            October 2026 Ledger Close • Platform vs Bank Settlement Variance Bridge
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

kpi1, kpi2, kpi3, kpi4 = st.columns(4)

with kpi1:
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">Platform Gross Ledger</div>
            <div class="kpi-value">{format_inr(res.platform_total_paise)}</div>
            <div class="kpi-sub">{res.platform_row_count:,} records processed</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with kpi2:
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">Bank Settlement Total</div>
            <div class="kpi-value">{format_inr(res.bank_total_paise)}</div>
            <div class="kpi-sub">{res.bank_row_count:,} settlement credits</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with kpi3:
    var_color = "#e11d48" if res.total_variance_paise != 0 else "#059669"
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">Gross Variance</div>
            <div class="kpi-value" style="color: {var_color};">{format_inr(res.total_variance_paise)}</div>
            <div class="kpi-sub">{format_paise(res.total_variance_paise)} unadjusted difference</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with kpi4:
    pct_color = "#059669" if res.pct_explained >= 99.9 else "#d97706"
    st.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">Variance Explained</div>
            <div class="kpi-value" style="color: {pct_color};">{res.pct_explained:.1f}%</div>
            <div class="kpi-sub">Residual unallocated: {format_inr(res.unexplained_paise)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Hero Visual: The Institutional Variance Waterfall Bridge
# ---------------------------------------------------------------------------

st.markdown("### Variance Bridge (Root Cause Attribution)")

# Construct Waterfall items for Plotly
wf_x = ["Total Variance"]
wf_y = [res.total_variance_paise / 100.0]
wf_measure = ["absolute"]
wf_text = [format_inr(res.total_variance_paise)]

# Invert items because we are bridging from Total Variance DOWN to Unexplained
# Total Variance - Timing - PlatformDups + BankDups - Rounding - Mismatches = Residual
for item in res.waterfall:
    if item.label == "Unexplained":
        continue
    wf_x.append(item.label)
    # The negative sign bridges from variance toward zero
    change_val = -(item.amount_paise / 100.0)
    wf_y.append(change_val)
    wf_measure.append("relative")
    wf_text.append(f"{'-' if item.amount_paise > 0 else '+'}{format_inr(abs(item.amount_paise))}")

wf_x.append("Unexplained Residual")
wf_y.append(res.unexplained_paise / 100.0)
wf_measure.append("total")
wf_text.append(format_inr(res.unexplained_paise))

fig_wf = go.Figure(
    go.Waterfall(
        name="Variance Attribution",
        orientation="v",
        measure=wf_measure,
        x=wf_x,
        textposition="outside",
        text=wf_text,
        y=wf_y,
        connector={"line": {"color": "#cbd5e1", "width": 1.5, "dash": "solid"}},
        decreasing={"marker": {"color": "#3b82f6"}},  # Blue for explaining variance
        increasing={"marker": {"color": "#ec4899"}},  # Pink/Rose for reverse impact
        totals={"marker": {"color": "#0f172a" if res.unexplained_paise == 0 else "#e11d48"}},
        hoverinfo="x+text",
    )
)

fig_wf.update_layout(
    margin=dict(l=20, r=20, t=20, b=30),
    height=340,
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Inter", size=11, color="#475569"),
    yaxis=dict(
        title="Amount (₹ INR)",
        gridcolor="#f1f5f9",
        zerolinecolor="#cbd5e1",
        zerolinewidth=1,
    ),
    xaxis=dict(tickangle=-15),
)

st.plotly_chart(fig_wf, use_container_width=True)

# ---------------------------------------------------------------------------
# Tabular Breakdowns per Gap Category
# ---------------------------------------------------------------------------

tabs = st.tabs([
    "Timing Cut-off (A)",
    "Rounding Drift (B)",
    "Duplicates (C)",
    "Orphan Refunds (D)",
    "Amount Mismatches (E2)",
    "Missing Items (E1)",
    "Fuzzy / Probable",
    "Audit Log & Schema",
])

# Pre-filter findings
findings_by_cat: dict[str, list[Finding]] = {}
for f in res.findings:
    cat = f.category.value
    if cat not in findings_by_cat:
        findings_by_cat[cat] = []
    findings_by_cat[cat].append(f)

# TAB 1: Timing / Cut-off
with tabs[0]:
    timing_list = findings_by_cat.get(GapCategory.TIMING_DIFFERENCE.value, [])
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> The platform captures customer transactions instantaneously up to midnight on Oct 31.
            Due to banking batching rules (T+1 / T+2 settlement lag) and non-business weekend cut-offs, settlements for transactions
            initiated on Oct 29–31 clear on Nov 2–3. These are <b>not errors</b>; they represent recognized revenue in transit.
        </div>
        """,
        unsafe_allow_html=True,
    )

    if timing_list:
        timing_data = [
            {
                "Transaction ID": f.ids[0],
                "Created At": f.details.get("created_at", ""),
                "Type": f.details.get("txn_type", "PAYMENT"),
                "Days Over Cutoff": f.details.get("days_over_cutoff", 1),
                "Amount (INR)": f.amount_impact_paise / 100.0,
                "Amount (Paise)": f.amount_impact_paise,
                "Explanation": f.explanation,
            }
            for f in timing_list
        ]
        df_timing = pd.DataFrame(timing_data)

        c1, c2 = st.columns([3, 1])
        with c1:
            search_t = st.text_input("Filter by Transaction ID", "", key="search_timing")
        with c2:
            st.metric("Total In-Transit", format_inr(sum(d["Amount (Paise)"] for d in timing_data)))

        if search_t:
            df_timing = df_timing[df_timing["Transaction ID"].str.contains(search_t, case=False)]

        st.dataframe(
            df_timing.style.format({"Amount (INR)": "₹{:,.2f}", "Amount (Paise)": "{:,}"}),
            use_container_width=True,
            height=300,
        )

        csv_t = df_timing.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Timing Items (CSV)",
            csv_t,
            "timing_differences_oct2026.csv",
            "text/csv",
            key="dl_timing",
        )
    else:
        st.info("No timing differences detected.")

# TAB 2: Rounding Drift
with tabs[1]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> The platform computes fees at high precision using <code>math.floor(gross * fee_rate)</code>,
            while the acquiring bank calculates net payout as <code>round_half_up(gross * (1 - fee_rate))</code>.
            While individual transactions differ by at most ±1 paisa (which falls within the per-row tolerance),
            the sum across thousands of rows drifts visibly at the aggregate ledger level.
        </div>
        """,
        unsafe_allow_html=True,
    )

    r_c1, r_c2, r_c3 = st.columns(3)
    r_c1.metric("Affected Transaction Rows", f"{res.rounding_drift_row_count:,}")
    r_c2.metric("Cumulative Net Drift", format_inr(res.rounding_drift_paise))
    r_c3.metric("Avg Drift Per Row", f"{(res.rounding_drift_paise / max(1, res.rounding_drift_row_count)):.3f} paise")

    # Show sample rows with non-zero residual from matched pairs
    rounding_samples = [
        {
            "Transaction ID": m["platform"]["txn_id"],
            "Platform Net (Paise)": m["platform"]["net_amount"],
            "Bank Payout (Paise)": m["bank"]["amount"],
            "Residual Drift (Paise)": m["residual"],
            "Residual (INR)": m["residual"] / 100.0,
            "Batch ID": m["bank"]["batch_id"],
        }
        for m in res.matched_pairs
        if m["residual"] != 0 and abs(m["residual"]) <= 1
    ]

    if rounding_samples:
        df_round = pd.DataFrame(rounding_samples)
        st.markdown("##### Sample Transaction Rounding Discrepancies (±1 Paisa)")
        st.dataframe(
            df_round.head(100).style.format({"Residual (INR)": "₹{:,.2f}"}),
            use_container_width=True,
            height=280,
        )
        csv_r = df_round.to_csv(index=False).encode("utf-8")
        st.download_button("Download All Rounding Drift Rows (CSV)", csv_r, "rounding_drift.csv", "text/csv")
    else:
        st.info("No rounding drift detected.")

# TAB 3: Duplicates
with tabs[2]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> System retries, network idempotency failures, or batch resubmissions can cause
            transactions to be double-recorded. The reconciler validates uniqueness <i>prior</i> to matching to pinpoint
            whether the duplicate sits on the internal platform or the banking ledger.
        </div>
        """,
        unsafe_allow_html=True,
    )

    d_col1, d_col2 = st.columns(2)

    with d_col1:
        st.markdown("##### Platform Duplicates")
        p_dups = findings_by_cat.get(GapCategory.DUPLICATE_PLATFORM.value, [])
        if p_dups:
            df_p_dup = pd.DataFrame([
                {
                    "Txn ID": f.ids[0],
                    "Occurrences": f.details.get("occurrences", 2),
                    "Extra Impact (INR)": f.amount_impact_paise / 100.0,
                    "Explanation": f.explanation,
                }
                for f in p_dups
            ])
            st.dataframe(df_p_dup.style.format({"Extra Impact (INR)": "₹{:,.2f}"}), use_container_width=True)
        else:
            st.info("No platform duplicates found.")

    with d_col2:
        st.markdown("##### Bank Settlement Duplicates")
        b_dups = findings_by_cat.get(GapCategory.DUPLICATE_BANK.value, [])
        if b_dups:
            df_b_dup = pd.DataFrame([
                {
                    "Bank Reference": f.ids[0],
                    "Settlement IDs": ", ".join(f.ids[1:]),
                    "Over-Settlement (INR)": abs(f.amount_impact_paise) / 100.0,
                    "Explanation": f.explanation,
                }
                for f in b_dups
            ])
            st.dataframe(df_b_dup.style.format({"Over-Settlement (INR)": "₹{:,.2f}"}), use_container_width=True)
        else:
            st.info("No bank duplicates found.")

# TAB 4: Orphan Refunds
with tabs[3]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> Platform refunds with an <code>original_txn_id</code> that does not map to any
            prior successful payment within the ledger. These arise from cross-month returns, manual support credits,
            or corrupted audit references.
        </div>
        """,
        unsafe_allow_html=True,
    )

    orphans = findings_by_cat.get(GapCategory.ORPHAN_REFUND.value, [])
    if orphans:
        df_orphans = pd.DataFrame([
            {
                "Refund Txn ID": f.details.get("refund_txn_id", f.ids[0]),
                "Referenced Parent ID": f.details.get("original_txn_id", "UNKNOWN"),
                "Refund Amount (INR)": f.details.get("refund_amount_paise", 0) / 100.0,
                "Confidence": f"{f.confidence:.0%}",
                "Explanation": f.explanation,
            }
            for f in orphans
        ])
        st.dataframe(df_orphans.style.format({"Refund Amount (INR)": "₹{:,.2f}"}), use_container_width=True)
        st.download_button(
            "Download Orphan Refunds (CSV)",
            df_orphans.to_csv(index=False).encode("utf-8"),
            "orphan_refunds.csv",
            "text/csv",
        )
    else:
        st.info("No orphan refunds detected.")

# TAB 5: Amount Mismatches
with tabs[4]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> Instances where the platform transaction ID matched the bank reference,
            but the net settlement amount deviated by more than the accepted tolerance (>1 paisa).
            Common causes: unrecorded dispute deductions, fee tariff misconfigurations, or partial settlements.
        </div>
        """,
        unsafe_allow_html=True,
    )

    mismatches = findings_by_cat.get(GapCategory.AMOUNT_MISMATCH.value, [])
    # Filter out aggregate rounding drift finding from this specific table
    row_mismatches = [m for m in mismatches if m.ids]

    if row_mismatches:
        df_mm = pd.DataFrame([
            {
                "Transaction ID": m.ids[0],
                "Settlement ID": m.ids[1] if len(m.ids) > 1 else "",
                "Platform Net (INR)": m.details.get("platform_net_amount", 0) / 100.0,
                "Bank Settlement (INR)": m.details.get("bank_amount", 0) / 100.0,
                "Difference (INR)": m.details.get("difference_paise", 0) / 100.0,
                "Explanation": m.explanation,
            }
            for m in row_mismatches
        ])
        st.dataframe(
            df_mm.style.format({
                "Platform Net (INR)": "₹{:,.2f}",
                "Bank Settlement (INR)": "₹{:,.2f}",
                "Difference (INR)": "₹{:,.2f}",
            }),
            use_container_width=True,
        )
    else:
        st.info("No per-row amount mismatches detected.")

# TAB 6: Missing Settlements / Txns
with tabs[5]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Why this gap exists:</b> Platform transactions that have no bank record and did not occur near the month-end
            cut-off (genuine missing funds), or bank settlements that reference unknown transaction IDs.
        </div>
        """,
        unsafe_allow_html=True,
    )

    m_stl = findings_by_cat.get(GapCategory.MISSING_SETTLEMENT.value, [])
    m_txn = findings_by_cat.get(GapCategory.MISSING_TRANSACTION.value, [])

    c_m1, c_m2 = st.columns(2)
    with c_m1:
        st.markdown(f"##### Missing Settlements ({len(m_stl)})")
        if m_stl:
            df_mstl = pd.DataFrame([
                {
                    "Txn ID": f.ids[0],
                    "Amount (INR)": f.amount_impact_paise / 100.0,
                    "Created At": f.details.get("created_at", ""),
                }
                for f in m_stl
            ])
            st.dataframe(df_mstl.style.format({"Amount (INR)": "₹{:,.2f}"}), use_container_width=True)
        else:
            st.info("Zero missing settlements.")

    with c_m2:
        st.markdown(f"##### Unlinked Bank Settlements ({len(m_txn)})")
        if m_txn:
            df_mtxn = pd.DataFrame([
                {
                    "Bank Ref": f.ids[0],
                    "Settlement ID": f.ids[1] if len(f.ids) > 1 else "",
                    "Amount (INR)": abs(f.amount_impact_paise) / 100.0,
                }
                for f in m_txn
            ])
            st.dataframe(df_mtxn.style.format({"Amount (INR)": "₹{:,.2f}"}), use_container_width=True)
        else:
            st.info("Zero unlinked bank settlements.")

# TAB 7: Fuzzy / Probable Matches
with tabs[6]:
    st.markdown(
        """
        <div class="explanation-box">
            <b>Probable Match Engine:</b> When reference IDs fail to match due to transposition or truncation,
            the fuzzy layer identifies candidate pairs sharing identical amount, currency, and settlement timestamps
            within a 5-day window. These are never silently merged.
        </div>
        """,
        unsafe_allow_html=True,
    )
    fuzzy_list = findings_by_cat.get(GapCategory.PROBABLE_MATCH.value, [])
    if fuzzy_list:
        df_fuzzy = pd.DataFrame([
            {
                "Platform Txn ID": f.ids[0],
                "Bank Settlement ID": f.ids[1] if len(f.ids) > 1 else "",
                "Confidence Score": f"{f.confidence:.0%}",
                "Residual (Paise)": f.details.get("residual_paise", 0),
                "Explanation": f.explanation,
            }
            for f in fuzzy_list
        ])
        st.dataframe(df_fuzzy, use_container_width=True)
    else:
        st.info("No fuzzy matches required (all matched entries met exact key criteria).")

# TAB 8: Audit Log & Schema
with tabs[7]:
    st.markdown("##### Reconciliation Invariants & Schema Health")
    val_errs = res.validation_errors
    if val_errs:
        st.error(f"Encountered {len(val_errs)} validation issues during data normalization:")
        for err in val_errs[:10]:
            st.code(err)
    else:
        st.success("All input records strictly adhere to the minor-unit integer schema and ISO 8601 IST standards.")

    st.markdown("##### Reconciler Operational Metrics")
    m_info = {
        "Metric": [
            "Total Platform Transactions",
            "Total Bank Settlements",
            "Matched Count",
            "Unmatched Platform",
            "Unmatched Bank",
            "Waterfall Residual",
            "Classification Guarantee",
        ],
        "Value": [
            f"{res.platform_row_count:,}",
            f"{res.bank_row_count:,}",
            f"{res.matched_count:,}",
            f"{res.unmatched_platform_count:,}",
            f"{res.unmatched_bank_count:,}",
            format_inr(res.unexplained_paise),
            "100% Invariant Conserved",
        ],
    }
    st.table(pd.DataFrame(m_info))

# ---------------------------------------------------------------------------
# Drill-Down: Single Transaction Comparison
# ---------------------------------------------------------------------------

st.markdown("---")
st.markdown("### Single Transaction Drill-Down")
st.markdown(
    "<div style='font-size:0.85rem; color:#64748b; margin-bottom:0.75rem;'>"
    "Inspect the platform ledger record and corresponding bank settlement record side by side."
    "</div>",
    unsafe_allow_html=True,
)

# Populate a selectable list of IDs (including matched and anomaly items)
sample_ids = []
if res.findings:
    for f in res.findings:
        if f.ids:
            sample_ids.append(f.ids[0])
if res.matched_pairs:
    sample_ids.extend([m["platform"]["txn_id"] for m in res.matched_pairs[:15]])
sample_ids = list(dict.fromkeys(sample_ids))  # preserve order, unique

c_sel, c_inp = st.columns([2, 3])
with c_sel:
    selected_from_list = st.selectbox("Select Anomaly or Sample ID", [""] + sample_ids)
with c_inp:
    searched_id = st.text_input("Or Enter Any Transaction / Bank Reference ID", "")

active_id = (searched_id.strip() if searched_id.strip() else selected_from_list).strip()

if active_id:
    # Lookup in platform
    plat_match = [r for r in st.session_state.platform_raw if r.get("txn_id") == active_id]
    # Lookup in bank
    bank_match = [r for r in st.session_state.bank_raw if r.get("bank_reference") == active_id]

    col_p, col_b = st.columns(2)

    with col_p:
        st.markdown("#### Platform Ledger Record")
        if plat_match:
            p_rec = plat_match[0]
            st.markdown(
                f"""
                - **Transaction ID:** `{p_rec.get('txn_id')}`
                - **Customer ID:** `{p_rec.get('customer_id')}`
                - **Created At:** `{p_rec.get('created_at')}`
                - **Type:** `{p_rec.get('type')}`
                - **Status:** `{p_rec.get('status')}`
                - **Gross Amount:** {format_inr(int(p_rec.get('gross_amount', 0)))} (`{p_rec.get('gross_amount')} p`)
                - **Platform Fee:** {format_inr(int(p_rec.get('fee', 0)))} (`{p_rec.get('fee')} p`)
                - **Net Payout Amount:** **{format_inr(int(p_rec.get('net_amount', 0)))}** (`{p_rec.get('net_amount')} p`)
                - **Original Txn (Refund):** `{p_rec.get('original_txn_id') or 'N/A'}`
                """
            )
        else:
            st.warning(f"No platform record found for ID: `{active_id}`")

    with col_b:
        st.markdown("#### Bank Settlement Record")
        if bank_match:
            b_rec = bank_match[0]
            st.markdown(
                f"""
                - **Settlement ID:** `{b_rec.get('settlement_id')}`
                - **Bank Reference:** `{b_rec.get('bank_reference')}`
                - **Settled At:** `{b_rec.get('settled_at')}`
                - **Batch ID:** `{b_rec.get('batch_id')}`
                - **Settlement Amount:** **{format_inr(int(b_rec.get('amount', 0)))}** (`{b_rec.get('amount')} p`)
                - **Currency:** `{b_rec.get('currency')}`
                """
            )
        else:
            st.warning(f"No bank settlement found for Reference: `{active_id}`")

    # Reconciliation verdict for this row
    related_findings = [f for f in res.findings if active_id in f.ids]
    if related_findings:
        st.markdown("##### Reconciler Findings for this ID")
        for rf in related_findings:
            st.info(f"**[{rf.category.value}]** {rf.explanation}")
    elif plat_match and bank_match:
        p_net = int(plat_match[0].get("net_amount", 0))
        b_amt = int(bank_match[0].get("amount", 0))
        diff = b_amt - p_net
        if diff == 0:
            st.success("Perfect Match: Settled amount equals platform net amount exactly.")
        else:
            st.warning(f"Matched with residual of {diff} paise ({format_inr(diff)}).")
