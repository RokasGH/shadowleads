# DECISIONS

Key design decisions, rejected alternatives and known limitations.
Figures refer to the committed run (`examples/2026-09`).

## 1. Unit of analysis: the legal entity
Declared activity (Sodra, VMI, financial statements) exists only per **company code**, so every
Google place is linked to one entity and visible activity is summed over the entity's Vilnius places.

## 2. Linking: precision first, every candidate and piece of evidence stored
1. **Exact full name**: JAR legal name or VMI *branch trade name* (e.g. Google "Bromas Baras" ->
   VMI branch "Bromas"), and only with corroboration: the activity code fits the category or the
   registered address is the same building (flat numbers ignored). Same-name companies with
   unrelated activities are left for the fallbacks.
2. **Core name** (generic words removed): only with address agreement, or a rare name plus fitting
   activity. A name is rare when it has two or more words, or its one word appears in at most 8
   company names.
3. **Address only**: one consistent entity at a Vilnius address that is not multi-tenant.
4. **Fallbacks** for the rest, all independent of names: company/VAT code on the business's own
   website, the **VMVT** food-premises register, company codes in Google snippets that also name
   the business (via **Oxylabs**), **trademark owners** (LINTA) and **job-ad employers** (Oxylabs).
   Trademark and job-ad evidence name the company behind a *brand*, which for franchises and groups
   is not the venue operator, so they never create a link on their own.

## 3. Proving the links: automatic checks first, then an audit
Linking picks a company; validation never creates a link, it decides whether a link is trusted
(`usable`). Details and examples are in [labels/README.md](labels/README.md).
- **Automatic, on every link (before any audit):**
  - *Plausibility:* the company is active, its activity codes fit the category, its registered
    address is in Vilnius when the link used an address, it is not spread over more than 3 places
    in different categories and websites, and its website is not shared by 3+ addresses.
  - *Cross-check:* evidence that did not create the link (website code, VMVT, snippets) either
    confirms it or names another company. A conflict is never usable.
- **Analyst audit:** a stratified sample, for what automation cannot decide: reading the footer or
  privacy policy by hand, matching the phone number in a company directory, checking the premises
  licence, judging scale and franchise cases.
- **Overrides:** audit verdicts go to `labels/link_overrides.csv` and apply on every run.

**Audit 1** covered 48 links and found 75% correct.

**Audit 2** after slight rule refinement covered 54 new links: 84% correct (43 of 51 decided) and
85% among usable links.

| Method | Precision in audit 2 |
|---|---|
| Exact name, website code, VMVT premises | 100% |
| Search snippet | 83% |
| Core name, brand-level evidence | 75% |
| Address only | 67% |

## 4. Metric: busy on Google, low declared figures compared with similar businesses
**Visible activity** = lifetime Google reviews over the company's places ÷ years active (since JAR
registration, 1–7 years: most reviews are recent, so a longer window understates older companies).

**Busy** = reviews per active year in the top 40% of linked companies in the category, with at
least 30 reviews. Ratings ≤3.5 or ≥4.8 need the top 20% and 75 reviews: extreme ratings attract
disproportionate reviews.

**Peers** = same category × visible-activity quintile × legal form (companies vs MB/IĮ). All
quintiles are computed; in practice busy companies are in quintiles 4–5, plus a few on the cutoff
in quintile 3. The peer **median** of each declared figure is taken only from *clean* peers: no
branches outside Vilnius, a VMI record, an activity code that fits the category, and at most 3
places. A group with fewer than 8 clean peers falls back to category × quintile, then category.

**Score** = a weighted average of log gaps, `ln((peer median + 1) / (declared + 1))`, per figure:

| Figure | Weight |
|---|---|
| VMI taxes paid, last complete year (main) | 0.5 |
| Sodra contributions; headcount when Sodra hides them (≤3 insured) | 0.3 |
| Revenue from the latest filed statement, labelled with its fiscal year | 0.2 |

