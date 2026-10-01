.PHONY: up run demo down reset logs test test-unit lint eval report

COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm cli

up:            ## everything: postgres, migrations, one pipeline run + report, api, worker (foreground)
	$(COMPOSE) up --build

up-d:          ## same, detached
	$(COMPOSE) up -d --build --wait postgres api worker

run: up-d      ## run the full pipeline once (bronze -> silver -> quality gate -> gold)
	$(RUN) medallion run

demo: run      ## pipeline + idempotent API submission + a look at the gold layer
	$(RUN) medallion run
	$(RUN) sh -c 'API_URL=http://api:8000 medallion submit --idempotency-key demo-key-0001 --wait'
	$(RUN) sh -c 'API_URL=http://api:8000 medallion submit --idempotency-key demo-key-0001'
	$(RUN) medallion report

report:        ## print a summary of every layer
	$(RUN) medallion report

eval:          ## score the classification strategies (add providers via LLM_PROVIDERS in .env)
	$(RUN) medallion eval --strategies rules --show-errors

logs:
	$(COMPOSE) logs -f api worker

down:
	$(COMPOSE) --profile kafka down

reset:         ## drop all data (volumes) - next `make run` starts from scratch
	$(COMPOSE) --profile kafka down -v

test-unit:     ## unit tests, no database needed
	uv run pytest -m "not integration" -q

test:          ## all tests (integration tests need: make up)
	uv run pytest -q

lint:
	uv run ruff check src tests
