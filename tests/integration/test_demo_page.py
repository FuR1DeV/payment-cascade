"""The demo page is static, talks only to the public API and can be switched off."""

import httpx

from app.main import create_app
from app.modules.payments.bootstrap import build_container
from tests.fakes import FakeProvider, RecordingJobs

from .helpers import make_settings


async def test_demo_page_is_served(client: httpx.AsyncClient) -> None:
    page = await client.get("/demo", follow_redirects=True)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert 'src="demo.js"' in page.text

    script = await client.get("/demo/demo.js")
    assert script.status_code == 200
    for endpoint in ('"/payments"', "/alerts?payment_id=", '"/stats/providers"'):
        assert endpoint in script.text
    # API data goes into the page as text only.
    assert "innerHTML" not in script.text


async def test_demo_page_can_be_switched_off(
    alpha: FakeProvider, beta: FakeProvider, gamma: FakeProvider, jobs: RecordingJobs
) -> None:
    settings = make_settings(demo_page_enabled=False)
    container = await build_container(settings, providers=[alpha, beta, gamma], jobs=jobs)
    try:
        app = create_app(container)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/demo/")).status_code == 404
            assert (await client.get("/health")).status_code == 200
    finally:
        await container.close()
