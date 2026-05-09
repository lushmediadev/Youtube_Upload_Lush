import unittest
import sys
import types
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


if "selenium" not in sys.modules:
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Chrome = object
    webdriver.ChromeOptions = type("ChromeOptions", (), {})

    common = types.ModuleType("selenium.common")
    exceptions = types.ModuleType("selenium.common.exceptions")
    exceptions.StaleElementReferenceException = type("StaleElementReferenceException", (Exception,), {})
    exceptions.TimeoutException = type("TimeoutException", (Exception,), {})

    chrome = types.ModuleType("selenium.webdriver.chrome")
    service = types.ModuleType("selenium.webdriver.chrome.service")
    service.Service = type("Service", (), {"__init__": lambda self, *args, **kwargs: None})

    common_by = types.ModuleType("selenium.webdriver.common.by")
    common_by.By = type("By", (), {"XPATH": "xpath", "CSS_SELECTOR": "css selector"})

    support = types.ModuleType("selenium.webdriver.support")
    expected_conditions = types.ModuleType("selenium.webdriver.support.expected_conditions")
    ui = types.ModuleType("selenium.webdriver.support.ui")
    ui.WebDriverWait = type("WebDriverWait", (), {"__init__": lambda self, *args, **kwargs: None})

    sys.modules.update(
        {
            "selenium": selenium,
            "selenium.webdriver": webdriver,
            "selenium.common": common,
            "selenium.common.exceptions": exceptions,
            "selenium.webdriver.chrome": chrome,
            "selenium.webdriver.chrome.service": service,
            "selenium.webdriver.common.by": common_by,
            "selenium.webdriver.support": support,
            "selenium.webdriver.support.expected_conditions": expected_conditions,
            "selenium.webdriver.support.ui": ui,
        }
    )

from workers.agent import browser_uploader
from workers.agent.config import WorkerConfig
from workers.agent.control_plane import YouTubeUploadTarget


