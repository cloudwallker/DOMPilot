"""A synchronous, bounded Playwright session for semantic browser actions."""

import os
import time
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from dompilot.actions import (
    ActionResult,
    AgentDecision,
    RunConfig,
    build_action_space,
    compact_json,
    fit_snapshot,
    validate_decision,
)
from dompilot.snapshot import capture_snapshot, current_signature, detect_blocked


class BrowserSession:
    """One Chromium context. Decisions only address nodes from its latest observation."""

    def __init__(self, config: RunConfig | None = None, *, headless: bool | None = None):
        self.config = config or RunConfig()
        self.headless = self.config.headless if headless is None else headless
        self.page = None
        self.blocked_reason: str | None = None
        self.metadata: dict[str, str] = {}
        self.deadline: float | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._session_id = uuid.uuid4().hex
        self._sequence = 0
        self._snapshot = None
        self._handles = {}
        self._signatures = {}
        self._document = None
        self._consumed = True
        self._navigation_generation = 0
        self._snapshot_generation = 0

    def __enter__(self):
        if self.page is not None and not self.page.is_closed():
            return self
        launch_timeout = self._timeout_ms(self.config.navigation_timeout)
        channel = os.environ.get("DOMPILOT_BROWSER_CHANNEL")
        if channel not in (None, "", "chrome", "msedge"):
            raise ValueError("unsupported_browser_channel")
        self._playwright = sync_playwright().start()
        try:
            options = {
                "headless": self.headless,
                "timeout": min(launch_timeout, self._timeout_ms(self.config.navigation_timeout)),
            }
            if channel:
                options["channel"] = channel
            self._browser = self._playwright.chromium.launch(**options)
            self.metadata = {"channel": channel or "chromium", "version": self._browser.version}
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800}, accept_downloads=False
            )
            self._context.on("page", self._on_new_page)
            self.page = self._context.new_page()
            self.page.on("dialog", self._on_dialog)
            self.page.on("download", self._on_download)
            self.page.on("filechooser", self._on_filechooser)
            self.page.on("framenavigated", self._on_navigation)
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *_exc):
        self.close()

    @property
    def alive(self) -> bool:
        return (
            self.page is not None
            and not self.page.is_closed()
            and self._browser is not None
            and self._browser.is_connected()
        )

    def _on_new_page(self, page):
        if page is self.page or self.page is None:
            return
        self.blocked_reason = "popup_blocked"
        try:
            page.close()
        except PlaywrightError:
            pass

    def _on_dialog(self, dialog):
        self.blocked_reason = "dialog_blocked"
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_download(self, download):
        self.blocked_reason = "download_blocked"
        try:
            download.cancel()
        except PlaywrightError:
            pass

    def _on_filechooser(self, _chooser):
        self.blocked_reason = "upload_blocked"

    def _on_navigation(self, frame):
        if self.page is None or frame != self.page.main_frame:
            return
        self._navigation_generation += 1
        parts = urlsplit(frame.url)
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            self.blocked_reason = "navigation_blocked"

    def _timeout_ms(self, seconds: float) -> float:
        if self.deadline is not None:
            seconds = min(seconds, self.deadline - time.monotonic())
        if seconds <= 0:
            raise TimeoutError("deadline_exceeded")
        return max(1, seconds * 1000)

    def _clear_snapshot(self):
        for handle in self._handles.values():
            try:
                handle.dispose()
            except PlaywrightError:
                pass
        self._handles = {}
        self._signatures = {}
        if self._document is not None:
            try:
                self._document.dispose()
            except PlaywrightError:
                pass
        self._document = None
        self._snapshot = None
        self._consumed = True

    def open(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("http(s)_url_required")
        if parts.username or parts.password:
            raise ValueError("url_credentials_forbidden")
        if self.page is None and self._playwright is None:
            self.__enter__()
        if self.page is None or self.page.is_closed():
            raise RuntimeError("browser_closed")
        self._clear_snapshot()
        self.blocked_reason = None
        self.page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self._timeout_ms(self.config.navigation_timeout),
        )
        if self.blocked_reason:
            raise RuntimeError(self.blocked_reason)

    def observe(self):
        if self.page is None or self.page.is_closed():
            raise RuntimeError("browser_closed")
        self._timeout_ms(self.config.action_timeout)
        self._clear_snapshot()
        self._sequence += 1
        snapshot, handles, signatures = capture_snapshot(
            self.page, f"{self._session_id}:{self._sequence}"
        )
        snapshot = fit_snapshot(snapshot)
        exposed = {item.id for item in snapshot.targets}
        for target_id, handle in handles.items():
            if target_id not in exposed:
                handle.dispose()
        self._handles = {key: value for key, value in handles.items() if key in exposed}
        self._signatures = {key: value for key, value in signatures.items() if key in exposed}
        self._document = self.page.evaluate_handle("document")
        self._snapshot_generation = self._navigation_generation
        if self.blocked_reason is None and snapshot.blocked_reason:
            self.blocked_reason = snapshot.blocked_reason
        if self.blocked_reason is not None:
            snapshot = snapshot.model_copy(update={"blocked_reason": self.blocked_reason})
        self._snapshot = snapshot
        self._consumed = False
        return snapshot

    def check_blocked(self) -> str | None:
        """Catch a challenge inserted since the last observation without consuming it."""
        if self.blocked_reason or self.page is None or self.page.is_closed():
            return self.blocked_reason
        try:
            reason = detect_blocked(self.page)
        except PlaywrightError:
            return self.blocked_reason
        if reason:
            self.blocked_reason = reason
        return self.blocked_reason

    def execute(self, decision: AgentDecision) -> ActionResult:
        start = time.monotonic()

        def result(success, error=None, *, executed=False, side_effect_possible=False):
            return ActionResult(
                success=success,
                error=error,
                executed=executed,
                side_effect_possible=side_effect_possible,
                latency=time.monotonic() - start,
            )

        snapshot = self._snapshot
        if snapshot is None or self._consumed:
            return result(False, "stale_target")
        self._consumed = True
        if self.page is None or self.page.is_closed() or not self.alive:
            return result(False, "browser_closed")
        if self.check_blocked():
            return result(False, self.blocked_reason)
        try:
            if (
                self._snapshot_generation != self._navigation_generation
                or self.page.url != snapshot.url
                or not self._document.evaluate("node => node === document")
            ):
                return result(False, "stale_target")
        except PlaywrightError:
            return result(False, "stale_target")
        try:
            # Even internal callers pass through the same strict validator as model replies.
            action = validate_decision(
                compact_json(decision.model_dump()), snapshot, build_action_space(snapshot)
            ).decision
        except (ValueError, TypeError) as exc:
            return result(False, str(exc))
        name = action.action
        if name in ("DONE", "BLOCKED"):
            return result(True)
        handle = None
        if name in ("CLICK", "TYPE_TEXT", "SELECT"):
            handle = self._handles.get(action.target)
            if handle is None:
                return result(False, "stale_target")
            try:
                if current_signature(handle) != self._signatures[action.target]:
                    return result(False, "stale_target")
            except PlaywrightError:
                return result(False, "stale_target")
        try:
            timeout = self._timeout_ms(self.config.action_timeout)
            if name == "CLICK":
                handle.click(timeout=timeout)
            elif name == "TYPE_TEXT":
                handle.fill(action.text, timeout=timeout)
            elif name == "SELECT":
                handle.select_option(value=action.value, timeout=timeout)
            elif name in ("SCROLL_UP", "SCROLL_DOWN"):
                direction = -1 if name == "SCROLL_UP" else 1
                self.page.evaluate(
                    "direction => window.scrollBy(0, direction * innerHeight * 0.8)", direction
                )
            elif name == "WAIT":
                self.page.wait_for_timeout(min(500, timeout))
            if self.blocked_reason:
                return result(False, self.blocked_reason, executed=True, side_effect_possible=True)
            return result(True, executed=True)
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            return result(False, str(exc), executed=True, side_effect_possible=True)
        except PlaywrightError as exc:
            return result(False, str(exc), executed=True, side_effect_possible=True)

    def settle(self) -> float:
        """Wait briefly for DOM changes, with a hard one-second cap."""
        start = time.monotonic()
        if self.page is None or self.page.is_closed() or self.blocked_reason:
            return 0.0
        try:
            remaining = min(1.0, self._timeout_ms(1.0) / 1000)
            if self.page.evaluate("document.readyState") == "loading":
                self.page.wait_for_load_state("domcontentloaded", timeout=remaining * 1000)
            remaining = min(1.0 - (time.monotonic() - start), self._timeout_ms(1.0) / 1000)
            if remaining > 0:
                self.page.evaluate(
                    """milliseconds => new Promise(resolve => {
                    let quiet;
                    const finish = () => { observer.disconnect(); clearTimeout(quiet); resolve(); };
                    const observer = new MutationObserver(() => {
                        clearTimeout(quiet);
                        quiet = setTimeout(finish, 100);
                    });
                    observer.observe(document, {subtree: true, childList: true,
                                                attributes: true, characterData: true});
                    quiet = setTimeout(finish, 100);
                    setTimeout(finish, milliseconds);
                })""",
                    round(remaining * 1000),
                )
        except (PlaywrightError, TimeoutError):
            pass
        return time.monotonic() - start

    def close(self) -> None:
        self._clear_snapshot()
        for item in (self._context, self._browser, self._playwright):
            if item is None:
                continue
            try:
                item.stop() if item is self._playwright else item.close()
            except PlaywrightError:
                pass
        self._context = self._browser = self._playwright = None
        self.page = None
