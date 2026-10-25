"""Where uploaded media lives: Supabase Storage (a PUBLIC bucket, because WhatsApp's servers must be able to fetch the file by URL).

The database keeps only the URL string; the bytes live here, one folder per tenant: `<tenant_id>/<random>.jpg`. Random names make
URLs unguessable and mean two uploads never overwrite each other.
"""
from __future__ import annotations
from datetime import datetime, timezone
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


    async def list_files(self, prefix: str) -> list[dict]:
        """Every file directly under `prefix/` as [{"path", "created_at"}]. Pages through the whole folder."""
        out, offset = [], 0
        async with self._client() as c:
            while True:
                r = await c.post(f"{self.base}/storage/v1/object/list/{self.bucket}",
                                 json={"prefix": prefix, "limit": 1000, "offset": offset, "sortBy": {"column": "created_at", "order": "asc"}})
                if r.status_code == 404:                           # bucket not created yet => nothing stored
                    return out
                if r.status_code >= 400:
                    raise MediaStorageError(f"Could not list files ({r.status_code}): {r.text[:160]}")
                page = r.json() or []
                out += [{"path": f"{prefix}/{o['name']}", "created_at": o.get("created_at")} for o in page if o.get("id")]     # id is null for sub-folders
                if len(page) < 1000:
                    return out
                offset += 1000

    async def delete(self, paths: list[str]) -> int:
        """Delete these exact files. Returns how many the server reports deleted."""
        if not paths:
            return 0
        done = 0
        async with self._client() as c:
            for i in range(0, len(paths), 100):
                r = await c.request("DELETE", f"{self.base}/storage/v1/object/{self.bucket}", json={"prefixes": paths[i:i + 100]})
                if r.status_code >= 400:
                    raise MediaStorageError(f"Could not delete files ({r.status_code}): {r.text[:160]}")
                done += len(r.json() or [])
        return done


class MemoryMediaStorage:
    """Tests and the offline demo."""
    def __init__(self, base: str = "https://media.test/public"):
        self.base, self.files, self.created = base, {}, {}

    def public_url(self, path: str) -> str:
        return f"{self.base}/{path}"

    async def put(self, path: str, data: bytes, content_type: str) -> str:
        self.files[path] = (data, content_type)
        self.created.setdefault(path, datetime.now(timezone.utc).isoformat())
        return self.public_url(path)

    async def list_files(self, prefix: str) -> list[dict]:
        return [{"path": p, "created_at": self.created.get(p)} for p in sorted(self.files) if p.startswith(prefix + "/")]

    async def delete(self, paths: list[str]) -> int:
        gone = [p for p in paths if p in self.files]
        for p in gone:
            self.files.pop(p, None); self.created.pop(p, None)
        return len(gone)
