# Developer entry points. CI runs the same targets, so "works locally" means
# "passes CI".

PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help venv install lint format typecheck test cov check build docker clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## create the virtualenv
	$(PYTHON) -m venv $(VENV)

install: venv ## install the package with dev + server extras
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[server,dev]"

lint: ## ruff check + format check
	$(BIN)/ruff check fwcopilot tests
	$(BIN)/ruff format --check fwcopilot tests

format: ## apply ruff formatting and autofixes
	$(BIN)/ruff check --fix fwcopilot tests
	$(BIN)/ruff format fwcopilot tests

typecheck: ## mypy
	$(BIN)/mypy fwcopilot

test: ## run the test suite
	$(BIN)/pytest -q

cov: ## run tests with coverage and enforce the threshold
	$(BIN)/pytest --cov=fwcopilot --cov-report=term-missing --cov-report=xml

check: lint typecheck cov ## everything CI runs

build: ## build sdist + wheel
	$(BIN)/pip install --quiet build
	$(BIN)/python -m build

docker: ## build the container image
	docker build -t fwcopilot:latest .

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
