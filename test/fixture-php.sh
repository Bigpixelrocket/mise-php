#!/usr/bin/env bash

set -euo pipefail

case "${1:-}" in
  -v|--version)
    echo "PHP 8.4.99 (cli) (built: fixture)"
    ;;
  -m)
    printf '%s\n' '[PHP Modules]' Core json PDO '[Zend Modules]'
    ;;
  *)
    echo "PHP 8.4.99 fixture"
    ;;
esac

