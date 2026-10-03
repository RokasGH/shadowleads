"""Analyst app: prioritised leads, the evidence behind each one, coverage, history and ad-hoc SQL.

Reads the DuckDB warehouse read-only; nothing here re-runs the pipeline.
Run: `streamlit run app/streamlit_app.py` (or `docker compose up`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd  # streamlit dependency; used only for display
import streamlit as st

DB_PATH = Path(os.environ.get("SHADOWLEADS_DATA_DIR", "data")) / "warehouse.duckdb"

st.set_page_config(page_title="Shadow-economy leads · Vilnius", layout="wide")


@st.cache_resource
def connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


def q(sql: str, params: list[object] | None = None) -> pd.DataFrame:
    return connection().execute(sql, params or []).df()


def scalar(sql: str, params: list[object] | None = None) -> object:
    row = connection().execute(sql, params or []).fetchone()
    return row[0] if row else None


CATEGORY_CAVEATS = {
    "hair_beauty": "Chair rental to self-employed hairdressers (individual activity / business "
    "certificates) is legal and keeps payroll low; owner-operators of MB/IĮ pay part of their "
    "taxes personally.",
    "nightlife": "Staff may be employed by a sister company or agency; venues are sometimes run "
    "one company per location under a group brand; tourist venues collect more reviews per guest.",
    "auto": "Owner-mechanics in MB/IĮ, subcontracted body work and self-service equipment reduce "
    "declared payroll legitimately; dealerships report through national entities.",
}
TIER_LABEL = {
    "A_priority": "A · Priority",
    "B_watchlist": "B · Watchlist",
    "C_not_flagged": "C · Not flagged",
    "D_insufficient_evidence": "D · Insufficient evidence",
}


def eur(v: Any) -> str:
    return "—" if v is None or pd.isna(v) else f"€{float(v):,.0f}"


def months() -> list[str]:
    return [
        r
        for (r,) in connection()
        .execute("SELECT DISTINCT run_month FROM mart.lead ORDER BY 1 DESC")
        .fetchall()
    ]


def reasons(r: pd.Series) -> list[str]:
    """Plain-language explanation of a lead, built only from stored numbers."""
    out = []
    form = "owner-operated (MB/IĮ)" if r.form_class == "owner_operated" else "companies"
    out.append(
        f"**Visible activity:** {int(r.reviews_total):,} Google reviews across {int(r.n_places)} "
        f"Vilnius place(s), ≈{r.reviews_per_year:,.0f} per year, rating {r.rating_weighted} — above "
        f"the busy threshold for the category ({int(r.busy_floor or 0)}+ reviews)."
    )
    if pd.notna(r.peer_median_taxes):
        out.append(
            f"**Peers:** {int(r.peer_n)} {form} in the same category and review band "
            f"({int(r.peer_reviews_per_year_lo or 0)}–{int(r.peer_reviews_per_year_hi or 0)} reviews/yr) "
            f"paid a median **{eur(r.peer_median_taxes)}** in VMI taxes in {int(r.tax_year)}; "
            f"this entity paid **{eur(r.taxes_paid)}** "
            f"({'%.0f' % (100 * (r.taxes_paid or 0) / r.peer_median_taxes) if r.peer_median_taxes else '—'}% of peers)."
        )
    if pd.notna(r.insured_avg):
        out.append(
            f"**Payroll:** {r.insured_avg:.1f} insured persons on average in {int(r.tax_year)} "
            f"(peer median {r.peer_median_headcount:.1f}); contributions "
            f"{eur(r.contributions) if pd.notna(r.contributions) else 'suppressed by Sodra (≤3 insured)'}."
        )
    if pd.notna(r.revenue):
        stale = " — stale: latest filed statement" if r.revenue_is_stale else ""
        out.append(
            f"**Revenue FY{int(r.revenue_fy)}:** {eur(r.revenue)} (peer median {eur(r.peer_median_revenue)}){stale}."
        )
    sig = []
    if r.sig_near_zero_declared:
        sig.append("near-zero declared taxes or revenue")
    if r.sig_staffing_floor:
        sig.append(
            f"opening hours ({r.weekly_open_hours_max:.0f} h/week) need more staff than declared"
        )
    if r.sig_vat_gap:
        sig.append("not VAT-registered although peers' turnover is above the €45k threshold")
    out.append("**Corroborating signals:** " + ("; ".join(sig) if sig else "none"))
    if r.hold_reason:
        out.append(f"**Why not higher priority:** {r.hold_reason}.")
    return out


def page_leads() -> None:
    st.title("Lead list")
    st.caption(
        "Leads, not accusations: each entity is compared with peers that look equally busy on "
        "Google. Priority A requires a verified link, a clear tax gap and at least one corroborating "
        "signal; capacity is capped at 20 per month."
    )
    ms = months()
    if not ms:
        st.warning("No scored run in the warehouse yet.")
        return
    c1, c2, c3 = st.columns([1, 2, 2])
    month = c1.selectbox("Run month", ms)
    tiers = c2.multiselect(
        "Tier",
        list(TIER_LABEL),
        default=["A_priority", "B_watchlist"],
        format_func=lambda t: TIER_LABEL.get(t, t),
    )
    cats = c3.multiselect(
        "Category",
        ["hair_beauty", "nightlife", "auto"],
        default=["hair_beauty", "nightlife", "auto"],
    )
    df = q(
        """SELECT priority_rank AS "#", tier, legal_name, ja_kodas, main_category, n_places,
                  reviews_total, rating_weighted, taxes_paid, peer_median_taxes, insured_avg,
                  revenue, revenue_fy, score, n_signals, hold_reason
           FROM mart.lead WHERE run_month = ? AND list_contains(?, tier) AND list_contains(?, main_category)
           ORDER BY priority_rank""",
        [month, tiers, cats],
    )
    st.dataframe(df, hide_index=True, width="stretch")
    st.download_button(
        "Download CSV", df.to_csv(index=False).encode(), f"leads_{month}.csv", "text/csv"
    )
    if df.empty:
        return

    st.divider()
    pick: Any = st.selectbox(
        "Open lead",
        df.itertuples(),
        format_func=lambda r: f"#{r[1]} {r.legal_name} ({r.main_category})",
    )
    lead_detail(month, int(pick.ja_kodas))


def lead_detail(month: str, ja: int) -> None:
    r = q("SELECT * FROM mart.lead WHERE run_month = ? AND ja_kodas = ?", [month, ja]).iloc[0]
    st.header(f"{r.legal_name}")
    st.write(
        f"Company code **{ja}** · {r.legal_form} · {TIER_LABEL.get(r.tier, r.tier)} · "
        f"score **{r.score}** · rank #{r.priority_rank}"
    )
    for line in reasons(r):
        st.markdown(f"- {line}")
    st.info(CATEGORY_CAVEATS.get(r.main_category, ""))

    tab_vis, tab_decl, tab_link, tab_score, tab_lineage = st.tabs(
        ["Google listings", "Declared (official)", "Link evidence", "Score", "Lineage"]
    )
    with tab_vis:
        places = q(
            """SELECT p.name, p.formatted_address, p.user_rating_count AS reviews, p.rating,
                      p.weekly_open_hours, p.price_from, p.price_to, p.website, p.maps_uri, p.fetched_at
               FROM core.place_snapshot p JOIN core.link_validation v USING (run_month, place_id)
               WHERE p.run_month = ? AND v.ja_kodas = ?""",
            [month, ja],
        )
        st.dataframe(places, hide_index=True, width="stretch",
                     column_config={"maps_uri": st.column_config.LinkColumn("Google Maps")})  # fmt: skip
    with tab_decl:
        taxes = q(
            "SELECT year, through_month, taxes_paid, updated_on FROM stg.vmi_taxes WHERE ja_kodas = ? ORDER BY year",
            [ja],
        )
        st.subheader("VMI taxes paid (calendar years; current year is year-to-date)")
        st.dataframe(taxes, hide_index=True)
        sodra = q(
            "SELECT month, num_insured, contributions, avg_wage FROM stg.sodra_monthly "
            "WHERE ja_kodas = ? ORDER BY month",
            [ja],
        )
        st.subheader("Sodra monthly (insured persons; contributions hidden when ≤3 insured)")
        if sodra.empty:
            st.write("Not present in Sodra employer data (no insured employees reported).")
        else:
            st.line_chart(sodra.set_index("month")[["num_insured"]])
            st.dataframe(sodra, hide_index=True)
        rev = q(
            "SELECT fiscal_year, revenue, filed_on FROM stg.rc_revenue WHERE ja_kodas = ? ORDER BY fiscal_year",
            [ja],
        )
        st.subheader("Revenue from filed financial statements")
        st.dataframe(rev, hide_index=True)
        st.markdown(
            f"Official records: [Registrų centras JAR search](https://www.registrucentras.lt/jar/p/index.php?kod={ja}) · "
            f"[Sodra open data](https://atvira.sodra.lt/imones/rinkiniai/index.html) · VAT registered: **{r.vat_registered}**"
        )
    with tab_link:
        links = q(
            """SELECT p.name AS place, v.method, v.confidence, v.validation_status, v.usable,
                      v.activity_fits, v.address_agrees, v.chain_site, v.agreement_detail, v.audit_verdict
               FROM core.link_validation v JOIN core.place_snapshot p USING (run_month, place_id)
               WHERE v.run_month = ? AND v.ja_kodas = ?""",
            [month, ja],
        )
        st.dataframe(links, hide_index=True, width="stretch")
        st.caption(
            "Methods: exact/core name = VMI branch trade name or JAR legal name; fallback = independent "
            "evidence (company/VAT code on own website, VMVT food premises, company code in search "
            "snippets, trademark owner, job-ad employer). Brand-level evidence alone never feeds Priority A."
        )
    with tab_score:
        comp = pd.DataFrame(
            {"gap (ln peers/declared)": [r.gap_taxes, r.gap_payroll, r.gap_revenue]},
            index=["VMI taxes (w .5)", "payroll (w .3)", "revenue (w .2)"],
        )
        st.bar_chart(comp)
        st.write(
            f"Secondary ratios — reviews per €1k taxes: **{r.reviews_per_1k_taxes}**, reviews per €1k "
            f"revenue: **{r.reviews_per_1k_revenue}**, insured per 100 reviews: **{r.insured_per_100_reviews}**"
        )
    with tab_lineage:
        lin = q(
            "SELECT source, url, fetched_at, sha256, row_count FROM meta.source_fetch "
            "WHERE fetch_id IN (?, ?, ?, ?)",
            [r.jar_fetch_id, r.vmi_fetch_id, r.sodra_fetch_id, r.revenue_fetch_id],
        )
        st.dataframe(lin, hide_index=True, width="stretch")
        st.caption(f"Sodra data as of {r.sodra_as_of}; VMI taxes updated {r.taxes_updated_on}.")


def page_coverage() -> None:
    st.title("Coverage")
    ms = [
        m
        for (m,) in connection()
        .execute("SELECT DISTINCT run_month FROM core.place_snapshot ORDER BY 1 DESC")
        .fetchall()
    ]
    month = st.selectbox("Run month", ms)
    st.dataframe(
        q(
            """SELECT p.category, count(*) AS places,
                      count(*) FILTER (WHERE l.status = 'linked') AS linked,
                      round(100.0 * count(*) FILTER (WHERE l.status = 'linked') / count(*), 1) AS pct_linked,
                      round(100.0 * sum(p.user_rating_count) FILTER (WHERE l.status = 'linked')
                            / sum(p.user_rating_count), 1) AS pct_reviews_linked
               FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
               WHERE p.run_month = ? AND p.in_scope GROUP BY 1 ORDER BY 1""",
            [month],
        ),
        hide_index=True,
    )
    pts = q(
        """SELECT p.lat, p.lng, CASE WHEN l.status = 'linked' THEN '#1f77b4' ELSE '#d62728' END AS color
           FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
           WHERE p.run_month = ? AND p.in_scope""",
        [month],
    )
    st.map(pts, latitude="lat", longitude="lng", color="color", size=20)
    st.caption("Blue = linked to a legal entity, red = not linked. Google coordinates are shown "
               "for this internal analysis only (see DECISIONS.md on Maps terms).")  # fmt: skip


def page_history() -> None:
    st.title("Month-over-month")
    st.dataframe(
        q("SELECT * FROM mart.category_summary ORDER BY run_month DESC, main_category"),
        hide_index=True,
    )
    st.subheader("Changes in flagged entities")
    st.dataframe(
        q(
            """SELECT run_month, legal_name, main_category, prev_tier, tier, score_change,
                      months_flagged, change_type, change_reason
               FROM mart.lead_history
               WHERE tier IN ('A_priority', 'B_watchlist') OR prev_tier IN ('A_priority', 'B_watchlist')
               ORDER BY run_month DESC, score DESC"""
        ),
        hide_index=True,
        width="stretch",
    )


def page_quality() -> None:
    st.title("Data quality")
    has = scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='meta' AND table_name='dq_result'"
    )
    if not has:
        st.info("No data-quality results yet.")
        return
    st.dataframe(
        q("SELECT * FROM meta.dq_result ORDER BY checked_at DESC, passed, severity"),
        hide_index=True,
        width="stretch",
    )
    st.subheader("Sources")
    st.dataframe(
        q(
            "SELECT source, url, fetched_at, row_count, bytes, sha256 FROM meta.source_fetch ORDER BY fetched_at DESC"
        ),
        hide_index=True,
    )


EXAMPLES = {
    "Share of flagged businesses per category": """SELECT main_category, count(*) AS scored,
       count(*) FILTER (WHERE tier IN ('A_priority','B_watchlist')) AS flagged,
       round(100.0 * flagged / scored, 1) AS pct_flagged
