-- Ontology (config/ontology.yaml) materialised for SQL and graph consumers.
CREATE TABLE ref.category_relations (
    category_a text NOT NULL REFERENCES ref.category_taxonomy (category),
    category_b text NOT NULL REFERENCES ref.category_taxonomy (category),
    PRIMARY KEY (category_a, category_b)
);
CREATE TABLE ref.party_qualifications (
    party    text NOT NULL,
    category text NOT NULL REFERENCES ref.category_taxonomy (category),
    PRIMARY KEY (party, category)
);
