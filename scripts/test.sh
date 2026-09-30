#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

"$SCRIPT_DIR/check-public-language.sh"
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
package_fixture 8.5.0 current
package_fixture 9.0.1 current
package_fixture 8.5.1 current
package_fixture 8.5.1-1 shared demo_on:true demo_off:false demo_zend:false:zend
package_fixture 8.5.2 shared demo_on:true demo_off:false demo_zend:false:zend demo_new:true
package_fixture 8.5.2-1 shared demo_on:true
package_fixture 8.5.3 shared demo_on:true demo_off:false demo_zend:false:zend demo_new:true demo_later:true
ARCHIVE_NAME="php-8.4.99-cli-macos-aarch64.tar.gz"
(
  cd "$ASSETS"
  shasum -a 256 php-*-cli-macos-aarch64.tar.gz > SHA256SUMS
)

# API order is publish order, newest first: a rebuild of the older 8.5.1 was
# published after 8.5.2, and the newest 8.5.2 revision is still a draft. One
# hundred releases of an unmaintained branch come first, so every maintained
# release sits on the second page of the listing.
{
  printf '[\n'
  for patch in {1..100}; do printf '  {"tag": "7.4.%s"},\n' "$patch"; done
  cat <<'JSON'
  {"tag": "8.5.1-1"},
  {"tag": "8.5.2-1", "draft": true},
  {"tag": "8.5.3"},
  {"tag": "8.5.2"},
  {"tag": "8.5.1"},
  {"tag": "8.5.0"},
  {"tag": "8.4.99"},
  {"tag": "8.1.99"},
  {"tag": "9.0.1"}
]
JSON
} > "$ASSETS/releases.json"

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

# A token is set throughout, and the mock server is not api.github.com, so no
# request may carry it: not the API, and not an asset download.
export MISE_PHP_API_BASE_URL="http://127.0.0.1:$PORT"
export MISE_PHP_GITHUB_TOKEN="fixture-token-never-sent"
export GITHUB_TOKEN="fixture-token-never-sent"
REQUESTS="$ASSETS/requests.log"
# Print the requests made since mark_requests last ran.
mark_requests() { REQUEST_MARK="$({ wc -l < "$REQUESTS"; } 2>/dev/null || echo 0)"; }
new_requests() { tail -n +"$((REQUEST_MARK + 1))" "$REQUESTS"; }
mark_requests
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
test "$(grep -c '^8\.5\.' <<< "$AVAILABLE_VERSIONS")" = 4
# Every maintained release is on the second page, so listing it at all proves
# the pages were followed; a full page is never the last one.
new_requests | grep -F '/releases?per_page=100&page=2 '
test "$(mise latest php@8.5)" = "8.5.3"

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
# gets a php.ini with its bundled extensions and a relocated build kit. The
# listing that resolved the revision already holds its release, so the install
# reads no release by tag.
mark_requests
mise install php@8.5.1
new_requests | grep -F '/assets/php-8.5.1-1-cli-macos-aarch64.tar.gz '
if new_requests | grep -F '/releases/tags/'; then
  echo "A plain-version install read its release again by tag." >&2
  exit 1
