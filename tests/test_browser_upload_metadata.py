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
