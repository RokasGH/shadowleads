-- Link validation for one run month. Grain: (run_month, place_id) for linked places.
--   V1 automatic plausibility checks, V2 independent cross-source agreement,
--   V3 manual audit labels (labels/match_audit*.csv) and analyst overrides (labels/link_overrides.csv),
--   V4 link stability: was the place linked to a different company in the previous snapshot?
-- Other months' rows are kept, so links can be compared over time.
-- Evidence families: premises (VMVT), website, serp, brand (trademark owner / job-ad
-- employer). Brand-level sources name the company behind a BRAND - for franchises and groups that
-- is not the venue operator - so they count as one family and never confirm a link on their own.
-- `usable` = the link may feed Priority-A leads; everything else is at most watchlist.

CREATE TABLE IF NOT EXISTS core.link_audit_label (
    place_id VARCHAR, ja_kodas BIGINT, verdict VARCHAR, auditor VARCHAR, audited_on DATE, note VARCHAR
);

CREATE OR REPLACE TEMP TABLE _lv AS
WITH link AS (
    SELECT l.*, p.category, p.name AS place_name, p.website
    FROM core.place_entity_link l
    JOIN core.place_snapshot p USING (run_month, place_id)
    WHERE l.run_month = '{run_month}' AND l.status = 'linked'
),
chosen AS (  -- evidence row of the chosen entity (activity fit, address agreement, similarity)
    SELECT run_month, place_id, ja_kodas,
           bool_or(evrk_consistent) AS activity_fits,
           bool_or(address_agrees)  AS address_agrees,
           max(name_similarity)     AS name_similarity,
           any_value(matched_key)   AS matched_key
    FROM core.match_candidate
    WHERE run_month = '{run_month}'
    GROUP BY ALL
),
places_per_entity AS (
    SELECT ja_kodas, count(*) AS n_places, count(DISTINCT category) AS n_categories,
           count(DISTINCT regexp_extract(website, 'https?://(?:www\.)?([^/]+)', 1)) AS n_domains
    FROM link GROUP BY ja_kodas
),
chain_sites AS (  -- one website for 3+ different addresses: a brand/franchisor site
    SELECT regexp_extract(website, 'https?://(?:www\.)?([^/]+)', 1) AS domain
    FROM core.place_snapshot
    WHERE run_month = '{run_month}' AND in_scope AND website IS NOT NULL
    GROUP BY 1 HAVING count(DISTINCT lower(coalesce(street, '') || street_number)) >= 3
),
v2 AS (
    SELECT place_id,
           count(*) FILTER (WHERE verdict = 'confirms' AND source NOT IN ('trademark', 'job_ads'))  AS sources_confirming,
           count(*) FILTER (WHERE verdict = 'conflicts' AND source NOT IN ('trademark', 'job_ads')) AS sources_conflicting,
           count(*) FILTER (WHERE verdict = 'confirms' AND source IN ('trademark', 'job_ads'))      AS brand_confirming,
           string_agg(source || ':' || verdict, ', ')    AS agreement_detail
    FROM core.link_agreement WHERE run_month = '{run_month}'
    GROUP BY place_id
),
prev AS (
    SELECT place_id, ja_kodas AS prev_ja_kodas
    FROM core.place_entity_link
    WHERE status = 'linked'
      AND run_month = (SELECT max(run_month) FROM core.place_entity_link WHERE run_month < '{run_month}')
),
audit AS (
    SELECT place_id, ja_kodas, arg_max(verdict, audited_on) AS audit_verdict
    FROM core.link_audit_label GROUP BY ALL
)
SELECT
    l.run_month, l.place_id, l.ja_kodas, l.category, l.method, l.confidence, l.stage,
    -- V1
    e.is_active                                              AS entity_active,
    coalesce(c.activity_fits, false)                         AS activity_fits,
    e.vilnius_nexus,
    coalesce(c.address_agrees, false)                        AS address_agrees,
    c.name_similarity,
    (len(string_split(coalesce(c.matched_key, ''), ' ')) = 1
        AND l.method LIKE 'core_name%')                      AS single_token_name,
    (ppe.n_places > 3 AND ppe.n_categories > 1 AND ppe.n_domains > 1) AS implausible_spread,
    -- V2
    coalesce(v2.sources_confirming, 0)                       AS sources_confirming,
    coalesce(v2.sources_conflicting, 0)                      AS sources_conflicting,
    coalesce(v2.brand_confirming, 0)                         AS brand_confirming,
    v2.agreement_detail,
    cs.domain IS NOT NULL                                    AS chain_site,
    -- V3
    a.audit_verdict,
    -- V4
    prev.prev_ja_kodas,
    prev.prev_ja_kodas IS NOT NULL AND prev.prev_ja_kodas <> l.ja_kodas AS relinked_since_last_run,
    CASE
        WHEN l.stage = 'override' THEN 'confirmed_by_audit'
        WHEN a.audit_verdict = 'wrong' THEN 'rejected_by_audit'
        WHEN a.audit_verdict = 'correct' THEN 'confirmed_by_audit'
        WHEN coalesce(v2.sources_conflicting, 0) > 0 AND coalesce(v2.sources_confirming, 0) = 0 THEN 'conflict'
        WHEN coalesce(v2.sources_confirming, 0) > 0 THEN 'confirmed'
        -- fallback links ARE independent evidence: 2+ distinct evidence families confirm each other
        WHEN l.stage = 'fallback' AND len(list_distinct(list_transform(list_filter(
                string_split(replace(l.method, 'fallback:', ''), '+'), x -> x <> 'primary_candidate'),
                x -> CASE WHEN x IN ('trademark', 'job_ads') THEN 'brand' ELSE x END))) >= 2 THEN 'confirmed'
        WHEN l.stage = 'fallback' AND regexp_full_match(replace(l.method, 'fallback:', ''),
                '(primary_candidate\+)?(trademark|job_ads)(\+(trademark|job_ads))*') THEN 'brand_level_only'
        WHEN l.stage = 'fallback' AND cs.domain IS NOT NULL
             AND regexp_full_match(replace(l.method, 'fallback:', ''), '(primary_candidate\+)?website')
             THEN 'brand_site_code'
        WHEN l.stage = 'fallback' THEN 'single_source'
        ELSE 'unverified'
    END                                                      AS validation_status
