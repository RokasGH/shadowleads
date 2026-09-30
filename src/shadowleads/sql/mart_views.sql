-- Analyst-facing views: month-over-month history and category summary. Views, so they always
-- reflect every run stored in the warehouse without re-running the pipeline.

CREATE OR REPLACE VIEW mart.lead_history AS
WITH ranked AS (
    SELECT l.*,
           lag(tier)    OVER w AS prev_tier,
           lag(score)   OVER w AS prev_score,
           lag(run_month) OVER w AS prev_run_month,
           lag(taxes_paid) OVER w AS prev_taxes_paid,
           lag(reviews_total) OVER w AS prev_reviews_total,
           lag(link_methods) OVER w AS prev_link_methods
    FROM mart.lead l
    WINDOW w AS (PARTITION BY ja_kodas ORDER BY run_month)
)
SELECT
    run_month, ja_kodas, legal_name, main_category, tier, prev_tier, score, prev_score,
    round(score - prev_score, 3) AS score_change,
    min(run_month) FILTER (WHERE tier = 'A_priority') OVER (PARTITION BY ja_kodas) AS first_priority_month,
    count(*) FILTER (WHERE tier IN ('A_priority', 'B_watchlist')) OVER (
        PARTITION BY ja_kodas ORDER BY run_month ROWS UNBOUNDED PRECEDING)       AS months_flagged,
    CASE
        WHEN prev_run_month IS NULL THEN 'new_in_universe'
        WHEN prev_tier IS DISTINCT FROM tier THEN 'tier_changed'
        ELSE 'unchanged_tier'
    END AS change_type,
    CASE
        WHEN prev_run_month IS NULL THEN NULL
        WHEN prev_link_methods IS DISTINCT FROM link_methods THEN 're-linked (matching evidence changed)'
        WHEN prev_taxes_paid IS DISTINCT FROM taxes_paid THEN 'declared figures changed'
        WHEN prev_reviews_total IS DISTINCT FROM reviews_total THEN 'visible activity changed'
        ELSE 'no input change'
    END AS change_reason
FROM ranked;

CREATE OR REPLACE VIEW mart.category_summary AS
SELECT
    run_month, main_category,
    count(*)                                                AS scored_entities,
    count(*) FILTER (WHERE tier = 'A_priority')             AS priority_a,
    count(*) FILTER (WHERE tier = 'B_watchlist')            AS watchlist,
    round(100.0 * count(*) FILTER (WHERE tier IN ('A_priority', 'B_watchlist'))
          / nullif(count(*) FILTER (WHERE tier <> 'D_insufficient_evidence'), 0), 1) AS pct_flagged,
    median(taxes_paid)                                      AS median_taxes_paid,
    median(reviews_per_year)                                AS median_reviews_per_year
FROM mart.lead
GROUP BY ALL;
