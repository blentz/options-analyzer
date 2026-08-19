"""Regression tests for N1: a batch-level scraper failure must record the
real cause against every due contract, and a failing close() must not
discard results already collected.

Before this fix, `_download_batch` used `with StockNearScraper() as
scraper:`. start() does a real page.goto auth probe; if it raised (missing
Firefox, bad profile path, slow site), __exit__ never ran (leaking a
Playwright driver process) and the exception propagated straight out of
the function, discarding `results` and surfacing as a bare 500 with
nothing recorded for any contract -- contradicting the spec's promise
that failures record per contract and surface in the UI.

`app.stocknear.StockNearScraper` is monkeypatched rather than imported
directly, matching the deferred import inside `_download_batch` (`from
app.stocknear import StockNearScraper`), which resolves the name from the
module at call time.
"""

from pathlib import Path

from app.services.contract_history import _download_batch


class _ExplodingScraper:
    """Fails inside start(), like a missing Firefox binary or bad profile path."""

    def __init__(self, *a, **kw):
        self.closed = False

    def start(self):
        raise RuntimeError("Firefox executable not found")

    def close(self):
        self.closed = True


def test_batch_start_failure_records_every_job(monkeypatch, tmp_path):
    monkeypatch.setattr("app.stocknear.StockNearScraper", _ExplodingScraper)

    jobs = [("AAA", "AAA261016P00001000"), ("BBB", "BBB261016P00002000")]
    results = _download_batch(jobs, tmp_path)

    assert set(results.keys()) == {"AAA261016P00001000", "BBB261016P00002000"}
    for exc in results.values():
        assert isinstance(exc, RuntimeError)
        assert "Firefox executable not found" in str(exc)


def test_batch_start_failure_still_closes_scraper(monkeypatch, tmp_path):
    scrapers = []

    class TrackingExplodingScraper(_ExplodingScraper):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            scrapers.append(self)

    monkeypatch.setattr("app.stocknear.StockNearScraper", TrackingExplodingScraper)

    _download_batch([("AAA", "AAA261016P00001000")], tmp_path)

    assert len(scrapers) == 1
    assert scrapers[0].closed is True


def test_batch_close_failure_does_not_discard_successful_results(monkeypatch, tmp_path):
    """A raising close() after a successful batch must not discard results
    built before teardown -- unlike a `with` block, where a raising
    __exit__ propagates before the caller's `return results` is reached.
    """

    class _FlakyCloseScraper:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def download_contract_history(self, symbol, occ, dest_dir):
            return Path(dest_dir) / f"{occ}.csv"

        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr("app.stocknear.StockNearScraper", _FlakyCloseScraper)

    jobs = [("AAA", "AAA261016P00001000")]
    results = _download_batch(jobs, tmp_path)

    assert results["AAA261016P00001000"] == tmp_path / "AAA261016P00001000.csv"
