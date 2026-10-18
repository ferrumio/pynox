.PHONY: setup build check test test-rust docs bundle wheel clean distclean
PYNOX_PYTHON_VERSION ?= $(shell cat .python-version)
export PYNOX_PYTHON_VERSION
UV_PYTHON ?= $(or $(PYO3_PYTHON),$(PYNOX_PYTHON_VERSION))
export UV_PYTHON
PYTEST_ARGS ?=
PYTEST_GROUPS ?=
setup:
	uv sync --locked
build:
	bash scripts/build.sh
check:
	cargo fmt --all -- --check
	bash scripts/build.sh --check
	uv run --locked ruff check python tests scripts examples deploy
	uv run --locked ruff format --check python tests scripts examples deploy
	uv run --locked python scripts/check_metadata.py
test-rust:
	bash scripts/build.sh --test
test: build test-rust
	uv run --locked --group integration $(PYTEST_GROUPS) pytest $(PYTEST_ARGS)
docs:
	uv run --locked python scripts/prepare_docs.py
	uv run --locked --group docs zensical build
bundle: build
	uv run --locked python scripts/package.py
wheel: bundle
	uv run --locked python scripts/wheel.py
clean:
	rm -rf -- target dist site docs/adr .cache .pytest_cache .ruff_cache
	find python tests scripts examples -type d -name __pycache__ -prune -exec rm -rf -- {} +
distclean: clean
	rm -rf -- .venv
