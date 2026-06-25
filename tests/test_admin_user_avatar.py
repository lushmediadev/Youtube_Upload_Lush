from pathlib import Path

from backend.app.store import AppStore


ROOT = Path(__file__).resolve().parents[1]


def test_user_avatar_initials_skip_invisible_prefix_characters() -> None:
    assert AppStore._initials("\u200bm-user1") == "M"
    assert AppStore._initials("\ufeffmanager2") == "M"
    assert AppStore._initials("  \u200b-user") == "U"


def test_admin_static_css_contains_all_avatar_palette_backgrounds() -> None:
    source = (ROOT / "backend" / "app" / "store.py").read_text(encoding="utf-8")
    css = (ROOT / "backend" / "app" / "static" / "css" / "admin-tailwind.css").read_text(encoding="utf-8")
    config = (ROOT / "tailwind.admin.config.js").read_text(encoding="utf-8")

    palette_classes = sorted(set(source.split('palettes = [', 1)[1].split(']', 1)[0].split('"')[1::2]))

    for class_group in palette_classes:
        background_class = class_group.split()[0]
        assert f'"{background_class}"' in config
        assert f".{background_class}" in css or f".{background_class.replace('-', r'\\-')}" in css
