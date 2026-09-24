"""Static assets must be revalidated so a rebuilt container's JS reaches users."""

from fastapi.testclient import TestClient

from app.main import app


def test_static_files_are_revalidated():
    res = TestClient(app).get("/static/risk.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"


def test_page_loads_bokehjs_matching_the_python_package():
    import bokeh

    html = TestClient(app).get("/import").text
    assert f"bokeh-{bokeh.__version__}.min.js" in html