fi
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
# extension off, and add extensions built there with PIE, by name, by absolute
# path, with an upper-case directive, and by a relative path climbing into
# 8.5.1 from any sibling's extension_dir. A value ending in "/" must not break
# the carry-forward. The file has no global extension_dir, a multi-line quoted
# value with a bracketed line, and a special section, whose quoted name holds
# a bracket, followed by an ordinary one that holds its only extension_dir,
# which PHP never applies globally.
#
# It ends with values PHP's ini scanner reads over several lines: an
# apostrophe in an unquoted value opens a raw string that the next apostrophe
# closes, even one in a comment, and single- and double-quoted strings may
# span lines. Every extension= line inside them is text, not a directive. A
# single or double quote that never closes, or an "=" inside a value, makes
# PHP stop reading, so the lines after it are handled one by one as before.
sed -i '' -e 's/^extension=demo_on$/;extension=demo_on/' -e '/^extension_dir = /d' \
  "$ROOT_851/bin/php.ini"
{
  printf 'memory_limit = 512M\nextension=userbuilt\nextension=%s\nextension=/opt/ext/\n' \
    "$ROOT_851/lib/php/extensions/abspath.so"
  printf 'EXTENSION=uppercase\nzend_extension=../../../../8.5.1/lib/php/extensions/userbuilt.so\n'
  printf 'error_prepend_string = "\n[PHP error]\nextension=inside_quotes\n"\n'
  printf '["PATH=/srv/app[1]"]\nmemory_limit = 64M\n[PHP]\nextension_dir = "/old/place"\n'
  printf "user_agent = it's mine\nextension=inside_apostrophes\n; that's all\n"
  printf "error_append_string = 'raw\nextension=inside_raw\n'\n"
  printf 'docref_root = "broken\nextension=after_equals\n" = here\n'
  printf "error_log = /tmp/O'Brien.log\nextension=after_apostrophe\n"
  printf 'html_errors = "never closed\nextension=after_quote\n'
} >> "$ROOT_851/bin/php.ini"
: > "$ROOT_851/lib/php/extensions/userbuilt.so"
: > "$ROOT_851/lib/php/extensions/abspath.so"
: > "$ROOT_851/lib/php/extensions/uppercase.so"

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
first_setting() { grep -v -E '^[[:space:]]*(;|$)' "$1" | head -n 1; }
test "$(first_setting "$INI_852")" = "extension_dir = \"$ROOT_852/lib/php/extensions\""
test "$(grep -n -Fx 'extension=demo_new' "$INI_852" | cut -d: -f1)" \
  -lt "$(grep -n -Fx 'memory_limit = 512M' "$INI_852" | cut -d: -f1)"
test "$(grep '^extension_dir' "$INI_852" | sort -u)" = "extension_dir = \"$ROOT_852/lib/php/extensions\""
grep -Fx 'memory_limit = 512M' "$INI_852"
grep -Fx ';extension=demo_on' "$INI_852"
grep -Fx ';extension=demo_off' "$INI_852"
grep -A1 -Fx '; userbuilt is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_852" \
  | grep -Fx ';extension=userbuilt'
grep -A1 -Fx '; abspath is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_852" \
  | grep -Fx ";extension=$ROOT_851/lib/php/extensions/abspath.so"
grep -Fx 'extension=/opt/ext/' "$INI_852"
grep -A1 -Fx '; uppercase is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_852" \
  | grep -Fx ';EXTENSION=uppercase'
grep -A1 -Fx '; userbuilt is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_852" \
  | grep -Fx ';zend_extension=../../../../8.5.1/lib/php/extensions/userbuilt.so'
# The new bundled default lands in the global scope, before any setting, so
# neither the quoted value nor the special section can hold it, and both keep
# their own lines.
grep -A3 -Fx 'error_prepend_string = "' "$INI_852" | tail -n 3 | tr '\n' '|' \
  | grep -Fx '[PHP error]|extension=inside_quotes|"|'
grep -A2 -Fx "user_agent = it's mine" "$INI_852" | tr '\n' '|' \
  | grep -Fx "user_agent = it's mine|extension=inside_apostrophes|; that's all|"
grep -A2 -Fx "error_append_string = 'raw" "$INI_852" | tr '\n' '|' \
  | grep -Fx "error_append_string = 'raw|extension=inside_raw|'|"
grep -A1 -Fx '; after_apostrophe is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_852" | grep -Fx ';extension=after_apostrophe'
grep -A1 -Fx '; after_quote is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_852" | grep -Fx ';extension=after_quote'
grep -A1 -Fx '; after_equals is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_852" | grep -Fx ';extension=after_equals'
grep -A1 -Fx '["PATH=/srv/app[1]"]' "$INI_852" | grep -Fx 'memory_limit = 64M'
grep -Fx 'extension=demo_new' "$INI_852"
if grep -Fx 'memory_limit = 1G' "$INI_852"; then
  echo "Settings were carried from a symbolic link." >&2
  exit 1
fi
test ! -e "$ROOT_852/lib/php/extensions/userbuilt.so"

# An explicit revision stays an exact pin, read by its tag. It carries from
# 8.5.2, whose leading managed extension_dir is rewritten in place, not added
# again.
mark_requests
mise install php@8.5.1-1
test "$(new_requests | grep -c -F '/releases/tags/8.5.1-1 ')" = 1
grep -Fq '"release":"8.5.1-1"' "$INSTALLS/8.5.1-1/share/php-bin/manifest.json"
test "$(first_setting "$INSTALLS/8.5.1-1/bin/php.ini")" \
  = "extension_dir = \"$INSTALLS/8.5.1-1/lib/php/extensions\""
