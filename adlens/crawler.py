"""
crawler.py — Playwright-based stealth web crawler with network capture,
video recording, screenshots, and cookie consent dismissal.
"""

import os
import time
import random
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    Page,
    BrowserContext,
    Browser,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright_stealth import Stealth

from .config import (
    VIEWPORTS,
    USER_AGENTS,
    PAGE_LOAD_TIMEOUT_MS,
    SCROLL_PAUSE_MS,
    PROVENANCE_SCRIPT,
    COOKIE_CONSENT_SELECTORS,
    CONSENT_JS_DISMISS,
)
from .utils import extract_domain, ensure_dir, timeout_phase


class NetworkLog:
    """Captures and stores network requests/responses during a page visit."""

    def __init__(self, page_domain: str):
        self.page_domain = page_domain
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self._active = True

    def deactivate(self):
        """Stop recording to avoid callback races during browser teardown."""
        self._active = False

    def on_request(self, request):
        if not self._active:
            return
        try:
            self.requests.append({
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                # Avoid frame URL lookups during shutdown; they can race with page close.
                "frame_url": None,
                "timestamp": time.time(),
            })
        except Exception:
            pass

    def on_response(self, response):
        if not self._active:
            return
        try:
            self.responses.append({
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type", ""),
                # Avoid frame URL lookups during shutdown; they can race with page close.
                "frame_url": None,
                "timestamp": time.time(),
            })
        except Exception:
            pass

    def to_dict(self) -> dict:
        return {
            "total_requests": len(self.requests),
            "total_responses": len(self.responses),
            "requests": self.requests,
            "responses": self.responses,
        }


