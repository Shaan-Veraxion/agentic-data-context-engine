"""Runtime configuration, validated at startup. Read from ADCE_* environment variables (and a .env file)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADCE_", env_file=(".env", "deploy/compose/.env"), extra="ignore")

    pg_dsn: str = "postgresql://adce:adce@localhost:5433/adce"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "adce-neo4j-pass"
    kg_group: str = "adce"  # Graphiti partition; one per tenant in client installs

    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "adce"
    s3_secret_key: str = "adce-s3-pass"
    s3_bucket: str = "adce"
    s3_region: str = "us-east-1"

    # Model access through Google Cloud Vertex AI. "replay" answers from recordings (tests, CI, offline work);
    # "record" calls Vertex and stores the response; "live" calls Vertex only.
    gateway_mode: Literal["replay", "record", "live"] = "replay"
    vertex_project: str | None = None
    vertex_region: str = "global"
    gateway_replay_dir: str = "eval/recordings"

    # Embeddings: "vertex" (Gemini embeddings on Vertex AI) or "hash" (deterministic, offline, not semantic).
    embedder: Literal["vertex", "hash"] = "hash"
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768

    langfuse_host: str | None = None

    # Identity: "off" for local development only; "keycloak" validates bearer tokens against the realm's JWKS.
    auth_mode: Literal["off", "keycloak"] = "off"
    keycloak_issuer: str = "http://localhost:8080/realms/adce"
    keycloak_audience: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