test "$(grep -c '^; Managed by mise-php' "$INSTALLS/8.5.1-1/bin/php.ini")" \
  = "$(grep -c '^; Managed by mise-php' "$INI_852")"

# 8.5.3 also carries from 8.5.2 and bundles one more default, which follows
# the leading extension_dir instead of adding a second managed line.
mise install php@8.5.3
INI_853="$INSTALLS/8.5.3/bin/php.ini"
test "$(first_setting "$INI_853")" = "extension_dir = \"$INSTALLS/8.5.3/lib/php/extensions\""
test "$(grep -v -E '^[[:space:]]*(;|$)' "$INI_853" | sed -n 2p)" = "extension=demo_later"
test "$(grep -c '^; Managed by mise-php' "$INI_853")" = "$(grep -c '^; Managed by mise-php' "$INI_852")"
test "$(grep -c -F -x '; Bundled shared extensions' "$INI_853")" = 1

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

# php.ini carry-forward across layouts and PIE, in a home of its own so the
# newest install of the branch is always the one each step names.
use_mise_home "$TEMP_DIR/mise-carry"
INSTALLS="$MISE_DATA_DIR/installs/php"
mise plugin link php "$PROJECT_ROOT"
count_lines() { grep -c -F -x "$1" "$2" || true; }

# PIE adds its own line for an extension a commented line already names, here
# the bundled demo_off, as it does for any commented line.
mise install php@8.5.2
{
  printf '\n; PIE automatically added this to enable the demo/off extension\n'
  printf '; priority=80\nextension=demo_off\n'
} >> "$INSTALLS/8.5.2/bin/php.ini"

# 8.5.0 has no manifest and compiles demo_new in. Its lines are turned off
# with a note that says so, never a PIE rebuild note; demo_on and PIE's
# demo_off are truly absent, so they get the PIE note.
mise install php@8.5.0
INI_850="$INSTALLS/8.5.0/bin/php.ini"
grep -A1 -Fx '; demo_new is built into this PHP binary, so it needs no extension line here' "$INI_850" \
  | grep -Fx ';extension=demo_new'
grep -A1 -Fx '; demo_on is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_850" \
  | grep -Fx ';extension=demo_on'
grep -A1 -Fx '; demo_off is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_850" \
  | grep -Fx ';extension=demo_off'
if grep -E '^(zend_)?extension=' "$INI_850"; then
  echo "An extension line stayed active in an archive without shared extensions." >&2
  exit 1
fi

# Carried back into a layout that bundles them, every line mise-php turned off
# is active again. PIE's demo_off line wins, and the bundled demo_off line is
# marked so removing its ";" cannot load the extension twice.
mise uninstall php@8.5.2
mise install php@8.5.3
INI_853="$INSTALLS/8.5.3/bin/php.ini"
for name in demo_on demo_new demo_off; do
  test "$(count_lines "extension=$name" "$INI_853")" = 1
done
grep -A1 -Fx '; demo_off is enabled by another line in this file: keep this one commented' "$INI_853" \
  | grep -Fx ';extension=demo_off'
if grep -F -e 'rebuild it with PIE' -e 'built into this PHP binary' "$INI_853"; then
  echo "A note survived for an extension this install has." >&2
  exit 1
fi

# The user removes the ";" anyway: the next install keeps one active line and
# turns the second off. demo_new and demo_later are not in 8.5.1-1.
sed -i '' 's/^;extension=demo_off$/extension=demo_off/' "$INI_853"
test "$(count_lines 'extension=demo_off' "$INI_853")" = 2
mise install php@8.5.1-1
INI_8511="$INSTALLS/8.5.1-1/bin/php.ini"
test "$(count_lines 'extension=demo_off' "$INI_8511")" = 1
grep -A1 -Fx '; demo_off is already enabled by another line in this file: PHP warns when it loads one twice' "$INI_8511" \
  | grep -Fx ';extension=demo_off'
grep -A1 -Fx '; demo_later is not bundled with this build: rebuild it with PIE (pie install <package>)' "$INI_8511" \
  | grep -Fx ';extension=demo_later'

# pie install rebuilds demo_later for 8.5.1-1 and adds its own line. The next
# install drops the line mise-php turned off, with the duplicate demo_off and
# PIE's comments above it, instead of stacking notes on every upgrade.
: > "$INSTALLS/8.5.1-1/lib/php/extensions/demo_later.so"
{
  printf '\n; PIE automatically added this to enable the demo/later extension\n'
  printf '; priority=80\nextension=demo_later\n'
} >> "$INI_8511"
mise uninstall php@8.5.3
mise install php@8.5.3
for name in demo_later demo_off; do
  test "$(grep -c -E "^;?extension=$name\$" "$INI_853")" = 1
  test "$(count_lines "extension=$name" "$INI_853")" = 1
