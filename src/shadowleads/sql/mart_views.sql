-- Analyst-facing views over every stored monthly snapshot (views, so they always reflect all runs
-- without re-running the pipeline): lead history month over month, and a category summary.

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

-- Google Maps link for any place_id: SELECT maps_url(place_id) FROM ...
-- documented URL format: https://developers.google.com/maps/documentation/urls/get-started
CREATE OR REPLACE MACRO maps_url(pid) AS
    'https://www.google.com/maps/search/?api=1&query=place&query_place_id=' || pid;

-- One row per Google place with its link, company and lead tier - the easiest table to search.
CREATE OR REPLACE VIEW mart.place AS
SELECT
    p.run_month, p.place_id, p.name, p.category, p.formatted_address AS address,
    p.user_rating_count AS reviews, p.rating, p.phone, p.website,
    coalesce(p.maps_uri, maps_url(p.place_id)) AS google_maps,
    l.status AS link_status, l.method AS link_method, l.ja_kodas, e.legal_name,
    ml.tier, ml.score, p.in_scope
FROM core.place_snapshot p
LEFT JOIN core.place_entity_link l USING (run_month, place_id)
LEFT JOIN core.entity e ON e.ja_kodas = l.ja_kodas
LEFT JOIN mart.lead ml ON ml.run_month = p.run_month AND ml.ja_kodas = l.ja_kodas;
