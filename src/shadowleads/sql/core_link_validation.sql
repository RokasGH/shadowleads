-- Link validation for one run month. Grain: (run_month, place_id) for linked places.
--   V1 automatic plausibility checks, V2 independent cross-source agreement,
--   V3 manual audit labels (labels/match_audit.csv, optional), V4 link stability vs previous month.
-- `usable` = the link may feed Priority-A leads; everything else is at most watchlist.

CREATE TABLE IF NOT EXISTS core.link_audit_label (
    place_id VARCHAR, ja_kodas BIGINT, verdict VARCHAR, auditor VARCHAR, audited_on DATE, note VARCHAR
);

CREATE OR REPLACE TABLE core.link_validation AS
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
v2 AS (
    SELECT place_id,
           count(*) FILTER (WHERE verdict = 'confirms')  AS sources_confirming,
           count(*) FILTER (WHERE verdict = 'conflicts') AS sources_conflicting,
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
    v2.agreement_detail,
    -- V3
    a.audit_verdict,
    -- V4
    prev.prev_ja_kodas,
    prev.prev_ja_kodas IS NOT NULL AND prev.prev_ja_kodas <> l.ja_kodas AS relinked_since_last_run,
    CASE
        WHEN a.audit_verdict = 'wrong' THEN 'rejected_by_audit'
        WHEN a.audit_verdict = 'correct' THEN 'confirmed_by_audit'
        WHEN coalesce(v2.sources_conflicting, 0) > 0 AND coalesce(v2.sources_confirming, 0) = 0 THEN 'conflict'
        WHEN coalesce(v2.sources_confirming, 0) > 0 THEN 'confirmed'
        ELSE 'unverified'
    END                                                      AS validation_status
FROM link l
JOIN core.entity e USING (ja_kodas)
LEFT JOIN chosen c USING (run_month, place_id, ja_kodas)
LEFT JOIN places_per_entity ppe USING (ja_kodas)
LEFT JOIN v2 USING (place_id)
LEFT JOIN prev USING (place_id)
LEFT JOIN audit a ON a.place_id = l.place_id AND a.ja_kodas = l.ja_kodas;

ALTER TABLE core.link_validation ADD COLUMN usable BOOLEAN;
UPDATE core.link_validation SET usable =
    entity_active
    AND validation_status NOT IN ('conflict', 'rejected_by_audit')
    AND NOT implausible_spread
    AND (
        validation_status IN ('confirmed', 'confirmed_by_audit')
        OR (confidence = 'HIGH' AND (activity_fits OR address_agrees))
    );