done
test "$(count_lines '; PIE automatically added this to enable the demo/later extension' "$INI_853")" = 1
test "$(count_lines '; PIE automatically added this to enable the demo/off extension' "$INI_853")" = 0
if grep -F 'enabled by another line in this file' "$INI_853"; then
  echo "A note stayed above a line the user made active." >&2
  exit 1
fi
if grep -F -e 'rebuild it with PIE' -e 'already enabled by another line' "$INI_853"; then
  echo "A superseded note was carried forward." >&2
  exit 1
fi

# A line that loads the extension from another install does not supersede one
# mise-php turned off: that one loads in an install that bundles it, and the
# other becomes the commented duplicate. The note is one from before notes
# named their extension, with whitespace around it, which still pairs.
OTHER_DEMO_NEW="extension=$INSTALLS/8.5.1-1/lib/php/extensions/demo_new.so"
awk -v other="$OTHER_DEMO_NEW" '
  $0 == "extension=demo_new" {
    print "  ; not bundled with this build: rebuild it with PIE (pie install <package>)\t "
    print ";extension=demo_new"
    print other
    next
  }
  { print }
' "$INI_853" > "$INI_853.edited"
mv "$INI_853.edited" "$INI_853"
mise install php@8.5.2
INI_852="$INSTALLS/8.5.2/bin/php.ini"
test "$(count_lines 'extension=demo_new' "$INI_852")" = 1
grep -A1 -Fx '; demo_new is already enabled by another line in this file: PHP warns when it loads one twice' "$INI_852" \
  | grep -Fx ";$OTHER_DEMO_NEW"
if grep -F '; not bundled with this build' "$INI_852"; then
  echo "A note from an earlier release with whitespace around it did not pair with its line." >&2
  exit 1
fi

# A note stays paired only with its own line: the exact commented form
# mise-php writes, of a line for the extension a named note names. A line of
# the user's that ends up below a note, because the note's own line was
# removed, stays the user's, and the note goes.
use_mise_home "$TEMP_DIR/mise-own"
INSTALLS="$MISE_DATA_DIR/installs/php"
mise plugin link php "$PROJECT_ROOT"
mise install php@8.5.1-1
{
  printf '; demo_zend is not bundled with this build: rebuild it with PIE (pie install <package>)\n'
  printf ';extension=demo_off\n'
  printf '; not bundled with this build: rebuild it with PIE (pie install <package>)\n'
  printf '; extension=demo_new\n'
  printf '; demo_on is not bundled with this build: rebuild it with PIE (pie install <package>)\n'
  printf ';;extension=demo_on\n'
} >> "$INSTALLS/8.5.1-1/bin/php.ini"
mise install php@8.5.2
INI_OWN="$INSTALLS/8.5.2/bin/php.ini"
test "$(count_lines 'extension=demo_off' "$INI_OWN")" = 0
test "$(count_lines 'extension=demo_new' "$INI_OWN")" = 0
test "$(count_lines 'extension=demo_on' "$INI_OWN")" = 1
grep -Fx '; extension=demo_new' "$INI_OWN"
grep -B1 -Fx ';;extension=demo_on' "$INI_OWN" \
  | grep -Fx '; demo_on is enabled by another line in this file: keep this one commented'
if grep -F -e 'is not bundled with this build' -e '; not bundled with this build' "$INI_OWN"; then
  echo "A note stayed without its own line, or took over a line of the user's." >&2
  exit 1
fi

# Repeated installs across patches converge on one file. The settings hold a
# header an earlier release wrote for the defaults one carry-forward added,
# and a line that loads demo_on from another install, which loads nowhere and
# stands apart between blank lines. Where neither that line nor demo_on's own
# line loads, both stay with their notes; once demo_on loads, the other line
# goes without leaving a gap. New defaults join the bundled header, the
# earlier release's header merges into it, and no header names the install a
# file was carried from.
use_mise_home "$TEMP_DIR/mise-converge"
INSTALLS="$MISE_DATA_DIR/installs/php"
mise plugin link php "$PROJECT_ROOT"
mise install php@8.5.1-1
ELSEWHERE_DEMO_ON="extension=$TEMP_DIR/elsewhere/lib/php/extensions/demo_on.so"
awk -v elsewhere="$ELSEWHERE_DEMO_ON" '
  { print }
  /^extension_dir = / {
    print ""
    print "; Bundled shared extensions not in the settings carried from 8.5.0"
    print "extension=demo_later"
  }
  /^;opcache.enable_cli = 0$/ {
    print ""
    print "; already enabled by another line in this file: PHP warns when it loads one twice"
    print ";" elsewhere
    print ""
    print "memory_limit = 256M"
  }
