"""Analyst app: prioritised leads, the evidence behind each one, coverage, data quality and SQL.

Reads the DuckDB warehouse read-only; nothing here re-runs the pipeline.
Run: `streamlit run app/streamlit_app.py` (or `docker compose up`).
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd  # streamlit dependency; used only for display
import pydeck as pdk
import streamlit as st

DB_PATH = Path(os.environ.get("SHADOWLEADS_DATA_DIR", "data")) / "warehouse.duckdb"
# Application state (saved queries) lives in its own small database, separate from the read-only
# warehouse and from the code - like Athena named queries or Hue's saved queries.
STATE_DB = Path(os.environ.get("SHADOWLEADS_STATE_DIR", "state")) / "app_state.sqlite"

st.set_page_config(page_title="Shadow-economy leads · Vilnius", page_icon="🔎", layout="wide")


# ----------------------------------------------------------------------------- data access
@st.cache_resource
def connection() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DB_PATH), read_only=True)


def q(sql: str, params: list[object] | None = None) -> pd.DataFrame:
    return connection().execute(sql, params or []).df()


def scalar(sql: str, params: list[object] | None = None) -> Any:
    row = connection().execute(sql, params or []).fetchone()
    return row[0] if row else None


def table_exists(name: str) -> bool:
    schema, table = name.split(".")
    return bool(
        scalar(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema, table],
        )
    )


# ----------------------------------------------------------------------------- formatting
# numeric columns that are identifiers / years, never thousand-separated
_ID_LIKE = ("kodas", "code", "year", "fy", "month", "rank", "lat", "lng", "branch_no", "level", "#")


def _is_id(col: str) -> bool:
    c = col.lower()
    return any(c == k or c.endswith("_" + k) for k in _ID_LIKE)


def styled(df: pd.DataFrame) -> Any:
    """Thousand separators for amounts and counts; identifiers and years left as they are."""
    fmt: dict[str, Any] = {}
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_bool_dtype(df[col]):
            continue
        if _is_id(str(col)):
            fmt[col] = lambda v: "" if pd.isna(v) else f"{int(v)}"
            continue
        values = df[col].dropna()
        if values.empty or bool((values == values.round()).all()):
            fmt[col] = "{:,.0f}"
        elif bool((values == values.round(1)).all()):
            fmt[col] = "{:,.1f}"
        else:
            fmt[col] = "{:,.2f}"
    return df.style.format(fmt, na_rep="—")


def show(df: pd.DataFrame, **kwargs: Any) -> None:
    st.dataframe(styled(df), hide_index=True, width="stretch", **kwargs)


def eur(v: Any) -> str:
    return "—" if v is None or pd.isna(v) else f"€{float(v):,.0f}"


def human_size(n: Any) -> str:
    if n is None or pd.isna(n):
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


# ----------------------------------------------------------------------------- domain text
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
CATEGORY_LABEL = {
    "hair_beauty": "Hair & beauty",
    "nightlife": "Bars & clubs",
    "auto": "Car wash & repair",
}


def official_links(ja: int, sodra_code: Any) -> list[tuple[str, str, str]]:
    """Deep links to the public records of one company (label, url, what it shows)."""
    base = "https://get.data.gov.lt/datasets/gov"
    rev_query = (
        "select(juridinis_asmuo.ja_kodas,line_name,reiksme,laikotarpis_nuo,laikotarpis_iki,reg_date)"
        f"&juridinis_asmuo.ja_kodas={ja}&line_name=%22PARDAVIMO%20PAJAMOS%22&sort(laikotarpis_iki)"
    )
    links = [
        ("Registrų centras - filed documents",
         f"https://www.registrucentras.lt/jar/p/dok.php?kod={ja}",
         "registration documents and filed financial statements (incl. ones not yet in open data)"),
        ("JAR register row (open data)",
         f"{base}/rc/jar/iregistruoti/JuridinisAsmuo/:format/html?ja_kodas={ja}",
         "legal name, form, status, registration date"),
        ("VMI - taxes paid (open data)",
         f"{base}/vmi/ja_mokesciai/Moketojas/:format/html?mm_kodas.ja_kodas={ja}",
         "taxes paid per calendar year - the rows used for the score"),
        ("VMI - taxpayer register (open data)",
         f"{base}/vmi/mm_registras/MokesciuMoketojas/:format/html?ja_kodas={ja}",
         "branches and their trade names, activity codes, VAT registration"),
        ("Registrų centras - revenue (open data)",
         f"{base}/rc/jar/pelno_ataskaitos/PelnoAtaskaita/:format/html?{rev_query}",
         "sales revenue lines of filed profit & loss statements"),
    ]  # fmt: skip
    if sodra_code is not None and not pd.isna(sodra_code):
        links.append(
            (
                "Sodra - employer page",
                f"https://atvira.sodra.lt/imones/detaliai/index.html?code={sodra_code}",
                "insured persons, average wage and contributions by month",
            )
        )
    return links


def reasons(r: pd.Series) -> list[str]:
    """Plain-language explanation of a lead, built only from stored numbers."""
    out = []
    form = "owner-operated (MB/IĮ)" if r.form_class == "owner_operated" else "companies"
    out.append(
        f"**Visible activity:** {int(r.reviews_total):,} Google reviews across {int(r.n_places)} "
        f"Vilnius place(s), ≈{r.reviews_per_year:,.0f} per year, rating {r.rating_weighted}. "
        f"Busy threshold for the category: {int(r.busy_floor or 0):,}+ reviews."
    )
    if pd.notna(r.peer_median_taxes):
        share = (
            f"{100 * (r.taxes_paid or 0) / r.peer_median_taxes:,.0f}%"
            if r.peer_median_taxes
            else "—"
        )
        out.append(
            f"**Peers:** {int(r.peer_n)} {form} in the same category and review band "
            f"({int(r.peer_reviews_per_year_lo or 0):,}–{int(r.peer_reviews_per_year_hi or 0):,} "
            f"reviews/yr) paid a median **{eur(r.peer_median_taxes)}** in VMI taxes in "
            f"{int(r.tax_year)}; this company paid **{eur(r.taxes_paid)}** ({share} of peers)."
        )
    if r.get("taxes_assumed_zero") is True:
        last = (
            f"last published: {int(r.taxes_last_reported_year)} {eur(r.taxes_last_reported)}"
            if pd.notna(r.taxes_last_reported_year)
            else "no earlier year either"
        )
        out.append(
            f"**Tax data gap:** VMI publishes no {int(r.tax_year)} row for this company ({last}); "
            "the score assumes €0 for that year - check before inspecting."
        )
    if pd.notna(r.insured_avg):
        contrib = (
            eur(r.contributions) if pd.notna(r.contributions) else "hidden by Sodra (≤3 insured)"
        )
        out.append(
            f"**Payroll:** {r.insured_avg:.1f} insured persons on average in {int(r.tax_year)} "
            f"(peer median {r.peer_median_headcount:.1f}); contributions {contrib}."
        )
    if pd.notna(r.revenue):
        stale = (
            " - latest statement in open data, older than the tax year"
            if r.revenue_is_stale
            else ""
        )
        out.append(
            f"**Revenue FY{int(r.revenue_fy)}:** {eur(r.revenue)} "
            f"(peer median {eur(r.peer_median_revenue)}){stale}."
        )
    sig = []
    if r.sig_near_zero_declared:
        sig.append("near-zero declared taxes or revenue")
    if r.sig_staffing_floor:
        sig.append(
            f"opening hours ({r.weekly_open_hours_max:,.0f} h/week) need more staff than declared"
        )
    if r.sig_vat_gap:
        sig.append("not VAT-registered although peers' turnover is above the €45,000 threshold")
    out.append("**Corroborating signals:** " + ("; ".join(sig) if sig else "none"))
    if r.hold_reason:
        out.append(f"**Why not higher priority:** {r.hold_reason}.")
    return out


def snapshots() -> list[str]:
    return [
        m
        for (m,) in connection()
        .execute("SELECT DISTINCT run_month FROM mart.lead ORDER BY 1 DESC")
        .fetchall()
    ]


def snapshot() -> str | None:
    """Snapshot (run month) chosen in the sidebar; defaults to the latest."""
    available = snapshots()
    chosen = st.session_state.get("snapshot")
    return chosen if chosen in available else (available[0] if available else None)


# ----------------------------------------------------------------------------- leads
def page_leads() -> None:
    st.title("Lead list")
    with st.expander("How to read this list"):
        st.markdown(
            "- Each row is a **company** (code from the JAR register); its Google places in "
            "Vilnius are summed.\n"
            "- The **score** compares what the company declared (VMI taxes, Sodra payroll, revenue) "
            "with the median of **peers that look equally busy on Google** (same category, review "
            "volume band and legal-form class). Higher = declares less than such peers.\n"
            "- **A · Priority** needs a verified link to the company, a visibly busy business, peers "
            "paying ≥3× more tax, no other company at the same premises and at least one "
            "corroborating signal. Capped at 20 - the inspection capacity.\n"
            "- **B · Watchlist** scored high but misses one of those conditions (see *hold reason*).\n"
            "- Pick a lead below the table for the full evidence, Google listings and links to the "
            "official records. These are leads, not accusations."
        )
    month = snapshot()
    if not month:
        st.warning("No scored snapshot in the warehouse yet.")
        return
    c1, c2 = st.columns(2)
    tiers = c1.multiselect(
        "Tier", list(TIER_LABEL), default=["A_priority", "B_watchlist"],
        format_func=lambda t: TIER_LABEL.get(t, t),
    )  # fmt: skip
    cats = c2.multiselect(
        "Category", list(CATEGORY_LABEL), default=list(CATEGORY_LABEL),
        format_func=lambda c: CATEGORY_LABEL.get(c, c),
    )  # fmt: skip
    df = q(
        """SELECT l.priority_rank AS "#", l.tier, l.legal_name, l.ja_kodas, l.main_category,
                  p.formatted_address AS main_address, p.maps_uri AS google_maps, l.n_places,
                  l.reviews_total, l.rating_weighted, l.taxes_paid, l.peer_median_taxes,
                  l.insured_avg, l.revenue, l.revenue_fy, l.score, l.n_signals,
                  coalesce(l.hold_reason, CASE WHEN l.tier = 'A_priority'
                           THEN 'meets all Priority A conditions' END) AS hold_reason
           FROM mart.lead l
           LEFT JOIN core.place_snapshot p
             ON p.run_month = l.run_month AND p.place_id = l.place_ids[1]
           WHERE l.run_month = ? AND list_contains(?, l.tier) AND list_contains(?, l.main_category)
           ORDER BY l.priority_rank""",
        [month, tiers, cats],
    )
    show(
        df,
        column_config={
            "google_maps": st.column_config.LinkColumn("Google Maps", display_text="open map"),
            "legal_name": st.column_config.TextColumn("legal_name", width="medium"),
            "main_address": st.column_config.TextColumn("main_address", width="medium"),
            "hold_reason": st.column_config.TextColumn("hold_reason", width="large"),
        },
    )
    st.download_button(
        "Download this list (CSV)",
        df.to_csv(index=False).encode(),
        f"leads_{month}.csv",
        "text/csv",
    )
    st.caption(f"Snapshot {month}. Amounts in EUR; taxes are for the last complete calendar year.")
    if df.empty:
        return
    st.divider()
    pick: Any = st.selectbox(
        "Open lead",
        df.itertuples(),
        format_func=lambda r: f"#{r[1]} {r.legal_name} ({CATEGORY_LABEL.get(r.main_category)})",
    )
    lead_detail(month, int(pick.ja_kodas))


def lead_detail(month: str, ja: int) -> None:
    r = q("SELECT * FROM mart.lead WHERE run_month = ? AND ja_kodas = ?", [month, ja]).iloc[0]
    reg = scalar("SELECT registered_address FROM core.entity WHERE ja_kodas = ?", [ja])
    st.header(r.legal_name)
    st.write(
        f"Company code **{ja}** · {r.legal_form} · {TIER_LABEL.get(r.tier, r.tier)} · score "
        f"**{r.score:.2f}** · rank #{r.priority_rank}"
        + (f" · registered address: {reg}" if reg else "")
    )
    for line in reasons(r):
        st.markdown(f"- {line}")
    st.info(CATEGORY_CAVEATS.get(r.main_category, ""))

    tab_vis, tab_peer, tab_decl, tab_link, tab_src = st.tabs(
        [
            "Google listings",
            "Peer comparison",
            "Declared (official data)",
            "How the company was identified",
            "Official records & sources",
        ]
    )
    with tab_vis:
        places = q(
            """SELECT p.name, p.formatted_address AS address, p.maps_uri AS google_maps, p.website,
                      p.phone, p.user_rating_count AS reviews, p.rating, p.primary_type,
                      p.weekly_open_hours, p.price_from, p.price_to, p.business_status,
                      array_to_string(p.opening_hours_text, ' · ') AS opening_hours,
                      CAST(p.fetched_at AS DATE) AS seen_on
               FROM core.place_snapshot p JOIN core.link_validation v USING (run_month, place_id)
               WHERE p.run_month = ? AND v.ja_kodas = ?
               ORDER BY p.user_rating_count DESC NULLS LAST""",
            [month, ja],
        )
        show(
            places,
            column_config={
                "google_maps": st.column_config.LinkColumn("Google Maps", display_text="open map"),
                "website": st.column_config.LinkColumn("Website"),
            },
        )
        st.caption(
            "Price = Google's price per person (EUR). Weekly hours from regular opening hours."
        )
    with tab_peer:
        payroll_label = (
            "Sodra contributions"
            if pd.notna(r.contributions)
            else "Insured persons (contributions hidden)"
        )
        rows = [
            ("VMI taxes paid", r.taxes_paid, r.peer_median_taxes, r.gap_taxes, 0.5),
            (
                payroll_label,
                r.contributions if pd.notna(r.contributions) else r.insured_avg,
                r.peer_median_contributions
                if pd.notna(r.contributions)
                else r.peer_median_headcount,
                r.gap_payroll,
                0.3,
            ),
            (
                f"Revenue (FY{int(r.revenue_fy)})" if pd.notna(r.revenue_fy) else "Revenue",
                r.revenue,
                r.peer_median_revenue,
                r.gap_revenue,
                0.2,
            ),
        ]
        table = [
            (
                dim,
                own,
                med,
                gap,
                w,
                (100 * own / med) if pd.notna(own) and pd.notna(med) and med else None,
            )
            for dim, own, med, gap, w in rows
        ]
        cols = ["dimension", "this company", "peer median", "gap ln(peer/declared)", "weight"]
        peer = pd.DataFrame(table, columns=[*cols, "company as % of peers"])
        show(peer)
        peer_n = int(r.peer_n) if pd.notna(r.peer_n) else 0
        st.markdown(
            f"Peer group: **{peer_n:,}** {str(r.form_class).replace('_', '-')} businesses in "
            f"*{CATEGORY_LABEL.get(r.main_category)}* with "
            f"{int(r.peer_reviews_per_year_lo or 0):,}–{int(r.peer_reviews_per_year_hi or 0):,} "
            f"Google reviews per year (single-site, activity-consistent). Score = weighted mean of "
            f"the gaps = **{r.score:.2f}** (0 = declares like its peers; 1.1 ≈ peers declare 3× more)."
        )
        st.markdown(
            f"Secondary ratios: **{r.reviews_per_1k_taxes:,.1f}** reviews per €1,000 of taxes · "
            f"**{r.reviews_per_1k_revenue:,.1f}** reviews per €1,000 of revenue · "
            f"**{r.insured_per_100_reviews:,.2f}** insured persons per 100 yearly reviews."
        )
    with tab_decl:
        st.subheader("VMI taxes paid")
        show(
            q(
                "SELECT year, through_month, taxes_paid, updated_on FROM stg.vmi_taxes "
                "WHERE ja_kodas = ? ORDER BY year",
                [ja],
            )
        )
        st.caption("Calendar years; the current year is year-to-date through the given month.")
        sodra = q(
            "SELECT month, num_insured, avg_wage, contributions FROM stg.sodra_monthly "
            "WHERE ja_kodas = ? ORDER BY month",
            [ja],
        )
        st.subheader("Sodra - insured persons by month")
        if sodra.empty:
            st.write("Not present in Sodra employer data (no insured employees reported).")
        else:
            st.line_chart(sodra.set_index("month")[["num_insured"]])
            show(sodra)
            st.caption(
                "Sodra hides average wage and contributions when a company has 3 or fewer insured."
            )
        st.subheader("Revenue from filed financial statements")
        show(
            q(
                "SELECT fiscal_year, revenue, filed_on FROM stg.rc_revenue "
                "WHERE ja_kodas = ? ORDER BY fiscal_year",
                [ja],
            )
        )
        st.write(f"VAT registered: **{'yes' if r.vat_registered else 'no'}**")
    with tab_link:
        show(
            q(
                """SELECT p.name AS place, v.method, v.confidence, v.validation_status, v.usable,
                          v.activity_fits, v.address_agrees, v.chain_site, v.audit_verdict
                   FROM core.link_validation v JOIN core.place_snapshot p USING (run_month, place_id)
                   WHERE v.run_month = ? AND v.ja_kodas = ?""",
                [month, ja],
            )
        )
        ids = list(r.place_ids)
        evidence: list[tuple[str, str, str, list[object]]] = [
            ("stg.website_code", "Company / VAT codes published on the business's website",
             "SELECT page_url, code_type, code, context FROM stg.website_code "
             "WHERE place_id IN (SELECT unnest(?)) AND code IS NOT NULL", [ids]),
            ("stg.vmvt_premises", "Food-business premises registered with VMVT",
             "SELECT trade_name, address, main_activity, issued_on FROM stg.vmvt_premises "
             "WHERE ja_kodas = ?", [ja]),
            ("stg.brand_evidence",
             "Brand-level evidence (trademark owner / job-ad employer - may be a franchisor or group)",
             "SELECT source, brand, company_name, detail FROM stg.brand_evidence "
             "WHERE place_id IN (SELECT unnest(?))", [ids]),
            ("core.entity_branch", "Vilnius branches registered with VMI (trade names used for matching)",
             "SELECT DISTINCT branch_name, activity_code, activity_from FROM core.entity_branch "
             "WHERE ja_kodas = ? AND in_vilnius AND is_current", [ja]),
        ]  # fmt: skip
        for table, title, sql, params in evidence:
            if table_exists(table):
                ev = q(sql, list(params))
                if not ev.empty:
                    st.markdown(f"**{title}**")
                    show(ev, column_config={"page_url": st.column_config.LinkColumn("Page")})
        with st.expander("How links are validated (levels of fallback)"):
            st.markdown(
                "0. **Name / address rules**: exact or core name vs JAR legal name or VMI branch "
                "trade name, corroborated by activity code or the same building.\n"
                "1. **Independent evidence**: company/VAT code on the business's own website "
                "(privacy-policy and terms pages win), VMVT food premises, directory URL, search "
                "snippets naming the business.\n"
                "2. **Brand-level evidence** (never enough alone): trademark owner, job-ad employer.\n"
                "3. **Plausibility**: active, registered in Vilnius, activity fits, no franchisor "
                "site, not an erroneous Google listing.\n"
                "4. **Manual checks** (audit): website footer, privacy policy / terms, phone number "
                "via a company directory, only business of its kind at the address, listing "
                "sanity, scale plausibility, franchise check, premises licence (hygiene passport) "
                "in the LIS register.\n"
                "5. **Analyst override**: the verified decision is recorded and applied every run."
            )
    with tab_src:
        sodra_code = scalar(
            "SELECT any_value(sodra_code) FROM stg.sodra_monthly WHERE ja_kodas = ?", [ja]
        )
        st.markdown("**This company in the official sources**")
        for label, url, what in official_links(ja, sodra_code):
            st.markdown(f"- [{label}]({url}) - {what}")
        st.markdown("**Source files this lead was computed from**")
        src = q(
            "SELECT source, url, strftime(fetched_at, '%Y-%m-%d %H:%M') AS fetched_at, row_count AS rows, "
            "bytes FROM meta.source_fetch WHERE fetch_id IN (?, ?, ?, ?)",
            [r.jar_fetch_id, r.vmi_fetch_id, r.sodra_fetch_id, r.revenue_fetch_id],
        )
        src["size"] = src.pop("bytes").map(human_size)
        show(src)
        st.caption(
            f"Sodra data as of {r.sodra_as_of:%Y-%m}; VMI taxes updated {r.taxes_updated_on}."
        )


# ----------------------------------------------------------------------------- coverage
def page_coverage() -> None:
    st.title("Coverage")
    st.caption(
        "Which Google places could be tied to a registered company. **Purple** = Priority A lead, "
        "**orange** = watchlist, blue = linked (not flagged), red = not linked (no candidate, "
        "ambiguous, or likely a natural person). Hover a dot for details; dot size grows with "
        "the number of reviews."
    )
    month = snapshot()
    stats = q(
        """SELECT p.category, count(*) AS places,
                  count(*) FILTER (WHERE l.status = 'linked') AS linked,
                  round(100.0 * count(*) FILTER (WHERE l.status = 'linked') / count(*), 1) AS pct_linked,
                  round(100.0 * sum(p.user_rating_count) FILTER (WHERE l.status = 'linked')
                        / sum(p.user_rating_count), 1) AS pct_reviews_linked
           FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
           WHERE p.run_month = ? AND p.in_scope GROUP BY 1 ORDER BY 1""",
        [month],
    )
    show(stats)
    c1, c2, c3 = st.columns(3)
    cats = c1.multiselect(
        "Category", list(CATEGORY_LABEL), default=list(CATEGORY_LABEL),
        format_func=lambda c: CATEGORY_LABEL.get(c, c), key="cov_cat",
    )  # fmt: skip
    tiers = c3.multiselect(
        "Tier", [*TIER_LABEL, "not_scored"], default=[*TIER_LABEL, "not_scored"],
        format_func=lambda t: TIER_LABEL.get(t, "Not scored (no linked company)"), key="cov_tier",
    )  # fmt: skip
    status = c2.multiselect(
        "Link status", ["linked", "ambiguous", "unmatched"],
        default=["linked", "ambiguous", "unmatched"],
    )  # fmt: skip
    pts = q(
        """SELECT p.name, coalesce(p.formatted_address, '') AS address, p.category,
                  coalesce(p.user_rating_count, 0) AS reviews, p.lat, p.lng, l.status,
                  coalesce(e.legal_name, '-') AS company, coalesce(ml.tier, 'not_scored') AS tier
           FROM core.place_snapshot p
           JOIN core.place_entity_link l USING (run_month, place_id)
           LEFT JOIN core.entity e ON e.ja_kodas = l.ja_kodas
           LEFT JOIN mart.lead ml ON ml.run_month = p.run_month AND ml.ja_kodas = l.ja_kodas
           WHERE p.run_month = ? AND p.in_scope
             AND list_contains(?, p.category) AND list_contains(?, l.status)
             AND list_contains(?, coalesce(ml.tier, 'not_scored'))""",
        [month, cats, status, tiers],
    )
    tier_colors = {
        "A_priority": [128, 0, 128, 220],
        "B_watchlist": [255, 140, 0, 200],
    }
    pts["color"] = [
        tier_colors.get(t, [31, 119, 180, 150] if st_ == "linked" else [214, 39, 40, 150])
        for t, st_ in zip(pts["tier"], pts["status"], strict=True)
    ]
    pts["radius"] = pts["reviews"].map(lambda n: 12 + 5 * math.sqrt(n))
    layer = pdk.Layer(
        "ScatterplotLayer", data=pts, get_position="[lng, lat]", get_fill_color="color",
        get_radius="radius", radius_min_pixels=2, radius_max_pixels=11, pickable=True,
    )  # fmt: skip
    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=pdk.ViewState(latitude=54.687, longitude=25.28, zoom=11.5),
            map_provider="carto",
            map_style="light",
            tooltip={  # type: ignore[arg-type]
                "text": "{name}\n{address}\nreviews: {reviews} · {status}\ncompany: {company}\ntier: {tier}"
            },
        ),
        height=620,
    )
    st.caption(f"{len(pts):,} places shown.")


# ----------------------------------------------------------------------------- history
def page_history() -> None:
    st.title("Month over month")
    st.caption(
        "Each monthly run is stored as a snapshot. This page compares the selected snapshot with "
        "the one before it: new and dropped leads, tier changes and *why* a company changed - "
        "because it was linked to a different company, because its declared figures changed, or "
        "because its Google activity changed."
    )
    months = snapshots()
    show(q("SELECT * FROM mart.category_summary ORDER BY run_month DESC, main_category"))
    if len(months) < 2:
        st.info(
            f"Only one snapshot ({months[0] if months else '-'}) is stored so far. Changes appear "
            "here after the next monthly run (`shadowleads run --run-month YYYY-MM`)."
        )
        return
    month = snapshot()
    st.subheader(f"Changes in {month}")
    show(
        q(
            """SELECT legal_name, ja_kodas, main_category, prev_tier, tier, prev_score, score,
                      score_change, months_flagged, first_priority_month, change_type, change_reason
               FROM mart.lead_history
               WHERE run_month = ?
                 AND (tier IN ('A_priority', 'B_watchlist') OR prev_tier IN ('A_priority', 'B_watchlist'))
               ORDER BY tier, score DESC""",
            [month],
        ),
        column_config={
            "change_reason": st.column_config.TextColumn("change_reason", width="large")
        },
    )


# ----------------------------------------------------------------------------- data quality
def page_quality() -> None:
    st.title("Data quality")
    st.caption(
        "Every run checks its own inputs. **error** blocks the lead export; **warn** is shown "
        "here so the analyst can judge the lead list. *Observed* is the measured value, "
        "*expected* the rule it is held to."
    )
    if not table_exists("meta.dq_result"):
        st.info("No data-quality results yet.")
        return
    show(
        q(
            "SELECT check_name, description, severity, passed, observed, expected "
            "FROM meta.dq_result WHERE run_month = ? ORDER BY passed, severity, check_name",
            [snapshot()],
        ),
        column_config={"description": st.column_config.TextColumn("description", width="large")},
    )
    st.subheader("Source files")
    st.caption(
        "Every downloaded artefact, when it was fetched and how many rows were staged from it."
    )
    src = q(
        "SELECT source, url, strftime(fetched_at, '%Y-%m-%d %H:%M') AS fetched_at, row_count AS rows, bytes "
        "FROM meta.source_fetch ORDER BY fetched_at DESC"
    )
    src["size"] = src.pop("bytes").map(human_size)
    show(src)


# ----------------------------------------------------------------------------- SQL console
TABLE_HELP = {
    "mart.lead": "One row per company: score, tier, hold reason, peer figures, signals. Start here.",
    "mart.entity_activity": "Per company: Google activity summed over its places, declared figures, flags.",
    "mart.category_summary": "Per category and snapshot: scored companies, flagged share, medians.",
    "mart.lead_history": "Per company and snapshot: tier and score vs the previous snapshot, and why it changed.",
    "core.place_snapshot": "Every Google place found (name, address, reviews, rating, hours, website, map link).",
    "core.place_entity_link": "Which company each Google place was linked to, how, and why not when unlinked.",
    "core.link_validation": "Trust checks on each link (activity fit, address, independent evidence, audit).",
    "core.match_candidate": "All candidate companies considered for each place, with match features.",
    "core.entity": "Company reference: legal name, form, registered address, activity codes, VAT status.",
    "core.entity_branch": "VMI branches (trade names, municipality, activity) per company.",
    "stg.vmi_taxes": "VMI taxes paid per company and calendar year.",
    "stg.sodra_monthly": "Sodra insured persons, average wage, contributions per company and month.",
    "stg.rc_revenue": "Sales revenue from filed financial statements per fiscal year.",
    "stg.vmvt_premises": "VMVT food-business premises (company code, trade name, address).",
    "stg.website_code": "Company/VAT codes found on businesses' own websites.",
    "stg.brand_evidence": "Trademark owners and job-ad employers found for brands.",
    "meta.dq_result": "Data-quality checks of the snapshot.",
    "meta.source_fetch": "Every downloaded source file (URL, time, size, rows).",
}
SEED_QUERIES = {
    "Share of flagged businesses per category": """SELECT main_category, count(*) AS scored,
       count(*) FILTER (WHERE tier IN ('A_priority','B_watchlist')) AS flagged,
       round(100.0 * flagged / scored, 1) AS pct_flagged
