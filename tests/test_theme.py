from timeexisting.ui.theme import load_theme


def test_grimdark_palette_loads_every_role():
    theme = load_theme()
    assert theme.header_border == "yellow"
    assert theme.danger == "bold red"


def test_weekday_colors_keyed_monday_zero_through_sunday_six():
    theme = load_theme()
    assert set(theme.weekday) == set(range(7))
    assert theme.weekday[0] == "red"  # Monday
    assert theme.weekday[6] == "blue"  # Sunday


def test_unknown_palette_falls_back_to_grimdark():
    theme = load_theme("does-not-exist")
    assert theme == load_theme("grimdark")


def test_progress_color_thresholds():
    theme = load_theme()
    assert theme.progress_color(0) == theme.progress_low
    assert theme.progress_color(32) == theme.progress_low
    assert theme.progress_color(33) == theme.progress_mid
    assert theme.progress_color(65) == theme.progress_mid
    assert theme.progress_color(66) == theme.progress_high
    assert theme.progress_color(100) == theme.progress_high
