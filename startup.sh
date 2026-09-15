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
# Тот же лок, что в crontab: recreate/restart поднимает cron заново, @reboot
# запускает startup.sh, и его волна не должна идти параллельно с часовой.
# --max-responses 15 как в cron: стартовая волна тоже не должна пулемётить.
run_step "apply-vacancies" /usr/bin/flock -n /tmp/hh_apply.lock /usr/local/bin/python -m hh_applicant_tool apply-vacancies --max-responses 15

log "Startup tasks finished."
