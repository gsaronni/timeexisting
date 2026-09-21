from importlib.resources import files

from timeexisting.content.art import ART_DIR, MANIFEST, load_art, missing_art


def test_every_manifest_entry_resolves_to_a_file():
    art_dir = files("timeexisting").joinpath(ART_DIR)
    for key, filename in MANIFEST.items():
        assert art_dir.joinpath(filename).is_file(), f"{key} -> {filename} does not exist"


def test_every_manifest_entry_loads_nonempty_text():
    for key in MANIFEST:
        assert load_art(key).strip()


def test_every_file_on_disk_is_in_the_manifest():
    art_dir = files("timeexisting").joinpath(ART_DIR)
    on_disk = {entry.name for entry in art_dir.iterdir() if entry.is_file()}
    assert on_disk == set(MANIFEST.values())


def test_missing_art_is_empty_for_a_clean_manifest():
    assert missing_art() == []
