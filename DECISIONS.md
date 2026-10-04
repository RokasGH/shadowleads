# DECISIONS

Key design decisions, rejected alternatives and known limitations.
Figures refer to the committed run (`examples/2026-09`).

## 1. Unit of analysis: the legal entity, not the Google place
Declared activity (Sodra, VMI, financial statements) exists only per **company code**, so every
Google place is linked to one entity and visible activity is summed over the entity's Vilnius places.
*Rejected:* allocating entity-level taxes to places (no basis for the split). *Consequence:* a
company with branches outside Vilnius (`multi_site`) declares taxes and staff for **all** its
locations, but only its **Vilnius** places are counted as visible activity, because only Vilnius was
swept. Its declared figures therefore look large relative to what we see, so it is *less* likely to
be flagged. That is the safe direction for honest businesses (no false accusation), at the cost of
possibly missing evasion at such companies.
A venue run by two companies at once would look worse; a rule for it was tried, but in the data
it only caught different businesses in the same building, so it was dropped.

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

## 3. Proving the links: validation levels and two audits
Links are checked in levels; each is used when the previous ones cannot decide. The full list, with
examples, is in [labels/README.md](labels/README.md).
- **Level 0:** name and address rules.
- **Level 1:** independent evidence. The company code on the business's own website (privacy-policy
  and terms pages win), VMVT premises, directory URLs, and search snippets that name the business.
- **Level 2:** brand-level evidence (trademark owner, job-ad employer). Never sufficient alone.
- **Level 3:** plausibility. Active company, registered in Vilnius city, fitting activity code; no
  franchisor website; the listing is not an erroneous entry.
- **Level 4:** analyst verification, from the audit notes. Several checks also run automatically
  at levels 1 and 3, and the analyst covers what automation could not decide:
  - **Partly automated:** website footer, and privacy policy or terms of service.
  - **Analyst only:** phone number via a company directory, and the premises licence (hygiene
    passport) in the LIS register.
  - **Analyst completes automated rules:** only business of its kind at the address, Google listing
    sanity, scale plausibility (a brewery is not a bar operator), and franchise check.

  Company age vs venue age was rejected as unreliable.
- **Level 5:** analyst overrides (`labels/link_overrides.csv`), applied on every run.

**Audit 1** covered 48 links and found 75% correct. It drove these fixes:
- Address matches only for companies registered in Vilnius city (Kaunas matches had slipped through).
- Corroboration required for unique exact names.
- A website shared by 3 or more locations is treated as a franchisor or brand site.

**Audit 2** covered 54 new links, measured after those fixes: 84% correct (43 of 51 decided), and
85% among links trusted for Priority A.

| Method | Precision in audit 2 |
|---|---|
| Exact name, website code, VMVT premises | 100% |
| Search snippet | 83% |
| Core name, brand-level evidence | 75% |
| Address only | 67% |

Two level 4 methods are now automated:
- privacy-policy and terms pages outrank other codes on a website;
- listings named only by an address are dropped.

Only links that are confirmed, or strong and plausible, are `usable` for Priority A.

