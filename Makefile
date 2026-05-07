# AEVS SDK developer Makefile
# Run `make help` for the list of targets.

POETRY ?= poetry
SRC := src
TESTS := tests

.DEFAULT_GOAL := help
.PHONY: help install install-dev test test-cov test-fast lint format typecheck check clean build version

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  %-15s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install: ## Install runtime + all extras + dev deps via Poetry.
	$(POETRY) install --all-extras --no-interaction

install-dev: install ## Alias for install (kept for clarity in CI scripts).

test: ## Run the full test suite.
	$(POETRY) run pytest

test-cov: ## Run tests with coverage report.
	$(POETRY) run pytest --cov=aevs --cov-report=term-missing --cov-report=html

test-fast: ## Run tests, stop on first failure, no captured output.
	$(POETRY) run pytest -x -vv -s

lint: ## Lint with ruff.
	$(POETRY) run ruff check $(SRC) $(TESTS)

format: ## Auto-format with ruff.
	$(POETRY) run ruff format $(SRC) $(TESTS)
	$(POETRY) run ruff check --fix $(SRC) $(TESTS)

typecheck: ## Run mypy in strict mode on the source tree.
	$(POETRY) run mypy $(SRC)

check: lint typecheck test ## Run all CI gates (lint + typecheck + tests).

build: clean ## Build sdist and wheel into ./dist.
	$(POETRY) build

version: ## Print the current pyproject + runtime version (must match).
	@echo "pyproject: $$($(POETRY) version -s)"
	@echo "package:   $$($(POETRY) run python -c 'import aevs; print(aevs.__version__)')"

# Publishing to PyPI is intentionally NOT a Makefile target.
# Releases happen via .github/workflows/release.yml using PyPI Trusted
# Publishing (OIDC) — see RELEASING.md.  Manual `poetry publish` from
# a laptop bypasses the OIDC flow and would require a long-lived API
# token, defeating the security model.

clean: ## Remove build artefacts and caches.
	rm -rf dist build *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
