"""Where uploaded media lives: Supabase Storage (a PUBLIC bucket, because WhatsApp's servers must be able to fetch the file by URL).

The database keeps only the URL string; the bytes live here, one folder per tenant: `<tenant_id>/<random>.jpg`. Random names make
URLs unguessable and mean two uploads never overwrite each other.
"""
from __future__ import annotations
import httpx


class MediaStorageError(RuntimeError):
    pass


class SupabaseMediaStorage:
    def __init__(self, supabase_url: str, service_key: str, bucket: str = "media", max_video_mb: int = 16, transport=None):
        self.base = supabase_url.rstrip("/")
        self.bucket = bucket
        self.max_mb = max_video_mb
        self._headers = {"apikey": service_key}
        if service_key.startswith("eyJ"):                       # legacy JWT-style keys also go in Authorization; new sb_secret_ keys must not
            self._headers["Authorization"] = f"Bearer {service_key}"
        self._transport = transport
        self._ready = False

    def public_url(self, path: str) -> str:
        return f"{self.base}/storage/v1/object/public/{self.bucket}/{path}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60.0, headers=self._headers, transport=self._transport)

    async def _ensure_bucket(self) -> None:
        if self._ready:
            return
        async with self._client() as c:
            r = await c.post(f"{self.base}/storage/v1/bucket", json={"id": self.bucket, "name": self.bucket, "public": True,
                                                                        "file_size_limit": self.max_mb * 1024 * 1024 + 1024})
        if r.status_code >= 400 and "exist" not in r.text.lower() and r.status_code != 409:
            raise MediaStorageError(f"Could not create the storage bucket ({r.status_code}): {r.text[:160]}")
        self._ready = True

    async def put(self, path: str, data: bytes, content_type: str) -> str:
        await self._ensure_bucket()
        async with self._client() as c:
            r = await c.post(f"{self.base}/storage/v1/object/{self.bucket}/{path}", content=data,
                             headers={"content-type": content_type, "x-upsert": "false", "cache-control": "max-age=31536000"})
        if r.status_code >= 400:
            raise MediaStorageError(f"Storage rejected the upload ({r.status_code}): {r.text[:160]}")
        return self.public_url(path)


class MemoryMediaStorage:
    """Tests and the offline demo."""
    def __init__(self, base: str = "https://media.test/public"):
        self.base, self.files = base, {}

    def public_url(self, path: str) -> str:
        return f"{self.base}/{path}"

    async def put(self, path: str, data: bytes, content_type: str) -> str:
        self.files[path] = (data, content_type)
        return self.public_url(path)
