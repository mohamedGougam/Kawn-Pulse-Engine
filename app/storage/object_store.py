from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger("kawn.object_store")


def _slugify(value: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in value.strip()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "untitled"


def cache_key(source_name: str, topic: str) -> str:
    return f"{settings.r2_key_prefix}/{_slugify(source_name)}/{_slugify(topic)}.json"


class ObjectStoreUnavailable(Exception):
    """Raised when R2 isn't configured or a call fails after retries."""


class ObjectStore:
    """Thin async wrapper around R2 for the per-source JSON cache.

    A single PUT to R2/S3 is atomic from a reader's point of view — readers
    either see the previous full object or the new full object, never a
    partial write. Unlike a local-disk cache, we do NOT need a
    write-to-tmp-then-rename dance here.
    """

    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client_ctx(self):
        if not settings.r2_configured():
            raise ObjectStoreUnavailable("R2 credentials are not configured")

        return self._session.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url(),
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 2}),
        )

    async def get_json(self, source_name: str, topic: str) -> dict[str, Any] | None:
        key = cache_key(source_name, topic)
        try:
            async with self._client_ctx() as client:
                resp = await client.get_object(Bucket=settings.r2_bucket_name, Key=key)
                body = await resp["Body"].read()
            return json.loads(body)
        except ObjectStoreUnavailable:
            raise
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None
            logger.warning("R2 get_json failed for key=%s: %s", key, e)
            return None
        except Exception as e:
            # Cache misses/timeouts should degrade to "no cache", never crash a search.
            logger.warning("R2 get_json failed for key=%s: %s", key, e)
            return None

    async def put_json(
        self,
        source_name: str,
        topic: str,
        items: list[dict[str, Any]],
    ) -> None:
        key = cache_key(source_name, topic)
        payload = {
            "source": source_name,
            "topic": topic,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "items": items,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            async with self._client_ctx() as client:
                await client.put_object(
                    Bucket=settings.r2_bucket_name,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                )
        except ObjectStoreUnavailable:
            raise
        except Exception as e:
            logger.warning("R2 put_json failed for key=%s: %s", key, e)

    async def list_cached_sources(self, topic: str) -> list[str]:
        """Which sources have a cache file for this topic (used to decide
        whether a background refresh is worth queuing)."""
        prefix = f"{settings.r2_key_prefix}/"
        suffix = f"/{_slugify(topic)}.json"
        found: list[str] = []
        try:
            async with self._client_ctx() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=settings.r2_bucket_name, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        k = obj["Key"]
                        if k.endswith(suffix):
                            found.append(k[len(prefix) : -len(suffix)])
        except ObjectStoreUnavailable:
            raise
        except Exception as e:
            logger.warning("R2 list_cached_sources failed for topic=%s: %s", topic, e)
        return found


object_store = ObjectStore()
