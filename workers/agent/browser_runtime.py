from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_snap_chromium(chromium_bin: str) -> bool:
    """Detect whether the resolved chromium binary is a snap wrapper."""
    resolved = shutil.which(chromium_bin) or chromium_bin
    try:
        real = str(Path(resolved).resolve())
    except OSError:
        real = resolved
    if "/snap/" in real:
        return True
    try:
        head = Path(resolved).read_bytes()[:512]
        if b"snap" in head and (b"chromium" in head or b"CHROMIUM" in head):
            return True
    except OSError:
        pass
    return False


def _pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if sys.platform.startswith("linux"):
        stat_path = Path("/proc") / str(pid) / "stat"
        try:
            stat_parts = stat_path.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if len(stat_parts) >= 3 and stat_parts[2] == "Z":
            return False
    return True


def _build_browser_env(
    *,
    base_env: dict[str, str],
    display: str,
    profile_dir: Path,
    session_dir: Path,
    chromium_bin: str,
) -> dict[str, str]:
    """Build an environment dict that forces browser state into *profile_dir*.

    Native Chrome/Chromium will honor HOME + XDG overrides. Snap-backed
    chromium additionally needs snap-specific paths overridden or it will
    keep leaking state into the global `~/snap/chromium` tree.
    """
    xdg_runtime = session_dir / "xdg-runtime"
    xdg_config = profile_dir / "xdg" / "config"
    xdg_cache = profile_dir / "xdg" / "cache"
    xdg_data = profile_dir / "xdg" / "data"

    for path in (xdg_runtime, xdg_config, xdg_cache, xdg_data):
        path.mkdir(parents=True, exist_ok=True)
    try:
        xdg_runtime.chmod(0o700)
    except OSError:
        pass

    env = {
        **base_env,
        "DISPLAY": display,
        "HOME": str(profile_dir),
        "DBUS_SESSION_BUS_ADDRESS": "/dev/null",
        "XDG_RUNTIME_DIR": str(xdg_runtime),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
    }

    if _is_snap_chromium(chromium_bin):
        snap_common = profile_dir / "snap" / "chromium" / "common"
        snap_data = profile_dir / "snap" / "chromium" / "current"
        snap_common.mkdir(parents=True, exist_ok=True)
        snap_data.mkdir(parents=True, exist_ok=True)
        env["SNAP_USER_COMMON"] = str(snap_common)
        env["SNAP_USER_DATA"] = str(snap_data)
        env["SNAP_REAL_HOME"] = str(profile_dir)

    return env


def _terminate_pid(pid: int | None, *, timeout: float = 5.0) -> None:
    if not pid or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.15)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _graceful_shutdown_chromium(pid: int | None, profile_dir: Path | None = None) -> None:
    """Gracefully close Chromium so cookies, local-storage and session
    data are flushed to disk before the process exits.

    Strategy:
      1. Send SIGTERM and wait up to 8s for the process to finish.
         Chromium hooks SIGTERM and performs an orderly shutdown
         including flushing its LevelDB / SQLite stores.
      2. After the process exits, give the filesystem an extra second
         to settle (particularly relevant on networked/slow disks).
      3. If the process refuses to die within the timeout, SIGKILL.
      4. If *profile_dir* is given, verify the Cookies file exists
         after shutdown — log a warning if it does not.
    """
    if not pid or pid <= 0:
        return

    # Step 1 — ask nicely
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not _pid_is_running(pid):
            break
        time.sleep(0.25)
    else:
        # Step 3 — force-kill
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.5)

    # Step 2 — let filesystem settle
    time.sleep(1.0)

    # Step 4 — verify cookies were actually written
    if profile_dir:
        cookies_path = profile_dir / "Default" / "Cookies"
        if not cookies_path.exists():
            # Also check one level up (some --user-data-dir layouts)
            alt = profile_dir / "Cookies"
            if not alt.exists():
                print(
                    f"[browser_runtime] WARNING: Cookies file not found after "
                    f"graceful shutdown in {profile_dir}. "
                    f"Login session may NOT have been persisted.",
                    flush=True,
                )


