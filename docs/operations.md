# Public reader synthetic check

`scripts/ops/check-public-reader.py` checks the lightweight public reader path:

- `GET /api/v1/public/objects/page?page=1&page_size=3` returns a valid public collection page.
- `POST /api/v1/public/search/segments` accepts a transcript-only synthetic query and returns a valid search response.
- `GET /sitemap.xml` returns valid sitemap XML whose locations use the configured site origin and public reader paths.

The checker does not fetch models, posters, or video. It sends the
`LociPublicReaderSynthetic/1.0` User-Agent, refuses redirects, and writes JSON
containing only check names, coarse status, elapsed milliseconds, HTTP status
when available, and a fixed failure category. It omits request text, response
data, identifiers, URLs, and credentials.

Run it manually with the public site origin:

```sh
python3 scripts/ops/check-public-reader.py --base-url https://example.org
```

The process exits `0` when every check passes, `1` when a check fails, and `2`
when its configuration is invalid. Set `--timeout` to change the per-request
limit. Set `--query` to a stable synthetic phrase that the public transcript
index is expected to match, and set `--min-results` when the deployment has a
maintained result floor for that phrase. The default minimum is zero so a valid
empty response still confirms that the search route is functioning.

An existing operations monitor should run this checker at its established
synthetic-check cadence and route nonzero exits to the service owner. Alert on
check failure while keeping the JSON output and synthetic request metadata free
of queries, result content, and object identifiers. This repository provides
the checker; deployment-specific scheduling and notification routing remain
part of the operator's monitoring configuration.

The authenticated API runtime health snapshot also reports `media_root` and
`runtime_root_filesystem` capacity. Runtime root alerts use
`RUNTIME_ROOT_DISK_ALERT_THRESHOLD_PERCENT` (default `85`) and
`RUNTIME_ROOT_DISK_MIN_FREE_BYTES` (default 5 GiB). The latter measures the
filesystem visible as `/` to the API process. In a container, that filesystem
may map to Docker-managed storage, so it does not guarantee visibility into the
host's root disk. Keep host disk monitoring in the host-level monitor when the
host filesystem is a separate capacity boundary.
