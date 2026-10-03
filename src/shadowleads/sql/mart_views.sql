-- Analyst-facing summary view (a view, so it always reflects the stored snapshot).

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
