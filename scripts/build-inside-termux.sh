#!/usr/bin/env bash
set -euxo pipefail

TARGET_NAME="${1:-android-termux}"

export DEBIAN_FRONTEND=noninteractive

echo "Updating Termux package index..."
pkg update -y

echo "Installing build dependencies..."
pkg install -y \
  python \
  clang \
  make \
  binutils \
  patchelf \
  tar

# Note: We do NOT run `python -m pip install --upgrade pip` here,
# as Termux strictly prohibits overwriting the system-managed pip package.

echo "Installing Nuitka and Textual..."
pip install nuitka textual

# The file uv_review.py is expected to already exist in the mounted workspace,
# as it is copied by the GitHub Actions workflow before starting the container.

if [ ! -f uv_review.py ]; then
  echo "Error: could not find uv_review.py in the workspace." >&2
  exit 1
fi

mkdir -p dist artifacts

COMMON_FLAGS="
--standalone
--static-libpython=no
--assume-yes-for-downloads
--enable-plugin=anti-bloat
--include-package=textual
--output-dir=dist
"

if python -m nuitka $COMMON_FLAGS --onefile --include-package-data=textual uv_review.py; then
  echo "One-file build succeeded with package data."
elif python -m nuitka $COMMON_FLAGS --onefile uv_review.py; then
  echo "One-file build succeeded without package data."
elif python -m nuitka $COMMON_FLAGS uv_review.py; then
  echo "Standalone directory build succeeded."
else
  echo "Nuitka build failed." >&2
  exit 1
fi

if [ -f dist/uv_review ]; then
  OUTPUT_FILE="artifacts/uv-review-${TARGET_NAME}"
  cp dist/uv_review "$OUTPUT_FILE"
  chmod +x "$OUTPUT_FILE"
elif [ -d dist/uv_review.dist ]; then
  OUTPUT_FILE="artifacts/uv-review-${TARGET_NAME}-standalone.tar.gz"
  tar -czf "$OUTPUT_FILE" -C dist uv_review.dist
else
  echo "Error: no Nuitka output artifact was found." >&2
  ls -lah dist || true
  exit 1
fi

chmod -R a+rX artifacts || true

echo "Build artifact created:"
ls -lah artifacts
