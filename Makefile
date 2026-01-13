.PHONY: fmt lint type test check security
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

security:
	$(PYTHON) scripts/check_no_sensitive_runtime_files.py

check: lint test security

check-all: lint type test
