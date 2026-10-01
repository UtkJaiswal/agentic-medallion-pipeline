-- Model routers (e.g. OpenRouter's Jev Router) choose the answering model per request: record it.
ALTER TABLE ops.llm_calls ADD COLUMN served_model text;
