from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .browser_runtime import BrowserRuntimeManager, _build_browser_env
from .config import WorkerConfig
from .control_plane import YouTubeUploadTarget
from .ffmpeg_pipeline import probe_media


UploadProgressCallback = Callable[[float, str | None], None]

VI_15_MINUTES = "15 ph\u00fat"
VI_VERIFY = "x\u00e1c minh"
VI_PHONE = "\u0111i\u1ec7n tho\u1ea1i"
VI_UPLOAD_LIMIT = "qu\u00e1 gi\u1edbi h\u1ea1n t\u1ea3i l\u00ean"
VI_UPLOAD_LIMIT_EXCEEDED = "\u0111\u00e3 v\u01b0\u1ee3t gi\u1edbi h\u1ea1n t\u1ea3i l\u00ean"
VI_UPLOAD_FAILED = "t\u1ea3i l\u00ean kh\u00f4ng th\u00e0nh c\u00f4ng"
VI_CANT_UPLOAD = "kh\u00f4ng th\u1ec3 t\u1ea3i video"
VI_UPLOAD_ERROR = "\u0111\u00e3 x\u1ea3y ra l\u1ed7i khi t\u1ea3i l\u00ean"
VI_CONTINUE_TO_YOUTUBE = "\u0111\u1ec3 ti\u1ebfp t\u1ee5c v\u1edbi youtube"
VI_SELFIE = "selfie"

@dataclass
class BrowserUploadResult:
    studio_url: str
    output_url: str | None = None
    cleanup_safe: bool = False
    completion_message: str | None = None


class RefreshRequiredUploadError(RuntimeError):
    pass


class UploadTransferTimeoutError(RuntimeError):
    pass


