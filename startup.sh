#!/bin/bash

log() {
  printf '[%s] %s\n' "$(date)" "$*"
}

run_step() {
  local name="$1"
  shift

  log "[START] ${name}"
  "$@"
  local code=$?

  if [ "$code" -eq 0 ]; then
    log "[OK] ${name}"
  else
    log "[FAIL:${code}] ${name}"
  fi

  return "$code"
}

log "Running startup tasks..."

run_step "refresh-token" /usr/local/bin/python -m hh_applicant_tool refresh-token
run_step "update-resumes" /usr/local/bin/python -m hh_applicant_tool update-resumes

# Редеплой/перезапуск контейнера не должен сам запускать новую волну откликов.
# Запланированные запуски остаются в crontab; стартовую волну можно включить явно.
startup_apply="${HH_APPLY_ON_STARTUP:-false}"
case "${startup_apply,,}" in
  1|true|yes|on)
    run_step "apply-vacancies" /usr/bin/flock -n /tmp/hh_apply.lock /usr/local/bin/python -m hh_applicant_tool apply-vacancies --max-responses 15
    ;;
  *)
    log "[SKIP] apply-vacancies (HH_APPLY_ON_STARTUP=false)"
    ;;
esac

log "Startup tasks finished."
