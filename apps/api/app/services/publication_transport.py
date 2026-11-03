"""Execute only the immutable generation selected by a durable publication job."""
import json
from pathlib import Path
import secrets
import shlex

from app.core.config import settings
from app.scripts.export_public_projection import build_projection
from app.services.public_sync import _load_sync_config
from app.services.publication_http_readback import public_base, verify_http
from app.services.publication_readback import bounded_readback_command, canonical, digest, expected_receipt
from app.services.publication_selection_store import (
    SelectionError, opened_verified_bundle, operation_uuid, seal_selection,
)
from app.services.publication_transfer import stream_command


class PublicationOutcomeUnknown(RuntimeError):
    pass


def _configuration():
    config = _load_sync_config()
    # Existing deployment files are read, never altered by admission or workers.
    from dotenv import dotenv_values
    values = dotenv_values(Path(settings.semantic_public_sync_env_file).expanduser())
    import os
    base = str(values.get("SEMANTIC_PUBLIC_SYNC_PUBLIC_BASE_URL") or
               os.environ.get("SEMANTIC_PUBLIC_SYNC_PUBLIC_BASE_URL") or "").strip()
    base = public_base(base)
    fingerprint = digest({"remote": config.remote, "app_dir": config.remote_app_dir,
                          "import_root": config.remote_import_root,
                          "media_root": config.remote_media_root, "public_base_url": base})
    return config, base, fingerprint


def capture_selection(operation_id, actor):
    operation_uuid(operation_id)
    config, _, fingerprint = _configuration()
    _roundtrip(config, "protocol")
    head = _roundtrip(config, "head")["head"]
    manifest, paths = build_projection()
    return seal_selection(settings.media_root, operation_id, manifest, paths, fingerprint,
                          max_bytes=settings.publication_selection_max_bytes, expected_head=head)