' "$INSTALLS/8.5.1-1/bin/php.ini" > "$TEMP_DIR/converge.ini"
mv "$TEMP_DIR/converge.ini" "$INSTALLS/8.5.1-1/bin/php.ini"

# A carried file has no note directly above another note, no two blank lines
# in a row, no note from before notes named their extension, no header
# naming a source install, and at most one bundled header.
assert_tidy() {
  if [[ "$(count_lines '; Bundled shared extensions' "$1")" -gt 1 ]]; then
    echo "The bundled header appears more than once in $1." >&2
    exit 1
  fi
  if ! awk '
    /^; .* is (not bundled with|built into|already enabled by|enabled by) / {
      if (note) bad = 1
      note = 1
      next
    }
    { note = 0 }
    $0 == "" { if (blank) bad = 1; blank = 1; next }
    { blank = 0 }
    END { exit bad }
  ' "$1"; then
    echo "Notes or blank lines stacked up in $1." >&2
    exit 1
  fi
  if grep -F -e '; not bundled with this build' -e '; built into this PHP' -e '; already enabled by' \
    -e '; enabled by another' -e 'not in the settings carried from' "$1"; then
    echo "An outdated note or header was carried forward in $1." >&2
    exit 1
  fi
}

previous=8.5.1-1
round=0
for version in 8.5.0 8.5.3 8.5.0 8.5.3 8.5.0 8.5.3; do
  mise install "php@$version"
  mise uninstall "php@$previous"
  assert_tidy "$INSTALLS/$version/bin/php.ini"
  cp "$INSTALLS/$version/bin/php.ini" "$TEMP_DIR/converge-$version-$((++round))"
  previous="$version"
done
cmp "$TEMP_DIR/converge-8.5.0-3" "$TEMP_DIR/converge-8.5.0-5"
cmp "$TEMP_DIR/converge-8.5.3-4" "$TEMP_DIR/converge-8.5.3-6"
INI_CONVERGED="$INSTALLS/8.5.3/bin/php.ini"
grep -A1 -Fx '; Bundled shared extensions' "$INI_CONVERGED" | grep -Fx 'extension=demo_new'
# One list under one header holds the new default, the one the earlier
# release's header held, and the bundled ones.
BUNDLED_LIST="$(awk '$0 == "; Bundled shared extensions" { list = 1; next } list && $0 == "" { exit } list' \
  "$INI_CONVERGED")"
for line in extension=demo_new extension=demo_later extension=demo_on ';extension=demo_off'; do
  grep -Fx "$line" <<< "$BUNDLED_LIST"
done
test "$(count_lines 'extension=demo_on' "$INI_CONVERGED")" = 1
test "$(count_lines 'extension=demo_later' "$INI_CONVERGED")" = 1
test "$(count_lines 'memory_limit = 256M' "$INI_CONVERGED")" = 1
grep -A1 -Fx '; demo_on is already enabled by another line in this file: PHP warns when it loads one twice' \
  "$INI_CONVERGED" | grep -Fx ";$ELSEWHERE_DEMO_ON"
INI_CONVERGED_850="$TEMP_DIR/converge-8.5.0-5"
grep -A1 -Fx '; demo_on is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_CONVERGED_850" | grep -Fx ";$ELSEWHERE_DEMO_ON"
grep -A1 -Fx '; demo_on is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_CONVERGED_850" | grep -Fx ';extension=demo_on'
# The next install from one where demo_on loads drops the other line.
mise install php@8.5.2
assert_tidy "$INSTALLS/8.5.2/bin/php.ini"
test "$(count_lines 'extension=demo_on' "$INSTALLS/8.5.2/bin/php.ini")" = 1
if grep -F "$ELSEWHERE_DEMO_ON" "$INSTALLS/8.5.2/bin/php.ini"; then
  echo "A line superseded by one that loads demo_on was carried forward." >&2
  exit 1