## 4. Metric: "declares far less than peers that look equally busy"
*Visible activity* = the lifetime Google review count across the company's places divided by its
years active (no official API gives visitor counts or reviews per period; Popular
Times is relative to each venue's own peak and was dropped). *Busy* = at or above the category's
75th percentile of **all** Google places in Vilnius (90th percentile when the rating is ≤3.5 or
≥4.8: extreme ratings attract disproportionate reviews, so they need more evidence instead of
having their counts adjusted).
**Limitation: reviews cannot be matched to the tax year.** Declared figures are per calendar or
fiscal year (VMI 2025, revenue FY2024/25), but the Places API returns only the lifetime review count
plus 5 "most relevant" reviews, with no per-period counts. Reviews per year is therefore a lifetime
average: total reviews divided by years active (time since the company's registration date in the
JAR register, at least 1 and at most 10 years; the cap reflects that Google reviews only became
common in the mid-2010s). It understates recent growth and overstates a venue whose popularity has faded. Exact
reviews per tax year would need review dates, which can only be scraped from the review list
(SerpApi or Oxylabs, about 1 request per 10–20 reviews). That is affordable for the ~35 Priority A
and watchlist companies but not for every place; monthly snapshots of the review count give exact
per-month deltas from the second snapshot onwards.
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
busy entity, peers paying at least 3x more tax, the entity at least 12 months old, a VMI record
(missing ≠ zero; a company whose 2025 row is not yet published is scored on its 2024 taxes, labelled
`taxes_year`), and at least one corroborating signal. Two are needed if the rating is extreme (a
review-weighted average of 3.5 or lower, or 4.8 or higher), because polarised customers review more
often, which inflates the review count:
- **near-zero declared figures:** taxes under €500 or revenue under €5,000 for the year;
- **staffing floor:** the busiest place's weekly opening hours ÷ 40 is greater than the average
  insured people over the last 12 months **+ 1** (one person on site whenever it is open, plus one
  uninsured owner or manager allowed). Not applied to MB/IĮ or self-service car washes;
- **VAT gap:** not VAT-registered although the peers' median revenue is above the €45,000
  threshold.

Everything else is watchlist or "not flagged", each with a stated `hold_reason`, and every lead
prints legitimate explanations for its category (chair rental, family labour, group staffing).

## 6. Data & system
Python 3.13, uv, DuckDB (single-file warehouse; layers `stg` -> `core` -> `mart`, plain SQL
transforms), httpx/tenacity, pydantic-settings, structlog, cyclopts, Streamlit; ruff + pyrefly +
pytest/Hypothesis in CI.
- **Lineage:** every raw artefact is stored immutably in `meta.source_fetch` (URL, time, size, rows
  and a sha256 fingerprint, kept in the warehouse). Leads carry the ids of the files they were
  computed from, and the app links each company to its records at Registrų centras, VMI, Sodra
  and data.gov.lt. The fingerprint proves which exact version of a file a lead came from, because
  the official files are overwritten in place.
- **Monthly snapshots:** each stage is idempotent per run month and every monthly table keeps
  earlier months. `mart.lead_history` compares each snapshot with the previous one: new or dropped
  leads, tier changes, and why a company changed (re-linked, declared figures changed, or Google
  activity changed). `relinked_since_last_run` flags link drift.
- **App state:** saved SQL queries live in a small app-state database (SQLite on its own Docker
  volume), separate from both the read-only warehouse and the code. This is the same pattern as
  Athena named queries or Hue's saved queries.
- **Data quality:** 18 DQ assertions; an `error` blocks the export.
- **Cost:**
  - Google uses Nearby Search with Enterprise fields and a density-aware quadtree (distance
    ranking, k×k splits); ~1,100 calls cover the city. Every response is cached and every call
    goes through a budget ledger, so the run stays within the free tier.
  - Oxylabs is only used for unresolved places and validation samples.

## 7. Known limitations
- **Google EEA terms** restrict caching Places content and using it for analytics or on non-Google
  maps. This prototype keeps raw payloads locally and commits only the structured snapshot. A real
  deployment would need a licence or a legal basis.
- **Coverage:** natural persons (individual activity, business certificates) cannot be linked, which
  is a large blind spot in the beauty category; hair/beauty linking recall is low because most
  salons have only Facebook or Treatwell pages.
- **Hairdressers and beauty salons:** many salon companies only control the premises; the
  specialists are self-employed (individual activity or business certificates), rent a chair and pay
  their taxes personally, so they do not appear in the company's taxes or Sodra headcount. A salon
  can look like a lead while being compliant. VMI internally sees individual-activity declarations
  and business certificates by activity, which is the data that would close this gap.
- **Hard-to-match operators:** companies registered at a different address, running several venues
  under unrelated brand names, or franchise locations run by a separate company. The registers have
  no premises address for beauty or auto. JAR's management-body data (`JAR_VALDYMAS.csv`) could
  link group companies through shared directors (not used yet); the beneficial-owners register is
  not freely open.
- **One website, several companies:** an e-shop company and a service company can share a site
  (Synth), so a code on the site is not always the operator's.
- **Schema drift is absorbed silently:** if a new Sodra file drops a column, the loader fills it
  with empty values instead of failing. The DQ volume checks would not notice a missing
  contributions column.
- **Streetlight bias:** only linkable businesses are scored.
- **Reviews ≠ visitors:** review propensity varies with clientele (tourists), bought reviews exist,
  and reviews may predate the current operator (PERONAS: operator in bankruptcy since 2026-09, bar
  still busy).
- **Lagging and partial declared data:** revenue comes from filed statements, and the FY2025 open
  export covers ~3% of companies. VMI does not document which taxpayers it publishes. Sodra hides
  wages and contributions at ≤3 insured.
- **Review timeframe:** visible activity is a lifetime average, not reviews in the tax year (see §4).
- **Revenue:** FY2025 statements are filed (they appear on each company's Registrų centras documents
  page), but the open-data export still holds FY2024 for most companies.