def _read_visible_page_text(driver: webdriver.Chrome) -> str:
    snippets: list[str] = []
    for xpath in (
        "//ytcp-uploads-dialog",
        "//*[@role='dialog']",
        "//body",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            if not element.is_displayed():
                continue
            text = re.sub(r"\s+", " ", (element.text or "").strip())
            if text:
                snippets.append(text)
        if snippets:
            break
    return " ".join(snippets)


def _read_upload_dialog_text(driver: webdriver.Chrome) -> str:
    snippets: list[str] = []
    for xpath in (
        "//ytcp-uploads-dialog",
        "//*[@role='dialog']",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if not element.is_displayed():
                    continue
                text = re.sub(r"\s+", " ", (element.text or "").strip())
            except StaleElementReferenceException:
                continue
            if text:
                snippets.append(text)
        if snippets:
            break
    return " ".join(snippets)


def _fold_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _detect_blocking_upload_error_safe(driver: webdriver.Chrome) -> str | None:
    current_url = (driver.current_url or "").casefold()
    page_text = _read_visible_page_text(driver).casefold()
    if not page_text:
        return None

    if (
        "accounts.google.com" in current_url
        or "servicelogin" in current_url
        or ("sign in" in page_text and ("continue to youtube" in page_text or VI_CONTINUE_TO_YOUTUBE in page_text))
    ):
        return "Chrome profile cua kenh da mat dang nhap Google/YouTube Studio. Hay reconnect kenh tren VPS nay roi thu lai."

    if "video-verification" in current_url or VI_SELFIE in page_text:
        return "Google dang yeu cau xac minh bo sung tren profile nay (selfie/video verification). Bot khong the tiep tuc upload cho toi khi ban xu ly thu cong."

    if (
        any(token in page_text for token in ("15 minutes", "longer than 15 minutes", VI_15_MINUTES))
        and (
            "verify" in page_text
            or VI_VERIFY in page_text
            or "phone number" in page_text
            or "verify your phone number" in page_text
            or VI_PHONE in page_text
            or "increase your limit" in page_text
        )
    ):
        return "K\u00eanh n\u00e0y ch\u01b0a x\u00e1c minh n\u00ean ch\u01b0a th\u1ec3 \u0111\u0103ng video d\u00e0i h\u01a1n 15 ph\u00fat."

    if any(
        token in page_text
        for token in (
            "daily upload limit",
            "upload limit exceeded",
            "too many videos",
            VI_UPLOAD_LIMIT,
            VI_UPLOAD_LIMIT_EXCEEDED,
        )
    ):
        return "K\u00eanh n\u00e0y \u0111\u00e3 ch\u1ea1m gi\u1edbi h\u1ea1n upload c\u1ee7a YouTube. H\u00e3y th\u1eed l\u1ea1i sau."

    if any(
        token in page_text
        for token in (
            "there was a problem uploading",
            "couldn't upload",
            "we couldn't upload your video",
            "upload failed",
            VI_UPLOAD_FAILED,
            VI_CANT_UPLOAD,
            VI_UPLOAD_ERROR,
        )
    ):
        return "YouTube Studio b\u00e1o l\u1ed7i upload. H\u00e3y ki\u1ec3m tra l\u1ea1i k\u00eanh r\u1ed3i th\u1eed l\u1ea1i job."

    return None


def _detect_refresh_required_upload_gate(driver: webdriver.Chrome) -> str | None:
    folded_text = _fold_text(
        " ".join(
            part
            for part in (
                _read_upload_dialog_text(driver),
                _read_visible_page_text(driver),
            )
            if part
        )
    )
    if not folded_text:
        return None

    has_access_message = (
        "you already have access to this feature" in folded_text
        or "ban da co quyen su dung tinh nang nay roi" in folded_text
    )
    refresh_message = (
        "try refreshing the page" in folded_text
        or "refresh the page" in folded_text
        or "hay thu lam moi trang" in folded_text
    )
    completion_message = (
        "you are all set" in folded_text
        or "ban da hoan tat" in folded_text
    )
    if refresh_message and (has_access_message or completion_message):
        return "YouTube Studio yeu cau lam moi trang truoc khi mo lai form upload."
    return None


def _raise_if_refresh_required_upload_gate(driver: webdriver.Chrome) -> None:
    detected_message = _detect_refresh_required_upload_gate(driver)
    if detected_message:
        raise RefreshRequiredUploadError(detected_message)


def _detect_blocking_upload_error(driver: webdriver.Chrome) -> str | None:
    current_url = (driver.current_url or "").lower()
    page_text = _read_visible_page_text(driver).lower()
    if not page_text:
        return None

    if (
        "accounts.google.com" in current_url
        or "servicelogin" in current_url
        or ("sign in" in page_text and "continue to youtube" in page_text)
    ):
        return "Chrome profile cua kenh da mat dang nhap Google/YouTube Studio. Hay reconnect kenh tren VPS nay roi thu lai."

    if "video-verification" in current_url or "selfie" in page_text:
        return "Google dang yeu cau xac minh bo sung tren profile nay (selfie/video verification). Bot khong the tiep tuc upload cho toi khi ban xu ly thu cong."

    if (
        any(token in page_text for token in ("15 minutes", "longer than 15 minutes", "15 phút"))
        and (
            "verify" in page_text
            or "xác minh" in page_text
            or "phone number" in page_text
            or "verify your phone number" in page_text
            or "điện thoại" in page_text
            or "increase your limit" in page_text
        )
    ):
        return "Kênh này chưa xác minh nên chưa thể đăng video dài hơn 15 phút."

    if any(
        token in page_text
        for token in (
            "daily upload limit",
            "upload limit exceeded",
            "too many videos",
            "quá giới hạn tải lên",
            "đã vượt giới hạn tải lên",
        )
    ):
        return "Kênh này đã chạm giới hạn upload của YouTube. Hãy thử lại sau."

    if any(
        token in page_text
        for token in (
            "there was a problem uploading",
            "couldn't upload",
            "we couldn't upload your video",
            "upload failed",
            "tải lên không thành công",
            "không thể tải video",
            "đã xảy ra lỗi khi tải lên",
        )
    ):
        return "YouTube Studio báo lỗi upload. Hãy kiểm tra lại kênh rồi thử lại job."

    return None


def _raise_if_upload_blocked(driver: webdriver.Chrome) -> None:
    _raise_if_refresh_required_upload_gate(driver)
    detected_message = _detect_blocking_upload_error_safe(driver) or _detect_blocking_upload_error(driver)
    if detected_message:
        raise RuntimeError(detected_message)


def _format_browser_exception(exc: Exception, driver: webdriver.Chrome | None = None) -> str:
    detected_message = (
        (_detect_blocking_upload_error_safe(driver) or _detect_blocking_upload_error(driver))
        if driver is not None
        else None
    )
    if detected_message:
        return detected_message

    exception_name = exc.__class__.__name__
    detail = re.sub(r"\s+", " ", str(exc or "")).strip(" :\n\t")
    if detail and detail.lower() not in {"message", exception_name.lower()}:
        if exception_name == "RuntimeError":
            return detail
        return f"{exception_name}: {detail}"
    return f"{exception_name}: Khong the hoan tat browser upload tren YouTube Studio."


def _normalize_title(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _pick_display_number(start: int = 300, end: int = 360) -> int:
    for candidate in range(start, end):
        if not Path(f"/tmp/.X11-unix/X{candidate}").exists():
            return candidate
    raise RuntimeError("Khong tim thay DISPLAY trong de mo browser upload.")


def _pick_unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_debug_endpoint(port: int, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Chromium khong mo remote debugging endpoint kip thoi.")


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.kill()
    process.wait(timeout=5)


def _kill_profile_processes(profile_path: Path) -> None:
    profile_marker = str(profile_path)
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return

    candidate_pids: list[int] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or profile_marker not in line:
            continue
        if not any(token in line for token in ("chromedriver", "/chrome", "google-chrome", "chromium")):
            continue
        try:
            pid_text, _ = line.split(None, 1)
            candidate_pids.append(int(pid_text))
        except Exception:
            continue

    for pid in sorted(set(candidate_pids), reverse=True):
        try:
            subprocess.run(["kill", "-TERM", str(pid)], check=False, capture_output=True, text=True)
        except Exception:
            continue

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and candidate_pids:
        remaining: list[int] = []
        for pid in candidate_pids:
            try:
                os.kill(pid, 0)
                remaining.append(pid)
            except OSError:
                continue
        if not remaining:
            return
        candidate_pids = remaining
        time.sleep(0.2)

    for pid in sorted(set(candidate_pids), reverse=True):
        try:
            subprocess.run(["kill", "-KILL", str(pid)], check=False, capture_output=True, text=True)
        except Exception:
            continue


def _set_textbox(driver: webdriver.Chrome, element, value: str) -> None:
    driver.execute_script(
        """
        const element = arguments[0];
        const value = arguments[1];
        element.focus();
        if ('value' in element) {
          element.value = value;
        } else {
          element.textContent = value;
          element.innerText = value;
        }
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


def _read_textbox_value(driver: webdriver.Chrome, element) -> str:
    try:
        value = driver.execute_script(
            """
            const element = arguments[0];
            if (!element) {
              return '';
            }
            if ('value' in element && typeof element.value === 'string') {
              return element.value;
            }
            return element.innerText || element.textContent || '';
            """,
            element,
        )
    except StaleElementReferenceException:
        return ""
    except Exception:
        value = ""
    return re.sub(r"\s+", " ", str(value or "").strip())


def _fill_upload_metadata(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    *,
    title: str,
) -> None:
    expected_title = _normalize_title(title)
    last_error: Exception | None = None
    for _ in range(4):
        try:
            title_box, _ = _find_title_and_description_boxes(driver, wait)
            _set_textbox(driver, title_box, title)
            time.sleep(0.4)
            current_title = _normalize_title(_read_textbox_value(driver, title_box))
            if current_title != expected_title:
                continue
            try:
                driver.execute_script("arguments[0].blur();", title_box)
            except Exception:
                pass
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
            continue
    if last_error:
        raise RuntimeError("Khong the xac nhan title trong YouTube Studio upload dialog.") from last_error
    raise RuntimeError("Khong the xac nhan title trong YouTube Studio upload dialog.")


def _find_title_and_description_boxes(driver: webdriver.Chrome, wait: WebDriverWait) -> tuple[object, object | None]:
    def _locate(drv: webdriver.Chrome):
        _raise_if_refresh_required_upload_gate(drv)
        visible = _collect_upload_dialog_editors(drv)
        if not visible:
            return False
        title_box = visible[0]
        description_box = visible[1] if len(visible) > 1 else None
        return title_box, description_box

    result = wait.until(_locate)
    return result[0], result[1]


def _collect_upload_dialog_editors(driver: webdriver.Chrome) -> list[object]:
    script = """
    const selectors = [
      '#textbox',
      '[contenteditable="true"]',
      'textarea',
      'input:not([type="file"])',
      '[role="textbox"]'
    ];
    const roots = [];
    const seenRoots = new Set();
    const queue = [document];
    while (queue.length) {
      const root = queue.shift();
      if (!root || seenRoots.has(root)) continue;
      seenRoots.add(root);
      roots.push(root);
      const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const element of elements) {
        if (element && element.shadowRoot && !seenRoots.has(element.shadowRoot)) {
          queue.push(element.shadowRoot);
        }
      }
    }
    const dialogs = [];
    for (const root of roots) {
      if (!root.querySelectorAll) continue;
      for (const dialog of root.querySelectorAll('ytcp-uploads-dialog, [role="dialog"]')) {
        if (!dialog || dialogs.includes(dialog)) continue;
        dialogs.push(dialog);
      }
    }
    const searchRoots = dialogs.length ? dialogs : roots;
    const nodes = [];
    for (const root of searchRoots) {
      if (!root.querySelectorAll) continue;
      for (const selector of selectors) {
        for (const node of root.querySelectorAll(selector)) {
          if (!node || nodes.some((item) => item.element === node)) continue;
          const rect = node.getBoundingClientRect();
          const style = window.getComputedStyle(node);
          if (!rect || rect.width < 8 || rect.height < 8) continue;
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          const text = [
            node.getAttribute('aria-label') || '',
            node.getAttribute('label') || '',
            node.getAttribute('placeholder') || '',
            node.id || '',
            node.name || '',
            node.textContent || ''
          ].join(' ').toLowerCase();
          nodes.push({
            element: node,
            score:
              (text.includes('title') || text.includes('tiêu đề') ? 6 : 0) +
              (text.includes('description') || text.includes('mô tả') ? 4 : 0) +
              (text.includes('details') ? 2 : 0) +
              (node.id === 'textbox' ? 1 : 0),
          });
        }
      }
    }
    nodes.sort((a, b) => b.score - a.score);
    return nodes.map((item) => item.element);
    """
    try:
        return list(driver.execute_script(script) or [])
    except Exception:
        return []


def _collect_upload_dialog_debug(driver: webdriver.Chrome) -> dict[str, object]:
    script = """
    const roots = [];
    const seenRoots = new Set();
    const queue = [document];
    while (queue.length) {
      const root = queue.shift();
      if (!root || seenRoots.has(root)) continue;
      seenRoots.add(root);
      roots.push(root);
      const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const element of elements) {
        if (element && element.shadowRoot && !seenRoots.has(element.shadowRoot)) {
          queue.push(element.shadowRoot);
        }
      }
    }
    const selectors = ['#textbox', '[contenteditable="true"]', 'textarea', 'input:not([type="file"])', '[role="textbox"]'];
    const candidates = [];
    for (const root of roots) {
      if (!root.querySelectorAll) continue;
      for (const selector of selectors) {
        for (const node of root.querySelectorAll(selector)) {
          if (!node || candidates.some((item) => item.element === node)) continue;
          const rect = node.getBoundingClientRect();
          const style = window.getComputedStyle(node);
          if (!rect || rect.width < 8 || rect.height < 8) continue;
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          candidates.push({
            element: node,
            tag: (node.tagName || '').toLowerCase(),
            id: node.id || '',
            name: node.getAttribute('name') || '',
            ariaLabel: node.getAttribute('aria-label') || '',
            label: node.getAttribute('label') || '',
            placeholder: node.getAttribute('placeholder') || '',
            role: node.getAttribute('role') || '',
            text: (node.textContent || '').trim().slice(0, 160),
            classes: (node.className || '').toString().slice(0, 160),
            rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          });
        }
      }
    }
    return {
      url: window.location.href,
      title: document.title,
      readyState: document.readyState,
      rootCount: roots.length,
      dialogCount: roots.reduce((count, root) => count + (root.querySelectorAll ? root.querySelectorAll('ytcp-uploads-dialog, [role="dialog"]').length : 0), 0),
      visibleText: (document.body?.innerText || '').slice(0, 4000),
      candidates: candidates.slice(0, 25),
    };
    """
    try:
        payload = driver.execute_script(script)
    except Exception as exc:
        return {"collection_error": str(exc)}
    if isinstance(payload, dict):
        return payload
    return {"payload": payload}


def _capture_upload_debug_artifacts(
    driver: webdriver.Chrome,
    *,
    debug_root: Path,
    job_id: str,
    stage: str,
) -> Path | None:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir = debug_root / "browser-upload-debug" / f"{job_id}-{stage}-{timestamp}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(target_dir / "page.png"))
        (target_dir / "page.html").write_text(driver.page_source, encoding="utf-8")
        payload = _collect_upload_dialog_debug(driver)
        (target_dir / "dialog.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target_dir
    except Exception:
        return None


def _collect_upload_file_inputs(driver: webdriver.Chrome) -> list[object]:
    script = """
    const roots = [];
    const seenRoots = new Set();
    const queue = [document];
    while (queue.length) {
      const root = queue.shift();
      if (!root || seenRoots.has(root)) continue;
      seenRoots.add(root);
      roots.push(root);
      const elements = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const element of elements) {
        if (element && element.shadowRoot && !seenRoots.has(element.shadowRoot)) {
          queue.push(element.shadowRoot);
        }
      }
    }
    const dialogs = [];
    for (const root of roots) {
      if (!root.querySelectorAll) continue;
      for (const dialog of root.querySelectorAll('ytcp-uploads-dialog, [role="dialog"]')) {
        if (!dialog || dialogs.includes(dialog)) continue;
        dialogs.push(dialog);
      }
    }
    const searchRoots = dialogs.length ? dialogs : roots;
    const inputs = [];
    for (const root of searchRoots) {
      if (!root.querySelectorAll) continue;
      for (const node of root.querySelectorAll('input[type="file"]')) {
        if (!node || inputs.includes(node)) continue;
        const accept = (node.getAttribute('accept') || '').toLowerCase();
        const style = node instanceof Element ? window.getComputedStyle(node) : null;
        inputs.push({
          element: node,
          score:
            (accept.includes('video') ? 4 : 0) +
            (accept.includes('image') ? -1 : 0) +
            (style && style.display !== 'none' ? 1 : 0),
        });
      }
    }
    inputs.sort((a, b) => b.score - a.score);
    return inputs.map((item) => item.element);
    """
    try:
        return list(driver.execute_script(script) or [])
    except Exception:
        return []


def _find_upload_file_input(driver: webdriver.Chrome, wait: WebDriverWait):
    def _locate(drv: webdriver.Chrome):
        inputs = _collect_upload_file_inputs(drv)
        if not inputs:
            return False
        return inputs[0]

    return wait.until(_locate)


def _attach_file_to_upload_dialog(
    driver: webdriver.Chrome,
    *,
    wait: WebDriverWait,
    studio_url: str,
    file_path: Path,
    debug_root: Path,
    job_id: str,
) -> None:
    last_exc: TimeoutException | None = None
    for attempt in range(2):
        _raise_if_upload_blocked(driver)
        if attempt:
            driver.get(studio_url)
            time.sleep(1.5)
        try:
            file_input = _find_upload_file_input(driver, wait)
            file_input.send_keys(str(file_path))
            return
        except TimeoutException as exc:
            last_exc = exc
    debug_dir = _capture_upload_debug_artifacts(
        driver,
        debug_root=debug_root,
        job_id=job_id,
        stage="file-input-timeout",
    )
    detail = "Khong tim thay file input cua YouTube Studio upload dialog."
    if debug_dir is not None:
        detail = f"{detail} Debug: {debug_dir}"
    raise RuntimeError(detail) from last_exc


def _dismiss_refresh_required_upload_gate(driver: webdriver.Chrome) -> bool:
    script = """
    const fold = (value) =>
      (value || '')
        .normalize('NFD')
        .replace(/[\\u0300-\\u036f]/g, '')
        .replace(/\\s+/g, ' ')
        .trim()
        .toLowerCase();
    const labels = ['da hieu', 'got it', 'ok', 'okay'];
    const elements = Array.from(document.querySelectorAll('button, [role="button"]'));
    for (const element of elements) {
      const text = fold(
        [
          element.innerText || '',
          element.textContent || '',
          element.getAttribute('aria-label') || '',
          element.getAttribute('title') || '',
        ].join(' ')
      );
      if (!labels.some((label) => text.includes(label))) continue;
      const rect = element.getBoundingClientRect();
      if (!rect || rect.width < 6 || rect.height < 6) continue;
      element.click();
      return true;
    }
    return false;
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def _click_when_available(driver: webdriver.Chrome, wait: WebDriverWait, xpath: str) -> bool:
    try:
        element = wait.until(EC.element_to_be_clickable((By.XPATH, xpath)))
    except TimeoutException:
        return False
    driver.execute_script("arguments[0].click();", element)
    return True


def _extract_upload_ratio(driver: webdriver.Chrome) -> float | None:
    candidate_texts: list[str] = []
    for xpath in (
        "//ytcp-uploads-dialog//*[self::span or self::div or self::p][contains(., '%') or contains(., 'Đã tải') or contains(., 'Uploading') or contains(., 'Uploaded')]",
        "//*[@role='dialog']//*[self::span or self::div or self::p][contains(., '%') or contains(., 'Đã tải') or contains(., 'Uploading') or contains(., 'Uploaded')]",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if not element.is_displayed():
                    continue
                text = re.sub(r"\s+", " ", (element.text or "").strip())
            except StaleElementReferenceException:
                continue
            if text:
                candidate_texts.append(text)

    for pattern in (
        r"Da tai(?: len)? duoc\s*(\d{1,3})%",
        r"Uploading\s*(\d{1,3})%",
        r"Uploaded\s*(\d{1,3})%",
        r"Đã tải(?: lên)? được\s*(\d{1,3})%",
    ):
        for text in candidate_texts:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if 0 <= value <= 100:
                return value / 100.0

    candidates: list[float] = []
    for xpath in (
        "//*[@aria-valuenow]",
        "//progress[@value]",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            if not element.is_displayed():
                continue
            raw_value = (element.get_attribute("aria-valuenow") or element.get_attribute("value") or "").strip()
            if not raw_value:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            if 0 <= value <= 100:
                candidates.append(value)

    if not candidates:
        return None
    return max(candidates) / 100.0


def _has_cancel_upload_button(driver: webdriver.Chrome) -> bool:
    for xpath in (
        "//button[contains(normalize-space(.), 'Huy tai len')]",
        "//button[contains(normalize-space(.), 'Cancel upload')]",
        "//button[contains(normalize-space(.), 'Hủy tải lên')]",
        "//ytcp-button[contains(normalize-space(.), 'Hủy tải lên')]",
        "//ytcp-button[contains(normalize-space(.), 'Cancel upload')]",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed():
                    return True
            except StaleElementReferenceException:
                continue
    return False


def _extract_dialog_status_text(driver: webdriver.Chrome) -> str:
    snippets: list[str] = []
    for xpath in (
        "//ytcp-uploads-dialog//*[self::span or self::div or self::p][contains(., 'Đã tải') or contains(., 'Dang tai') or contains(., 'đang xử lý') or contains(., 'processing') or contains(., 'uploaded') or contains(., 'Còn')]",
        "//*[@role='dialog']//*[self::span or self::div or self::p][contains(., 'Đã tải') or contains(., 'Dang tai') or contains(., 'đang xử lý') or contains(., 'processing') or contains(., 'uploaded') or contains(., 'Còn')]",
    ):
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if not element.is_displayed():
                    continue
                text = re.sub(r"\s+", " ", (element.text or "").strip())
            except StaleElementReferenceException:
                continue
            if text and len(text) <= 160:
                snippets.append(text)
    if snippets:
        return " | ".join(list(dict.fromkeys(snippets))[:3])

    page_text = re.sub(r"\s+", " ", _read_upload_dialog_text(driver)).strip()
    folded_page = _fold_text(page_text)
    if "da luu o che do rieng tu" in folded_page:
        return "Đã lưu ở chế độ riêng tư"
    if "da luu o che do ban nhap" in folded_page:
        return "Đã lưu ở chế độ bản nháp"
    if "qua trinh xu ly se som bat dau" in folded_page or "processing will begin shortly" in folded_page:
        return "Quá trình xử lý sẽ sớm bắt đầu"
    if "qua trinh xu ly bi cham tre den vai gio" in folded_page or "upload is delayed for a few hours" in folded_page:
        return "Quá trình xử lý bị chậm trễ đến vài giờ"
    if "dang xu ly video" in folded_page or "processing video" in folded_page:
        return "Đang xử lý video"
    for pattern in (
        r"(Đã tải(?: lên)? được\s*\d{1,3}%[^|]*)",
        r"(Uploaded\s*\d{1,3}%[^|]*)",
        r"(Đang tải video lên[^|]*)",
        r"(đang xử lý[^|]*)",
        r"(processing[^|]*)",
        r"(Đã lưu ở chế độ riêng tư)",
        r"(Đã lưu ở chế độ bản nháp)",
        r"(Saved as private)",
        r"(Saved as draft)",
    ):
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _dialog_has_percent_marker(driver: webdriver.Chrome) -> bool:
    try:
        dialog_text = _read_upload_dialog_text(driver)
    except Exception:
        return False
    return "%" in dialog_text


def _extract_uploaded_video_url(driver: webdriver.Chrome) -> str | None:
    for selector in (".video-url-fadeable", "a[href*='watch?v=']", "a[href*='youtu.be/']"):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not element.is_displayed():
                    continue
                href = (element.get_attribute("href") or "").strip()
                text = (element.text or "").strip()
            except StaleElementReferenceException:
                continue
            for candidate in (href, text):
                match = re.search(r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=[^\s&]+|youtu\.be/[^\s?&]+))", candidate, flags=re.IGNORECASE)
                if match:
                    return match.group(1).strip()
    return None


def _wait_for_uploaded_video_url(driver: webdriver.Chrome, *, timeout_seconds: int = 30) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    last_seen: str | None = None
    while time.monotonic() < deadline:
        _raise_if_upload_blocked(driver)
        candidate = _extract_uploaded_video_url(driver)
        if candidate:
            return candidate
        time.sleep(0.5)
    return last_seen


def _require_uploaded_video_url(uploaded_video_url: str | None) -> str:
    cleaned_url = str(uploaded_video_url or "").strip()
    if cleaned_url:
        return cleaned_url
    raise RuntimeError(
        "Khong lay duoc duong link video sau khi YouTube bao upload xong. "
        "Worker se khong danh dau job thanh cong khi chua verify duoc video trong Studio."
    )


def _wait_for_legacy_draft_upload_completion(
    driver: webdriver.Chrome,
    *,
    progress_callback: UploadProgressCallback | None,
    timeout_seconds: int = 3600,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    progress_state: dict[str, Any] = {"last_ratio": -1.0}
    upload_started = False

    while time.monotonic() < deadline:
        _raise_if_upload_blocked(driver)
        uploading_elements = driver.find_elements(By.CSS_SELECTOR, ".ytcp-uploads-dialog ytcp-video-upload-progress[uploading]")
        upload_started = upload_started or bool(uploading_elements)

        status_text = _extract_dialog_status_text(driver)
        ratio = _extract_upload_ratio(driver)
        if ratio is not None:
            _emit_upload_progress(
                progress_callback,
                max(0.0, min(0.99, ratio)),
                status_text or f"Uploading {int(ratio * 100)}%",
                state=progress_state,
            )

        if upload_started and not uploading_elements:
            if progress_callback:
                progress_callback(1.0, status_text or "Upload Hoan tat")
            return

        time.sleep(0.5)

    raise UploadTransferTimeoutError(
        f"Khong xac nhan duoc YouTube da hoan tat pha upload transfer sau {max(1, int(timeout_seconds // 60))} phut."
    )


def _emit_upload_progress(
    progress_callback: UploadProgressCallback | None,
    ratio: float,
    message: str,
    *,
    state: dict[str, Any],
) -> None:
    if not progress_callback:
        return
    now = time.monotonic()
    last_ratio = float(state.get("last_ratio", -1.0))
    clamped_ratio = max(0.0, min(1.0, ratio))
    if last_ratio >= 0:
        clamped_ratio = max(last_ratio, clamped_ratio)
    last_message = str(state.get("last_message", ""))
    last_emit_at = float(state.get("last_emit_at", 0.0))
    if (
        clamped_ratio >= 1.0
        or last_ratio < 0
        or abs(clamped_ratio - last_ratio) >= 0.01
        or message != last_message
        or now - last_emit_at >= 15.0
    ):
        state["last_ratio"] = clamped_ratio
        state["last_message"] = message
        state["last_emit_at"] = now
        progress_callback(clamped_ratio, message)


def _estimate_upload_completion_timeout_seconds(config: WorkerConfig, file_path: Path) -> int:
    base_timeout_seconds = 3600
    max_timeout_seconds = 12 * 3600
    duration_seconds = 0.0
    size_gb = 0.0

    try:
        duration_seconds = max(0.0, float(probe_media(config.ffprobe_bin, file_path).duration_seconds or 0.0))
    except Exception:
        duration_seconds = 0.0

    try:
        size_gb = max(0.0, float(file_path.stat().st_size) / float(1024 ** 3))
    except OSError:
        size_gb = 0.0

    duration_budget_seconds = int(duration_seconds * 0.25)
    size_budget_seconds = int(size_gb * 7200)
    timeout_seconds = max(
        base_timeout_seconds,
        1800 + duration_budget_seconds,
        1800 + size_budget_seconds,
    )
    return max(base_timeout_seconds, min(max_timeout_seconds, timeout_seconds))


def upload_video_via_browser(
    *,
    config: WorkerConfig,
    target: YouTubeUploadTarget,
    file_path: Path,
    progress_callback: UploadProgressCallback | None = None,
) -> BrowserUploadResult:
    if target.connection_mode != "browser_profile":
        raise RuntimeError("Browser uploader chi dung cho browser_profile channel.")
    if not file_path.exists():
        raise RuntimeError("Khong tim thay file output de upload browser.")

    browser_runtime = BrowserRuntimeManager(config.work_root / "browser-runtime")
    browser_config = browser_runtime.load_config()
    profile_path = Path(str(target.browser_profile_path or "").strip()) if target.browser_profile_path else None
    if profile_path is None or not profile_path.exists():
        if not target.browser_profile_key:
            raise RuntimeError("Channel browser_profile thieu browser profile key/path.")
        profile_path = browser_config.profile_root / str(target.browser_profile_key)
    if not profile_path.exists():
        raise RuntimeError(f"Khong tim thay Chrome profile cua kenh tai {profile_path}.")

    _kill_profile_processes(profile_path)
    for lock_name in ("SingletonCookie", "SingletonLock", "SingletonSocket"):
        (profile_path / lock_name).unlink(missing_ok=True)
    (profile_path / "DevToolsActivePort").unlink(missing_ok=True)
    display_number = _pick_display_number()
    display = f":{display_number}"
    xvfb_process: subprocess.Popen[bytes] | None = None
    openbox_process: subprocess.Popen[bytes] | None = None
    chromium_process: subprocess.Popen[bytes] | None = None
    driver: webdriver.Chrome | None = None
    studio_url = f"https://studio.youtube.com/channel/{target.channel_id}/videos?d=ud"
    upload_committed = False
    transfer_confirmed = False
    uploaded_video_url: str | None = None
    upload_timeout_seconds = _estimate_upload_completion_timeout_seconds(config, file_path)
    runtime_dir = config.work_root / "browser-upload-runtime" / target.job_id
    try:
        xvfb_process = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1440x900x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.7)
        if xvfb_process.poll() is not None:
            raise RuntimeError("Khong the khoi dong Xvfb cho browser upload.")

        runtime_dir.mkdir(parents=True, exist_ok=True)
        runtime_env = _build_browser_env(
            base_env=os.environ.copy(),
            display=display,
            profile_dir=profile_path,
            session_dir=runtime_dir,
            chromium_bin=browser_config.chromium_bin,
        )

        openbox_process = subprocess.Popen(
            ["openbox"],
            env=runtime_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)

        debug_port = _pick_unused_tcp_port()
        chromium_log = (runtime_dir / "chromium.log").open("ab")
        chromium_process = subprocess.Popen(
            [
                browser_config.chromium_bin,
                f"--user-data-dir={profile_path}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--password-store=basic",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-features=PasswordManagerOnboarding,PasswordsImport,Translate,MediaRouter,SigninIntercept,DiceWebSigninInterception",
                "--window-size=1440,900",
                "--lang=en-US",
                f"--remote-debugging-port={debug_port}",
                studio_url,
            ],
            env=runtime_env,
            stdout=chromium_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.5)
        if chromium_process.poll() is not None:
            raise RuntimeError("Chromium da thoat ngay sau khi khoi dong browser upload.")
        _wait_for_debug_endpoint(debug_port)

        options = ChromeOptions()
        options.binary_location = browser_config.chromium_bin
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")

        service = ChromeService(log_output=str(runtime_dir / "chromedriver.log"))
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 90)

        if progress_callback:
            progress_callback(0.01, "Da mo Chrome profile cua kenh")
        if "studio.youtube.com" not in (driver.current_url or ""):
            driver.get(studio_url)

        editor_boxes: tuple[object, object | None] | None = None
        for editor_attempt in range(2):
            if editor_attempt:
                if progress_callback:
                    progress_callback(0.02, "YouTube yeu cau lam moi trang, dang thu lai upload lan 2")
                _dismiss_refresh_required_upload_gate(driver)
                driver.get(studio_url)
                time.sleep(2.0)

            try:
                _attach_file_to_upload_dialog(
                    driver,
                    wait=WebDriverWait(driver, 45),
                    studio_url=studio_url,
                    file_path=file_path,
                    debug_root=config.work_root,
                    job_id=target.job_id,
                )
                _raise_if_upload_blocked(driver)
                if progress_callback:
                    progress_callback(0.03, "Da gan file output vao Studio upload")
                uploaded_video_url = _wait_for_uploaded_video_url(driver)
                editor_boxes = _find_title_and_description_boxes(driver, wait)
                break
            except RefreshRequiredUploadError as exc:
                stage = "editor-refresh-required" if editor_attempt == 0 else "editor-refresh-required-final"
                debug_dir = _capture_upload_debug_artifacts(
                    driver,
                    debug_root=config.work_root,
                    job_id=target.job_id,
                    stage=stage,
                )
                if editor_attempt >= 1:
                    detail = str(exc)
                    if debug_dir is not None:
                        detail = f"{detail} Debug: {debug_dir}"
                    raise RuntimeError(detail) from exc
                continue
            except TimeoutException as exc:
                debug_dir = _capture_upload_debug_artifacts(
                    driver,
                    debug_root=config.work_root,
                    job_id=target.job_id,
                    stage="editor-timeout",
                )
                detail = "Khong tim thay title/description editor tren YouTube Studio."
                if debug_dir is not None:
                    detail = f"{detail} Debug: {debug_dir}"
                raise RuntimeError(detail) from exc

        if editor_boxes is None:
            raise RuntimeError("Khong the mo lai form upload YouTube Studio sau khi lam moi trang.")
        _fill_upload_metadata(
            driver,
            wait,
            title=target.title,
        )
        upload_committed = True
        _raise_if_upload_blocked(driver)
        if progress_callback:
            progress_callback(0.05, "Da dien title")

        _click_when_available(
            driver,
            WebDriverWait(driver, 5),
            "//tp-yt-paper-radio-button[@name='VIDEO_MADE_FOR_KIDS_NOT_MFK']",
        )
        if progress_callback:
            progress_callback(0.08, "Da vao buoc Chi tiet, dang theo doi upload ban nhap")
        try:
            _wait_for_legacy_draft_upload_completion(
                driver,
                progress_callback=progress_callback,
                timeout_seconds=upload_timeout_seconds,
            )
        except UploadTransferTimeoutError as exc:
            debug_dir = _capture_upload_debug_artifacts(
                driver,
                debug_root=config.work_root,
                job_id=target.job_id,
                stage="transfer-timeout",
            )
            detail = str(exc)
            if debug_dir is not None:
                detail = f"{detail} Debug: {debug_dir}"
            raise RuntimeError(detail) from exc
        transfer_confirmed = True
        time.sleep(2.0)
        if not uploaded_video_url:
            uploaded_video_url = _wait_for_uploaded_video_url(driver, timeout_seconds=60)
        uploaded_video_url = _require_uploaded_video_url(uploaded_video_url)
        return BrowserUploadResult(
            studio_url=studio_url,
            output_url=uploaded_video_url,
            cleanup_safe=True,
            completion_message="Đã upload YouTube thành công",
        )
    except Exception as exc:
        blocking_error = (_detect_blocking_upload_error_safe(driver) or _detect_blocking_upload_error(driver)) if driver is not None else None
        if upload_committed and driver is not None and blocking_error is None and transfer_confirmed and uploaded_video_url:
            if progress_callback:
                progress_callback(1.0, "YouTube da nhan file upload va worker ket thuc theo mode draft")
            return BrowserUploadResult(
                studio_url=studio_url,
                output_url=_require_uploaded_video_url(uploaded_video_url),
                cleanup_safe=True,
                completion_message="Đã upload YouTube thành công",
            )
        raise RuntimeError(_format_browser_exception(exc, driver)) from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        _stop_process(chromium_process)
        _stop_process(openbox_process)
        _stop_process(xvfb_process)
        shutil.rmtree(runtime_dir, ignore_errors=True)

