"""The demo server must show the photos you upload to it."""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


async def test_an_uploaded_photo_link_on_the_demo_server_actually_serves_the_image():
    import demo_server
    app = await demo_server.make_app()
    container = app.state.container if hasattr(app.state, "container") else None
    storage = container.media if container else None
    assert storage is not None, "demo container not reachable"
    url = await storage.put("tenant-x/photo.jpg", b"\xff\xd8\xffJPEGDATA", "image/jpeg")
    assert url == f"http://127.0.0.1:{demo_server.PORT}/demo-media/tenant-x/photo.jpg"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/demo-media/tenant-x/photo.jpg")
        missing = await c.get("/demo-media/tenant-x/nope.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8\xffJPEGDATA" and r.headers["content-type"] == "image/jpeg"
    assert missing.status_code == 404
