-- Data-quality assertions for the snapshot -> meta.dq_result.
-- Each check states what it protects against, the observed value, the exact expectation and unit.
-- severity 'error' blocks the lead export; 'warn' is shown to the analyst next to the leads.

CREATE OR REPLACE TEMP TABLE _dq AS
WITH checks (check_name, description, severity, observed, op, threshold, unit) AS (
    -- volumes: a truncated download must never silently shrink the universe
    SELECT 'jar_rows', 'Companies loaded from the JAR register', 'error',
           (SELECT count(*) FROM stg.jar_entity), '>=', 200000, 'rows'
    UNION ALL SELECT 'sodra_rows', 'Employer-month rows loaded from Sodra', 'error',
           (SELECT count(*) FROM stg.sodra_monthly), '>=', 2000000, 'rows'
    UNION ALL SELECT 'vmi_tax_rows', 'Company-year rows of VMI taxes paid', 'error',
           (SELECT count(*) FROM stg.vmi_taxes), '>=', 300000, 'rows'
    UNION ALL SELECT 'vmi_register_rows', 'Rows of the VMI taxpayer register (branches, VAT)', 'error',
           (SELECT count(*) FROM stg.vmi_register), '>=', 800000, 'rows'
    UNION ALL SELECT 'google_places_in_scope', 'Google places in the three categories in Vilnius', 'error',
           (SELECT count(*) FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope), '>=', 1000, 'places'
    -- freshness
    UNION ALL SELECT 'sodra_lag', 'Months between the latest Sodra month and the snapshot month', 'error',
           date_diff('month', (SELECT max(month) FROM stg.sodra_monthly), CAST('{run_month}-01' AS DATE)), '<=', 3, 'months'
    UNION ALL SELECT 'vmi_taxes_age', 'Days since VMI last updated taxes paid', 'warn',
           date_diff('day', (SELECT max(updated_on) FROM stg.vmi_taxes), current_date), '<=', 60, 'days'
    -- Google sweep completeness
    UNION ALL SELECT 'google_cells_not_swept', 'Map cells skipped because of API budget or quota', 'warn',
           (SELECT count(*) FROM stg.google_sweep WHERE run_month = '{run_month}'
              AND status IN ('budget_exhausted', 'quota_exhausted')), '=', 0, 'cells'
    UNION ALL SELECT 'google_cells_truncated', 'Dense cells still at the 20-result cap at minimum radius (places may be missing)', 'warn',
           (SELECT count(*) FROM stg.google_sweep WHERE run_month = '{run_month}' AND status = 'truncated'), '<=', 25, 'cells'
    -- integrity
    UNION ALL SELECT 'duplicate_place_ids', 'Google places listed twice in the snapshot', 'error',
           (SELECT count(*) - count(DISTINCT place_id) FROM core.place_snapshot WHERE run_month = '{run_month}'), '=', 0, 'places'
    UNION ALL SELECT 'places_outside_vilnius_box', 'In-scope places with coordinates outside the Vilnius area', 'error',
           (SELECT count(*) FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope
              AND NOT (lat BETWEEN 54.50 AND 54.90 AND lng BETWEEN 24.95 AND 25.55)), '=', 0, 'places'
    UNION ALL SELECT 'places_without_reviews', 'Share of places with no Google reviews (no review count returned)', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE user_rating_count IS NULL) / count(*)
              FROM core.place_snapshot WHERE run_month = '{run_month}' AND in_scope), '<=', 30, '%'
    UNION ALL SELECT 'duplicate_lead_entities', 'Companies appearing twice in the lead list', 'error',
           (SELECT count(*) - count(DISTINCT ja_kodas) FROM mart.lead WHERE run_month = '{run_month}'), '=', 0, 'companies'
    -- linking health
    UNION ALL SELECT 'places_linked', 'Share of in-scope places linked to a company', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE status = 'linked') / count(*)
              FROM core.place_entity_link WHERE run_month = '{run_month}'), '>=', 20, '%'
    UNION ALL SELECT 'links_in_conflict', 'Share of links contradicted by independent evidence', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE validation_status = 'conflict') / nullif(count(*), 0)
              FROM core.link_validation WHERE run_month = '{run_month}'), '<=', 10, '%'
    UNION ALL SELECT 'places_change_vs_previous_snapshot', 'Change in the number of in-scope places vs the previous snapshot (0 when there is none)', 'warn',
           coalesce((SELECT abs(100.0 * (cur - prev) / nullif(prev, 0)) FROM (
               SELECT count(*) FILTER (WHERE run_month = '{run_month}') AS cur,
                      count(*) FILTER (WHERE run_month = (SELECT max(run_month) FROM core.place_snapshot
                                                          WHERE run_month < '{run_month}')) AS prev
               FROM core.place_snapshot WHERE in_scope)), 0), '<=', 20, '%'
    UNION ALL SELECT 'links_changed_vs_previous_snapshot', 'Places now linked to a different company than in the previous snapshot', 'warn',
           (SELECT count(*) FILTER (WHERE relinked_since_last_run) FROM core.link_validation
              WHERE run_month = '{run_month}'), '<=', 25, 'places'
    UNION ALL SELECT 'audited_links_correct', 'Share of manually audited links judged correct', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE verdict = 'correct') / nullif(count(*), 0)
              FROM core.link_audit_label), '>=', 80, '%'
    -- declared-data staleness (informative: the open revenue export lags by design)
    UNION ALL SELECT 'revenue_for_last_tax_year', 'Share of linked companies whose revenue covers the last tax year', 'warn',
           (SELECT 100.0 * count(*) FILTER (WHERE revenue_fy >= tax_year) / nullif(count(*), 0)
              FROM mart.entity_activity WHERE run_month = '{run_month}'), '>=', 50, '%'
)
SELECT
    '{run_month}' AS run_month, check_name, description, severity,
    CASE op WHEN '>=' THEN observed >= threshold WHEN '<=' THEN observed <= threshold
            ELSE observed = threshold END                                   AS passed,
    CASE WHEN observed IS NULL THEN 'n/a'
         WHEN unit = '%' THEN format('{{:,.1f}}', observed::DOUBLE) || ' %'
         ELSE format('{{:,}}', round(observed)::BIGINT) || ' ' ||
              CASE WHEN round(observed) <> 1 THEN unit WHEN unit = 'companies' THEN 'company'
                   ELSE rtrim(unit, 's') END END                            AS observed,
    CASE op WHEN '=' THEN 'exactly ' WHEN '>=' THEN 'at least ' ELSE 'at most ' END
        || CASE WHEN unit = '%' THEN format('{{:,}}', threshold::BIGINT) || ' %'
                ELSE format('{{:,}}', threshold::BIGINT) || ' ' || unit END   AS expected,
    now()::TIMESTAMP                                                        AS checked_at
FROM checks;

CREATE TABLE IF NOT EXISTS meta.dq_result AS SELECT * FROM _dq WHERE false;
DELETE FROM meta.dq_result WHERE run_month = '{run_month}';
INSERT INTO meta.dq_result BY NAME SELECT * FROM _dq;
