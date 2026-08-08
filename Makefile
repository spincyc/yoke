PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv lint type test verify clean

venv: $(VENV)/.stamp
$(VENV)/.stamp: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet -e '.[dev]'
	touch $@

lint: venv
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests

type: venv
	$(BIN)/mypy

test: venv
	$(BIN)/pytest

verify: lint type test

clean:
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache dist