def _command(config, mode, operation_id=None, staging_id=None):
    command = (f"cd {shlex.quote(config.remote_app_dir)} "
               "&& docker compose --env-file .env.public -f docker-compose.public.yml "
               "exec -T api env PYTHONPATH=/app python -m app.scripts.publication_generation " + mode)
    if mode == "apply":
        operation_uuid(operation_id)
        if not isinstance(staging_id, str) or len(staging_id) != 32 or any(c not in "0123456789abcdef" for c in staging_id):
            raise SelectionError("Invalid private staging identity.")
        container_bundle = f"/var/lib/semantic/import/.loci-durable/{operation_id}/{staging_id}/media.tar.gz"
        command += " --bundle " + shlex.quote(container_bundle)
    return ["ssh", "-i", str(config.ssh_key), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", config.remote, command]


def _roundtrip(config, mode, payload=None, *, staging_id=None):
    value = bounded_readback_command(_command(config, mode,
        payload["operation_id"] if payload else None, staging_id), canonical(payload) if payload else b"", timeout=1200)
    result = json.loads(value)
    if mode == "protocol":
        if canonical(result) != canonical({"protocol": "loci-durable-publication", "version": 1}):
            raise SelectionError("Destination durable publication protocol is unavailable.")
    elif mode == "head":
        head = result.get("head") if isinstance(result, dict) else None
        if (not isinstance(head, dict) or set(head) != {"generation_id", "version"}
                or type(head["version"]) is not int or head["version"] < 0):
            raise SelectionError("Destination generation predecessor is invalid.")
        if head["generation_id"] is not None:
            operation_uuid(head["generation_id"])
    elif (not isinstance(result, dict) or set(result) != {"receipt", "challenge"}
          or result["challenge"] != payload["challenge"]):
        raise SelectionError("Destination generation acknowledgement is stale or invalid.")
    return result


def _payload(operation_id, selection_digest, metadata):
    return {"operation_id": operation_id, "selection_digest": selection_digest,
            "metadata": metadata, "challenge": secrets.token_hex(32)}


def _confirm(config, base, payload):
    result = _roundtrip(config, "reconcile", payload)
    receipt = result["receipt"]
    if receipt is None:
        return None
    if receipt.get("outcome") in {"not_applied", "superseded"}:
        # The job service validates typed, identity-bound resolution receipts.
        # Neither outcome is represented as successful HTTP publication.
        return receipt
    expected = expected_receipt(payload["metadata"]["manifest"], payload["metadata"]["media_inventory"])
    if (receipt.get("operation_id") != payload["operation_id"]
            or receipt.get("generation_id") != payload["operation_id"]
            or receipt.get("selection_digest") != payload["selection_digest"]
            or any(receipt.get(k) != v for k, v in expected.items())):
        raise SelectionError("Destination generation receipt differs from the selected content.")
    http = verify_http(payload["metadata"]["manifest"], payload["metadata"]["media_inventory"], base)
    # Detect a destination generation switch while anonymous HTTP was checked.
    again = _roundtrip(config, "reconcile", {**payload, "challenge": secrets.token_hex(32)})
    if canonical(again["receipt"]) != canonical(receipt):
        raise SelectionError("Destination generation changed during HTTP verification.")
    return {**receipt, "http_readback": http}


def execute_selected(operation_id, attempt_token, selection_ref, selection_digest,
                     metadata, actor, before_effect, progress_callback):
    """Private staging is repeatable; public apply is admitted only once.

    The source job durably records effect admission before this apply command.
    A lost acknowledgement/crash never re-enters apply: restart reconciliation
    uses the read-only function below. Target duplicate delivery also checks its
    committed operation identity under the global destination transaction lock.
    """
    operation_uuid(operation_id)
    config, base, fingerprint = _configuration()
    if fingerprint != metadata["destination_fingerprint"]:
        raise SelectionError("The configured destination changed after selection.")
    progress_callback(stage_key="check_destination", detail="Checking destination protocol.", progress_percent=10)
    _roundtrip(config, "protocol")
    staging_id = secrets.token_hex(16)
    remote = config.remote_import_root.rstrip("/") + "/.loci-durable/" + operation_id + "/" + staging_id
    # A unique operation directory is private staging, not the public generation.
    command = ["ssh", "-n", "-i", str(config.ssh_key), "-o", "BatchMode=yes", "-o",
               "StrictHostKeyChecking=yes", config.remote, "umask 077; mkdir -p " + shlex.quote(remote)]
    bounded_readback_command(command, b"", timeout=60)
    progress_callback(stage_key="stage_selection", detail="Sending the frozen selected media.", progress_percent=30)
    upload = ["ssh", "-i", str(config.ssh_key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
              config.remote, "umask 077; cat > " + shlex.quote(remote + "/media.tar.gz")]
    with opened_verified_bundle(settings.media_root, operation_id, selection_ref, selection_digest, metadata) as source:
        stream_command(upload, source, expected_bytes=metadata["bundle_bytes"],
                       expected_sha256=metadata["bundle_sha256"], timeout=1200)
    before_effect()
    progress_callback(stage_key="commit_generation", detail="Publishing the admitted generation.", progress_percent=60)
    payload = _payload(operation_id, selection_digest, metadata)
    try:
        _roundtrip(config, "apply", payload, staging_id=staging_id)
        progress_callback(stage_key="verify_generation", detail="Verifying public media, annotations and citations.", progress_percent=85)
        receipt = _confirm(config, base, payload)
        if receipt is None:
            raise SelectionError("The destination has not confirmed this generation.")
        return receipt
    except Exception:
        raise PublicationOutcomeUnknown("The admitted generation requires destination reconciliation.") from None


def reconcile_selected(operation_id, attempt_token, selection_ref, selection_digest, metadata, actor):
    """Read-only confirmation; never upload, import, prune or repeat an effect."""
    operation_uuid(operation_id)
    if operation_id != selection_ref or digest(metadata) != selection_digest:
        raise SelectionError("Publication reconciliation identity differs.")
    config, base, fingerprint = _configuration()
    if fingerprint != metadata["destination_fingerprint"]:
        raise SelectionError("The configured destination changed after selection.")
    _roundtrip(config, "protocol")
    return _confirm(config, base, _payload(operation_id, selection_digest, metadata))


def cancel_selected(operation_id, attempt_token, selection_ref, selection_digest, metadata, actor):
    """Explicitly fence a never-applied request; never undo a committed one."""
    operation_uuid(operation_id)
    if operation_id != selection_ref or digest(metadata) != selection_digest:
        raise SelectionError("Publication cancellation identity differs.")
    config, base, fingerprint = _configuration()
    if fingerprint != metadata["destination_fingerprint"]:
        raise SelectionError("The configured destination changed after selection.")
    _roundtrip(config, "protocol")
    payload = _payload(operation_id, selection_digest, metadata)
    result = _roundtrip(config, "abort", payload)
    receipt = result["receipt"]
    if isinstance(receipt, dict) and receipt.get("outcome") in {"not_applied", "superseded"}:
        return receipt
    return _confirm(config, base, payload)
