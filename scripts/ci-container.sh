#!/usr/bin/env bash
set -euo pipefail
mkdir -p /work
tar -C /source --exclude=.git --exclude=.venv --exclude=target --exclude=dist \
    --exclude=site --exclude=.cache --exclude=.pytest_cache --exclude=.ruff_cache \
    --exclude=__pycache__ --exclude=examples/out --exclude=docs/adr \
    -cf - . | tar -C /work --no-same-owner -xf -
cd /work
export CARGO_HOME=/work/.cargo-home
export RUSTUP_HOME=/work/.rustup
export UV_CACHE_DIR=/work/.uv-cache
export UV_PYTHON_INSTALL_DIR=/work/.python-runtimes
curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/pynox-rustup.sh
bash /tmp/pynox-rustup.sh -y --profile minimal --default-toolchain none --no-modify-path
export PATH="$CARGO_HOME/bin:$PATH"
export UV_PYTHON="$PYNOX_PYTHON_VERSION"
if [[ "$PYNOX_LINUX_FAMILY" == gnu ]]; then
    export PYNOX_GLIBC_MAX=2.28
fi
groups=(--group integration)
if [[ "$PYNOX_LINUX_FAMILY" == gnu ]]; then
    # Agent framework examples; some of their dependencies have no musl wheels.
    groups+=(--group agents)
fi
uv sync --locked "${groups[@]}"
make check
make test PYTEST_GROUPS="${groups[*]:2}" PYTEST_ARGS="--junitxml=dist/tests-${PYNOX_PYTHON_VERSION}-linux-${PYNOX_LINUX_FAMILY}.xml"
make bundle
uv run --locked python scripts/check_install.py
make wheel
uv run --locked python scripts/check_wheel.py
if [[ "$PYNOX_LINUX_FAMILY" == gnu ]]; then
    # Lambda runs Amazon Linux (glibc); layers come from glibc bundles only.
    uv run --locked python scripts/lambda_layer.py dist/pynox-*-linux-gnu-*.tar.gz
fi
cp dist/*.tar.gz dist/*.whl dist/*.sha256 dist/*.xml /artifacts/
cp dist/*.zip /artifacts/ 2>/dev/null || true
# The host can clean its artifacts after a root-owned container build.
chown "$PYNOX_OUTPUT_OWNER" /artifacts/*
