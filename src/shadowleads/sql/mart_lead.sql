-- Lead scoring for one run month. Grain: (run_month, ja_kodas).
--
-- PRIMARY SCORE - declared vs PEERS WITH SIMILAR VISIBLE ACTIVITY
--   visible activity  = Google reviews per active year, summed over the entity's Vilnius places
--   peer group        = category x reviews-per-year quintile x legal-form class (company | MB/IĮ)
--   clean peers only  = single-site in Vilnius, has a VMI record, activity code fits the category
--                       (national chains would otherwise inflate the medians for small firms)
--   gap_<dim>         = ln((peer median + 1) / (declared + 1))   (> 0: declares less than peers)
--   dimensions        = VMI taxes paid (main, weight .5), Sodra contributions or headcount when
--                       contributions are suppressed (.3), sales revenue of the latest FY (.2)
-- SECONDARY - ratios (reviews per EUR 1k taxes / revenue, insured per 100 reviews); near-zero
--   declared values are clamped to a floor so ratios cannot explode.
-- CORROBORATION - staffing floor (opening hours vs insured), VAT gap, near-zero declared.
-- TIERS - A needs: usable link, visibly busy, clear tax gap, >= 1 corroborating
--   signal (>= 2 when the rating is extreme: very low/high ratings need more evidence).

CREATE TABLE IF NOT EXISTS mart.lead AS SELECT NULL::VARCHAR AS run_month WHERE false;

CREATE OR REPLACE TEMP TABLE _cfg AS SELECT
    500.0   AS taxes_floor,          -- EUR; below this a declared tax figure is "near zero"
    5000.0  AS revenue_floor,        -- EUR
    45000.0 AS vat_threshold,        -- EUR, LT VAT registration threshold
    30      AS min_reviews,          -- absolute evidence floor (reviews on Google)
    2.5     AS extreme_multiplier,   -- rating <= 3.5 or >= 4.8 -> floor x 2.5
    3.0     AS min_gap_ratio,        -- peers pay >= 3x more tax
    40.0    AS fte_hours,            -- weekly hours of one full-time employee
    8       AS min_peers,
    20      AS analyst_capacity;     -- Priority-A leads per month

-- busy thresholds come from the whole Google universe of the category, not from linked entities
CREATE OR REPLACE TEMP TABLE _busy AS
SELECT category AS main_category,
       -- "visibly busy" = at/above p75 of ALL Google places in the category; p90 when the
       -- rating is extreme (very low/high ratings need more evidence before flagging)
       quantile_cont(user_rating_count, 0.75) AS busy_floor,
       quantile_cont(user_rating_count, 0.90) AS busy_floor_extreme
FROM core.place_snapshot
WHERE run_month = '{run_month}' AND in_scope AND user_rating_count IS NOT NULL
GROUP BY category;

