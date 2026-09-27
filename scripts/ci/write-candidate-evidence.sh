#!/usr/bin/env bash
set -euo pipefail
job_name="${1:?job name required}"
sha="${GITHUB_SHA:-local-working-tree}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'
  fi
}
{
  printf '### %s\n\n' "$job_name"
  printf -- '- Candidate commit: `%s`\n' "$sha"
  printf -- '- Workflow: `%s`\n' "${GITHUB_WORKFLOW_REF:-local}"
  printf -- '- Run: `%s/attempt-%s`\n' "${GITHUB_RUN_ID:-local}" "${GITHUB_RUN_ATTEMPT:-1}"
  printf -- '- Runner: `%s`\n' "${RUNNER_OS:-$(uname -s)}"
  if [ -f apps/web/package-lock.json ]; then printf -- '- Web lockfile SHA-256: `%s`\n' "$(file_sha256 apps/web/package-lock.json)"; fi
  if [ -f apps/web/tests/fixtures/synthetic-12s.mp4 ]; then printf -- '- Synthetic video SHA-256: `%s`\n' "$(file_sha256 apps/web/tests/fixtures/synthetic-12s.mp4)"; fi
  if [ -f apps/web/tests/fixtures/synthetic-cube.glb ]; then printf -- '- Synthetic model SHA-256: `%s`\n' "$(file_sha256 apps/web/tests/fixtures/synthetic-cube.glb)"; fi
  if [ -f apps/web/tests/fixtures/synthetic-poster.png ]; then printf -- '- Synthetic poster SHA-256: `%s`\n' "$(file_sha256 apps/web/tests/fixtures/synthetic-poster.png)"; fi
  printf '\n'
} >> "$summary"
