from pinpipe.credits import Credit, clean_site


def test_trademark_signs_are_removed_from_site_names():
    assert clean_site("Delicious Table \u00ae") == "Delicious Table"
    c = Credit("web", "Delicious Table \u00ae", "https://delicioustable.com/x", "", "https://i/x.jpg", title="Homemade Dog Food")
    assert "\u00ae" not in c.caption_html() and "Delicious Table" in c.caption_html()
    assert "\u2122" not in Credit("web", "Foo\u2122", "https://f.com/x", "", "u").site