FROM link l
JOIN core.entity e USING (ja_kodas)
LEFT JOIN chosen c USING (run_month, place_id, ja_kodas)
LEFT JOIN places_per_entity ppe USING (ja_kodas)
LEFT JOIN v2 USING (place_id)
LEFT JOIN prev USING (place_id)
LEFT JOIN audit a ON a.place_id = l.place_id AND a.ja_kodas = l.ja_kodas
LEFT JOIN chain_sites cs ON cs.domain = regexp_extract(l.website, 'https?://(?:www\.)?([^/]+)', 1);

ALTER TABLE _lv ADD COLUMN usable BOOLEAN;
UPDATE _lv SET usable =
    entity_active
    AND validation_status NOT IN ('conflict', 'rejected_by_audit')
    AND NOT implausible_spread
    AND (
        validation_status IN ('confirmed', 'confirmed_by_audit')
        -- strong primary rule + plausibility
        OR (confidence = 'HIGH' AND (activity_fits OR address_agrees))
        -- a company code the business publishes itself, or its food-premises registration,
        -- plus an activity code that fits the category
        OR (validation_status = 'single_source' AND activity_fits
            AND (method LIKE '%website%' OR method LIKE '%vmvt%'))
    );

CREATE TABLE IF NOT EXISTS core.link_validation AS SELECT * FROM _lv WHERE false;
CREATE OR REPLACE TABLE core.link_validation AS
SELECT * FROM _lv
UNION ALL BY NAME
SELECT * FROM core.link_validation WHERE run_month <> '{run_month}';