CREATE OR REPLACE TEMP TABLE _base AS
SELECT
    ea.*,
    c.*,
    -- visible activity: reviews per active year; years active = since JAR registration, 1..10 years
    -- (the 2015 floor below no longer binds: 10 years before the 2026 snapshots is 2016)
    greatest(1.0, least(10.0, date_diff('day',
        greatest(coalesce(ea.registered_on, DATE '2015-01-01'), DATE '2015-01-01'),
        CAST(ea.run_month || '-01' AS DATE)) / 365.25))                          AS years_active,
    (ea.rating_weighted <= 3.5 OR ea.rating_weighted >= 4.8)                    AS rating_extreme,
    ea.reviews_total >= c.min_reviews * CASE WHEN ea.rating_weighted <= 3.5
        OR ea.rating_weighted >= 4.8 THEN c.extreme_multiplier ELSE 1 END       AS enough_reviews,
    CASE WHEN ea.rating_weighted <= 3.5 OR ea.rating_weighted >= 4.8
         THEN bz.busy_floor_extreme ELSE bz.busy_floor END                      AS busy_floor,
    ea.reviews_total >= CASE WHEN ea.rating_weighted <= 3.5 OR ea.rating_weighted >= 4.8
         THEN bz.busy_floor_extreme ELSE bz.busy_floor END                      AS visibly_busy,
    CASE WHEN ea.owner_operated THEN 'owner_operated' ELSE 'company' END        AS form_class,
    -- declared dimensions (NULL = unknown, never silently 0)
    -- VMI taxes of the last complete year; companies whose 2025 row is not yet published are
    -- scored on their latest published year (2024), labelled in taxes_year / taxes_from_earlier_year
    CASE WHEN ea.has_vmi_record
         THEN coalesce(ea.taxes_prev_year, ea.taxes_last_reported) END        AS d_taxes,
    CASE WHEN ea.taxes_has_tax_year_row THEN ea.tax_year
         ELSE ea.taxes_last_reported_year END                                    AS taxes_year,
    CASE WHEN ea.has_sodra_record AND ea.contribution_months_suppressed = 0
              THEN coalesce(ea.contributions_prev_year, 0)
         WHEN NOT ea.has_sodra_record AND ea.has_vmi_record THEN 0 END          AS d_contributions,
    ea.insured_avg_prev_year                                                     AS d_headcount,
    ea.revenue_latest                                                            AS d_revenue,
    ea.weekly_open_hours_max / c.fte_hours                                       AS fte_needed_to_staff_hours,
    (NOT ea.multi_site AND ea.has_vmi_record AND coalesce(ea.activity_fits, false)
        AND ea.n_places <= 3)                                                    AS peer_eligible
FROM mart.entity_activity ea
CROSS JOIN _cfg c
LEFT JOIN _busy bz ON bz.main_category = ea.main_category
WHERE ea.run_month = '{run_month}' AND NOT coalesce(ea.any_self_service, false);

CREATE OR REPLACE TEMP TABLE _peers AS
SELECT *,
       reviews_total / years_active AS reviews_per_year,
       ntile(5) OVER (PARTITION BY main_category ORDER BY reviews_total / years_active) AS activity_quintile
FROM _base;

-- peer medians at three fallback levels (most specific level with >= min_peers wins)
CREATE OR REPLACE TEMP TABLE _medians AS
WITH e AS (SELECT * FROM _peers WHERE peer_eligible)
SELECT 3 AS lvl, main_category, activity_quintile, form_class, count(*) AS n,
       median(d_taxes) AS m_taxes, median(d_contributions) AS m_contrib,
       median(d_headcount) AS m_headcount, median(d_revenue) AS m_revenue,
       min(reviews_per_year) AS rpy_lo, max(reviews_per_year) AS rpy_hi
FROM e GROUP BY ALL
UNION ALL
SELECT 2, main_category, activity_quintile, NULL, count(*), median(d_taxes), median(d_contributions),
       median(d_headcount), median(d_revenue), min(reviews_per_year), max(reviews_per_year)
FROM e GROUP BY main_category, activity_quintile
UNION ALL
SELECT 1, main_category, NULL, NULL, count(*), median(d_taxes), median(d_contributions),
       median(d_headcount), median(d_revenue), min(reviews_per_year), max(reviews_per_year)
FROM e GROUP BY main_category;