fi

# A file from a release without shared extensions has no bundled header, so
# the defaults a later install adds start one, set off by single blank lines.
use_mise_home "$TEMP_DIR/mise-plain"
INSTALLS="$MISE_DATA_DIR/installs/php"
mise plugin link php "$PROJECT_ROOT"
mise install php@8.5.0
mise install php@8.5.3
assert_tidy "$INSTALLS/8.5.3/bin/php.ini"
test "$(count_lines '; Bundled shared extensions' "$INSTALLS/8.5.3/bin/php.ini")" = 1
grep -A1 -Fx '; Bundled shared extensions' "$INSTALLS/8.5.3/bin/php.ini" | grep -Fx 'extension=demo_on'

# MISE_PHP_CARRY_INI names a php.ini to carry from instead of the newest other
# install. mise install -f deletes the install folder before PostInstall, so a
# caller that saves the old php.ini first keeps its settings this way. The
# copy is carried exactly like a sibling's: extension_dir is rewritten, a new
# bundled extension gets its default line, and a line for an extension this
# install lacks is commented out with its note.
use_mise_home "$TEMP_DIR/mise-override"
INSTALLS="$MISE_DATA_DIR/installs/php"
mise plugin link php "$PROJECT_ROOT"
mise install php@8.5.1-1
printf 'memory_limit = 333M\n' >> "$INSTALLS/8.5.1-1/bin/php.ini"
mise install php@8.5.2
INI_OVERRIDE="$INSTALLS/8.5.2/bin/php.ini"
grep -Fx 'memory_limit = 333M' "$INI_OVERRIDE"
SAVED_INI="$TEMP_DIR/saved php.ini"
sed -e 's/^memory_limit = 333M$/memory_limit = 768M/' -e '/^extension=demo_new$/d' \
  -e 's|^extension_dir = .*$|extension_dir = "/old/place"|' "$INI_OVERRIDE" > "$SAVED_INI"
printf 'extension=userbuilt\n' >> "$SAVED_INI"
cp "$SAVED_INI" "$TEMP_DIR/saved-copy.ini"
MISE_PHP_CARRY_INI="$SAVED_INI" mise install -f php@8.5.2 > "$TEMP_DIR/override.log" 2>&1
grep -F "php.ini settings carried forward from $SAVED_INI" "$TEMP_DIR/override.log"
cmp "$SAVED_INI" "$TEMP_DIR/saved-copy.ini"
test "$(count_lines 'memory_limit = 768M' "$INI_OVERRIDE")" = 1
test "$(count_lines 'memory_limit = 333M' "$INI_OVERRIDE")" = 0
test "$(first_setting "$INI_OVERRIDE")" = "extension_dir = \"$INSTALLS/8.5.2/lib/php/extensions\""
test "$(count_lines 'extension_dir = "/old/place"' "$INI_OVERRIDE")" = 0
grep -A1 -Fx '; Bundled shared extensions' "$INI_OVERRIDE" | grep -Fx 'extension=demo_new'
grep -A1 -Fx '; userbuilt is not bundled with this build: rebuild it with PIE (pie install <package>)' \
  "$INI_OVERRIDE" | grep -Fx ';extension=userbuilt'
assert_tidy "$INI_OVERRIDE"

# A path that names nothing, a folder, or a file that cannot be read falls
# back to the newest other install and never fails the install.
chmod 000 "$SAVED_INI"
for unreadable in "$TEMP_DIR/missing.ini" "$TEMP_DIR" "$SAVED_INI"; do
  MISE_PHP_CARRY_INI="$unreadable" mise install -f php@8.5.2 > "$TEMP_DIR/override.log" 2>&1
  grep -F "MISE_PHP_CARRY_INI names $unreadable, which cannot be read: using the newest other install instead" \
    "$TEMP_DIR/override.log"
  test -x "$INSTALLS/8.5.2/bin/php"
  grep -Fx 'memory_limit = 333M' "$INI_OVERRIDE"
  test "$(count_lines 'memory_limit = 768M' "$INI_OVERRIDE")" = 0
done
chmod 600 "$SAVED_INI"

# No request to the mock server carried the token.
if grep -F 'auth=yes' "$REQUESTS"; then
  echo "A request to a server other than api.github.com carried the GitHub token." >&2
  exit 1
fi
grep -F 'auth=no' "$REQUESTS" > /dev/null

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
