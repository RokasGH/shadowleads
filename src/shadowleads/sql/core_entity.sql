-- Legal-entity reference: one row per JAR entity with the attributes linking and scoring need.
-- Sources: stg.jar_entity (JAR), stg.vmi_register (VMI taxpayer register), stg.sodra_monthly.

CREATE OR REPLACE TABLE core.entity_branch AS
SELECT
    ja_kodas,
    branch_no,
    branch_name,
    branch_municipality,
    seat_municipality,
    activity_code,
    activity_from,
    activity_to,
    is_main_activity,
    branch_municipality = 13 AS in_vilnius,               -- VMI municipality code 13 = Vilniaus m. sav.
    (activity_to IS NULL OR activity_to >= current_date)
        AND deregistered_on IS NULL AS is_current,
    fetch_id
FROM stg.vmi_register;

CREATE OR REPLACE TABLE core.entity AS
WITH sodra AS (
    SELECT
        ja_kodas,
        arg_max(evrk, month)         AS sodra_evrk,
        arg_max(municipality, month) AS sodra_municipality,
        min(month)                   AS sodra_first_month,
        max(month)                   AS sodra_last_month
    FROM stg.sodra_monthly
    GROUP BY ja_kodas
),
vmi AS (
    SELECT
        ja_kodas,
        list(DISTINCT activity_code) FILTER (WHERE activity_code IS NOT NULL) AS vmi_activity_codes,
        bool_or(in_vilnius OR seat_municipality = 13)                         AS vmi_vilnius,
        count(DISTINCT branch_municipality)
            FILTER (WHERE is_current AND branch_municipality <> 13)           AS other_municipality_branches,
        count(DISTINCT branch_no) FILTER (WHERE is_current AND in_vilnius)    AS vilnius_branches,
        min(activity_from)                                                    AS first_activity_from
    FROM core.entity_branch
    GROUP BY ja_kodas
),
vat AS (
    SELECT
        ja_kodas,
        arg_max(vat_code, coalesce(vat_registered_on, DATE '1900-01-01')) AS vat_code,
        max(vat_registered_on)                                            AS vat_registered_on,
        max(vat_deregistered_on)                                          AS vat_deregistered_on
    FROM stg.vmi_register
    WHERE vat_code IS NOT NULL
    GROUP BY ja_kodas
)
SELECT
    j.ja_kodas,
    j.legal_name,
    j.legal_form,
    j.legal_form_code,
    j.legal_form_code IN (960, 810)                   AS owner_operated,  -- MB, IĮ
    j.registered_address,
    j.registered_on,
    j.status,
    -- liquidating / bankrupt entities cannot be leads; reorganising ones still trade
    j.status_code NOT IN (5, 6, 7, 9, 26)
        AND j.legal_form_code NOT IN (260, 270, 700, 760, 950) AS is_active,
    s.sodra_evrk,
    s.sodra_municipality,
    s.sodra_first_month,
    s.sodra_last_month,
    list_distinct(list_concat(
        coalesce(v.vmi_activity_codes, []),
        CASE WHEN s.sodra_evrk IS NULL THEN [] ELSE [s.sodra_evrk] END
    ))                                                AS evrk_codes,
    (j.registered_address ILIKE 'Vilnius,%' OR j.registered_address ILIKE '%Vilniaus m.%'
        OR s.sodra_municipality = 'Vilniaus m. sav.'
        OR coalesce(v.vmi_vilnius, false))            AS vilnius_nexus,
    coalesce(v.other_municipality_branches, 0)        AS other_municipality_branches,
    coalesce(v.vilnius_branches, 0)                   AS vilnius_branches,
    v.first_activity_from,
    t.vat_code,
    t.vat_registered_on,
    t.vat_deregistered_on,
    (t.vat_registered_on IS NOT NULL
        AND (t.vat_deregistered_on IS NULL OR t.vat_deregistered_on < t.vat_registered_on))
                                                      AS vat_registered,
    j.fetch_id                                        AS jar_fetch_id
FROM stg.jar_entity j
LEFT JOIN sodra s USING (ja_kodas)
LEFT JOIN vmi v USING (ja_kodas)
LEFT JOIN vat t USING (ja_kodas);