FROM mart.lead WHERE tier <> 'D_insufficient_evidence'
GROUP BY ALL ORDER BY pct_flagged DESC""",
    "Busiest bars paying the least tax per review": """SELECT legal_name, reviews_total, taxes_paid, reviews_per_1k_taxes, tier
FROM mart.lead WHERE main_category = 'nightlife' AND taxes_paid IS NOT NULL
ORDER BY reviews_per_1k_taxes DESC LIMIT 20""",
    "Why were entities not flagged?": """SELECT tier, hold_reason, count(*) FROM mart.lead GROUP BY ALL ORDER BY 1, 3 DESC""",
    "Link quality by method": """SELECT method, validation_status, count(*) FROM core.link_validation GROUP BY ALL ORDER BY 3 DESC""",
    "Owner-operated vs companies: median taxes per review": """SELECT main_category, form_class,
       median(taxes_paid / nullif(reviews_per_year, 0)) AS eur_tax_per_review
FROM mart.lead GROUP BY ALL ORDER BY 1, 2""",
}


def page_sql() -> None:
    st.title("Ask the data (read-only SQL)")
    st.caption(
        "DuckDB SQL over the warehouse. Main tables: mart.lead, mart.entity_activity, "
        "core.place_snapshot, core.place_entity_link, core.link_validation, stg.vmi_taxes, "
        "stg.sodra_monthly, stg.rc_revenue. Views: mart.lead_history, mart.category_summary."
    )
    example = st.selectbox("Example", list(EXAMPLES))
    sql = st.text_area("SQL", EXAMPLES[example], height=180)
    if st.button("Run"):
        try:
            st.dataframe(q(sql), hide_index=True, width="stretch")
        except duckdb.Error as exc:
            st.error(str(exc))


if __name__ != "__main__":
    pass  # imported (tests): do not render
elif not DB_PATH.exists():
    st.error(f"Warehouse not found at {DB_PATH}. Run the pipeline first (`docker compose up`).")
else:
    st.navigation(
        [
            st.Page(page_leads, title="Leads", default=True),
            st.Page(page_coverage, title="Coverage & map"),
            st.Page(page_history, title="Month-over-month"),
            st.Page(page_quality, title="Data quality"),
            st.Page(page_sql, title="SQL console"),
        ]
    ).run()