CREATE OR REPLACE TEMP TABLE _scored AS
WITH picked AS (
    SELECT p.*, m.lvl AS peer_level, m.n AS peer_n, m.m_taxes, m.m_contrib, m.m_headcount,
           m.m_revenue, m.rpy_lo, m.rpy_hi,
           row_number() OVER (PARTITION BY p.ja_kodas ORDER BY m.lvl DESC) AS rn
    FROM _peers p
    JOIN _medians m
      ON m.main_category = p.main_category
     AND (m.activity_quintile IS NULL OR m.activity_quintile = p.activity_quintile)
     AND (m.form_class IS NULL OR m.form_class = p.form_class)
    WHERE m.n >= p.min_peers
),
g AS (
    SELECT *,
        ln((m_taxes + 1) / (d_taxes + 1))                       AS gap_taxes,
        CASE WHEN d_contributions IS NOT NULL
             THEN ln((m_contrib + 1) / (d_contributions + 1))
             ELSE ln((m_headcount + 1) / (d_headcount + 1)) END  AS gap_payroll,
        ln((m_revenue + 1) / (d_revenue + 1))                    AS gap_revenue
    FROM picked WHERE rn = 1
)
SELECT *,
    (coalesce(0.5 * gap_taxes, 0) + coalesce(0.3 * gap_payroll, 0) + coalesce(0.2 * gap_revenue, 0))
      / nullif((CASE WHEN gap_taxes IS NOT NULL THEN 0.5 ELSE 0 END)
             + (CASE WHEN gap_payroll IS NOT NULL THEN 0.3 ELSE 0 END)
             + (CASE WHEN gap_revenue IS NOT NULL THEN 0.2 ELSE 0 END), 0)   AS score,
    reviews_per_year / (greatest(coalesce(d_taxes, taxes_floor), taxes_floor) / 1000)       AS reviews_per_1k_taxes,
    reviews_per_year / (greatest(coalesce(d_revenue, revenue_floor), revenue_floor) / 1000) AS reviews_per_1k_revenue,
    100.0 * d_headcount / nullif(reviews_per_year, 0)                                      AS insured_per_100_reviews,
    -- corroborating signals
    coalesce((d_taxes < taxes_floor) OR (d_revenue < revenue_floor), false)                AS sig_near_zero_declared,
    coalesce(NOT owner_operated AND fte_needed_to_staff_hours > coalesce(insured_avg_t12m, 0) + 1,
             false)                                                                        AS sig_staffing_floor,
    coalesce(NOT coalesce(vat_registered, false) AND m_revenue > vat_threshold, false)     AS sig_vat_gap
FROM g;

