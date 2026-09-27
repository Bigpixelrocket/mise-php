#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

"$SCRIPT_DIR/check-public-language.sh"
"$SCRIPT_DIR/validate-codex-action-inputs"
"$SCRIPT_DIR/validate-structured-output-schemas"
"$SCRIPT_DIR/generate-policy-lua"
git -C "$PROJECT_ROOT" diff --exit-code lib/policy.lua

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
ORIGINAL_POLICY=""
cleanup() {
  if [[ -n "$ORIGINAL_POLICY" ]]; then
    printf '%s\n' "$ORIGINAL_POLICY" > "$PROJECT_ROOT/lib/policy.lua"
  fi
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

ASSETS="$TEMP_DIR/assets"
mkdir -p "$ASSETS"

# Package a fixture archive for <tag>. "current" is the layout published before
# shared extensions: only bin/php and the notices. "shared" adds the build kit
# with its install-prefix placeholder, lib/php/extensions, and the manifest,
# whose extensions are the remaining arguments as name:default[:zend].
package_fixture() {
  local tag="$1"
  local layout="$2"
  shift 2
  local package="$TEMP_DIR/package-$tag"
  local extensions="" entry name default loader zend separator=""

  mkdir -p "$package/bin"
  cp "$PROJECT_ROOT/test/fixture-php.sh" "$package/bin/php"
  chmod 0755 "$package/bin/php"
  cp "$PROJECT_ROOT/LICENSE" "$package/LICENSE"
  cp "$PROJECT_ROOT/NOTICE" "$package/NOTICE"

  if [[ "$layout" == "shared" ]]; then
    mkdir -p "$package/lib/php/extensions" "$package/share/php-bin"
    printf '#! /bin/sh\nprefix="@PHP_BIN_PREFIX@"\nextension_dir="@PHP_BIN_PREFIX@/lib/php/extensions"\n' \
      > "$package/bin/php-config"
    printf "#! /bin/sh\nprefix='@PHP_BIN_PREFIX@'\n" > "$package/bin/phpize"
    chmod 0755 "$package/bin/php-config" "$package/bin/phpize"
    for entry in "$@"; do
      IFS=: read -r name default loader <<< "$entry"
      zend=false
      if [[ "$loader" == "zend" ]]; then zend=true; fi
      : > "$package/lib/php/extensions/$name.so"
      extensions+="$separator{\"name\":\"$name\",\"zend\":$zend,\"default\":$default,\"requires\":[]}"
      separator=","
    done
    printf '{"schemaVersion":1,"release":"%s","phpVersion":"%s","extensions":[%s]}\n' \
      "$tag" "${tag%%-*}" "$extensions" > "$package/share/php-bin/manifest.json"
  fi

  COPYFILE_DISABLE=1 tar -czf "$ASSETS/php-$tag-cli-macos-aarch64.tar.gz" -C "$package" .
}

package_fixture 8.4.99 current
package_fixture 8.1.99 current
package_fixture 9.0.1 current
package_fixture 8.5.1 current
package_fixture 8.5.1-1 shared demo_on:true demo_off:false demo_zend:false:zend
package_fixture 8.5.2 shared demo_on:true demo_off:false demo_zend:false:zend demo_new:true
package_fixture 8.5.2-1 shared demo_on:true
ARCHIVE_NAME="php-8.4.99-cli-macos-aarch64.tar.gz"
(
  cd "$ASSETS"
  shasum -a 256 php-*-cli-macos-aarch64.tar.gz > SHA256SUMS
)

# API order is publish order, newest first: a rebuild of the older 8.5.1 was
# published after 8.5.2, and the newest 8.5.2 revision is still a draft.
cat > "$ASSETS/releases.json" <<'JSON'
[
  {"tag": "8.5.1-1"},
  {"tag": "8.5.2-1", "draft": true},
  {"tag": "8.5.2"},
  {"tag": "8.5.1"},
  {"tag": "8.4.99"},
  {"tag": "8.1.99"},
  {"tag": "9.0.1"}
]
JSON

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
python3 "$PROJECT_ROOT/test/mock_server.py" "$PORT" "$ASSETS" \
  > "$TEMP_DIR/server.log" 2>&1 &
SERVER_PID="$!"

for _ in {1..50}; do
  if curl --silent --fail "http://127.0.0.1:$PORT/health" >/dev/null; then
    break
  fi
  sleep 0.1
done
curl --silent --fail "http://127.0.0.1:$PORT/health" >/dev/null

use_mise_home() {
  export MISE_DATA_DIR="$1/data"
  export MISE_CACHE_DIR="$1/cache"
  export MISE_CONFIG_DIR="$1/config"
  export MISE_STATE_DIR="$1/state"
  export MISE_GLOBAL_CONFIG_FILE="$1/config/config.toml"
  export MISE_SYSTEM_CONFIG_FILE="$1/config/system.toml"
}

export MISE_PHP_API_BASE_URL="http://127.0.0.1:$PORT"
use_mise_home "$TEMP_DIR/mise"
INSTALLS="$MISE_DATA_DIR/installs/php"

mise plugin link php "$PROJECT_ROOT"
AVAILABLE_VERSIONS="$(mise ls-remote php)"
grep -Fx "8.4.99" <<< "$AVAILABLE_VERSIONS"
if grep -Fx "8.1.99" <<< "$AVAILABLE_VERSIONS"; then
  echo "EOL PHP release was unexpectedly listed." >&2
  exit 1
fi

# Rebuild revisions list as their plain patch, once, in numeric order, so a
# late rebuild of an old patch cannot become the branch's newest version.
if grep -E -- '-[0-9]+$' <<< "$AVAILABLE_VERSIONS"; then
  echo "A rebuild revision was listed instead of its plain version." >&2
  exit 1
fi
test "$(grep -c '^8\.5\.' <<< "$AVAILABLE_VERSIONS")" = 2
test "$(mise latest php@8.5)" = "8.5.2"

# A future branch appears in listings the moment the snapshot maintains it.
if grep -Fx "9.0.1" <<< "$AVAILABLE_VERSIONS"; then
  echo "Unmaintained future branch was unexpectedly listed." >&2
  exit 1
fi
# cleanup restores lib/policy.lua, so a failure mid-swap cannot leave it mutated.
ORIGINAL_POLICY="$(cat "$PROJECT_ROOT/lib/policy.lua")"
printf 'return {\n    maintained = { "8.2", "8.3", "8.4", "8.5", "9.0" },\n}\n' > "$PROJECT_ROOT/lib/policy.lua"
FUTURE_VERSIONS="$(mise ls-remote php)"
printf '%s\n' "$ORIGINAL_POLICY" > "$PROJECT_ROOT/lib/policy.lua"
grep -Fx "9.0.1" <<< "$FUTURE_VERSIONS"

# A plain patch installs its newest published revision, and the new layout
# gets a php.ini with its bundled extensions and a relocated build kit.
mise install php@8.5.1
ROOT_851="$INSTALLS/8.5.1"
grep -Fq '"release":"8.5.1-1"' "$ROOT_851/share/php-bin/manifest.json"
grep -Fx "extension_dir = \"$ROOT_851/lib/php/extensions\"" "$ROOT_851/bin/php.ini"
grep -Fx 'extension=demo_on' "$ROOT_851/bin/php.ini"
grep -Fx ';extension=demo_off' "$ROOT_851/bin/php.ini"
grep -Fx ';zend_extension=demo_zend' "$ROOT_851/bin/php.ini"
grep -Fx ';memory_limit = 128M' "$ROOT_851/bin/php.ini"
grep -Fx "prefix=\"$ROOT_851\"" "$ROOT_851/bin/php-config"
grep -Fx "extension_dir=\"$ROOT_851/lib/php/extensions\"" "$ROOT_851/bin/php-config"
grep -Fx "prefix='$ROOT_851'" "$ROOT_851/bin/phpize"
if grep -F '@PHP_BIN_PREFIX@' "$ROOT_851/bin/php-config" "$ROOT_851/bin/phpize"; then
  echo "The build kit still holds the install-prefix placeholder." >&2
  exit 1
fi

# Edit the 8.5.1 settings the way a user would: raise a limit, turn a default
# extension off, and add an extension built there with PIE.
sed -i '' 's/^extension=demo_on$/;extension=demo_on/' "$ROOT_851/bin/php.ini"
printf 'memory_limit = 512M\nextension=userbuilt\n' >> "$ROOT_851/bin/php.ini"
: > "$ROOT_851/lib/php/extensions/userbuilt.so"

# A symbolic link named like a newer patch must never be a carry-forward source.
mkdir -p "$TEMP_DIR/decoy/bin"
printf 'memory_limit = 1G\n' > "$TEMP_DIR/decoy/bin/php.ini"
ln -s "$TEMP_DIR/decoy" "$INSTALLS/8.5.99"

# The next patch carries those settings forward; only extension_dir changes,
# the user-built extension is commented out rather than copied, and a bundled
# extension the old settings never mentioned gets its default line.
mise install php@8.5.2
rm "$INSTALLS/8.5.99"
ROOT_852="$INSTALLS/8.5.2"
INI_852="$ROOT_852/bin/php.ini"
grep -Fq '"release":"8.5.2"' "$ROOT_852/share/php-bin/manifest.json"
grep -Fx "extension_dir = \"$ROOT_852/lib/php/extensions\"" "$INI_852"
test "$(grep -c '^extension_dir' "$INI_852")" = 1
grep -Fx 'memory_limit = 512M' "$INI_852"
grep -Fx ';extension=demo_on' "$INI_852"
grep -Fx ';extension=demo_off' "$INI_852"
grep -A1 -Fx '; not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_852" \
  | grep -Fx ';extension=userbuilt'
grep -Fx 'extension=demo_new' "$INI_852"
if grep -Fx 'memory_limit = 1G' "$INI_852"; then
  echo "Settings were carried from a symbolic link." >&2
  exit 1
fi
test ! -e "$ROOT_852/lib/php/extensions/userbuilt.so"

# An explicit revision stays an exact pin.
mise install php@8.5.1-1
grep -Fq '"release":"8.5.1-1"' "$INSTALLS/8.5.1-1/share/php-bin/manifest.json"

# The current layout still gets a php.ini, and another branch's settings never
# carry over.
mise install php@8.4
test -x "$INSTALLS/8.4.99/bin/php"
mise exec php@8.4 -- php -v | grep -F "PHP 8.4.99"
grep -Fx "extension_dir = \"$INSTALLS/8.4.99/lib/php/extensions\"" "$INSTALLS/8.4.99/bin/php.ini"
if grep -E '^(zend_)?extension=|^memory_limit' "$INSTALLS/8.4.99/bin/php.ini"; then
  echo "The 8.4 php.ini holds settings it should not have." >&2
  exit 1
fi

# EOL releases are absent from branch discovery but remain installable exactly.
mise install php@8.1.99
test -x "$INSTALLS/8.1.99/bin/php"

printf '%064d  %s\n' 0 "$ARCHIVE_NAME" > "$ASSETS/SHA256SUMS"
use_mise_home "$TEMP_DIR/mise-bad"
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
