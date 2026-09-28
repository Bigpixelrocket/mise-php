#!/usr/bin/env bash

set -euo pipefail

# -n (no php.ini) changes nothing this fixture reports.
if [[ "${1:-}" == "-n" ]]; then
  shift
fi

case "${1:-}" in
  -v|--version)
    echo "PHP 8.4.99 (cli) (built: fixture)"
    ;;
  -m)
    printf '%s\n' '[PHP Modules]' Core json PDO
    # An archive without a manifest compiles demo_new in, the way archives
    # published before shared extensions compile in every module.
    if [[ ! -f "$(dirname "$0")/../share/php-bin/manifest.json" ]]; then
      echo demo_new
    fi
    printf '%s\n' '' '[Zend Modules]'
    ;;
  *)
    echo "PHP 8.4.99 fixture"
    ;;
esac
