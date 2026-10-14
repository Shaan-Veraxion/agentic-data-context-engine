"""Artifact storage over the S3 API: inputs, BI IR, SQL and migration packs.

S3ArtifactStore works with SeaweedFS locally and with GCS (S3 interoperability) or any S3 service in the cloud.
LocalArtifactStore is a directory, for tests and single-machine runs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str) -> list[str]: ...


def put_json(store: ArtifactStore, key: str, obj: Any) -> str:
    return store.put(key, json.dumps(obj, indent=2, sort_keys=True, default=str).encode(), "application/json")


def get_json(store: ArtifactStore, key: str) -> Any:
    return json.loads(store.get(key))


def content_key(prefix: str, data: bytes, suffix: str = "") -> str:
    return f"{prefix.rstrip('/')}/{hashlib.sha256(data).hexdigest()[:20]}{suffix}"


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"key escapes the store: {key}")
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list(self, prefix: str) -> list[str]:
        base = self.root
        return sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("*")
                      if p.is_file() and str(p.relative_to(base)).replace("\\", "/").startswith(prefix))


def s3_client(endpoint: str, access_key: str, secret_key: str, region: str = "us-east-1") -> Any:
    """An S3 client that uses only the given credentials, never the machine's AWS profile or environment."""
    import boto3.session
    import botocore.session
    from botocore.config import Config

    bs = botocore.session.Session()
    bs.set_config_variable("profile", "default")  # ignore AWS_PROFILE pointing at unrelated accounts
    return boto3.session.Session(botocore_session=bs).client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        region_name=region, config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))


class S3ArtifactStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str = "us-east-1") -> None:
        self.bucket = bucket
        self.s3 = s3_client(endpoint, access_key, secret_key, region)
        existing = {b["Name"] for b in self.s3.list_buckets().get("Buckets", [])}
        if bucket not in existing:
            self.s3.create_bucket(Bucket=bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return key

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return sorted(keys)


def from_settings(s: Any) -> S3ArtifactStore:
    return S3ArtifactStore(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_bucket, s.s3_region)
