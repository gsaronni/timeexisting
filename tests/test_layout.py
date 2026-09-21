from timeexisting.ui.layout import build_layout
from timeexisting.ui.theme import load_theme

THEME = load_theme()


def test_layout_has_the_expected_regions():
    layout = build_layout(THEME)
    assert layout["header"] is not None
    assert layout["main"]["left"]["year"] is not None
    assert layout["main"]["left"]["week"] is not None
    assert layout["main"]["right"] is not None
    assert layout["footer"] is not None


def test_building_the_layout_twice_does_not_raise():
    build_layout(THEME)
    build_layout(THEME)
