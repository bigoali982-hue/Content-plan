import threading

import pytest

from pinpipe import brightdata
from pinpipe.brightdata import BrightData
from pinpipe.utils import BudgetExceeded, CostTracker, Deadline


def test_cost_tracker_is_thread_safe_and_enforces_cap():
    cost = CostTracker(1000)
    threads = [threading.Thread(target=lambda: [cost.add("x", 0.01) for _ in range(200)]) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert abs(cost.total - 16.0) < 1e-6
    with pytest.raises(BudgetExceeded):
        CostTracker(0.01).add("x", 0.02)


def test_deadline():
    assert not Deadline(0).expired()
    d = Deadline(60)
    assert not d.expired()


class FakeResp:
    def __init__(self, text, js=None):
        self.text, self._js = text, js

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


def _bd(monkeypatch, responses):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    monkeypatch.setenv("BRIGHTDATA_SERP_ZONE", "z")
    monkeypatch.setenv("BRIGHTDATA_UNLOCKER_ZONE", "u")
    bd = BrightData({"costs": {"brightdata_serp_per_request": 0.001, "brightdata_unlocker_per_request": 0.001, "brightdata_dataset_per_record": 0.001}}, CostTracker(5))
    it = iter(responses)
    monkeypatch.setattr(bd, "_request", lambda zone, url: next(it))
    waits = []
    monkeypatch.setattr(brightdata.time, "sleep", lambda s: waits.append(s))
    return bd, waits


def test_serp_waits_out_the_lockout(monkeypatch):
    bd, waits = _bd(monkeypatch, [FakeResp(""), FakeResp("This query recently failed and cannot be attempted at this time."), FakeResp("ok", {"organic": [1]})])
    assert bd.serp("q") == {"organic": [1]}
    assert waits == [brightdata.LOCKOUT_WAIT, brightdata.LOCKOUT_WAIT]


def test_serp_gives_up_after_three_attempts(monkeypatch):
    bd, waits = _bd(monkeypatch, [FakeResp("")] * 3)
    assert bd.serp("q") == {}
    assert len(waits) == 2


def test_fetch_many_returns_none_for_failures_and_reraises_budget(monkeypatch):
    bd, _ = _bd(monkeypatch, [])

    def fake_fetch(url):
        if "bad" in url:
            raise RuntimeError("boom")
        return "<html>" + url + "</html>"

    monkeypatch.setattr(bd, "fetch_html", fake_fetch)
    out = bd.fetch_html_many(["https://a.com/1", "https://bad.com/2"], workers=2)
    assert out["https://a.com/1"].startswith("<html>") and out["https://bad.com/2"] is None

    def budget(url):
        raise BudgetExceeded("cap")

    monkeypatch.setattr(bd, "fetch_html", budget)
    with pytest.raises(BudgetExceeded):
        bd.fetch_html_many(["https://a.com/1"], workers=1)
