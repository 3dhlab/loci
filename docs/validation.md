# Validation

The version prepared on September 27, 2026 passed 609 backend tests across 28 modules with zero skips, 123 frontend tests, the production frontend build, a synthetic Chromium/WebGL regression, and a fresh PostgreSQL migration cycle from base to head and back. Tests against the running Docker demonstration checked model, video and poster delivery, partial file requests, linked clips and transcript timing. They also checked signed-in annotation creation, privacy before publication, review after replacing media, and transcript search.

Dependency audits of the resolved Node and Python environments reported zero known vulnerabilities on that date. These results describe the checked dependency versions and advisory data available at validation time. Repeat audits when publishing and updating dependencies.

A browser test against the running demonstration also verified the jump from 0–4 seconds to 8–12 seconds, clip selection, annotation context and restoration of the clip start after a shared-link reload, and citation text.

The automated checks cover the local synthetic demonstration. Hosted operation, broader browser/device compatibility, optional external integrations, institutional deployment, collection rights and security review require their own validation. Large viewer bundles and feature components remain documented maintenance work.

Run the commands in CONTRIBUTING.md and README.md to reproduce the checks. Keep generated credentials, media, installed dependencies and test results on the local computer.

## Hosted demonstration storage

The hosted demo job uses a Linux-only Compose override with a 256 MiB in-memory media volume. The volume is shared by seeding and runtime containers and removed after the job. This gives the generated demonstration its own bounded storage area while keeping the application's 80% disk-health threshold. Local installation continues to use persistent media storage. CI captures credential-redacted service logs and health diagnostics before cleanup.