def _cleanup_display_state(display_number: int) -> None:
    display = f":{int(display_number)}"
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        result = None
    if result is not None:
        marker = f"Xvfb {display}"
        for line in result.stdout.splitlines():
            text = line.strip()
            if not text or marker not in text:
                continue
            parts = text.split(None, 1)
            try:
                _terminate_pid(int(parts[0]))
            except (ValueError, IndexError):
                continue
    Path(f"/tmp/.X{int(display_number)}-lock").unlink(missing_ok=True)
    Path("/tmp/.X11-unix", f"X{int(display_number)}").unlink(missing_ok=True)


@dataclass
class BrowserRuntimeConfig:
    enabled: bool
    public_base_url: str
    start_url: str
    bind_host: str
    novnc_web_dir: Path
    chromium_bin: str
    profile_root: Path
    session_root: Path
    viewport: str


class BrowserRuntimeManager:
    _NOVNC_CUSTOM_SCRIPT = "paste_shortcuts.js"
    _NOVNC_UI_SCRIPT_TAG = '    <script type="module" crossorigin="anonymous" src="app/ui.js"></script>'
    _NOVNC_CUSTOM_SCRIPT_TAG = (
        '    <script type="module" crossorigin="anonymous" src="paste_shortcuts.js"></script>'
    )

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def load_config(self) -> BrowserRuntimeConfig:
        profile_root = Path(os.getenv("BROWSER_SESSION_PROFILE_ROOT", str(self.data_dir / "browser-profiles")))
        session_root = Path(os.getenv("BROWSER_SESSION_STATE_ROOT", str(self.data_dir / "browser-sessions")))
        chromium_bin = os.getenv("BROWSER_SESSION_CHROMIUM_BIN", "").strip()
        chromium_candidates: list[str] = []
        if chromium_bin:
            chromium_candidates.append(chromium_bin)
        chromium_candidates.extend(["google-chrome-stable", "google-chrome", "chromium-browser", "chromium"])
        chromium_bin = self._resolve_executable(list(dict.fromkeys(chromium_candidates)))
        return BrowserRuntimeConfig(
            enabled=_truthy(os.getenv("BROWSER_SESSION_ENABLED")),
            public_base_url=str(os.getenv("BROWSER_SESSION_PUBLIC_BASE_URL", "")).strip().rstrip("/"),
            start_url=str(os.getenv("BROWSER_SESSION_START_URL", "https://studio.youtube.com")).strip()
            or "https://studio.youtube.com",
            bind_host=str(os.getenv("BROWSER_SESSION_BIND_HOST", "0.0.0.0")).strip() or "0.0.0.0",
            novnc_web_dir=Path(os.getenv("BROWSER_SESSION_NOVNC_WEB_DIR", "/usr/share/novnc")),
            chromium_bin=chromium_bin,
            profile_root=profile_root,
            session_root=session_root,
            viewport=str(os.getenv("BROWSER_SESSION_VIEWPORT", "1440,900")).strip() or "1440,900",
        )

    @staticmethod
    def _resolve_executable(candidates: list[str]) -> str:
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return candidates[0]

    def ensure_available(self, config: BrowserRuntimeConfig) -> None:
        if not config.enabled:
            raise ValueError("Browser session chua duoc bat tren worker.")
        if sys.platform != "linux":
            raise ValueError("Browser session chi ho tro host Linux/Ubuntu.")
        if not config.public_base_url:
            raise ValueError("Thieu BROWSER_SESSION_PUBLIC_BASE_URL tren worker.")
        if not config.novnc_web_dir.exists():
            raise ValueError(f"Khong tim thay noVNC web dir tai {config.novnc_web_dir}.")
        if not shutil.which(config.chromium_bin):
            raise ValueError(f"Khong tim thay Chromium binary '{config.chromium_bin}'.")
        for binary in ("Xvfb", "openbox", "x11vnc", "websockify"):
            if not shutil.which(binary):
                raise ValueError(f"Khong tim thay binary '{binary}'.")

    @staticmethod
    def _ensure_chromium_preferences(profile_dir: Path) -> None:
        default_dir = profile_dir / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        preferences_path = default_dir / "Preferences"
        preferences: dict[str, Any] = {}
        if preferences_path.exists():
            try:
                preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                preferences = {}

        preferences["credentials_enable_service"] = False
        profile_prefs = preferences.get("profile")
        if not isinstance(profile_prefs, dict):
            profile_prefs = {}
        profile_prefs["password_manager_enabled"] = False
        profile_prefs["password_manager_leak_detection"] = False
        preferences["profile"] = profile_prefs

        browser_prefs = preferences.get("browser")
        if not isinstance(browser_prefs, dict):
            browser_prefs = {}
        browser_prefs["show_profile_picker_on_startup"] = False
        browser_prefs["check_default_browser"] = False
        preferences["browser"] = browser_prefs

        signin_prefs = preferences.get("signin")
        if not isinstance(signin_prefs, dict):
            signin_prefs = {}
        signin_prefs["allowed_on_next_startup"] = False
        preferences["signin"] = signin_prefs

        preferences_path.write_text(
            json.dumps(preferences, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
    @staticmethod
    def _build_novnc_url(base_url: str, web_port: int, access_password: str) -> str:
        encoded_password = quote(access_password, safe="")
        return (
            f"{base_url}:{web_port}/vnc.html?autoconnect=1&resize=scale&view_only=0"
            f"#password={encoded_password}"
        )

    @classmethod
    def _novnc_overlay_script_path(cls) -> Path:
        return Path(__file__).resolve().parent / "novnc_overlay" / cls._NOVNC_CUSTOM_SCRIPT

    @classmethod
    def _link_or_copy_novnc_assets(cls, source_dir: Path, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            for child in source_dir.iterdir():
                if child.name == "vnc.html":
                    continue
                destination = target_dir / child.name
                os.symlink(child, destination, target_is_directory=child.is_dir())
            return
        except OSError:
            shutil.rmtree(target_dir, ignore_errors=True)
            shutil.copytree(source_dir, target_dir)

    @classmethod
    def _prepare_novnc_web_dir(cls, session_dir: Path, source_dir: Path) -> Path:
        web_dir = session_dir / "novnc-web"
        if web_dir.exists():
            shutil.rmtree(web_dir, ignore_errors=True)

        cls._link_or_copy_novnc_assets(source_dir, web_dir)

        vnc_source = source_dir / "vnc.html"
        vnc_html = vnc_source.read_text(encoding="utf-8")
        if cls._NOVNC_CUSTOM_SCRIPT_TAG not in vnc_html:
            if cls._NOVNC_UI_SCRIPT_TAG not in vnc_html:
                raise ValueError("Khong tim thay script tag mac dinh trong vnc.html cua noVNC.")
            vnc_html = vnc_html.replace(
                cls._NOVNC_UI_SCRIPT_TAG,
                f"{cls._NOVNC_UI_SCRIPT_TAG}\n{cls._NOVNC_CUSTOM_SCRIPT_TAG}",
                1,
            )

        (web_dir / "vnc.html").write_text(vnc_html, encoding="utf-8")
        shutil.copy2(cls._novnc_overlay_script_path(), web_dir / cls._NOVNC_CUSTOM_SCRIPT)
        return web_dir

    def launch(self, record: dict[str, Any]) -> dict[str, Any]:
        config = self.load_config()
        self.ensure_available(config)

        session_id = str(record["session_id"])
        profile_key = str(record["profile_key"])
        session_dir = config.session_root / session_id
        profile_dir = config.profile_root / profile_key
        log_dir = session_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_chromium_preferences(profile_dir)

        password = str(record["access_password"])
        password_file = session_dir / "vnc.passwd"
        subprocess.run(
            ["x11vnc", "-storepasswd", password, str(password_file)],
            check=True,
            capture_output=True,
            text=True,
        )

        display_number = int(record["display_number"])
        display = f":{display_number}"
        _cleanup_display_state(display_number)
        viewport = config.viewport if "x" not in config.viewport else config.viewport.replace("x", ",")
        viewport_width, viewport_height = [part.strip() for part in viewport.split(",", 1)]
        geometry = f"{viewport_width}x{viewport_height}x24"

        xvfb_log = (log_dir / "xvfb.log").open("ab")
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", geometry, "-ac", "-nolisten", "tcp"],
            stdout=xvfb_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.6)
        if xvfb.poll() is not None:
            raise ValueError("Khong the khoi dong Xvfb cho browser session.")

        env = _build_browser_env(
            base_env=os.environ.copy(),
            display=display,
            profile_dir=profile_dir,
            session_dir=session_dir,
            chromium_bin=config.chromium_bin,
        )

        openbox_log = (log_dir / "openbox.log").open("ab")
        openbox = subprocess.Popen(["openbox"], env=env, stdout=openbox_log, stderr=subprocess.STDOUT)
        time.sleep(0.4)

        chromium_log = (log_dir / "chromium.log").open("ab")
        start_url = str(record.get("start_url") or config.start_url or "https://studio.youtube.com").strip()
        if not start_url:
            start_url = "https://studio.youtube.com"

        chromium = subprocess.Popen(
            [
                config.chromium_bin,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--password-store=basic",
                "--disable-features=PasswordManagerOnboarding,PasswordsImport,Translate,MediaRouter,SigninIntercept,DiceWebSigninInterception",
                "--disable-sync",
                "--disable-background-networking",
                "--window-position=0,0",
                f"--window-size={viewport_width},{viewport_height}",
                f"--remote-debugging-port={record['debug_port']}",
                start_url,
            ],
            env=env,
            stdout=chromium_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.5)
        if chromium.poll() is not None:
            raise ValueError("Chromium da thoat ngay sau khi khoi dong.")

        x11vnc_log = (log_dir / "x11vnc.log").open("ab")
        x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                display,
                "-rfbport",
                str(record["vnc_port"]),
                "-rfbauth",
                str(password_file),
                "-forever",
                "-shared",
                "-localhost",
            ],
            stdout=x11vnc_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.8)
        if x11vnc.poll() is not None:
            raise ValueError("Khong the khoi dong x11vnc cho browser session.")

        novnc_web_dir = self._prepare_novnc_web_dir(session_dir, config.novnc_web_dir)
        websockify_log = (log_dir / "websockify.log").open("ab")
        websockify = subprocess.Popen(
            [
                "websockify",
                "--web",
                str(novnc_web_dir),
                f"{config.bind_host}:{record['web_port']}",
                f"127.0.0.1:{record['vnc_port']}",
            ],
            stdout=websockify_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.8)
        if websockify.poll() is not None:
            raise ValueError("Khong the khoi dong websockify/noVNC cho browser session.")

        novnc_url = self._build_novnc_url(
            config.public_base_url,
            int(record["web_port"]),
            password,
        )
        return {
            "profile_path": str(profile_dir),
            "session_path": str(session_dir),
            "novnc_web_path": str(novnc_web_dir),
            "password_file": str(password_file),
            "novnc_url": novnc_url,
            "xvfb_pid": xvfb.pid,
            "openbox_pid": openbox.pid,
            "chromium_pid": chromium.pid,
            "x11vnc_pid": x11vnc.pid,
            "websockify_pid": websockify.pid,
        }

    @staticmethod
    def _session_record_path(session_path: Path) -> Path:
        return session_path / "session.json"

    def save_session_record(self, record: dict[str, Any]) -> None:
        session_path = Path(str(record.get("session_path") or "").strip())
        if not session_path:
            return
        session_path.mkdir(parents=True, exist_ok=True)
        payload = {
            key: value
            for key, value in record.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        self._session_record_path(session_path).write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def load_persisted_sessions(self) -> dict[str, dict[str, Any]]:
        config = self.load_config()
        if not config.session_root.exists():
            return {}

        sessions: dict[str, dict[str, Any]] = {}
        for session_dir in config.session_root.iterdir():
            if not session_dir.is_dir():
                continue
            record_path = self._session_record_path(session_dir)
            if not record_path.exists():
                continue
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = str(record.get("session_id") or session_dir.name).strip()
            if not session_id:
                continue
            record["session_id"] = session_id
            record["session_path"] = str(record.get("session_path") or session_dir)
            sessions[session_id] = record
        return sessions

    def stop(self, record: dict[str, Any]) -> None:
        # Kill ancillary processes first (reverse dependency order)
        for key in ("websockify_pid", "x11vnc_pid"):
            _terminate_pid(int(record.get(key) or 0) or None)

        # Graceful shutdown for Chromium — wait for cookies/session flush
        chromium_pid = int(record.get("chromium_pid") or 0) or None
        profile_path_str = str(record.get("profile_path") or "").strip()
        profile_dir = Path(profile_path_str) if profile_path_str else None
        _graceful_shutdown_chromium(chromium_pid, profile_dir)

        # Clean up remaining display processes
        for key in ("openbox_pid", "xvfb_pid"):
            _terminate_pid(int(record.get(key) or 0) or None)

        session_path = str(record.get("session_path") or "").strip()
        if session_path:
            shutil.rmtree(session_path, ignore_errors=True)

    def delete_profile(self, profile_key: str) -> None:
        cleaned_key = str(profile_key or "").strip()
        if not cleaned_key:
            return
        config = self.load_config()
        profile_dir = config.profile_root / cleaned_key
        try:
            profile_dir.relative_to(config.profile_root)
        except ValueError:
            raise ValueError("Profile path cleanup nam ngoai browser profile root.")
        shutil.rmtree(profile_dir, ignore_errors=True)

    def is_running(self, record: dict[str, Any]) -> bool:
        chromium_pid = int(record.get("chromium_pid") or 0) or None
        websockify_pid = int(record.get("websockify_pid") or 0) or None
        x11vnc_pid = int(record.get("x11vnc_pid") or 0) or None
        return all(_pid_is_running(pid) for pid in (chromium_pid, websockify_pid, x11vnc_pid))

    def inspect(self, record: dict[str, Any]) -> dict[str, Any]:
        debug_port = int(record.get("debug_port") or 0)
        if debug_port <= 0:
            raise ValueError("Browser session chua co debug_port hop le.")

        try:
            with urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=5) as response:
                tabs = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ValueError("Khong the doc Chromium remote debugging endpoint.") from exc

        page_tabs = [item for item in tabs if item.get("type") == "page"]
        current = next(
            (
                item
                for item in page_tabs
                if (urlparse(str(item.get("url") or "").strip()).hostname or "").lower() == "studio.youtube.com"
            ),
            page_tabs[0] if page_tabs else None,
        )
        current_url = str((current or {}).get("url") or "").strip()
        current_title = str((current or {}).get("title") or "").strip()
        detected = self._extract_channel_identity(current_url, current_title)
        return {
            "current_url": current_url,
            "current_title": current_title,
            "channel_id": detected.get("channel_id"),
            "channel_name": detected.get("channel_name"),
        }

    @staticmethod
    def _extract_channel_identity(url: str, title: str) -> dict[str, str | None]:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)
        channel_id = None

        if host == "studio.youtube.com":
            if "/channel/" in path:
                channel_id = path.split("/channel/", 1)[1].split("/", 1)[0].strip() or None
            else:
                channel_ids = query.get("channel_id", [])
                channel_id = channel_ids[0].strip() if channel_ids and channel_ids[0].strip() else None
        elif host in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
            if path.startswith("/channel/"):
                channel_id = path.split("/channel/", 1)[1].split("/", 1)[0].strip() or None

        cleaned_title = title.replace(" - YouTube Studio", "").replace(" - YouTube", "").strip()
        if cleaned_title and cleaned_title.lower() in {"youtube studio", "dashboard"}:
            cleaned_title = None

        return {
            "channel_id": channel_id or None,
            "channel_name": cleaned_title or channel_id or None,
        }
