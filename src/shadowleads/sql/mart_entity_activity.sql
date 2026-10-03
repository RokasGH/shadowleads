-- Milestone-1 deliverable: one row per linked legal entity for the run month, with visible
-- activity (aggregated over its linked Vilnius places) next to declared activity.
-- Tax data is per legal entity, so places are aggregated to the entity (many places : one entity).

CREATE TABLE IF NOT EXISTS mart.entity_activity AS
SELECT NULL::VARCHAR AS run_month WHERE false;

DELETE FROM mart.entity_activity WHERE run_month = '{run_month}';

CREATE OR REPLACE TEMP TABLE _ea AS
WITH params AS (
    SELECT
        '{run_month}'                                                    AS run_month,
        (SELECT max(month) FROM stg.sodra_monthly)                       AS sodra_last_month,
        (SELECT max(year) FROM stg.vmi_taxes)                            AS vmi_current_year
),
lp AS (  -- linked places with their validation
    SELECT p.*, v.ja_kodas, v.method, v.confidence, v.validation_status, v.usable, v.activity_fits
    FROM core.link_validation v
    JOIN core.place_snapshot p USING (run_month, place_id)
    WHERE v.run_month = '{run_month}'
),
visible AS (
    SELECT
        ja_kodas,
        count(*)                                            AS n_places,
        list(DISTINCT category ORDER BY category)           AS categories,
        arg_max(category, coalesce(user_rating_count, 0))   AS main_category,
        sum(coalesce(user_rating_count, 0))                 AS reviews_total,
        max(coalesce(user_rating_count, 0))                 AS reviews_max_place,
        round(sum(rating * user_rating_count) / nullif(sum(user_rating_count), 0), 2) AS rating_weighted,
        sum(weekly_open_hours)                              AS weekly_open_hours_total,
        max(weekly_open_hours)                              AS weekly_open_hours_max,
        avg((price_from + price_to) / 2)                    AS price_per_person_mid,
        bool_or(self_service)                               AS any_self_service,
        list(place_id ORDER BY user_rating_count DESC)      AS place_ids,
        list(name ORDER BY user_rating_count DESC)          AS place_names,
        bool_and(usable)                                    AS all_links_usable,
        bool_or(activity_fits)                              AS activity_fits,
        max(confidence)                                     AS weakest_confidence,  -- 'MEDIUM' > 'HIGH'
        list(DISTINCT method)                               AS link_methods,
        list(DISTINCT validation_status)                    AS validation_statuses
    FROM lp GROUP BY ja_kodas
),
shared AS (
    -- the SAME venue linked to another company too (declared figures may be split between them):
    -- same building AND the same / near-identical business name. Different businesses in a
    -- multi-tenant building (salons in a beauty centre) are not shared premises.
    SELECT DISTINCT a.ja_kodas
    FROM lp a JOIN lp b
      ON lower(a.street) = lower(b.street) AND a.street_number = b.street_number
     AND a.ja_kodas <> b.ja_kodas
     AND (jaro_winkler_similarity(lower(a.name), lower(b.name)) >= 0.9
          OR contains(lower(a.name), lower(b.name)) OR contains(lower(b.name), lower(a.name)))
),
vmi AS (
    SELECT
        ja_kodas,
        max(taxes_paid) FILTER (WHERE year = (SELECT vmi_current_year FROM params) - 1) AS taxes_prev_year,
        max(taxes_paid) FILTER (WHERE year = (SELECT vmi_current_year FROM params) - 2) AS taxes_prev_year_2,
        bool_or(year = (SELECT vmi_current_year FROM params) - 1)                       AS taxes_has_tax_year_row,
        arg_max(year, year) FILTER (WHERE year < (SELECT vmi_current_year FROM params)) AS taxes_last_reported_year,
        arg_max(taxes_paid, year) FILTER (WHERE year < (SELECT vmi_current_year FROM params)) AS taxes_last_reported,
        max(taxes_paid) FILTER (WHERE year = (SELECT vmi_current_year FROM params))     AS taxes_ytd,
        max(through_month) FILTER (WHERE year = (SELECT vmi_current_year FROM params))  AS taxes_ytd_through_month,
        max(updated_on)                                                                 AS taxes_updated_on,
        any_value(fetch_id)                                                             AS vmi_fetch_id
    FROM stg.vmi_taxes GROUP BY ja_kodas
),
sodra AS (  -- months absent from the Sodra file = no insured persons that month
    SELECT
        s.ja_kodas,
        sum(num_insured) FILTER (WHERE year(month) = (SELECT vmi_current_year FROM params) - 1) / 12.0
                                                                        AS insured_avg_prev_year,
        sum(contributions) FILTER (WHERE year(month) = (SELECT vmi_current_year FROM params) - 1)
                                                                        AS contributions_prev_year,
        count(*) FILTER (WHERE year(month) = (SELECT vmi_current_year FROM params) - 1
                           AND contributions IS NULL AND num_insured > 0)
                                                                        AS contribution_months_suppressed,
        sum(num_insured) FILTER (WHERE month > (SELECT sodra_last_month FROM params) - INTERVAL 12 MONTH) / 12.0
                                                                        AS insured_avg_t12m,
        sum(contributions) FILTER (WHERE month > (SELECT sodra_last_month FROM params) - INTERVAL 12 MONTH)
                                                                        AS contributions_t12m,
        arg_max(num_insured, month)                                     AS insured_latest,
        max(month)                                                      AS sodra_latest_month,
        arg_max(avg_wage, month)                                        AS avg_wage_latest,
        any_value(fetch_id)                                             AS sodra_fetch_id
    FROM stg.sodra_monthly s
    WHERE s.ja_kodas IN (SELECT ja_kodas FROM visible)
    GROUP BY s.ja_kodas
),
revenue AS (
    SELECT ja_kodas,
           arg_max(revenue, fiscal_year) AS revenue_latest,
           max(fiscal_year)              AS revenue_fy,
           any_value(fetch_id)           AS revenue_fetch_id
    FROM stg.rc_revenue
    WHERE ja_kodas IN (SELECT ja_kodas FROM visible) AND revenue IS NOT NULL
    GROUP BY ja_kodas
)
SELECT
    '{run_month}' AS run_month,
    v.*,
    e.legal_name, e.legal_form, e.owner_operated, e.registered_address, e.registered_on,
    e.sodra_evrk, e.evrk_codes, e.vat_code, e.vat_registered, e.vat_registered_on,
    e.other_municipality_branches, e.vilnius_branches,
    vm.taxes_prev_year, vm.taxes_prev_year_2, vm.taxes_ytd, vm.taxes_ytd_through_month,
    vm.taxes_updated_on, vm.ja_kodas IS NOT NULL AS has_vmi_record,
    coalesce(vm.taxes_has_tax_year_row, false) AS taxes_has_tax_year_row,
    vm.taxes_last_reported_year, vm.taxes_last_reported,
    coalesce(so.insured_avg_prev_year, 0)  AS insured_avg_prev_year,
    so.contributions_prev_year, coalesce(so.contribution_months_suppressed, 0) AS contribution_months_suppressed,
    coalesce(so.insured_avg_t12m, 0)       AS insured_avg_t12m,
    so.contributions_t12m,
    coalesce(so.insured_latest, 0)         AS insured_latest,
    so.sodra_latest_month, so.avg_wage_latest, so.ja_kodas IS NOT NULL AS has_sodra_record,
    r.revenue_latest, r.revenue_fy,
    r.revenue_fy < (SELECT vmi_current_year FROM params) - 1 AS revenue_is_stale,
    -- flags that change how far a lead can be trusted
    e.other_municipality_branches > 0      AS multi_site,
    v.n_places > 1                         AS chain,
    sh.ja_kodas IS NOT NULL                AS shared_premises,
    e.registered_on > CAST('{run_month}-01' AS DATE) - INTERVAL 12 MONTH AS new_entity,
    date_diff('month', e.registered_on, CAST('{run_month}-01' AS DATE)) AS entity_age_months,
    e.jar_fetch_id, vm.vmi_fetch_id, so.sodra_fetch_id, r.revenue_fetch_id,
    (SELECT sodra_last_month FROM params) AS sodra_as_of,
    (SELECT vmi_current_year FROM params) - 1 AS tax_year
FROM visible v
JOIN core.entity e USING (ja_kodas)
LEFT JOIN vmi vm USING (ja_kodas)
LEFT JOIN sodra so USING (ja_kodas)
LEFT JOIN revenue r USING (ja_kodas)
LEFT JOIN shared sh USING (ja_kodas);

CREATE OR REPLACE TABLE mart.entity_activity AS
SELECT * FROM _ea
UNION ALL BY NAME
SELECT * FROM mart.entity_activity WHERE run_month <> '{run_month}' AND run_month IS NOT NULL;
