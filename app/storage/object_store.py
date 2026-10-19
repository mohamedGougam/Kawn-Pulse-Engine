from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
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

    One S3 client is created lazily and shared by every call, and the number
    of in-flight requests is capped (settings.r2_max_concurrency). The
    previous version built a brand-new client for each get/put. A single
    topic refresh makes ~15 cache reads and ~15 writes at once, so a handful
    of concurrent refreshes meant well over a hundred live clients - measured
    at roughly 0.8 MB each and not given back to the OS afterwards, which is
    what pushed the instance past its 512 MB limit.
    """

    def __init__(self) -> None:
        self._session = aioboto3.Session()
        self._stack: AsyncExitStack | None = None
        self._client = None
        # asyncio primitives are bound to the loop that first uses them, so
        # they (and the client) are rebuilt if the running loop ever changes.
        # In production there is one loop for the process lifetime; this only
        # matters for test runners that create a loop per test.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._init_lock: asyncio.Lock | None = None
        self._slots: asyncio.Semaphore | None = None

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._init_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max(1, settings.r2_max_concurrency))
        # A client from another loop can't be used (or cleanly closed) here.
        self._client = None
        self._stack = None

    async def _get_client(self):
        if not settings.r2_configured():
            raise ObjectStoreUnavailable("R2 credentials are not configured")
        self._bind_loop()
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is None:
                stack = AsyncExitStack()
                self._client = await stack.enter_async_context(
                    self._session.client(
                        "s3",
                        endpoint_url=settings.r2_endpoint_url(),
                        aws_access_key_id=settings.r2_access_key_id,
                        aws_secret_access_key=settings.r2_secret_access_key,
                        config=BotoConfig(
                            signature_version="s3v4",
                            retries={"max_attempts": 2},
                            # Bounded so a stalled R2 can't pin tasks (and
                            # their buffers) for botocore's 60s defaults.
                            connect_timeout=5,
                            read_timeout=10,
                            max_pool_connections=max(1, settings.r2_max_concurrency),
                        ),
                    )
                )
                self._stack = stack
        return self._client

    async def _reset_client(self) -> None:
        """Drop the shared client so the next call builds a fresh one."""
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        """Close the shared client (app shutdown)."""
        await self._reset_client()

    async def get_json(self, source_name: str, topic: str) -> dict[str, Any] | None:
        key = cache_key(source_name, topic)
        try:
            client = await self._get_client()
            async with self._slots:
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
        except RuntimeError as e:
            # e.g. the shared client's session was closed underneath us.
            logger.warning("R2 get_json failed for key=%s, resetting client: %s", key, e)
            await self._reset_client()
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
            client = await self._get_client()
            async with self._slots:
                await client.put_object(
                    Bucket=settings.r2_bucket_name,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                )
        except ObjectStoreUnavailable:
            raise
        except RuntimeError as e:
            logger.warning("R2 put_json failed for key=%s, resetting client: %s", key, e)
            await self._reset_client()
        except Exception as e:
            logger.warning("R2 put_json failed for key=%s: %s", key, e)

    async def list_cached_sources(self, topic: str) -> list[str]:
        """Which sources have a cache file for this topic (used to decide
        whether a background refresh is worth queuing)."""
        prefix = f"{settings.r2_key_prefix}/"
        suffix = f"/{_slugify(topic)}.json"
        found: list[str] = []
        try:
            client = await self._get_client()
            async with self._slots:
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
