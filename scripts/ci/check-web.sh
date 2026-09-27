#!/bin/sh
set -eu
cd "$(dirname "$0")/../../apps/web"
npm ci --ignore-scripts --no-audit
npm test
npm run build -- --outDir dist-ci
npm audit --audit-level=moderate
npm run test:browser
