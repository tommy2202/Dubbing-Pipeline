.PHONY: fmt lint type test check
.PHONY: check-all

PYTHON ?= python3
PATHS ?= src tests tools main.py

fmt:
	$(PYTHON) -m ruff check --fix $(PATHS)
	$(PYTHON) -m black $(PATHS)

lint:
	$(PYTHON) -m ruff check --no-fix $(PATHS)
	$(PYTHON) -m black --check $(PATHS)

type:
	$(PYTHON) -m mypy src

test:
	$(PYTHON) -m pytest -q

check: lint test

check-all: lint type test
