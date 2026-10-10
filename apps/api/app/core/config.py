import ipaddress
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _origin_hostname(origin: str) -> str | None:
    try:
        return urlsplit(origin).hostname
    except ValueError:
        return None


def _is_local_or_private_origin(origin: str) -> bool:
    hostname = _origin_hostname(origin)
    if not hostname:
        return False

    normalized_hostname = hostname.strip().lower()
    if normalized_hostname == "localhost":
        return True

    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        return False

    return address.is_loopback or address.is_private


def _dedupe_preserve_order(origins: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for origin in origins:
        if origin in seen:
            continue
        seen.add(origin)
        deduped.append(origin)
    return deduped


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "LOCI API"
    semantic_env: str = "development"
    log_level: str = "INFO"
    sentry_dsn: str = ""
    vite_sentry_dsn: str = ""

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4321,http://127.0.0.1:4321"
    cors_origin_regex: str = ""
    semantic_embed_allowed_origins: str = "http://localhost:4321,http://127.0.0.1:4321"
    semantic_embed_frame_ancestors: str = "http://localhost:4321 http://127.0.0.1:4321"
    semantic_embed_public_base_url: str = ""

    jwt_algorithm: str = "HS256"
    jwt_secret_key: str = "replace-me"
    access_token_expire_minutes: int = 60
    reset_token_expire_minutes: int = 60
    password_reset_base_url: str = "http://localhost:5173/reset-password"

    encryption_master_key: str = "replace-me"
    agent_shared_token: str = "replace-me"

    postgres_user: str = "semantic"
    postgres_password: str = "semantic"
    postgres_db: str = "semantic"
    database_url: str = "postgresql+psycopg://semantic:semantic@postgres:5432/semantic"
    redis_url: str = "redis://redis:6379/0"
    rate_limit_enabled: bool = True
    # Dark/default-off master switch for serving approved optimized model
    # variants on the public surface. When false (default) the public model-file
    # resolver always serves the canonical model regardless of any variant rows.
    public_model_variants_enabled: bool = False

    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_transcription_model: str = "whisper-1"
    openai_vision_model: str = "gpt-4o-mini"
    openai_api_base: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = 90.0
    openai_max_retries: int = 3
    openai_retry_backoff_seconds: float = 1.5
    openai_vision_temperature: float = 0.0
    embedding_vector_dimensions: int = 768
    embedding_provider: str = "auto"
    local_embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_batch_size: int = 64
    embedding_warmup_enabled: bool = True
    embedding_enabled: bool = True
    semantic_window_target_ms: int = 30000
    semantic_window_overlap_ms: int = 8000
    visual_rerank_alpha: float = 0.15
    visual_only_min_score: float = 0.55
    visual_window_frames_subdir: str = "transcript-window-frames"
    visual_description_max_tokens: int = 90
    visual_description_prompt_version: str = "step13-v1"
    visual_description_transcript_char_limit: int = 160
    visual_description_prompt_template: str = (
        "You are describing sampled frames from one short research-video window. Describe only directly visible "
        "content that stays important across the images. Prioritize the main artifact or object, clearly visible "
        "material or surface, and any hand or body interaction with it. Use the transcript excerpt only to disambiguate "
        "the object's name when the object is visibly present.\n"
        "Do not infer function, ritual meaning, history, symbolism, identity, intent, or anything off-screen. Never use "
        "the transcript excerpt to infer use or significance. Do not use speculative phrases such as suggests, seems, "
        "appears to, likely, maybe, or possibly.\n"
        "If some frames switch between presenter footage and object-display footage, describe the object or interaction "
        "that appears most consistently. Mention camera/view changes, indoor/outdoor setting, headset, pedestal, display "
        "case, or room context only when that context is the main subject of most frames.\n"
        "Window: {start_ms}-{end_ms} ms ({duration_ms} ms).\n"
        "Transcript excerpt: {transcript_text}\n"
        "Return exactly 2 short sentences, maximum 40 words total, in plain factual language. Sentence 1: main visible object and material or handling. Sentence 2: one additional visible detail about shape, surface, orientation, or nearby hands."
    )
    title_card_sample_cadence_seconds: int = 5
    title_card_ocr_detail: str = "high"
    title_card_ocr_max_tokens: int = 300
    title_card_prompt_version: str = "2026-04-24-v1"
    title_card_prompt_template: str = (
        "You are extracting only directly visible title-card metadata from a single sampled research-video frame. "
        "Read only on-screen text. Do not infer identity, dates, or locations from voice, transcript, prior frames, or diarization.\n"
        "Return exactly one JSON object with this schema: "
        "{\"title_card_visible\": boolean, \"object\": {\"name\": string|null, \"accession_number\": string|null, \"holding_institution\": string|null, \"city\": string|null, \"region\": string|null, \"country\": string|null}, "
        "\"presenter\": {\"name\": string|null, \"age\": integer|null, \"city\": string|null, \"region\": string|null, \"country\": string|null}, "
        "\"session_date\": string|null, \"additional_text\": string|null, \"raw_text_observed\": string, "
        "\"confidence\": {\"object\": \"high|medium|low|none\", \"presenter\": \"high|medium|low|none\", \"session_date\": \"high|medium|low|none\"}}.\n"
        "Rules: use null when a field is absent or unreadable; keep session_date and raw_text_observed verbatim from the visible title card; presenter.age must be an integer or null; confidence values must be one of high, medium, low, none; split location text into city / region / country only when directly shown; for 'Chicago, Illinois' use city='Chicago', region='Illinois', country=null; do not add markdown, comments, or extra keys."
    )

    postmark_server_token: str = ""
    postmark_sender_email: str = ""
    postmark_message_stream: str = "outbound"

    # shared bearer token for the admin digest-trigger endpoint
    # (POST /api/v1/public/admin/digest/trigger). The local authoring
    # console calls this on the public stack to fan out a digest after
    # publish + sync. Empty default means the endpoint is gated off until
    # the operator injects a token in .env.public — secure-by-default.
    loci_digest_trigger_token: str = ""

    media_root: str = "/var/lib/semantic/media"
    # Authenticated upload ceilings protect local storage and bound parser work.
    # Deployments can lower these values after measuring their accepted corpus.
    media_upload_max_bytes: int = 10 * 1024 * 1024 * 1024
    object_model_upload_max_bytes: int = 2 * 1024 * 1024 * 1024
    transcode_output_subdir: str = "transcoded"
    object_model_output_subdir: str = "object-models"
    transcode_concurrency: int = 1
    disk_alert_threshold_percent: int = 80
    # Monitor the filesystem backing the running API process. In containers,
    # this may be a Docker-managed filesystem rather than the host root disk.
    runtime_root_disk_alert_threshold_percent: int = Field(default=85, ge=1, le=100)
    runtime_root_disk_min_free_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=0)
    semantic_public_sync_env_file: str = ""
    public_object_priority_ids: str = ""

    def validate_shared_secrets(self) -> None:
        """Reject development credentials before a shared service starts."""
        if self.semantic_env.lower() not in {"shared", "staging", "production"}:
            return
        for name in ("jwt_secret_key", "encryption_master_key", "agent_shared_token"):
            value = getattr(self, name).strip()
            if len(value.encode("utf-8")) < 32 or any(
                marker in value.lower() for marker in ("replace-me", "changeme", "example", "placeholder")
            ):
                raise ValueError(f"{name} must be a generated secret of at least 32 bytes in shared environments")

    @property
    def cors_origin_list(self) -> list[str]:
        origins = _split_csv(self.cors_origins)
        embed_origins = _split_csv(self.semantic_embed_allowed_origins)

        if self.semantic_env.lower() == "production":
            origins = [origin for origin in origins if not _is_local_or_private_origin(origin)]
            embed_origins = [origin for origin in embed_origins if not _is_local_or_private_origin(origin)]

        return _dedupe_preserve_order([*origins, *embed_origins])

    @property
    def resolved_cors_origin_regex(self) -> str | None:
        if self.cors_origin_regex.strip():
            return self.cors_origin_regex.strip()

        if self.semantic_env.lower() != "production":
            return (
                r"^https?://((localhost|127\.0\.0\.1)"
                r"|(10\.\d+\.\d+\.\d+)"
                r"|(192\.168\.\d+\.\d+)"
                r"|(172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+))(:\d+)?$"
            )

        return None


settings = Settings()
