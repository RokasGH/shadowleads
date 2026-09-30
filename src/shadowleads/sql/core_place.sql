-- Google place snapshot for one run month + scope flags. Grain: (run_month, place_id).
CREATE TABLE IF NOT EXISTS core.place_snapshot AS
SELECT *, false AS in_scope, '' AS out_of_scope_reason, false AS self_service
FROM stg.google_place WHERE false;

DELETE FROM core.place_snapshot WHERE run_month = '{run_month}';

INSERT INTO core.place_snapshot
SELECT
    g.* EXCLUDE (reason, self_service),
    reason IS NULL AS in_scope,
    coalesce(reason, '') AS out_of_scope_reason,
    self_service
FROM (
    SELECT
        *,
        -- self-service car washes legitimately run without staff
        category = 'auto' AND regexp_matches(
            lower(strip_accents(name)), 'savitarn|self[ -]?service|bekontakt') AS self_service,
        CASE
            WHEN category IS NULL THEN 'primary type outside categories'
            WHEN coalesce(locality, '') <> 'Vilnius' THEN 'outside Vilnius city'
            WHEN business_status IS DISTINCT FROM 'OPERATIONAL' THEN 'not operational: ' || coalesce(business_status, 'unknown')
        END AS reason
    FROM stg.google_place
    WHERE run_month = '{run_month}'
) g;

-- place registry (first/last seen) to compare months
CREATE OR REPLACE TABLE core.place AS
SELECT place_id,
       min(run_month) AS first_seen_month,
       max(run_month) AS last_seen_month,
       count(*)       AS months_seen
FROM core.place_snapshot
GROUP BY place_id;