class StealthCrawler:
    """
    Crawls a page using Playwright with stealth mode, captures network activity,
    takes screenshots, and records video.
    """

    def __init__(self, viewport: str = "desktop", headless: bool = True, timeout_ms: int = PAGE_LOAD_TIMEOUT_MS, record_video: bool = True):
        self.viewport_name = viewport
        self.viewport = VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
        self.user_agent = USER_AGENTS.get(viewport, USER_AGENTS["desktop"])
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.record_video = record_video

        # Populated after crawl
        self.page: Optional[Page] = None
        self.context: Optional[BrowserContext] = None
        self.browser: Optional[Browser] = None
        self.playwright: Optional[Playwright] = None
        self.network_log: Optional[NetworkLog] = None
        self.session_dir: str = ""
        self.domain: str = ""

    def crawl(self, url: str, session_dir: str) -> Tuple[Page, NetworkLog]:
        """
        Navigate to a URL with stealth mode, capture network, scroll to trigger
        lazy ads, dismiss cookies, take full-page screenshot. Returns the live
        Page handle and NetworkLog for downstream analysis.

        IMPORTANT: caller must call self.close() when done.
        """
        self.session_dir = session_dir
        self.domain = extract_domain(url)
        video_dir = ensure_dir(os.path.join(session_dir, "videos")) if self.record_video else None

        # --- Launch browser ---
        self.playwright = sync_playwright().start()
        
        # Enhanced stealth arguments
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--exclude-switches=enable-automation",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=IsolateOrigins,site-per-process",
            "--mute-audio",
        ]
        
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=browser_args,
        )
        
        # Randomize viewport slightly to avoid fingerprinting
        width_jitter = random.randint(-15, 15)
        height_jitter = random.randint(-15, 15)
        stealth_viewport = {
            "width": self.viewport["width"] + width_jitter,
            "height": self.viewport["height"] + height_jitter
        }
        
        context_kwargs = {
            "viewport": stealth_viewport,
            "user_agent": self.user_agent,
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua-Platform": '"Windows"' if "Windows" in self.user_agent else '"Linux"',
                "Upgrade-Insecure-Requests": "1",
            },
        }
        if video_dir:
            context_kwargs["record_video_dir"] = video_dir
            context_kwargs["record_video_size"] = self.viewport
        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()

        # --- Stealth ---
        Stealth().apply_stealth_sync(self.page)

        # --- Provenance tracker injection ---
        self.page.add_init_script(PROVENANCE_SCRIPT)

        # --- Network capture ---
        self.network_log = NetworkLog(self.domain)
        self.page.on("request", self.network_log.on_request)
        self.page.on("response", self.network_log.on_response)

        # --- Navigate ---
        print(f"  [Crawler] Navigating to {url}")
        page_has_content = False
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            print(f"  [Crawler] First response received, waiting for content...")
            # Wait for DOM to be usable, but don't block forever
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
                print(f"  [Crawler] DOM ready")
            except PlaywrightTimeoutError:
                print(f"  [Crawler] DOM not fully parsed after 15s, continuing anyway...")
            # Wait for initial resources (scripts, images) to finish loading.
            # This is when ad scripts (async/defer) finish executing and inject
            # ad slots into the DOM. Cap at 10s to avoid hanging.
            try:
                self.page.wait_for_load_state("load", timeout=10_000)
                print(f"  [Crawler] Page fully loaded (scripts + resources)")
            except PlaywrightTimeoutError:
                print(f"  [Crawler] Full load not complete after 10s, continuing with what we have...")
            # Brief settle time for ad auctions / bid responses
            self.page.wait_for_timeout(2000)
            page_has_content = True
        except PlaywrightTimeoutError:
            print(f"  [Crawler] Page load timed out after 30s, checking if any content loaded...")
            # Check if there's any usable content despite the timeout
            try:
                body_len = self.page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
                if body_len and body_len > 100:
                    print(f"  [Crawler] Partial content available ({body_len} chars), continuing...")
                    page_has_content = True
                else:
                    print(f"  [Crawler] Page is empty or blocked. Will still attempt analysis...")
            except Exception:
                print(f"  [Crawler] Cannot evaluate page state. Continuing with empty page...")

        if page_has_content:
            # --- Dismiss cookie consent ---
            print(f"  [Crawler] Checking for cookie banners...")
            self._dismiss_cookies()

            # --- Simulate human behavior ---
            print(f"  [Crawler] Simulating browsing (scroll to trigger lazy ads)...")
            self._simulate_browsing()
        else:
            print(f"  [Crawler] Skipping cookie/scroll — insufficient page content")

        # --- Full page screenshot ---
        # --- Full page screenshot ---
        try:
            full_page_path = os.path.join(session_dir, "full_page.png")
            # Use both Playwright timeout and strict signal timeout
            with timeout_phase(20, "Full page screenshot"):
                self.page.screenshot(path=full_page_path, full_page=True, timeout=19000)
            print(f"  [Crawler] Full page screenshot saved")
        except Exception as e:
            print(f"  [Crawler] Full page screenshot failed/timed out: {e}")
            # Fallback to viewport screenshot
            try:
                viewport_path = os.path.join(session_dir, "viewport.png")
                # Use strict timeout for fallback too
                with timeout_phase(10, "Fallback screenshot"):
                    self.page.screenshot(path=viewport_path, full_page=False, timeout=9000)
                print(f"  [Crawler] Fallback viewport screenshot saved")
            except Exception as e2:
                print(f"  [Crawler] Fallback screenshot failed: {e2}")

        # --- Save network log ---
        from .utils import save_json
        save_json(self.network_log.to_dict(), os.path.join(session_dir, "network_log.json"))

        return self.page, self.network_log

    def _dismiss_cookies(self, max_time: float = 10.0):
        """Try to dismiss cookie / privacy consent banners.

        Strategy order:
        1. CSS selector click (fast, covers most CMPs).
        2. JS API calls (programmatic "accept all" on known CMPs).
        3. Force-hide any remaining high-z-index overlays that cover
           a significant portion of the viewport.
        """
        deadline = time.time() + max_time

        # ── 1. CSS-selector click ──────────────────────────────────
        for selector in COOKIE_CONSENT_SELECTORS:
            if time.time() >= deadline:
                print(f"  [Crawler] Cookie dismissal time limit reached")
                return
            try:
                locator = self.page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=1000):
                    locator.click(timeout=2000)
                    print(f"  [Crawler] Dismissed consent via selector: {selector}")
                    self.page.wait_for_timeout(500)
                    return
            except Exception:
                continue

        # ── 2. JS API calls ────────────────────────────────────────
        for js_snippet in CONSENT_JS_DISMISS:
            if time.time() >= deadline:
                break
            try:
                result = self.page.evaluate(js_snippet)
                if result:
                    print(f"  [Crawler] Dismissed consent via JS API")
                    self.page.wait_for_timeout(500)
                    return
            except Exception:
                continue

        # ── 3. Force-hide persistent overlays ──────────────────────
        try:
            hidden = self.page.evaluate("""() => {
                let count = 0;
                const vw = window.innerWidth, vh = window.innerHeight;
                for (const el of document.querySelectorAll('*')) {
                    const s = window.getComputedStyle(el);
                    if ((s.position === 'fixed' || s.position === 'sticky') &&
                        parseInt(s.zIndex) > 500) {
                        const r = el.getBoundingClientRect();
                        if (r.width > vw * 0.5 && r.height > vh * 0.3) {
                            el.style.setProperty('display', 'none', 'important');
                            count++;
                        }
                    }
                }
                // Also remove fides-overlay class from body (Condé Nast sites)
                document.body.classList.remove('fides-overlay');
                return count;
            }""")
            if hidden:
                print(f"  [Crawler] Force-hid {hidden} overlay(s)")
        except Exception:
            pass

    def _simulate_browsing(self, max_time: float = 30.0):
        """Scroll down and back up the page to trigger lazy-loaded ads.
        
        Enforces a strict wall-clock time limit (default 30s) to avoid
        getting stuck on infinite-scroll or unresponsive pages.
        """
        deadline = time.time() + max_time
        
        def _time_left():
            return deadline - time.time()
        
        try:
            if _time_left() <= 0:
                return
                
            # Initial mouse movement
            self._human_mouse_move(
                random.randint(0, 100), random.randint(0, 100),
                random.randint(100, 400), random.randint(100, 300),
                steps=8
            )
            self.page.wait_for_timeout(300)

            # Get page height (with a safety timeout)
            try:
                page_height = self.page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            except Exception:
                print(f"  [Crawler] Could not read page height, skipping scroll")
                return
                
            if not page_height or page_height <= 0:
                print(f"  [Crawler] Page height is 0, skipping scroll")
                return
                
            viewport_height = self.viewport["height"]
            scroll_step = viewport_height * 0.7
            
            # Cap max scroll distance to avoid very long scrolls on tall pages
            max_scroll = min(page_height, viewport_height * 15)

            # ── Scroll DOWN ──────────────────────────
            scroll_position = 0
            while scroll_position < max_scroll and _time_left() > 5:
                scroll_amount = scroll_step + random.randint(-100, 100)
                self.page.mouse.wheel(0, scroll_amount)
                scroll_position += scroll_amount
                pause = SCROLL_PAUSE_MS + random.randint(-200, 300)
                self.page.wait_for_timeout(max(pause, 200))

                if random.random() > 0.6:
                    self._human_mouse_move(
                        random.randint(0, self.viewport["width"]),
                        random.randint(0, viewport_height),
                        random.randint(50, self.viewport["width"] - 50),
                        random.randint(50, viewport_height - 50),
                        steps=6
                    )

            # Brief pause at bottom
            if _time_left() > 3:
                self.page.wait_for_timeout(600)

            # ── Scroll back UP ───────────────────────
            while scroll_position > 0 and _time_left() > 2:
                scroll_amount = scroll_step + random.randint(-100, 100)
                self.page.mouse.wheel(0, -scroll_amount)
                scroll_position -= scroll_amount
                pause = SCROLL_PAUSE_MS + random.randint(-200, 200)
                self.page.wait_for_timeout(max(pause, 200))

            # Ensure we're at top for the final screenshot
            try:
                self.page.evaluate("window.scrollTo(0, 0)")
                self.page.wait_for_timeout(500)
            except Exception:
                pass
                
            if _time_left() <= 0:
                print(f"  [Crawler] Scroll time limit reached ({max_time}s)")

        except Exception as e:
            print(f"  [Crawler] Scrolling error (non-fatal): {e}")

    def _human_mouse_move(self, start_x, start_y, end_x, end_y, steps=10):
        """Move mouse in a slightly curved/noisy path."""
        for i in range(steps):
            t = (i + 1) / steps
            # Linear interpolation
            x = start_x + (end_x - start_x) * t
            y = start_y + (end_y - start_y) * t
            # Add noise
            x += random.randint(-5, 5)
            y += random.randint(-5, 5)
            self.page.mouse.move(x, y)
            time.sleep(random.uniform(0.01, 0.05))

    def close(self):
        """Close browser and clean up resources."""
        try:
            if self.network_log:
                self.network_log.deactivate()
        except Exception:
            pass

        # 0. Detach listeners to prevent errors during shutdown
        try:
            if self.page and self.network_log:
                self.page.remove_listener("request", self.network_log.on_request)
                self.page.remove_listener("response", self.network_log.on_response)
        except Exception:
            pass

        # 0.5 Close Page first to drain page-scoped events before context shutdown.
        try:
            if self.page and not self.page.is_closed():
                with timeout_phase(2, "Page Cleanup"):
                    self.page.close()
        except Exception:
            pass

        # 1. Close Context
        try:
            if self.context:
                # Short timeout for context
                with timeout_phase(2, "Context Cleanup"):
                    self.context.close()
        except Exception:
            pass

        # 2. Close Browser
        try:
            if self.browser:
                # Moderate timeout for browser
                with timeout_phase(3, "Browser Cleanup"):
                    self.browser.close()
        except Exception:
            pass

        # 3. Stop Playwright (Critical to avoid zombie Node process)
        try:
            if self.playwright:
                # Ensure we send the stop signal
                with timeout_phase(2, "Playwright Stop"):
                    self.playwright.stop()
        except Exception:
            pass

        # Rename video file to something readable
        if self.record_video:
            try:
                video_dir = os.path.join(self.session_dir, "videos")
                if os.path.exists(video_dir):
                    for f in os.listdir(video_dir):
                        if f.endswith(".webm"):
                            src = os.path.join(video_dir, f)
                            dst = os.path.join(self.session_dir, "session_recording.webm")
                            os.rename(src, dst)
                            break
                    # Remove empty video dir
                    try:
                        os.rmdir(video_dir)
                    except OSError:
                        pass
            except Exception:
                pass
