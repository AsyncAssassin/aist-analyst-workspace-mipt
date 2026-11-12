import os
from dataclasses import dataclass, field

from sqlalchemy import URL


@dataclass(frozen=True)
class Settings:
    db_host: str = field(default_factory=lambda: os.getenv("AIST_DB_HOST", "db"))
    db_port: int = field(default_factory=lambda: int(os.getenv("AIST_DB_PORT", "5432")))
    db_name: str = field(default_factory=lambda: os.getenv("AIST_DB_NAME", "aist_d07"))
    db_user: str = field(default_factory=lambda: os.getenv("AIST_DB_USER", "aist_d07"))
    db_password: str = field(default_factory=lambda: os.getenv("AIST_DB_PASSWORD", ""), repr=False)
    live_models_enabled: bool = field(default_factory=lambda: os.getenv("AIST_LIVE_MODELS_ENABLED", "false").lower() == "true")
    extraction_thinking: str = field(default_factory=lambda: os.getenv('AIST_EXTRACTION_THINKING', 'enabled'))
    ready_facts_only: bool = field(default_factory=lambda: os.getenv("AIST_READY_FACTS_ONLY", "false").lower() == "true")
    model_profile_id: str = field(default_factory=lambda: os.getenv('AIST_MODEL_PROFILE_ID', 'model_novita-mimo-v1'))
    anthropic_secret_path: str = field(default_factory=lambda: os.getenv('AIST_ANTHROPIC_SECRET_PATH', '/run/secrets/anthropic.key'), repr=False)
    openai_secret_path: str = field(default_factory=lambda: os.getenv('AIST_OPENAI_SECRET_PATH', '/run/secrets/openai.key'), repr=False)

    storage_root: str = field(default_factory=lambda: os.getenv("AIST_STORAGE_ROOT", "/data/sources"))

    export_root: str = field(default_factory=lambda: os.getenv("AIST_EXPORT_ROOT", "/data/exports"))

    executor_mode: str = field(default_factory=lambda: os.getenv("AIST_EXECUTOR_MODE", "disabled"))
    qa_url: str = field(default_factory=lambda: os.getenv("AIST_QA_URL", ""))
    policy_path: str = field(default_factory=lambda: os.getenv("AIST_POLICY_PATH", ""))
    lease_seconds: int = field(default_factory=lambda: int(os.getenv("AIST_LEASE_SECONDS", "15")))
    shutdown_seconds: float = field(default_factory=lambda: float(os.getenv("AIST_SHUTDOWN_SECONDS", "5")))
    transport_seconds: float = field(default_factory=lambda: float(os.getenv("AIST_TRANSPORT_SECONDS", "60")))
    snapshot_path: str = field(default_factory=lambda: os.getenv("AIST_SNAPSHOT_PATH", "/tmp/aist-worker-snapshot.json"))
    openclaw_root: str = field(default_factory=lambda: os.getenv('AIST_OPENCLAW_ROOT', '/data/d10'))
    gateway_port: int = field(default_factory=lambda: int(os.getenv('AIST_GATEWAY_PORT', '29103')))
    provider_port: int = field(default_factory=lambda: int(os.getenv('AIST_PROVIDER_PORT', '29104')))
    novita_secret_path: str = field(default_factory=lambda: os.getenv('AIST_NOVITA_SECRET_PATH', '/run/secrets/novita.key'), repr=False)

    embeddings_enabled: bool = field(default_factory=lambda: os.getenv('AIST_EMBEDDINGS_ENABLED','false').lower()=='true')
    qdrant_url: str = field(default_factory=lambda: os.getenv('AIST_QDRANT_URL','http://qdrant:6333'))

    def __post_init__(self):
        from .live_profile import FABLE_ID, FULL_EXTRACTION_IDS
        if self.model_profile_id not in ('model_novita-mimo-v1', FABLE_ID, *FULL_EXTRACTION_IDS):
            raise ValueError('Unknown fixed model profile')
        if self.model_profile_id == FABLE_ID and (not self.ready_facts_only or self.embeddings_enabled):
            raise ValueError('Fable composer requires ready facts without embeddings')
        if self.model_profile_id in FULL_EXTRACTION_IDS and (self.ready_facts_only or self.embeddings_enabled):
            raise ValueError('Full model profile requires extraction without embeddings')
        if self.extraction_thinking not in ('enabled', 'disabled'):
            raise ValueError('Invalid extraction reasoning mode')
        if self.ready_facts_only and self.embeddings_enabled:
            raise ValueError('Ready-facts diagnostics cannot enable embeddings')
        if self.executor_mode not in ("disabled", "qa-scripted", "openclaw"):
            raise ValueError("Executor not delivered; use explicit QA injection")
        if self.executor_mode == "qa-scripted" and (not self.qa_url or not self.policy_path):
            raise ValueError("QA requires an explicit simulator and server policy")
        if self.executor_mode == "qa-scripted":
            from urllib.parse import urlsplit
            endpoint=urlsplit(self.qa_url)
            if endpoint.scheme!="http" or endpoint.hostname not in ("simulator","localhost","127.0.0.1") or endpoint.username or endpoint.password:
                raise ValueError("QA simulator must be local")
        if self.lease_seconds < 2 or not 0 < self.shutdown_seconds <= 60:
            raise ValueError("Invalid worker deadlines")
        if self.live_models_enabled != (self.executor_mode == 'openclaw'):
            raise ValueError('Live execution unavailable without both the OpenClaw mode and the explicit live switch')
        if self.executor_mode == 'openclaw' and not self.policy_path:
            raise ValueError('Live execution requires the common server budget policy')

    def database_url(self):
        return URL.create("postgresql+psycopg2", username=self.db_user,
                          password=self.db_password, host=self.db_host,
                          port=self.db_port, database=self.db_name)
