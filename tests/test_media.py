"""Uploads: the file's own bytes decide what it is, photos are shrunk and cleaned, each client's files stay in their own folder,
and one client can never upload into another's."""
import io

import httpx
import pytest
from PIL import Image

from hooze.media.processing import MB, MediaError, process, sniff
from hooze.media.storage import MediaStorageError, MemoryMediaStorage, SupabaseMediaStorage
from tests.conftest import food_config, make_world
from tests.test_api import PLATFORM, client, container

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def jpeg(w=4000, h=3000, exif=False) -> bytes:
    img = Image.new("RGB", (w, h), (200, 60, 30))
    out = io.BytesIO()
    if exif:
        ex = Image.Exif(); ex[0x010F] = "SecretPhoneMaker"; ex[0x0112] = 6        # make + "rotate 90°"
        img.save(out, "JPEG", exif=ex)
    else:
        img.save(out, "JPEG")
    return out.getvalue()


# ── processing ─────────────────────────────────────────────────────────
def test_photos_are_shrunk_rotated_upright_and_stripped_of_metadata():
    out = process(jpeg(4000, 3000, exif=True), max_px=1600)
    img = Image.open(io.BytesIO(out.data))
    assert out.kind == "image" and out.ext == "jpg" and out.content_type == "image/jpeg"
    assert max(img.size) == 1600 and img.size == (1200, 1600)                  # 6 = rotated 90°: 4000x3000 became portrait, then shrunk
    assert not img.getexif() and len(out.data) < out.original_bytes


def test_png_with_transparency_becomes_jpeg_on_white_not_black():
    png = io.BytesIO(); Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(png, "PNG")
    out = process(png.getvalue())
    assert Image.open(io.BytesIO(out.data)).getpixel((5, 5))[0] > 240


def test_the_bytes_decide_not_the_extension_and_every_rejection_explains_itself():
    assert sniff(b"MZ\x90\x00 pretend.jpg") is None
    for data, word in [(b"hello world this is text", "Only photos"), (b"\x00\x00\x00\x18ftypheic" + b"0" * 20, "HEIC"),
                       (b"\x00\x00\x00\x14ftypqt  " + b"0" * 20, ".mov"), (b"", "empty"), (b"\xff\xd8\xff\xe0 not really a jpeg", "damaged")]:
        with pytest.raises(MediaError, match=word):
            process(data)


def test_size_limits_are_enforced_per_kind_with_the_real_numbers():
    with pytest.raises(MediaError, match="limit is 1 MB"):
        process(jpeg(3000, 3000) + b"\0" * (2 * MB), max_image_mb=1)
    with pytest.raises(MediaError, match="16 MB"):
        process(MP4 + b"\0" * (17 * MB))
    assert process(MP4).kind == "video" and process(MP4).data == MP4            # videos are validated, not re-encoded


# ── endpoint ───────────────────────────────────────────────────────────
async def _tenant(c, slug):
    t = (await c.post("/v1/platform/tenants", json={"slug": slug, "name": slug}, headers={"x-api-key": PLATFORM})).json()
    return t["tenant"]["id"], {"x-api-key": t["api_key"]}


async def test_upload_returns_a_public_link_in_the_clients_own_folder_and_isolates_tenants():
    w = await make_world(food_config())
    cont = container(w); cont.media = MemoryMediaStorage()
    async with client(cont) as c:
        a, ka = await _tenant(c, "alpha")
        b, kb = await _tenant(c, "beta")
        r = await c.post(f"/v1/tenants/{a}/media", files={"file": ("Jollof Rice.PNG", jpeg(2400, 1800), "image/png")}, headers=ka)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "image" and body["url"].startswith(f"https://media.test/public/{a}/") and body["url"].endswith(".jpg")
        path = body["url"].split("/public/")[1]
        assert path in cont.media.files and cont.media.files[path][1] == "image/jpeg"
        assert (await c.post(f"/v1/tenants/{a}/media", files={"file": ("x.jpg", jpeg(100, 100), "image/jpeg")}, headers=kb)).status_code == 403   # B can't write into A
        assert (await c.post(f"/v1/tenants/{a}/media", files={"file": ("x.jpg", jpeg(100, 100), "image/jpeg")})).status_code == 401
        v = await c.post(f"/v1/tenants/{a}/media", files={"file": ("tour.mp4", MP4, "video/mp4")}, headers={"x-api-key": PLATFORM})
        assert v.status_code == 200 and v.json()["type"] == "video" and v.json()["url"].endswith(".mp4")
        names = {u.split("/")[-1] for u in cont.media.files}
        assert len(names) == len(cont.media.files) == 2                           # random names: nothing overwrites anything


async def test_upload_rejects_bad_files_with_a_clear_message_and_missing_storage_is_a_clear_503():
    w = await make_world(food_config())
    cont = container(w); cont.media = MemoryMediaStorage()
    async with client(cont) as c:
        t, k = await _tenant(c, "gamma")
        r = await c.post(f"/v1/tenants/{t}/media", files={"file": ("menu.jpg", b"%PDF-1.4 definitely not a jpg", "image/jpeg")}, headers=k)
        assert r.status_code == 422 and "Only photos" in r.json()["detail"] and not cont.media.files
        big = await c.post(f"/v1/tenants/{t}/media", files={"file": ("big.jpg", b"\xff\xd8\xff" + b"\0" * (17 * MB), "image/jpeg")}, headers=k)
        assert big.status_code == 413
        cont.media = None
        none = await c.post(f"/v1/tenants/{t}/media", files={"file": ("a.jpg", jpeg(50, 50), "image/jpeg")}, headers=k)
        assert none.status_code == 503 and "SUPABASE_URL" in none.json()["detail"]


# ── Supabase storage client ────────────────────────────────────────────
async def test_supabase_storage_creates_a_public_bucket_once_and_uploads_with_the_right_headers():
    seen = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.headers)))
        return httpx.Response(200, json={})
    st = SupabaseMediaStorage("https://abc.supabase.co/", "sb_secret_xyz", "media", transport=httpx.MockTransport(handler))
    url = await st.put("t1/a.jpg", b"DATA", "image/jpeg")
    await st.put("t1/b.jpg", b"DATA", "image/jpeg")
    assert url == "https://abc.supabase.co/storage/v1/object/public/media/t1/a.jpg"
    assert [p for _, p, _ in seen] == ["/storage/v1/bucket", "/storage/v1/object/media/t1/a.jpg", "/storage/v1/object/media/t1/b.jpg"]   # bucket ensured ONCE
    assert all(h["apikey"] == "sb_secret_xyz" and "authorization" not in h for _, _, h in seen)       # new-style keys go in apikey only
    legacy = SupabaseMediaStorage("https://abc.supabase.co", "eyJhbGciOi", "media", transport=httpx.MockTransport(handler))
    await legacy.put("t/a.jpg", b"x", "image/jpeg")
    assert seen[-1][2]["authorization"] == "Bearer eyJhbGciOi"


async def test_supabase_storage_tolerates_an_existing_bucket_and_reports_real_failures():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/bucket"):
            return httpx.Response(400, json={"error": "Duplicate", "message": "The resource already exists"})
        return httpx.Response(403, text="new row violates row-level security policy")
    st = SupabaseMediaStorage("https://abc.supabase.co", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(MediaStorageError, match="403"):
        await st.put("t/a.jpg", b"x", "image/jpeg")