## 5. Mitigating false positives
A **lead** (capped at 20/month) requires all of: a usable link, a busy entity, peers paying at
least 3× more tax, the entity at least 12 months old, a VMI record (a company whose 2025 row is not
yet published is scored on its 2024 taxes), and at least one corroborating signal (two if the
rating is ≤3.5 or ≥4.8):
- **near-zero declared figures:** taxes under €500 or revenue under €5,000 for the year;
- **staffing floor:** the busiest place's weekly opening hours ÷ 40 exceed the average insured
  people over the last 12 months **+ 1** (one uninsured owner allowed). Not applied to MB/IĮ or
  self-service car washes;
- **VAT gap:** not VAT-registered although the peers' median revenue is above the €45,000
  threshold.

Everything else is watchlist or "not flagged", each with a stated `hold_reason`, and every lead
prints legitimate explanations for its category.

## 6. Rejected alternatives
- **Popular Times / a busyness index:** no API, and it cannot give true visitor counts.
- **Lifetime review total as the busy test:** it let old, quiet companies pass and blocked young,
  busy ones.
- **Ratios as the main score** (reviews per €1k of taxes or revenue): they explode near zero, so
  they are kept only as context in the lead table.
- **Postgres or a daily cadence:** the workload is a monthly batch, so a single-file DuckDB
  warehouse and monthly runs fit.

## 7. Data & system
Python 3.13, uv, DuckDB (single-file warehouse; layers `stg` -> `core` -> `mart`, plain SQL
transforms), httpx/tenacity, pydantic-settings, structlog, cyclopts, Streamlit; ruff + pyrefly +
pytest/Hypothesis in CI.
- **Lineage:** every raw artefact is stored immutably in `meta.source_fetch` (URL, time, size, rows
  and a sha256 fingerprint). Leads carry the ids of the files they were computed from.
- **Monthly snapshots:** each stage is idempotent per run month and every monthly table keeps
  earlier months. `mart.lead_history` compares each snapshot with the previous one: new or dropped
  leads, tier changes, and why a company changed.
- **App state:** saved SQL queries live in a small SQLite database on its own Docker volume.
- **Data quality:** 18 DQ assertions; an `error` blocks the export.
- **Cost:** Google Nearby Search with a density-aware quadtree covers the city in ~1,100 calls;
  every response is cached and every call goes through a budget ledger. Oxylabs is only used for
  unresolved places and validation samples.

## 8. Known limitations
- **Individuals and hair/beauty:** natural persons (individual activity, business certificates)
  cannot be linked. Many salon companies only rent out chairs to self-employed specialists, whose
  taxes and insurance do not appear under the company. Most salons have only Facebook or Treatwell
  pages, so linking recall is low.
- **Hard-to-match operators:** companies registered at another address, running venues under
  unrelated brands, or franchise locations run by a separate company.
- **One location, several companies:** one salon can run its e-shop through one company and its
  hairdressing through another, on a shared website, so a code on the site may not be the
  operator's.
- **VMI coverage is unexplained:** the open tax data has rows for ~144k of ~235k registered
  companies (FY2025: ~129k; a company without a 2025 row is scored on its 2024 taxes). VMI does not
  say why the rest are absent (no tax paid, or withheld), so a company with no row in any year is
  never a lead.
- **Revenue lags:** FY2025 statements were due by mid-2026, but the open export was last refreshed
  in March 2026 and covers ~3% of companies.
- **Reviews ≠ visitors, and not per tax year:** the API returns only the lifetime review count, so
  visible activity cannot be matched to the tax year and lags growth or decline. Review habits
  vary with clientele (tourists), bought reviews exist, and reviews may predate the current
  operator. Monthly snapshots give exact review deltas from the second run on.
- **Streetlight effect:** only linkable businesses are scored.
- **Schema drift is absorbed silently:** a Sodra file missing a column loads as empty values, and
  the DQ volume checks would not notice.