FROM mart.lead WHERE tier <> 'D_insufficient_evidence'
GROUP BY ALL ORDER BY pct_flagged DESC""",
    "Busiest bars paying the least tax per review": """SELECT legal_name, reviews_total, taxes_paid, reviews_per_1k_taxes, tier
FROM mart.lead WHERE main_category = 'nightlife' AND taxes_paid IS NOT NULL
ORDER BY reviews_per_1k_taxes DESC LIMIT 20""",
    "Why were companies not flagged?": """SELECT tier, hold_reason, count(*) AS companies
FROM mart.lead GROUP BY ALL ORDER BY 1, 3 DESC""",
    "Link quality by method": """SELECT method, validation_status, count(*) AS links
FROM core.link_validation GROUP BY ALL ORDER BY 3 DESC""",
    "Busiest places we could not link": """SELECT p.name, p.category, p.user_rating_count AS reviews, l.status, l.reason
FROM core.place_snapshot p JOIN core.place_entity_link l USING (run_month, place_id)
WHERE p.in_scope AND l.status <> 'linked' ORDER BY reviews DESC NULLS LAST LIMIT 25""",
}


def state() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STATE_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS saved_query (
               name TEXT PRIMARY KEY, sql TEXT NOT NULL, description TEXT,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    if not con.execute("SELECT count(*) FROM saved_query").fetchone()[0]:
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany(
            "INSERT INTO saved_query VALUES (?, ?, 'built-in example', ?, ?)",
            [(name, sql, now, now) for name, sql in SEED_QUERIES.items()],
        )
        con.commit()
    return con


def load_saved() -> dict[str, str]:
    with state() as con:
        return dict(con.execute("SELECT name, sql FROM saved_query ORDER BY name").fetchall())


def save_query(name: str, sql: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with state() as con:
        con.execute(
            """INSERT INTO saved_query (name, sql, description, created_at, updated_at)
               VALUES (?, ?, NULL, ?, ?)
               ON CONFLICT(name) DO UPDATE SET sql = excluded.sql, updated_at = excluded.updated_at""",
            (name, sql, now, now),
        )


def delete_query(name: str) -> None:
    with state() as con:
        con.execute("DELETE FROM saved_query WHERE name = ?", (name,))


def filter_sql(table: str, column: str, op: str, value: str) -> str:
    """SELECT for the table browser; the value is quoted, the column comes from the catalog."""
    where = ""
    if column != "(no filter)":
        ident = '"' + column.replace('"', '""') + '"'
        literal = "'" + value.replace("'", "''") + "'"
        if op == "contains":
            where = f" WHERE CAST({ident} AS VARCHAR) ILIKE '%' || {literal} || '%'"
        elif op == "is empty":
            where = f" WHERE {ident} IS NULL"
        else:
            numeric = value.replace(".", "", 1).lstrip("-").isdigit()
            where = f" WHERE {ident} {op} {value if numeric else literal}"
    return f"SELECT * FROM {table}{where} LIMIT 200"


def page_sql() -> None:
    st.title("Ask the data")
    with st.expander("How to use the SQL console"):
        st.markdown(
            "1. **Browse a table** on the left: pick one to see what it holds, its columns and an "
            "optional filter; **Use as query** copies the generated SQL into the editor.\n"
            "2. **Write SQL** (DuckDB dialect) and press **Run**. The warehouse is opened "
            "read-only - nothing you run can change the data.\n"
            "3. **Download** any result as CSV.\n"
            "4. **Save** a query under a name to reuse it later (saving an existing name updates "
            "it); **Load** or **Delete** saved ones. Saved queries are shared by everyone using "
            "this app instance.\n\n"
            "Tips: tables are `schema.table` (e.g. `mart.lead`); `ILIKE '%text%'` for "
            "case-insensitive search; add `LIMIT 100` for quick looks; amounts are in EUR."
        )
    saved = load_saved()
    if "sql_text" not in st.session_state:
        st.session_state.sql_text = next(iter(saved.values()), "")

    left, right = st.columns([1, 2])
    with left:
        st.subheader("Browse tables")
        tables = [
            f"{s}.{t}"
            for s, t in connection()
            .execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('mart', 'core', 'stg', 'meta') ORDER BY 1, 2"
            )
            .fetchall()
        ]
        table = st.selectbox(
            "Table", tables, index=tables.index("mart.lead") if "mart.lead" in tables else 0
        )
        st.caption(TABLE_HELP.get(table, ""))
        cols = q(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema || '.' || table_name = ? ORDER BY ordinal_position",
            [table],
        )
        st.caption(f"{scalar(f'SELECT count(*) FROM {table}'):,} rows · {len(cols)} columns")
        with st.popover("Show columns"):
            show(cols)
        column = st.selectbox("Filter column", ["(no filter)", *cols["column_name"].tolist()])
        op = st.selectbox(
            "Operator", ["contains", "=", ">", "<", ">=", "<=", "is empty"],
            disabled=column == "(no filter)",
        )  # fmt: skip
        value = st.text_input("Value", disabled=column == "(no filter)" or op == "is empty")
        generated = filter_sql(table, column, op, value)
        st.code(generated, language="sql")
        if st.button("Use as query"):
            st.session_state.sql_text = generated
            st.rerun()

    with right:
        st.subheader("Query")
        c1, c2, c3 = st.columns([3, 1, 1])
        chosen = c1.selectbox("Saved queries", ["(choose)", *saved])
        if c2.button("Load", disabled=chosen == "(choose)"):
            st.session_state.sql_text = saved[chosen]
            st.rerun()
        if c3.button("Delete", disabled=chosen == "(choose)"):
            delete_query(chosen)
            st.toast(f"Deleted '{chosen}'")
            st.rerun()
        sql = st.text_area("SQL", key="sql_text", height=200)
        run = st.button("Run", type="primary")
        s1, s2 = st.columns([3, 1])
        name = s1.text_input("Save as", placeholder="name for this query")
        if s2.button("Save", disabled=not name.strip()):
            save_query(name.strip(), sql)
            st.toast(f"Saved '{name.strip()}'")
        if run:
            try:
                res = q(sql)
            except duckdb.Error as exc:
                st.error(str(exc))
            else:
                st.caption(f"{len(res):,} rows")
                show(res)
                st.download_button(
                    "Download result (CSV)", res.to_csv(index=False).encode(),
                    "query_result.csv", "text/csv",
                )  # fmt: skip


if __name__ != "__main__":
    pass  # imported (tests): do not render
elif not DB_PATH.exists():
    st.error(f"Warehouse not found at {DB_PATH}. Run the pipeline first (`docker compose up`).")
else:
    available = snapshots()
    if available:
        st.sidebar.selectbox(
            "Snapshot", available, key="snapshot", help="Monthly run to show on every page."
        )
    st.navigation(
        [
            st.Page(page_leads, title="Leads", icon="🔎", default=True),
            st.Page(page_coverage, title="Coverage & map", icon="🗺️", url_path="coverage"),
            st.Page(page_history, title="Month over month", icon="📈", url_path="history"),
            st.Page(page_quality, title="Data quality", icon="✅", url_path="data-quality"),
            st.Page(page_sql, title="SQL console", icon="🧮", url_path="sql"),
        ]
    ).run()
