from backend.app.store import AppStore


def test_user_avatar_initials_skip_invisible_prefix_characters() -> None:
    assert AppStore._initials("\u200bm-user1") == "M"
    assert AppStore._initials("\ufeffmanager2") == "M"
    assert AppStore._initials("  \u200b-user") == "U"
