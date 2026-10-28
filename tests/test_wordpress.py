import sys

import pytest
import requests

from pinpipe import main as main_mod
from pinpipe import wordpress
from pinpipe.wordpress import WordPress, WordPressUnavailable


class Resp:
    def __init__(self, status=200, js=None):
        self.status_code, self._js = status, js or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._js


def _wp(monkeypatch):
    monkeypatch.setenv("WP_URL", "https://site.test")
    monkeypatch.setenv("WP_USER", "u")
    monkeypatch.setenv("WP_APP_PASSWORD", "p")
    monkeypatch.setattr("pinpipe.utils.time.sleep", lambda s: None)
    return WordPress()


def test_unreachable_site_is_reported_as_wordpress_problem(monkeypatch):
    wp = _wp(monkeypatch)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("Network is unreachable")

    monkeypatch.setattr(wordpress.requests, "request", boom)
    with pytest.raises(WordPressUnavailable, match="Cannot reach"):
        wp.preflight()
    assert len(calls) == 4
    calls.clear()
    with pytest.raises(WordPressUnavailable):
        wp.upload_media(b"x", "a.jpg", "alt", "cap")
    assert len(calls) == 6


def test_bad_login_stops_immediately(monkeypatch):
    wp = _wp(monkeypatch)
    calls = []

    def unauthorized(*a, **k):
        calls.append(1)
        return Resp(401)

    monkeypatch.setattr(wordpress.requests, "request", unauthorized)
    with pytest.raises(WordPressUnavailable, match="refused"):
        wp.preflight()
    assert len(calls) == 1


def test_preflight_ok_and_permission_check(monkeypatch):
    wp = _wp(monkeypatch)
    monkeypatch.setattr(wordpress.requests, "request", lambda *a, **k: Resp(200, {"name": "Editor", "capabilities": {"edit_posts": True, "upload_files": True}}))
    assert wp.preflight() == "Editor"
    monkeypatch.setattr(wordpress.requests, "request", lambda *a, **k: Resp(200, {"name": "Sub", "capabilities": {"edit_posts": True, "upload_files": False}}))
    with pytest.raises(WordPressUnavailable, match="upload_files"):
        wp.preflight()


def _run_main(monkeypatch, tmp_path, wp_cls, process):
    (tmp_path / "keywords.txt").write_text("blue kitchens\n")
    cfg = {
        "costs": {"max_cost_per_run_usd": 5},
        "images": {"max_total_uses": 2, "cooldown_days": 30},
        "wordpress": {},
        "posts_per_day": 1,
        "max_retries": 3,
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "BrightData", lambda c, k: object())
    monkeypatch.setattr(main_mod, "LLM", lambda c, k: object())
    monkeypatch.setattr(main_mod, "WordPress", wp_cls)
    monkeypatch.setattr(main_mod, "process_keyword", process)
    monkeypatch.setattr(sys, "argv", ["pinpipe", "--keywords", str(tmp_path / "keywords.txt")])
    with pytest.raises(SystemExit) as e:
        main_mod.main()
    return e.value.code


def test_wordpress_down_at_start_spends_nothing(monkeypatch, tmp_path):
    class DownWP:
        def preflight(self):
            raise WordPressUnavailable("site unreachable")

    called = []
    code = _run_main(monkeypatch, tmp_path, DownWP, lambda *a, **k: called.append(1))
    assert code == 1 and called == []


def test_wordpress_failure_mid_run_keeps_the_keywords_retries(monkeypatch, tmp_path):
    class OkWP:
        def preflight(self):
            return "me"

    def fails(*a, **k):
        raise WordPressUnavailable("upload failed")

    code = _run_main(monkeypatch, tmp_path, OkWP, fails)
    assert code == 1
    state_file = tmp_path / "state.json"
    assert not state_file.exists() or "blue kitchens" not in state_file.read_text()


def test_normal_keyword_failure_uses_a_retry(monkeypatch, tmp_path):
    import json

    class OkWP:
        def preflight(self):
            return "me"

    def fails(*a, **k):
        raise RuntimeError("not enough images")

    code = _run_main(monkeypatch, tmp_path, OkWP, fails)
    assert code == 1
    assert json.loads((tmp_path / "state.json").read_text())["retries"]["blue kitchens"] == 1
