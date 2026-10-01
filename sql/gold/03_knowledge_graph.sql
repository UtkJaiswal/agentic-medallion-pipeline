-- Knowledge graph as (subject, predicate, object) edges: loadable as-is into Neo4j / RDF tooling, and
-- queryable in SQL ("which assets in Tower A have open safety hazards", "which vendors handle work
-- outside their qualifications"). Built from silver + the ontology in ref.*.
DROP TABLE IF EXISTS gold.kg_edges CASCADE;

CREATE TABLE gold.kg_edges AS
WITH t AS (SELECT * FROM silver.tickets WHERE NOT is_duplicate_closure)
SELECT 'ticket' AS subject_type, ticket_id AS subject, 'in_category' AS predicate, 'category' AS object_type, category AS object FROM t
UNION ALL SELECT 'ticket', ticket_id, 'located_in', 'building', building FROM t WHERE building IS NOT NULL
UNION ALL SELECT 'ticket', ticket_id, 'concerns_asset', 'asset', asset_id FROM t WHERE asset_id IS NOT NULL
UNION ALL SELECT 'ticket', ticket_id, 'assigned_to', 'party', assignee FROM t WHERE assignee IS NOT NULL
UNION ALL SELECT 'ticket', ticket_id, 'has_issue', 'issue_type', issue_type FROM t WHERE issue_type IS NOT NULL
UNION ALL SELECT DISTINCT 'asset', asset_id, 'located_in', 'building', building FROM t
          WHERE asset_id IS NOT NULL AND building IS NOT NULL
UNION ALL SELECT DISTINCT 'issue_type', issue_type, 'broader', 'category', category FROM t
          WHERE issue_type IS NOT NULL AND issue_type <> 'unspecified'
UNION ALL SELECT 'party', party, 'qualified_for', 'category', category FROM ref.party_qualifications
UNION ALL SELECT 'category', category_a, 'related_to', 'category', category_b FROM ref.category_relations;

CREATE INDEX kg_edges_subject_idx ON gold.kg_edges (subject_type, subject);
CREATE INDEX kg_edges_object_idx ON gold.kg_edges (object_type, object);
