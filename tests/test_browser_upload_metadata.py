import unittest
import sys
import types
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


class BrowserUploadMetadataTests(unittest.TestCase):
    def test_detect_blocking_error_when_youtube_requires_identity_verification(self) -> None:
        class FakeDriver:
            current_url = "https://studio.youtube.com/channel/UC-test/videos/upload?d=ud"

        page_text = (
            "Verify it's you To continue, we need to confirm it's really you. "
            "This extra layer of security helps keep your account safe. Learn more Next"
        )
        with patch.object(browser_uploader, "_read_visible_page_text", return_value=page_text):
            detected = browser_uploader._detect_blocking_upload_error_safe(FakeDriver())

        self.assertIsNotNone(detected)
        self.assertIn("Verify it's you", detected)
        self.assertIn("them lai kenh", detected)

    def test_detect_blocking_error_when_youtube_requires_vietnamese_identity_verification(self) -> None:
        class FakeDriver:
            current_url = "https://studio.youtube.com/channel/UC-test/videos/upload?d=ud"

        page_text = (
            "Xác minh danh tính của bạn "
            "Để tiếp tục, chúng tôi cần xác thực danh tính của bạn. "
            "Bước bảo mật bổ sung này giúp đảm bảo an toàn cho tài khoản của bạn. "
            "Tìm hiểu thêm Tiếp theo"
        )
        with patch.object(browser_uploader, "_read_visible_page_text", return_value=page_text):
            detected = browser_uploader._detect_blocking_upload_error_safe(FakeDriver())

        self.assertIsNotNone(detected)
        self.assertIn("Verify it's you", detected)
        self.assertIn("noVNC", detected)

    def test_detect_blocking_error_reads_overlay_dialog_after_upload_dialog(self) -> None:
        class FakeElement:
            def __init__(self, text: str) -> None:
                self.text = text

            def is_displayed(self) -> bool:
                return True

        class FakeDriver:
            current_url = "https://studio.youtube.com/channel/UC-test/videos/upload?d=ud"

            def find_elements(self, _by, xpath: str) -> list[FakeElement]:
                if "ytcp-uploads-dialog" in xpath:
                    return [FakeElement("Uploading video Video link Creating link Uploading 19%")]
                if "role='dialog'" in xpath:
                    return [
                        FakeElement(
                            "Verify it's you To continue, we need to confirm it's really you. "
                            "This extra layer of security helps keep your account safe. Learn more Next"
                        )
                    ]
                return []

        detected = browser_uploader._detect_blocking_upload_error_safe(FakeDriver())

        self.assertIsNotNone(detected)
        self.assertIn("Verify it's you", detected)

    def test_dialog_text_script_timeout_does_not_fail_upload_detection(self) -> None:
        class TimeoutTextElement:
            def is_displayed(self) -> bool:
                return True

            @property
            def text(self) -> str:
                raise browser_uploader.TimeoutException("script timeout")

        class FakeDriver:
            current_url = "https://studio.youtube.com/channel/UC-test/videos/upload?d=ud"

            def find_elements(self, _by, xpath: str) -> list[TimeoutTextElement]:
                if "ytcp-uploads-dialog" in xpath or "role='dialog'" in xpath:
                    return [TimeoutTextElement()]
                return []

        detected = browser_uploader._detect_blocking_upload_error_safe(FakeDriver())
        dialog_text = browser_uploader._read_upload_dialog_text(FakeDriver())
        status_text = browser_uploader._extract_dialog_status_text(FakeDriver())

        self.assertIsNone(detected)
        self.assertEqual(dialog_text, "")
        self.assertEqual(status_text, "")

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


if __name__ == "__main__":
    unittest.main()
