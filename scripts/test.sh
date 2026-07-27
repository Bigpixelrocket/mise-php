#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

"$SCRIPT_DIR/check-public-language.sh"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Plugin installation tests require macOS arm64." >&2
  exit 1
fi

if ! command -v mise >/dev/null 2>&1; then
  echo "mise is required to run plugin tests." >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mise-php-test.XXXXXX")"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ "${MISE_PHP_KEEP_TEST_TMP:-0}" == "1" ]]; then
    echo "Retained test directory: $TEMP_DIR" >&2
  else
    rm -rf "$TEMP_DIR"
  fi
}
trap cleanup EXIT

mkdir -p "$TEMP_DIR/assets/package/bin"
cp "$PROJECT_ROOT/test/fixture-php.sh" "$TEMP_DIR/assets/package/bin/php"
chmod 0755 "$TEMP_DIR/assets/package/bin/php"
cp "$PROJECT_ROOT/LICENSE" "$TEMP_DIR/assets/package/LICENSE"
cp "$PROJECT_ROOT/NOTICE" "$TEMP_DIR/assets/package/NOTICE"

ARCHIVE_NAME="php-8.4.99-cli-macos-aarch64.tar.gz"
EOL_ARCHIVE_NAME="php-8.1.99-cli-macos-aarch64.tar.gz"
COPYFILE_DISABLE=1 tar -czf "$TEMP_DIR/assets/$ARCHIVE_NAME" -C "$TEMP_DIR/assets/package" .
cp "$TEMP_DIR/assets/$ARCHIVE_NAME" "$TEMP_DIR/assets/$EOL_ARCHIVE_NAME"
(
  cd "$TEMP_DIR/assets"
  shasum -a 256 "$ARCHIVE_NAME" "$EOL_ARCHIVE_NAME" > SHA256SUMS
)

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
python3 "$PROJECT_ROOT/test/mock_server.py" "$PORT" "$TEMP_DIR/assets" \
  > "$TEMP_DIR/server.log" 2>&1 &
SERVER_PID="$!"

for _ in {1..50}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/health" >/dev/null; then
    break
  fi
  sleep 0.1
done
curl --silent --fail "http://127.0.0.1:$PORT/health" >/dev/null

export MISE_PHP_API_BASE_URL="http://127.0.0.1:$PORT"
export MISE_DATA_DIR="$TEMP_DIR/mise/data"
export MISE_CACHE_DIR="$TEMP_DIR/mise/cache"
export MISE_CONFIG_DIR="$TEMP_DIR/mise/config"
export MISE_STATE_DIR="$TEMP_DIR/mise/state"

mise plugin link php "$PROJECT_ROOT"
AVAILABLE_VERSIONS="$(mise ls-remote php)"
grep -Fx "8.4.99" <<< "$AVAILABLE_VERSIONS"
if grep -Fx "8.1.99" <<< "$AVAILABLE_VERSIONS"; then
  echo "EOL PHP release was unexpectedly listed." >&2
  exit 1
fi
mise install php@8.4
test -x "$MISE_DATA_DIR/installs/php/8.4.99/bin/php"
mise exec php@8.4 -- php -v | grep -F "PHP 8.4.99"

# EOL releases are absent from branch discovery but remain installable exactly.
mise install php@8.1.99
test -x "$MISE_DATA_DIR/installs/php/8.1.99/bin/php"

printf '%064d  %s\n' 0 "$ARCHIVE_NAME" > "$TEMP_DIR/assets/SHA256SUMS"
export MISE_DATA_DIR="$TEMP_DIR/mise-bad/data"
export MISE_CACHE_DIR="$TEMP_DIR/mise-bad/cache"
export MISE_CONFIG_DIR="$TEMP_DIR/mise-bad/config"
export MISE_STATE_DIR="$TEMP_DIR/mise-bad/state"
mise plugin link php "$PROJECT_ROOT"
if mise install php@8.4.99 > "$TEMP_DIR/bad-checksum.log" 2>&1; then
  echo "Installation unexpectedly accepted an invalid checksum." >&2
  exit 1
fi
grep -Eiq 'checksum|verification|hash' "$TEMP_DIR/bad-checksum.log"

(
  cd "$PROJECT_ROOT"
  python3 -m unittest discover -s test -p 'test_*.py'
)

echo "Plugin contract test passed."
