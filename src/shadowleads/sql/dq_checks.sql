-- Data-quality assertions for one run month -> meta.dq_result.
-- severity 'error' blocks the lead export; 'warn' is shown to the analyst next to the leads.

DELETE FROM meta.dq_result WHERE run_month = '{run_month}';

INSERT INTO meta.dq_result
WITH checks AS (
    -- volumes: a truncated download must never silently shrink the universe
    SELECT 'jar_rows' AS check_name, 'error' AS severity,
           (SELECT count(*) FROM stg.jar_entity) AS observed_n, 200000 AS min_n, NULL::DOUBLE AS max_n
    UNION ALL SELECT 'sodra_rows', 'error', (SELECT count(*) FROM stg.sodra_monthly), 2000000, NULL
    UNION ALL SELECT 'vmi_tax_rows', 'error', (SELECT count(*) FROM stg.vmi_taxes), 300000, NULL
    UNION ALL SELECT 'vmi_register_rows', 'error', (SELECT count(*) FROM stg.vmi_register), 800000, NULL
    UNION ALL SELECT 'google_places_in_scope', 'error',
           (SELECT count(*) FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope), 1000, NULL
    -- freshness
    UNION ALL SELECT 'sodra_months_behind_run', 'error',
           date_diff('month', (SELECT max(month) FROM stg.sodra_monthly), CAST('{run_month}-01' AS DATE)),
           NULL, 3
    UNION ALL SELECT 'vmi_taxes_days_since_update', 'warn',
           date_diff('day', (SELECT max(updated_on) FROM stg.vmi_taxes), current_date), NULL, 60
    -- Google sweep completeness
    UNION ALL SELECT 'google_cells_not_swept', 'warn',
           (SELECT count(*) FROM stg.google_sweep WHERE run_month = '{run_month}'
              AND status IN ('budget_exhausted', 'quota_exhausted')), NULL, 0
    UNION ALL SELECT 'google_cells_truncated_at_min_radius', 'warn',
           (SELECT count(*) FROM stg.google_sweep WHERE run_month = '{run_month}' AND status = 'truncated'), NULL, 25
    -- integrity
    UNION ALL SELECT 'duplicate_place_ids', 'error',
           (SELECT count(*) - count(DISTINCT place_id) FROM core.place_snapshot WHERE run_month = '{run_month}'), NULL, 0
    UNION ALL SELECT 'places_outside_bbox', 'error',
           (SELECT count(*) FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope
              AND NOT (lat BETWEEN 54.50 AND 54.90 AND lng BETWEEN 24.95 AND 25.55)), NULL, 0
    -- places with no reviews come back without userRatingCount; a jump would mean a field-mask change
    UNION ALL SELECT 'pct_places_without_review_count', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE user_rating_count IS NULL) / count(*)
              FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope), NULL, 30
    UNION ALL SELECT 'duplicate_lead_entities', 'error',
           (SELECT count(*) - count(DISTINCT ja_kodas) FROM mart.lead WHERE run_month = '{run_month}'), NULL, 0
    -- linking health
    UNION ALL SELECT 'pct_places_linked', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE status = 'linked') / count(*)
              FROM core.place_entity_link WHERE run_month = '{run_month}'), 20, NULL
    UNION ALL SELECT 'pct_links_in_conflict', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE validation_status = 'conflict') / nullif(count(*), 0)
              FROM core.link_validation WHERE run_month = '{run_month}'), NULL, 10
    UNION ALL SELECT 'audit_precision_pct', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE verdict = 'correct') / nullif(count(*), 0)
              FROM core.link_audit_label), 80, NULL
    -- month-over-month stability (no previous run -> passes)
    UNION ALL SELECT 'pct_change_places_vs_previous_run', 'warn',
           (SELECT abs(100.0 * (cur - prev) / nullif(prev, 0)) FROM (
               SELECT count(*) FILTER (WHERE run_month = '{run_month}') AS cur,
                      count(*) FILTER (WHERE run_month = (SELECT max(run_month) FROM core.place_snapshot
                                                          WHERE run_month < '{run_month}')) AS prev
               FROM core.place_snapshot WHERE in_scope)), NULL, 20
    -- declared-data staleness (informative: revenue lags by design)
    UNION ALL SELECT 'pct_linked_entities_with_last_fy_revenue', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE revenue_fy >= tax_year) / nullif(count(*), 0)
              FROM mart.entity_activity WHERE run_month = '{run_month}'), 50, NULL
)
SELECT '{run_month}', check_name, severity,
       coalesce((min_n IS NULL OR observed_n >= min_n) AND (max_n IS NULL OR observed_n <= max_n), true) AS passed,
       CAST(round(observed_n, 1) AS VARCHAR),
       CASE WHEN min_n IS NOT NULL THEN '>= ' || min_n ELSE '<= ' || max_n END,
       now()::TIMESTAMP
FROM checks;