CREATE OR REPLACE TEMP TABLE _lead AS
SELECT
    '{run_month}' AS run_month,
    s.ja_kodas, s.legal_name, s.legal_form, s.main_category, s.categories, s.n_places,
    s.place_ids, s.place_names,
    -- visible
    s.reviews_total, round(s.reviews_per_year, 1) AS reviews_per_year, s.rating_weighted,
    s.weekly_open_hours_max, round(s.price_per_person_mid, 1) AS price_per_person_mid,
    s.activity_quintile,
    -- declared
    s.tax_year, s.d_taxes AS taxes_paid, s.taxes_ytd, s.taxes_ytd_through_month,
    s.taxes_year, s.taxes_year < s.tax_year AS taxes_from_earlier_year,
    s.d_contributions AS contributions, s.contribution_months_suppressed,
    round(s.d_headcount, 2) AS insured_avg, round(s.insured_avg_t12m, 2) AS insured_avg_t12m,
    s.d_revenue AS revenue, s.revenue_fy, s.revenue_is_stale, s.vat_registered,
    -- peers
    s.peer_level, s.peer_n, s.form_class, s.peer_eligible,
    round(s.rpy_lo) AS peer_reviews_per_year_lo, round(s.rpy_hi) AS peer_reviews_per_year_hi,
    round(s.m_taxes) AS peer_median_taxes, round(s.m_contrib) AS peer_median_contributions,
    round(s.m_headcount, 2) AS peer_median_headcount, round(s.m_revenue) AS peer_median_revenue,
    -- score
    round(s.gap_taxes, 3) AS gap_taxes, round(s.gap_payroll, 3) AS gap_payroll,
    round(s.gap_revenue, 3) AS gap_revenue, round(s.score, 3) AS score,
    round(s.reviews_per_1k_taxes, 2) AS reviews_per_1k_taxes,
    round(s.reviews_per_1k_revenue, 2) AS reviews_per_1k_revenue,
    round(s.insured_per_100_reviews, 2) AS insured_per_100_reviews,
    s.sig_near_zero_declared, s.sig_staffing_floor, s.sig_vat_gap,
    (s.sig_near_zero_declared::INT + s.sig_staffing_floor::INT + s.sig_vat_gap::INT) AS n_signals,
    -- trust
    s.enough_reviews, s.visibly_busy, round(s.busy_floor) AS busy_floor, s.rating_extreme, s.all_links_usable, s.weakest_confidence,
    s.link_methods, s.validation_statuses, s.owner_operated, s.multi_site,
    s.new_entity, s.entity_age_months, s.has_vmi_record, s.has_sodra_record, s.activity_fits,
    -- lineage
    s.sodra_as_of, s.taxes_updated_on, s.jar_fetch_id, s.vmi_fetch_id, s.sodra_fetch_id,
    s.revenue_fetch_id,
    CASE
        WHEN s.new_entity THEN 'entity younger than 12 months'
        WHEN NOT s.has_vmi_record THEN 'no VMI tax record (absence is not proof of zero)'
        WHEN s.score IS NULL THEN 'too few comparable peers'
        WHEN NOT s.enough_reviews THEN 'too few reviews for the rating profile'
        WHEN NOT s.visibly_busy THEN 'not visibly busy vs category (' || round(s.busy_floor)::INT || '+ reviews needed)'
        WHEN s.gap_taxes < ln(s.min_gap_ratio) THEN 'taxes in line with peers'
        WHEN NOT s.all_links_usable THEN 'link to legal entity not verified enough'
        WHEN (s.sig_near_zero_declared::INT + s.sig_staffing_floor::INT + s.sig_vat_gap::INT)
             < CASE WHEN s.rating_extreme THEN 2 ELSE 1 END
             THEN 'no corroborating signal'
    END AS hold_reason,
    CASE
        WHEN s.new_entity OR NOT s.has_vmi_record OR s.score IS NULL THEN 'D_insufficient_evidence'
        WHEN NOT s.enough_reviews OR NOT s.visibly_busy
             OR s.gap_taxes < ln(s.min_gap_ratio) THEN 'C_not_flagged'
        WHEN s.all_links_usable
             AND (s.sig_near_zero_declared::INT + s.sig_staffing_floor::INT + s.sig_vat_gap::INT)
                 >= CASE WHEN s.rating_extreme THEN 2 ELSE 1 END
        THEN 'A_candidate'
        ELSE 'B_watchlist'
    END AS tier_raw,
    s.analyst_capacity
FROM _scored s
UNION ALL BY NAME
-- entities that could not be compared at all still appear, so the analyst sees why
SELECT '{run_month}' AS run_month, b.ja_kodas, b.legal_name, b.legal_form, b.main_category,
       b.categories, b.n_places, b.place_ids, b.place_names, b.reviews_total,
       'too few comparable peers' AS hold_reason, 'D_insufficient_evidence' AS tier_raw
FROM _peers b WHERE b.ja_kodas NOT IN (SELECT ja_kodas FROM _scored);

CREATE OR REPLACE TEMP TABLE _final AS
SELECT * EXCLUDE (tier_raw, analyst_capacity),
    CASE WHEN tier_raw = 'A_candidate'
              AND row_number() OVER (PARTITION BY tier_raw ORDER BY score DESC) <= analyst_capacity
         THEN 'A_priority'
         WHEN tier_raw = 'A_candidate' THEN 'B_watchlist'
         ELSE tier_raw END AS tier,
    rank() OVER (ORDER BY CASE WHEN tier_raw = 'A_candidate' THEN 0
                               WHEN tier_raw = 'B_watchlist' THEN 1 ELSE 2 END,
                          score DESC NULLS LAST) AS priority_rank
FROM _lead;

CREATE OR REPLACE TABLE mart.lead AS
SELECT * FROM _final
UNION ALL BY NAME
SELECT * FROM mart.lead WHERE run_month <> '{run_month}' AND run_month IS NOT NULL;
