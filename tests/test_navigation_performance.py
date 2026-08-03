import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
ADMIN_LAYOUT = ROOT / "backend" / "app" / "templates" / "admin" / "_layout.html"
USER_DASHBOARD = ROOT / "backend" / "app" / "templates" / "user_dashboard.html"
USER_LIVE_DASHBOARD = (
    ROOT / "backend" / "app" / "templates" / "user_live_dashboard.html"
)
MOBILE_DRAWER = ROOT / "backend" / "app" / "templates" / "_app_shell_mobile_drawer.html"
NAVIGATION_JS = ROOT / "backend" / "app" / "static" / "js" / "admin_navigation.js"
SHELL_ICON = ROOT / "backend" / "app" / "templates" / "_shell_icon.html"
LUCIDE_VENDOR = ROOT / "backend" / "app" / "static" / "vendor" / "lucide-admin.min.js"
LUCIDE_APP_VENDOR = (
    ROOT / "backend" / "app" / "static" / "vendor" / "lucide-app.min.js"
)
LUCIDE_ADMIN_INSTANT_VENDOR = (
    ROOT / "backend" / "app" / "static" / "vendor" / "lucide-admin-instant.min.js"
)
ADMIN_TABLES_JS = ROOT / "backend" / "app" / "static" / "js" / "admin_tables.js"
ADMIN_RENDER_INDEX = ROOT / "backend" / "app" / "templates" / "admin" / "render_index.html"
ADMIN_LIVE_RENDER_INDEX = ROOT / "backend" / "app" / "templates" / "admin" / "live_render_index.html"


def test_sidebar_icons_are_server_rendered_in_all_role_shells() -> None:
    assert SHELL_ICON.exists()
    macro = SHELL_ICON.read_text(encoding="utf-8")
    assert "macro shell_icon" in macro

    admin_layout = ADMIN_LAYOUT.read_text(encoding="utf-8")
    user_dashboard = USER_DASHBOARD.read_text(encoding="utf-8")
    mobile_drawer = MOBILE_DRAWER.read_text(encoding="utf-8")

    assert "shell_icon(item.icon" in admin_layout
    assert "shell_icon(item.icon" in user_dashboard
    assert "shell_icon(item.icon" in mobile_drawer


def test_server_rendered_sidebar_icons_match_bundled_lucide_paths() -> None:
    source = LUCIDE_VENDOR.read_text(encoding="utf-8")
    match = re.search(r"var icons=(\{.*?\});function copyAttrs", source)
    assert match
    bundled_icons = json.loads(match.group(1))
    template_env = Environment(
        loader=FileSystemLoader(str(SHELL_ICON.parent)),
        autoescape=True,
    )
    macro = template_env.get_template(SHELL_ICON.name).module.shell_icon

    for icon_name in (
        "users",
        "server",
        "video",
        "radio",
        "clapperboard",
        "radio-tower",
        "link",
        "layers",
    ):
        svg = ET.fromstring(str(macro(icon_name, "h-4 w-4")))
        actual = [
            (child.tag.rsplit("}", 1)[-1], dict(child.attrib))
            for child in svg
        ]
        expected = [(tag, attrs) for tag, attrs in bundled_icons[icon_name]]
        assert actual == expected


def test_navigation_prefetch_uses_browser_document_cache() -> None:
    source = NAVIGATION_JS.read_text(encoding="utf-8")

    assert 'link.rel = "prefetch"' in source
    assert 'link.as = "document"' in source
    assert "requestIdleCallback" in source
    assert "new Map()" not in source


def test_navigation_click_updates_active_state_without_fading_content() -> None:
    source = NAVIGATION_JS.read_text(encoding="utf-8")

    assert "setPendingNavigationState(link)" in source
    assert 'item.classList.toggle("active", active)' in source
    assert 'link.matches(".module-tab")' in source
    assert '"pointerdown"' in source
    assert "admin-fast-nav-loading" not in source


def test_all_main_shells_use_local_immediate_icon_runtime() -> None:
    assert LUCIDE_APP_VENDOR.exists()
    assert LUCIDE_ADMIN_INSTANT_VENDOR.exists()
    user_runtime = LUCIDE_APP_VENDOR.read_text(encoding="utf-8")
    admin_runtime = LUCIDE_ADMIN_INSTANT_VENDOR.read_text(encoding="utf-8")

    for runtime in (user_runtime, admin_runtime):
        assert "new MutationObserver" in runtime
        assert "data-lucide-rendered" in runtime
        assert "window.hydrateLucideIcons" in runtime

    admin_template = ADMIN_LAYOUT.read_text(encoding="utf-8")
    assert "vendor/lucide-admin-instant.min.js" in admin_template
    assert "vendor/lucide-app.min.js" not in admin_template

    for template_path in (USER_DASHBOARD, USER_LIVE_DASHBOARD):
        template = template_path.read_text(encoding="utf-8")
        assert "vendor/lucide-app.min.js" in template
        assert "unpkg.com/lucide@latest" not in template


def test_admin_instant_runtime_keeps_exact_existing_icon_paths() -> None:
    original = LUCIDE_VENDOR.read_text(encoding="utf-8")
    instant = LUCIDE_ADMIN_INSTANT_VENDOR.read_text(encoding="utf-8")
    original_match = re.search(r"var icons=(\{.*?\});function copyAttrs", original)
    instant_match = re.search(r"var icons=(\{.*?\});var NS=", instant)

    assert original_match
    assert instant_match
    assert json.loads(instant_match.group(1)) == json.loads(original_match.group(1))


def test_polling_reconciles_rows_instead_of_rebuilding_whole_tables() -> None:
    worker_template = (
        ROOT / "backend" / "app" / "templates" / "admin" / "worker_index.html"
    ).read_text(encoding="utf-8")
    user_dashboard_js = (
        ROOT / "backend" / "app" / "static" / "js" / "user_dashboard.js"
    ).read_text(encoding="utf-8")
    user_live_dashboard = USER_LIVE_DASHBOARD.read_text(encoding="utf-8")

    assert "reconcileWorkerRows(items)" in worker_template
    assert "tbody.innerHTML = items.map(buildWorkerRowMarkup).join('')" not in worker_template
    assert "reconcileRenderRows(pageJobs, start)" in user_dashboard_js
    assert "renderTableBody.innerHTML = pageJobs" not in user_dashboard_js
    assert "reconcileLiveRows(liveRows)" in user_live_dashboard
    assert (
        "liveStreamTableBody.innerHTML = liveRows.map((stream) => buildLiveRowMarkup(stream)).join('')"
        not in user_live_dashboard
    )


def test_admin_history_tables_use_server_side_pagination_and_search() -> None:
    upload_template = ADMIN_RENDER_INDEX.read_text(encoding="utf-8")
    live_template = ADMIN_LIVE_RENDER_INDEX.read_text(encoding="utf-8")
    table_runtime = ADMIN_TABLES_JS.read_text(encoding="utf-8")

    for template in (upload_template, live_template):
        assert 'data-admin-server-pagination="true"' in template
        assert 'data-current-page="{{ dashboard.history_pagination.page }}"' in template
        assert 'data-total-pages="{{ dashboard.history_pagination.total_pages }}"' in template
        assert 'data-search-query="{{ dashboard.history_pagination.query | e }}"' in template

    assert 'table.dataset.adminServerPagination === "true"' in table_runtime
    assert 'url.searchParams.set("page", String(page))' in table_runtime
    assert 'url.searchParams.set("q", keyword)' in table_runtime