class BrowserUploadMetadataTests(unittest.TestCase):
    def test_fill_upload_metadata_sets_title_without_touching_description(self) -> None:
        calls: list[tuple[object, str]] = []
        title_box = object()
        description_box = object()

        def fake_set_textbox(_driver, element, value: str) -> None:
            calls.append((element, value))

        def fake_read_textbox_value(_driver, element) -> str:
            if element is title_box:
                return "A Clean Video Title"
            return "Existing YouTube preset description"

        with (
            patch.object(browser_uploader, "_find_title_and_description_boxes", return_value=(title_box, description_box)),
            patch.object(browser_uploader, "_set_textbox", side_effect=fake_set_textbox),
            patch.object(browser_uploader, "_read_textbox_value", side_effect=fake_read_textbox_value),
        ):
            browser_uploader._fill_upload_metadata(
                object(),
                object(),
                title="A Clean Video Title",
            )

        self.assertEqual(calls, [(title_box, "A Clean Video Title")])

    def test_browser_upload_does_not_succeed_without_video_url(self) -> None:
        class FakeBrowserConfig:
            def __init__(self, profile_root: Path) -> None:
                self.profile_root = profile_root
                self.chromium_bin = "/usr/bin/chromium"

        class FakeRuntime:
            def __init__(self, _runtime_root: Path, profile_root: Path) -> None:
                self._profile_root = profile_root

            def load_config(self) -> FakeBrowserConfig:
                return FakeBrowserConfig(self._profile_root)

        class FakeProcess:
            def poll(self) -> None:
                return None

        class FakeOptions:
            binary_location = ""

            def add_experimental_option(self, *_args, **_kwargs) -> None:
                return None

        class FakeDriver:
            current_url = "https://studio.youtube.com/channel/UC-test/videos?d=ud"

            def get(self, _url: str) -> None:
                return None

            def quit(self) -> None:
                return None

        def fake_path_open(path: Path, *args, **kwargs):
            if path.name == "chromium.log":
                return BytesIO()
            return original_path_open(path, *args, **kwargs)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_root = root / "profiles"
            profile_path = profile_root / "user-profile"
            profile_path.mkdir(parents=True)
            upload_file = root / "video.mp4"
            upload_file.write_bytes(b"video")
            config = WorkerConfig(
                runtime_mode="upload",
                control_plane_url="http://control-plane",
                shared_secret="secret",
                worker_id="worker-1",
                worker_name="worker-1",
                manager_name="manager",
                group="group",
                capacity=1,
                threads=1,
                heartbeat_seconds=15,
                poll_seconds=5,
                simulate_jobs=False,
                execute_jobs=True,
                simulate_step_seconds=1.0,
                work_root=root / "work",
                keep_job_dirs=False,
                ffmpeg_bin="ffmpeg",
                ffprobe_bin="ffprobe",
                youtube_upload_enabled=True,
                youtube_upload_chunk_bytes=8388608,
                browser_public_base_url="",
                browser_session_enabled=True,
                browser_display_base=90,
                browser_vnc_port_base=15900,
                browser_web_port_base=16080,
                browser_debug_port_base=19220,
                live_normalize_enabled=True,
                live_normalize_concurrency=1,
                live_normalize_threads=1,
                live_normalize_preset="veryfast",
                live_normalize_max_height=1440,
                live_normalize_1080_maxrate_kbps=6000,
                live_normalize_1440_maxrate_kbps=13000,
                live_normalize_1080_crf=23,
                live_normalize_1440_crf=22,
                live_normalize_audio_bitrate_kbps=128,
                network_retry_base_seconds=1.0,
                network_retry_max_seconds=2.0,
                progress_retry_attempts=1,
            )
            target = YouTubeUploadTarget(
                job_id="job-1",
                channel_id="UC-test",
                channel_name="Test Channel",
                title="Test Upload",
                description=None,
                browser_profile_key="user-profile",
                browser_profile_path=str(profile_path),
            )

            original_path_open = Path.open
            with ExitStack() as stack:
                stack.enter_context(patch.object(browser_uploader, "BrowserRuntimeManager", lambda runtime_root: FakeRuntime(runtime_root, profile_root)))
                stack.enter_context(patch.object(Path, "open", fake_path_open))
                stack.enter_context(patch.object(browser_uploader, "_kill_profile_processes"))
                stack.enter_context(patch.object(browser_uploader, "_pick_display_number", return_value=300))
                stack.enter_context(patch.object(browser_uploader, "_pick_unused_tcp_port", return_value=9223))
                stack.enter_context(patch.object(browser_uploader, "_wait_for_debug_endpoint"))
                stack.enter_context(patch.object(browser_uploader, "_build_browser_env", return_value={}))
                stack.enter_context(patch.object(browser_uploader.subprocess, "Popen", return_value=FakeProcess()))
                stack.enter_context(patch.object(browser_uploader, "ChromeOptions", FakeOptions))
                stack.enter_context(patch.object(browser_uploader.webdriver, "Chrome", return_value=FakeDriver()))
                stack.enter_context(patch.object(browser_uploader, "WebDriverWait", lambda *_args, **_kwargs: object()))
                stack.enter_context(patch.object(browser_uploader, "_attach_file_to_upload_dialog"))
                stack.enter_context(patch.object(browser_uploader, "_raise_if_upload_blocked"))
                stack.enter_context(patch.object(browser_uploader, "_wait_for_uploaded_video_url", return_value=None))
                stack.enter_context(patch.object(browser_uploader, "_find_title_and_description_boxes", return_value=(object(), None)))
                stack.enter_context(patch.object(browser_uploader, "_fill_upload_metadata"))
                stack.enter_context(patch.object(browser_uploader, "_click_when_available", return_value=True))
                stack.enter_context(patch.object(browser_uploader, "_wait_for_legacy_draft_upload_completion"))
                stack.enter_context(patch.object(browser_uploader, "_detect_blocking_upload_error_safe", return_value=None))
                stack.enter_context(patch.object(browser_uploader, "_detect_blocking_upload_error", return_value=None))
                stack.enter_context(patch.object(browser_uploader, "_stop_process"))
                stack.enter_context(patch.object(browser_uploader.time, "sleep"))
                with self.assertRaisesRegex(RuntimeError, "Khong lay duoc duong link video"):
                    browser_uploader.upload_video_via_browser(
                        config=config,
                        target=target,
                        file_path=upload_file,
                    )


if __name__ == "__main__":
    unittest.main()
