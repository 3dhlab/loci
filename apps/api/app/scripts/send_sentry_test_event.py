from __future__ import annotations

import argparse

import sentry_sdk

from app.core.monitoring import configure_sentry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit a tagged backend Sentry test event.")
    parser.add_argument("--runtime", choices=["api", "worker"], default="api")
    parser.add_argument("--label", default="step3-backend-test")
    parser.add_argument("--path", default="/_operator/sentry-backend-test")
    parser.add_argument("--method", default="GET")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not configure_sentry(args.runtime):
        raise SystemExit("Sentry is not configured. Set SENTRY_DSN before running this helper.")

    try:
        raise RuntimeError(args.label)
    except RuntimeError as error:
        sentry_sdk.set_tag("request.path", args.path)
        sentry_sdk.set_tag("request.method", args.method.upper())
        sentry_sdk.capture_exception(error)
        sentry_sdk.flush(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())