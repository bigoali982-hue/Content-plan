from datetime import date

from pinpipe.state import ImageUsage, State


def test_keywords_retry_first_and_done_skipped(tmp_path):
    kw = tmp_path / "k.txt"
    kw.write_text("a\nb\n# comment\n\nc\nd\n")
    st = State(str(tmp_path / "s.json"))
    st.mark_done("a", "http://x")
    st.mark_failed("c", "boom")
    assert st.next_keywords(str(kw), 2) == ["c", "b"]


def test_keyword_dropped_after_max_retries(tmp_path):
    kw = tmp_path / "k.txt"
    kw.write_text("a\nb\n")
    st = State(str(tmp_path / "s.json"))
    for _ in range(3):
        st.mark_failed("a", "x")
    assert st.next_keywords(str(kw), 5) == ["b"]


def test_state_persists(tmp_path):
    path = str(tmp_path / "s.json")
    State(path).mark_done("a", "u")
    assert "a" in State(path).data["done"]


def test_image_usage_limits(tmp_path):
    u = ImageUsage(str(tmp_path / "u.json"), max_total_uses=2, cooldown_days=30)
    url = "https://site.com/p/img.jpg?x=1"
    assert u.can_use(url)
    u.record(url, date(2026, 1, 1))
    assert not u.can_use(url, date(2026, 1, 10))
    assert u.can_use("https://site.com/p/img.jpg?x=2", date(2026, 2, 5))
    u.record(url, date(2026, 2, 5))
    assert not u.can_use(url, date(2026, 6, 1))
