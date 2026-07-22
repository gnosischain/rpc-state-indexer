PYTHON ?= python3
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

.PHONY: help install-dev check-fast check validate-config run-migrations daemon job status validate probe discover census compute backfill densify bench refresh-catalog

help:
	@$(PYTHON) -c "print('Targets: install-dev, check-fast, check, validate-config, run-migrations, daemon, job, status, validate, probe, discover, census, compute, backfill, densify, bench, refresh-catalog')"

install-dev:
	$(PYTHON) -m pip install --require-hashes -r requirements-dev.txt
	$(PYTHON) -m pip install -e .

check-fast:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) scripts/no_zero_default.py
	$(PYTHON) scripts/no_silent_rpc_failures.py
	$(PYTHON) -m pytest -m 'not integration and not pinned_chain and not performance'

check: check-fast
	$(PYTHON) -m mypy src tests

validate-config:
	rpc-state-indexer validate-config

run-migrations:
	$(COMPOSE) --profile migrations run --rm --build migrations

daemon:
	$(COMPOSE) --profile daemon up --build daemon

job:
	$(COMPOSE) --profile jobs run --rm --build jobs $(ARGS)

status validate probe discover census compute backfill densify bench:
	$(COMPOSE) --profile jobs run --rm --build jobs $@ $(ARGS)

# Scheduled incremental catalog refresh: enumerate new pools since the committed watermark
# (inside the jobs container, which reaches the archive RPC), then additively assemble and
# validate on the host. Additive-only: existing targets and their config hashes are untouched.
# Recommended to run in CI and open a PR with the config/ diff, then rebuild the image.
refresh-catalog:
	$(COMPOSE) --profile jobs run --rm --build \
	  -v "$(CURDIR)/scripts:/app/scripts" -v "$(CURDIR)/src:/hostsrc:ro" \
	  -e PYTHONPATH=/hostsrc --entrypoint python jobs \
	  scripts/catalog/enumerate.py --incremental --out /app/scripts/catalog/out
	$(PYTHON) scripts/catalog/assemble.py
	$(MAKE) validate-config
