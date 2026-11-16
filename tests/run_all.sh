#!/usr/bin/env bash
# Прогон всех проверок: .venv/bin/python tests/<name>.py по очереди.
set -u
cd "$(dirname "$0")/.."
fail=0
for t in tests/*_check.py tests/ui_sim.py; do
  if timeout 300 .venv/bin/python "$t" > "/tmp/uccp_$(basename "$t" .py).log" 2>&1; then
    echo "OK   $t"
  else
    echo "FAIL $t (лог: /tmp/uccp_$(basename "$t" .py).log)"; fail=1
  fi
done
exit $fail
