# DECISIONS

Key design decisions, rejected alternatives and known limitations.
Figures refer to the committed run (`examples/2026-09`).

## 1. Unit of analysis: the legal entity, not the Google place
Declared activity (Sodra, VMI, financial statements) exists only per **company code**, so every
Google place is linked to one entity and visible activity is summed over the entity's Vilnius places.
*Rejected:* allocating entity-level taxes to places (no basis for the split). *Consequence:* entities
operating outside Vilnius look better-declared than they are (safe direction, flagged `multi_site`);
**several companies at one premises** look worse (unsafe direction, flagged `shared_premises` and
kept out of Priority A).

## 2. Linking: precision first, every candidate and piece of evidence stored
1. **Exact full name**: JAR legal name or VMI *branch trade name* (e.g. Google "Bromas Baras" ->
   VMI branch "Bromas"), and only with corroboration: the activity code fits the category or the
   registered address is the same building (flat numbers ignored: "Šaltkalvių g. 60A-245" =
   "60A"). Same-name companies with unrelated activities (a construction firm called "Meistras ir
   Margarita") are left for the fallbacks.
2. **Core name** (generic words removed): only with address agreement, or a rare name plus fitting
   activity.
3. **Address only**: one consistent entity at a Vilnius address that is not multi-tenant.
4. **Fallbacks** for the rest, all independent of names: company/VAT code on the business's own
   website (robots.txt honoured), the **VMVT** food-premises register (code + premises address), a
   company-directory URL used as the Google "website", company codes quoted in Google snippets (via
   **Oxylabs**), **trademark owners** (State Patent Bureau, LINTA) and **job-ad employers** (Oxylabs).
   Trademark and job-ad evidence name the company behind a *brand*, which for franchises and groups
   is not the venue operator (Švaros broliai franchise; Grill London runs one company per venue), so
   they count as one "brand-level" family and never confirm a link on their own.

Fuzzy similarity never creates a link; it is recorded as evidence only.
*Rejected:* scraping rekvizitai.lt and LIS licence pages (both forbid copying), .lt WHOIS (DOMREG
shows only the registrar, not the registrant), the Spinta copies of JAR/VMVT (fields are null).

## 3. Proving the links: four validation layers
V1 plausibility (active, Vilnius city, activity fits, not spread across unrelated places), V2
agreement with independent evidence, **V3 a stratified manual audit** (48 links, 4 per category x
method), V4 analyst overrides applied on every run (`labels/link_overrides.csv`). The audit drove
concrete fixes: same street in another city (Kaunas) matched -> address matches now Vilnius-only;
unique exact names collided with unrelated companies -> corroboration required; franchise websites
show the franchisor's code -> a code from a site shared by 3+ locations is `brand_site_code`.
Only links that are confirmed, or strong and plausible, are `usable` for Priority A.

## 4. Metric: "declares far less than peers that look equally busy"
*Visible activity* = Google reviews per active year (no official API gives visitor counts; Popular
Times is relative to each venue's own peak and was dropped). *Busy* = at or above the category's
75th percentile of **all** Google places in Vilnius (90th percentile when the rating is ≤3.5 or
≥4.8: extreme ratings attract disproportionate reviews, so they need more evidence instead of
having their counts adjusted).
*Score* = weighted log-gap between the peer median and the declared figure: VMI taxes paid (main,
0.5), Sodra contributions or headcount when Sodra suppresses contributions (≤3 insured) (0.3), and
revenue from the latest filed financial statement (0.2, labelled with its fiscal year). Peers =
category x reviews-per-year quintile x legal-form class (MB/IĮ owners pay part of their taxes
personally), with medians from **single-site, activity-consistent** peers only, because national
chains inflated the medians for small firms.
*Secondary:* ratios (reviews per EUR 1k taxes or revenue, insured per 100 reviews) with near-zero
values clamped. *Rejected:* raw ratios as the main score (they explode near zero and assume revenue
scales linearly with reviews), and an ML model (no ground truth; not explainable to an inspector).

## 5. Cost of a false accusation is designed in
Priority A (capped at 20/month, the analyst's capacity) requires all of: a usable link, a visibly
busy entity, peers paying at least 3x more tax, no shared premises, the entity at least 12 months
old, a VMI record (missing ≠ zero), no sign that reviews predate the operator, and at least one
corroborating signal (≥2 if the rating is extreme):
- near-zero declared figures;
- opening hours that need more staff than declared;
- no VAT registration while peers are above the €45k threshold.

Everything else is watchlist or "not flagged", each with a stated `hold_reason`, and every lead
prints legitimate explanations for its category (chair rental, family labour, group staffing).

## 6. Data & system
Python 3.13, uv, DuckDB (single-file warehouse; layers `stg` -> `core` -> `mart`, plain SQL
transforms), httpx/tenacity, pydantic-settings, structlog, cyclopts, Streamlit; ruff + pyrefly +
pytest/Hypothesis in CI.
- **Lineage:** every raw artefact is stored immutably with its sha256 in `meta.source_fetch`; leads
  carry the fetch ids.
- **Idempotent runs:** each stage is idempotent per run month; history is kept per month
  (`mart.lead_history`).
- **Data quality:** 18 DQ assertions; an `error` blocks the export.
- **Cost:**
  - Google uses Nearby Search with Enterprise fields and a density-aware quadtree (distance
    ranking, k×k splits); ~1,100 calls cover the city. Every response is cached and every call
    goes through a budget ledger, so the run stays within the free tier.
  - Oxylabs is only used for unresolved places and validation samples.

## 7. Known limitations
- **Google EEA terms** restrict caching Places content and using it for analytics or on non-Google
  maps. This prototype keeps raw payloads locally and publishes only a pseudonymised example. A real
  deployment would need a licence or a legal basis.
- **Coverage:** natural persons (individual activity, business certificates) cannot be linked, which
  is a large blind spot in the beauty category; hair/beauty linking recall is low because most
  salons have only Facebook or Treatwell pages.
- **Streetlight bias:** only linkable businesses are scored.
- **Reviews ≠ visitors:** review propensity varies with clientele (tourists), bought reviews exist,
  and reviews may predate the current operator (PERONAS: operator in bankruptcy since 2026-09, bar
  still busy).
- **Lagging and partial declared data:** revenue comes from filed statements, and the FY2025 open
  export covers ~3% of companies. VMI does not document which taxpayers it publishes. Sodra hides
  wages and contributions at ≤3 insured.
- **Pseudonymisation** protects the public example, not the local outputs. Reviewers can only see
  full traceability on the negative control, or by running with their own key.
