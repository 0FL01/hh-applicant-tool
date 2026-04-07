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
run_step "apply-vacancies" /usr/local/bin/python -m hh_applicant_tool apply-vacancies --skip-tests

log "Startup tasks finished."
