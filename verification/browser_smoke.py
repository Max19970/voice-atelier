"""Browser smoke for Voice Atelier using Playwright + the already-installed Edge.

Design constraints:
  * Uses ``channel="msedge"`` and NEVER downloads a Playwright browser. If Playwright or
    Edge is unavailable the script reports NOT RUN and exits 3 — it must never be
    confused with an audio/engine failure.
  * Drives the real UI: creates an isolated test project through actual clicks, types a
    script line, waits for autosave, navigates away and back, switches locale, and checks
    that user text is rendered as text (no HTML injection).
  * Does not touch GPU generation, voices or user recordings. It only creates/deletes its
    own project, and only after the UI created it.
  * Screenshots RU and EN for Studio, Voices, Voice Design and Models.

Exit codes: 0 PASS · 1 FAIL · 3 NOT RUN (tooling/UI/server missing).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

TABS = ("studio", "voices", "design", "models")

# Namespaces that exist in web/js/i18n.js. A raw, unresolved key reaches the DOM
# exactly as "<namespace>.<someKey>" (e.g. "common.saved", "studio.segments"), so a
# rendered key is detectable from user-visible text and attributes.
_I18N_NAMESPACES = "common|studio|voices|design|models|status|jobs|nav|errors|take"
I18N_KEY_RE = re.compile(
    rf"(?<![\w.])(?:{_I18N_NAMESPACES})\.[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)*"
)
# Technical identifiers legitimately contain underscores or dashes (model ids, error
# codes like "model_not_installed", warning text), never "<namespace>.<key>", so the
# regex above does not flag them. Add explicit exceptions here only if a real one shows up.
I18N_ALLOWED: frozenset[str] = frozenset()

# Collect everything the user can actually read: rendered text, document title, and the
# text-bearing attributes of visible controls. <option> text is added separately because
# innerText only exposes the selected option.
_I18N_SCAN_JS = """
() => {
  const out = [];
  const push = (kind, value, where) => {
    if (typeof value === 'string' && value.trim()) out.push({kind, value, where});
  };
  push('body.innerText', document.body ? document.body.innerText : '', 'body');
  push('document.title', document.title, 'title');
  for (const el of Array.from(document.querySelectorAll('[placeholder],[title],[aria-label],[alt]'))) {
    const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    if (!visible) continue;
    let where = el.tagName.toLowerCase();
    if (el.id) where = '#' + el.id;
    else if (typeof el.className === 'string' && el.className.trim()) {
      where += '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.');
    }
    for (const attr of ['placeholder', 'title', 'aria-label', 'alt']) {
      if (el.hasAttribute(attr)) push(attr, el.getAttribute(attr), where);
    }
  }
  for (const opt of Array.from(document.querySelectorAll('option'))) {
    push('option.text', opt.textContent, 'option');
  }
  return out;
}
"""

# Deliberately harmless but markup-shaped: if it is ever injected as HTML, the
# onerror handlers would set these globals and the check would catch it.
XSS_TITLE = '<img src=x onerror="window.__xssTitle=1">'
XSS_TEXT = '<img src=x onerror="window.__xssText=1"><script>window.__xssScript=1</script>'
TEST_LINE = "Проверочная строка для браузерного смоук-теста интерфейса."


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def extract_token(html: str) -> str | None:
    for tag in TOKEN_META_RE.findall(html):
        attrs = {}
        for name, quoted, single in ATTR_RE.findall(tag):
            attrs[name.lower()] = quoted or single
        if attrs.get("name") == "studio-token" and attrs.get("content"):
            return attrs["content"]
    return None


def tail(path: Path, limit: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | NOTRUN
    detail: str = ""
    elapsed_s: float = 0.0


@dataclass
class BrowserReport:
    checks: list[Check] = field(default_factory=list)
    console_errors: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    bad_responses: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    not_run_reason: str | None = None
    fatal: str | None = None

    def add(self, name: str, status: str, started: float, detail: str = "") -> Check:
        check = Check(name, status, detail, round(time.monotonic() - started, 3))
        self.checks.append(check)
        marker = {"PASS": "PASS", "FAIL": "FAIL", "NOTRUN": "NORUN"}[status]
        line = f"[{marker}] {name} ({check.elapsed_s}s)"
        if detail:
            line += f" :: {detail}"
        print(line, flush=True)
        return check

    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "NOTRUN": 0}
        for check in self.checks:
            out[check.status] = out.get(check.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "generated_at": utcnow(),
            "driver": "verification/browser_smoke.py",
            "environment": self.environment,
            "summary": {
                **counts,
                "verdict": "NOT RUN"
                if self.not_run_reason
                else ("PASS" if counts["FAIL"] == 0 else "FAIL"),
                "note": "browser result is independent of the audio smoke result",
            },
            "not_run_reason": self.not_run_reason,
            "fatal": self.fatal,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "elapsed_s": check.elapsed_s,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
            "bad_responses": self.bad_responses,
            "screenshots": self.screenshots,
            "limitations": self.limitations,
        }


class NotRun(RuntimeError):
    """Tooling/UI/server is unavailable: report NOT RUN, never FAIL."""


class Failed(RuntimeError):
    """An actual UI contract violation."""


class BrowserSmoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.project_root).resolve()
        self.artifacts = Path(args.artifacts_dir).resolve()
        self.base_url = args.base_url.rstrip("/")
        self.report = BrowserReport()
        self.token: str | None = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.server: subprocess.Popen[bytes] | None = None
        self.server_log_handle = None
        self.server_log = self.root / "verification" / "server-browser.log"
        self.exit_code = 0
        self.project_id: str | None = None
        self.project_title = f"UI smoke {datetime.now():%Y-%m-%d %H:%M:%S}"

    # -- setup -------------------------------------------------------------------------
    def preflight(self) -> None:
        if not (self.root / "web").is_dir():
            raise NotRun("web/ directory is missing; nothing to test")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.report.environment = {
            "started_at": utcnow(),
            "base_url": self.base_url,
            "mode": "own-server" if self.args.own_server else "attach",
            "headless": not self.args.headed,
            "channel": "msedge",
            "playwright_required": self.args.playwright_version,
            "artifacts": str(self.artifacts),
        }
        self.report.limitations = [
            "Not a visual regression test: no pixel baselines or diffs.",
            "Headless Edge cannot test real microphone capture or MediaRecorder permissions.",
            "No synthesis is triggered here; audio correctness is covered by verification/smoke.py.",
            "Only the chromium engine with the msedge channel is exercised (no Firefox/WebKit).",
            "Any console error now fails the run (favicon 404 is no longer exempt because "
            "web/favicon.svg exists); failed requests and 4xx API responses are still only reported.",
            "The i18n scan covers rendered body text, document title, <option> text and the "
            "placeholder/title/aria-label/alt of visible elements; text drawn inside <canvas> "
            "cannot be inspected.",
        ]

    def ensure_server(self) -> None:
        if port_is_open("127.0.0.1", self.args.port):
            self.report.environment["server_owner"] = "external"
            return
        if not self.args.own_server:
            raise NotRun(
                f"nothing is listening on {self.base_url}. Start the studio first "
                "(python -m studio --port 8765) or pass --own-server."
            )
        cmd = [self.args.python, "-m", "studio", "--port", str(self.args.port)]
        if self.args.home:
            cmd.extend(["--home", str(self.args.home)])
        self.server_log.parent.mkdir(parents=True, exist_ok=True)
        self.server_log_handle = self.server_log.open("w", encoding="utf-8", errors="replace")
        self.server_log_handle.write(f"$ {' '.join(cmd)}\ncwd={self.root}\n\n")
        self.server_log_handle.flush()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if self.args.home:
            env["VOICE_ATELIER_HOME"] = str(self.args.home)
        else:
            env.pop("VOICE_ATELIER_HOME", None)
        self.server = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=str(self.root),
            stdout=self.server_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self.report.environment["server_pid"] = self.server.pid
        deadline = time.monotonic() + self.args.boot_timeout
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise NotRun(
                    "own server exited before becoming healthy:\n" + tail(self.server_log)
                )
            if port_is_open("127.0.0.1", self.args.port):
                return
            time.sleep(0.4)
        raise NotRun(f"own server did not open port {self.args.port} within {self.args.boot_timeout}s")

    def fetch_token(self) -> None:
        try:
            with urllib.request.urlopen(self.base_url + "/", timeout=15) as response:
                html = response.read().decode("utf-8", errors="replace")
        except OSError as error:
            raise NotRun(f"could not load {self.base_url}/ : {error}") from error
        token = extract_token(html)
        if not token:
            raise NotRun("index HTML has no <meta name='studio-token'>; server not usable")
        self.token = token
        self.report.environment["token_source"] = "index meta"
        self.report.environment["index_bytes"] = len(html)
        if "Web interface is not installed yet" in html:
            raise NotRun(
                "web/index.html is missing (server is serving the placeholder page); "
                "browser smoke needs the frontend installed."
            )

    def launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise NotRun(
                f"playwright is not installed in this interpreter ({error}). "
                f"Install exactly: pip install playwright=={self.args.playwright_version} "
                "— no browser download is needed because channel='msedge' is used."
            ) from error

        try:
            self.playwright = sync_playwright().start()
        except Exception as error:  # noqa: BLE001
            raise NotRun(f"could not start playwright: {error}") from error

        try:
            # channel="msedge" reuses the installed Edge; we never call
            # ``playwright install`` and never fetch a bundled browser.
            self.browser = self.playwright.chromium.launch(
                channel="msedge", headless=not self.args.headed
            )
        except Exception as error:  # noqa: BLE001
            raise NotRun(
                "could not launch Microsoft Edge via channel='msedge' "
                f"({str(error).splitlines()[0] if str(error) else 'unknown error'}). "
                "Edge must already be installed; this script never downloads browsers."
            ) from error

        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 960},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.args.timeout)
        self._instrument()

    def _instrument(self) -> None:
        page = self.page
        assert page is not None

        def on_console(message: Any) -> None:
            if message.type == "error":
                self.report.console_errors.append(
                    {"type": "console", "text": message.text, "location": message.location}
                )

        def on_page_error(error: BaseException) -> None:
            self.report.page_errors.append({"type": "pageerror", "message": str(error)})

        def on_request_failed(request: Any) -> None:
            failure = request.failure or ""
            self.report.failed_requests.append(
                {"type": "requestfailed", "url": request.url, "failure": failure}
            )

        def on_response(response: Any) -> None:
            try:
                if response.status >= 400 and "/api/" in response.url:
                    self.report.bad_responses.append(
                        {"type": "response", "url": response.url, "status": response.status}
                    )
            except Exception:
                pass

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

    # -- helpers -----------------------------------------------------------------------
    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            raise Failed(message)

    def shot(self, name: str) -> None:
        assert self.page is not None
        target = self.artifacts / f"{name}.png"
        self.page.screenshot(path=str(target), full_page=False)
        self.report.screenshots.append(
            {"name": name, "path": str(target), "bytes": target.stat().st_size if target.is_file() else 0}
        )

    def goto(self) -> None:
        assert self.page is not None
        self.page.goto(self.base_url + "/", wait_until="load")
        try:
            self.page.wait_for_selector('#tabs [data-tab="studio"]', timeout=self.args.timeout)
        except Exception as error:  # noqa: BLE001
            raise NotRun(
                "app shell did not render (#tabs [data-tab=studio] never appeared). "
                f"web/index.html may be absent or the boot script failed: {error}"
            ) from error
        # The shell renders status asynchronously; give the first paint a moment but do
        # not fail the run if the engine is simply unavailable.
        try:
            self.page.wait_for_function(
                "() => document.querySelectorAll('#tabs [data-tab]').length >= 5",
                timeout=self.args.timeout,
            )
        except Exception as error:  # noqa: BLE001
            raise NotRun(f"expected five navigation tabs, not found: {error}") from error

    def click_tab(self, tab: str) -> None:
        assert self.page is not None
        self.page.click(f'#tabs [data-tab="{tab}"]')
        self.page.wait_for_function(
            "(tab) => { const v = document.getElementById('view');"
            " return v && v.dataset.tab === tab; }",
            arg=tab,
            timeout=self.args.timeout,
        )

    def lang(self) -> str:
        assert self.page is not None
        return str(self.page.evaluate("() => document.documentElement.lang"))

    def wait_saved(self, timeout: float | None = None) -> str:
        """Wait for autosave to settle on 'saved' and return the final data-state."""
        assert self.page is not None
        limit_ms = timeout or self.args.save_timeout
        deadline = time.monotonic() + (limit_ms / 1000.0)
        state = ""
        while time.monotonic() < deadline:
            state = str(
                self.page.evaluate(
                    "() => { const el = document.querySelector('#autosave');"
                    " return el ? el.dataset.state : ''; }"
                )
            )
            if state == "saved":
                return state
            if state == "error":
                return state
            time.sleep(0.25)
        return state

    def api(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        assert self.token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"X-Studio-Token": self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                try:
                    return response.status, json.loads(payload)
                except json.JSONDecodeError:
                    return response.status, payload.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw.decode("utf-8", errors="replace")

    # -- checks ------------------------------------------------------------------------
    def check_shell(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        self.expect(self.lang() == "ru", f"default locale should be ru, got {self.lang()!r}")
        title = self.page.title()
        self.expect("Voice Atelier" in title, f"document title should mention Voice Atelier, got {title!r}")
        tokens = str(self.page.evaluate("() => document.querySelector('meta[name=\"studio-token\"]')?.content || ''"))
        self.expect(bool(tokens), "page has no studio-token meta")
        self.shot("ru-studio")
        self.report.add("shell:boot-ru", "PASS", started, f"title={title!r} lang=ru")

    def check_tabs_screenshots(self) -> None:
        started = time.monotonic()
        visited = []
        for tab in TABS:
            self.click_tab(tab)
            heading = ""
            try:
                heading = self.page.inner_text("#page-title")
            except Exception:
                heading = ""
            visited.append({"tab": tab, "heading": heading.strip()})
            self.shot(f"ru-{tab}")
        self.expect(len(visited) == len(TABS), "not all tabs were reachable")
        self.expect(all(item["heading"] for item in visited), f"a tab rendered no heading: {visited}")
        self.click_tab("studio")
        self.report.add("shell:tabs-ru", "PASS", started, json.dumps(visited, ensure_ascii=False))

    def check_locale(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        self.page.click("#lang-toggle")
        self.page.wait_for_function(
            "() => document.documentElement.lang === 'en'", timeout=self.args.timeout
        )
        stored = str(
            self.page.evaluate("() => window.localStorage.getItem('voice-atelier.lang') || ''")
        )
        self.expect(stored == "en", f"localStorage lang should be 'en', got {stored!r}")
        english_title = self.page.title()
        self.shot("en-studio")
        for tab in ("voices", "design", "models"):
            self.click_tab(tab)
            self.shot(f"en-{tab}")
        self.click_tab("studio")

        # Locale must survive a reload (persisted choice).
        self.page.reload(wait_until="load")
        self.page.wait_for_selector('#tabs [data-tab="studio"]', timeout=self.args.timeout)
        self.expect(self.lang() == "en", "locale did not survive reload")

        # And back to Russian, which must also persist.
        self.page.click("#lang-toggle")
        self.page.wait_for_function(
            "() => document.documentElement.lang === 'ru'", timeout=self.args.timeout
        )
        self.page.reload(wait_until="load")
        self.page.wait_for_selector('#tabs [data-tab="studio"]', timeout=self.args.timeout)
        self.expect(self.lang() == "ru", "locale did not return to ru after reload")
        self.report.add(
            "shell:locale",
            "PASS",
            started,
            f"en title={english_title!r}; ru/en persisted across reloads",
        )

    def create_project_via_ui(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        self.click_tab("studio")
        self.page.locator('[data-act="new-project"]').first.click()
        self.page.wait_for_selector("#modal[open]", timeout=self.args.timeout)
        self.page.fill("#modal #modal-body input.input", self.project_title)
        self.page.click("#modal .modal-foot .btn.primary")

        # The editor may render the title field a moment later; the authoritative
        # assertion below is the API, so a slow/missing field must not be a false FAIL.
        title_field_ok = False
        try:
            self.page.wait_for_function(
                "(title) => { const el = document.getElementById('project-title');"
                " return el && el.value === title; }",
                arg=self.project_title,
                timeout=min(self.args.timeout, 8000),
            )
            title_field_ok = True
        except Exception:
            title_field_ok = bool(
                self.page.evaluate(
                    "(title) => { const el = document.getElementById('project-title');"
                    " return Boolean(el) && el.value === title; }",
                    self.project_title,
                )
            )
        if not title_field_ok:
            raise Failed(
                "project title field did not show the created title in the studio editor "
                "(the project may exist, but the UI did not reflect it)"
            )

        match = self.find_project_by_title()
        self.expect(match is not None, "project created in the UI is not present in the API")
        self.project_id = match.get("id")
        # Deliberately the *empty* editor here; the canonical "ru-studio-project" shot is
        # taken later, after a segment is filled and autosave has settled.
        self.shot("ru-studio-project-created")
        self.report.add(
            "workflow:create-project",
            "PASS",
            started,
            f"project_id={self.project_id} title={self.project_title!r}",
        )

    def find_project_by_title(self, attempts: int = 12) -> dict[str, Any] | None:
        """Poll the API briefly: UI creation is optimistic, the write is async."""
        for _ in range(attempts):
            status, listing = self.api("GET", "/api/projects")
            if status == 200:
                items = (listing or {}).get("items") or []
                match = next(
                    (item for item in items if item.get("title") == self.project_title), None
                )
                if match is not None:
                    return match
            time.sleep(0.4)
        return None

    def add_segment_and_save(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        self.page.locator('[data-act="add-segment"]').first.click()
        self.page.wait_for_selector('textarea[id^="seg-text-"]', timeout=self.args.timeout)
        textarea = self.page.locator('textarea[id^="seg-text-"]').first
        textarea.click()
        textarea.fill(TEST_LINE)
        self.page.locator("#page-title").click()  # blur so the debounced save can fire
        state = self.wait_saved()
        self.expect(state == "saved", f"autosave did not reach 'saved' (state={state!r})")
        # The server must actually hold the text, not just the DOM.
        self.expect(self.project_id is not None, "no project id to verify")
        status, project = self.api("GET", f"/api/projects/{self.project_id}")
        self.expect(status == 200, f"could not read project: HTTP {status}")
        texts = [segment.get("text") for segment in (project or {}).get("segments") or []]
        self.expect(
            TEST_LINE in texts,
            f"autosave did not persist the segment text to the server: {texts}",
        )

        # Screenshot only once the editor is stable and actually shows the filled
        # segment: blurred focus, autosave "saved", and a visible textarea with the text.
        self.settle_editor()
        self.expect(
            int(
                self.page.evaluate(
                    "() => document.querySelectorAll('textarea[id^=\"seg-text-\"]').length"
                )
            )
            >= 1,
            "no segment editor is visible for the project screenshot",
        )
        visible_text = self.page.locator('textarea[id^="seg-text-"]').first.input_value()
        self.expect(
            visible_text == TEST_LINE,
            f"segment editor does not show the saved text before screenshot (got {visible_text!r})",
        )
        self.shot("ru-studio-project")

        self.report.add(
            "workflow:segment-autosave",
            "PASS",
            started,
            f"state={state} segments={len(texts)}",
        )

    def check_autosave_across_navigation(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        self.click_tab("voices")
        self.click_tab("studio")
        self.page.wait_for_selector('textarea[id^="seg-text-"]', timeout=self.args.timeout)
        value = self.page.locator('textarea[id^="seg-text-"]').first.input_value()
        self.expect(
            value == TEST_LINE,
            f"segment text was lost when navigating away and back (got {value!r})",
        )
        self.report.add("workflow:autosave-navigation", "PASS", started, "text survived tab switch")

    def check_xss(self) -> None:
        started = time.monotonic()
        assert self.page is not None
        title_input = self.page.locator("#project-title")
        title_input.click()
        title_input.fill(XSS_TITLE)
        textarea = self.page.locator('textarea[id^="seg-text-"]').first
        textarea.click()
        textarea.fill(XSS_TEXT)
        self.page.locator("#page-title").click()
        state = self.wait_saved()
        self.expect(state == "saved", f"autosave failed during the XSS probe (state={state!r})")

        flags = self.page.evaluate(
            "() => ({ title: window.__xssTitle, text: window.__xssText, script: window.__xssScript })"
        )
        self.expect(
            not any(flags.values()),
            f"injected handlers executed: {flags}",
        )
        injected = int(self.page.evaluate("() => document.querySelectorAll('img[src=\"x\"]').length"))
        self.expect(injected == 0, f"markup was injected into the DOM ({injected} img[src=x] nodes)")

        # The payload must survive as literal text (proves escaping, not stripping).
        status, project = self.api("GET", f"/api/projects/{self.project_id}")
        self.expect(status == 200, f"could not read project during XSS probe: HTTP {status}")
        texts = [segment.get("text") for segment in (project or {}).get("segments") or []]
        self.expect(XSS_TEXT in texts, "the XSS payload was mutated server-side instead of stored verbatim")
        rendered_as_text = self.page.evaluate(
            "(payload) => Array.from(document.querySelectorAll('#project-select option'))"
            ".some((option) => option.textContent.includes(payload));",
            XSS_TITLE,
        )
        self.report.add(
            "security:xss-text",
            "PASS",
            started,
            f"no handler execution, stored verbatim, option-text-match={bool(rendered_as_text)}",
        )

        # Restore clean content so the test project is not left polluted.
        title_input = self.page.locator("#project-title")
        title_input.click()
        title_input.fill(self.project_title)
        textarea = self.page.locator('textarea[id^="seg-text-"]').first
        textarea.click()
        textarea.fill(TEST_LINE)
        self.page.locator("#page-title").click()
        self.wait_saved()

    def check_no_crash(self) -> None:
        started = time.monotonic()
        # A hard JS exception during boot or interaction is a real failure, not noise.
        self.expect(
            not self.report.page_errors,
            f"{len(self.report.page_errors)} uncaught JS exception(s): {self.report.page_errors[:3]}",
        )
        self.report.add(
            "runtime:no-js-exceptions",
            "PASS",
            started,
            f"console_errors={len(self.report.console_errors)} "
            f"failed_requests={len(self.report.failed_requests)} "
            f"bad_api_responses={len(self.report.bad_responses)}",
        )

    def settle(self, ms: int = 150) -> None:
        """Let the current render/compositor settle before asserting or screenshotting."""
        assert self.page is not None
        try:
            self.page.wait_for_timeout(ms)
        except Exception:
            pass

    def settle_editor(self) -> None:
        """Blur the editor, wait for autosave to settle, and confirm focus left the field."""
        assert self.page is not None
        self.page.locator("#page-title").click()
        state = self.wait_saved()
        self.expect(state == "saved", f"autosave did not settle before screenshot (state={state!r})")
        self.page.wait_for_function(
            "() => { const a = document.activeElement;"
            " return !a || !/^(TEXTAREA|INPUT)$/.test(a.tagName); }",
            timeout=self.args.timeout,
        )
        self.settle(200)

    def enter_locale(self, lang: str) -> None:
        assert self.page is not None
        if self.lang() == lang:
            return
        self.page.click("#lang-toggle")
        self.page.wait_for_function(
            "(want) => document.documentElement.lang === want",
            arg=lang,
            timeout=self.args.timeout,
        )

    def scan_i18n(self) -> list[dict[str, Any]]:
        """Return every rendered string that still looks like an unresolved i18n key."""
        assert self.page is not None
        sources = self.page.evaluate(_I18N_SCAN_JS) or []
        hits: list[dict[str, Any]] = []
        for item in sources:
            value = str(item.get("value") or "")
            for match in I18N_KEY_RE.findall(value):
                if match in I18N_ALLOWED:
                    continue
                hits.append(
                    {
                        "key": match,
                        "kind": item.get("kind"),
                        "where": item.get("where"),
                        "snippet": " ".join(value.split())[:120],
                    }
                )
        return hits

    def scan_i18n_all(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        scanned: list[dict[str, Any]] = []
        for lang in ("ru", "en"):
            self.enter_locale(lang)
            for tab in TABS:
                self.click_tab(tab)
                self.settle(120)
                hits = self.scan_i18n()
                scanned.append({"lang": lang, "tab": tab, "hits": len(hits)})
                for hit in hits:
                    hit.update({"lang": lang, "tab": tab})
                    findings.append(hit)
        self.enter_locale("ru")
        self.click_tab("studio")
        self.settle(120)
        return findings, scanned

    def assert_no_raw_keys(
        self,
        findings: list[dict[str, Any]],
        scanned: list[dict[str, Any]],
        started: float,
        name: str,
        phase: str,
    ) -> None:
        if findings:
            unique: dict[tuple, dict[str, Any]] = {}
            for item in findings:
                key = (item["lang"], item["tab"], item["key"], item["kind"])
                unique.setdefault(key, item)
            sample = [
                f'{item["lang"]}/{item["tab"]} {item["kind"]} {item["where"]}: {item["key"]}'
                f' (text {item["snippet"]!r})'
                for item in list(unique.values())[:10]
            ]
            raise Failed(
                f"unresolved i18n key(s) rendered in the UI ({phase}, "
                f"{len(unique)} unique): " + "; ".join(sample)
            )
        self.report.add(name, "PASS", started, json.dumps(scanned, ensure_ascii=False))

    def check_i18n_keys(self) -> None:
        started = time.monotonic()
        findings, scanned = self.scan_i18n_all()
        self.assert_no_raw_keys(findings, scanned, started, "i18n:no-raw-keys", "empty-project state")

    def check_i18n_keys_project(self) -> None:
        """Re-scan with a populated project on screen: keys like studio.segments only
        appear once the editor renders real segments, so an empty-state scan alone
        would have missed exactly the defect the parent saw in the screenshot."""
        started = time.monotonic()
        findings, scanned = self.scan_i18n_all()
        self.assert_no_raw_keys(
            findings, scanned, started, "i18n:no-raw-keys-project", "populated project"
        )

    def check_console_clean(self) -> None:
        """Any console error is a user-visible defect. 404s are no longer exempt:
        web/favicon.svg now exists, so a favicon 404 means something regressed."""
        started = time.monotonic()
        errors = list(self.report.console_errors)
        if errors:
            sample = "; ".join(
                f"{str(item.get('text', ''))[:160]} @ {item.get('location')}"
                for item in errors[:5]
            )
            raise Failed(f"{len(errors)} console error(s): {sample}")
        self.report.add(
            "runtime:no-unexpected-console-errors",
            "PASS",
            started,
            f"0 console errors; failed_requests={len(self.report.failed_requests)} "
            f"bad_api_responses={len(self.report.bad_responses)}",
        )

    def cleanup(self) -> None:
        started = time.monotonic()
        if not self.project_id or self.args.keep_project:
            self.report.add(
                "cleanup:project",
                "PASS",
                started,
                "kept" if self.args.keep_project else "no project created",
            )
            return
        status, _ = self.api("DELETE", f"/api/projects/{self.project_id}")
        self.report.add(
            "cleanup:project",
            "PASS" if status in (200, 404) else "FAIL",
            started,
            f"DELETE /api/projects/{self.project_id} -> {status}",
        )
        if status not in (200, 404):
            self.exit_code = max(self.exit_code, 1)

    # -- lifecycle ---------------------------------------------------------------------
    def shutdown(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:
            pass
        if self.server is not None:
            pid = self.server.pid
            try:
                if self.server.poll() is None:
                    self.server.terminate()
                    try:
                        self.server.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        self.server.kill()
                        self.server.wait(timeout=10)
            except Exception:
                pass
            finally:
                print(f"[info] terminated own server pid {pid}", flush=True)
                self.server = None
        if self.server_log_handle is not None:
            try:
                self.server_log_handle.flush()
                self.server_log_handle.close()
            except Exception:
                pass
            self.server_log_handle = None

    def run(self) -> int:
        self.preflight()
        self.ensure_server()
        self.fetch_token()
        self.launch()
        self.goto()

        checks = (
            ("shell:boot-ru", self.check_shell),
            ("shell:tabs-ru", self.check_tabs_screenshots),
            ("i18n:no-raw-keys", self.check_i18n_keys),
            ("shell:locale", self.check_locale),
            ("workflow:create-project", self.create_project_via_ui),
            ("workflow:segment-autosave", self.add_segment_and_save),
            ("i18n:no-raw-keys-project", self.check_i18n_keys_project),
            ("workflow:autosave-navigation", self.check_autosave_across_navigation),
            ("security:xss-text", self.check_xss),
            ("runtime:no-js-exceptions", self.check_no_crash),
            ("runtime:no-unexpected-console-errors", self.check_console_clean),
        )
        for name, function in checks:
            started = time.monotonic()
            try:
                function()
            except Failed as error:
                self.report.add(name, "FAIL", started, str(error))
                self.exit_code = max(self.exit_code, 1)
                if name.startswith(("shell:", "workflow:create-project")):
                    # Without a shell/project the remaining checks cannot be meaningful.
                    break
            except Exception as error:  # noqa: BLE001
                self.report.add(
                    name,
                    "FAIL",
                    started,
                    f"{type(error).__name__}: {error}",
                )
                self.exit_code = max(self.exit_code, 1)
                if name.startswith(("shell:", "workflow:create-project")):
                    break

        try:
            self.cleanup()
        except Exception as error:  # noqa: BLE001
            self.report.add("cleanup:project", "FAIL", time.monotonic(), str(error))
            self.exit_code = max(self.exit_code, 1)
        return self.exit_code

    def write_report(self) -> Path:
        target = self.artifacts / "browser-report.json"
        target.write_text(
            json.dumps(self.report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target


def default_python(root: Path) -> str:
    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = root / ".venv" / relative
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Browser smoke for Voice Atelier (Playwright + installed Edge, no browser download)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--python", default=None)
    parser.add_argument("--home", default=None)
    parser.add_argument("--own-server", action="store_true", help="start and terminate our own server")
    parser.add_argument("--headed", action="store_true", help="show the browser window (default: headless)")
    parser.add_argument("--timeout", type=float, default=20000.0, help="per-action timeout in ms")
    parser.add_argument("--save-timeout", type=float, default=15000.0, help="autosave settle timeout in ms")
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    parser.add_argument("--artifacts-dir", default=None)
    parser.add_argument("--keep-project", action="store_true", help="do not delete the UI test project")
    parser.add_argument("--playwright-version", default="1.64.1")
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    args.project_root = str(root)
    args.python = args.python or default_python(root)
    args.artifacts_dir = args.artifacts_dir or str(root / "verification" / "artifacts" / "browser")
    # Keep the port probe and the base URL in sync when a custom URL is supplied.
    from urllib.parse import urlparse

    parsed = urlparse(args.base_url)
    if parsed.hostname:
        args.base_url = f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port or args.port}"
    if parsed.port:
        args.port = parsed.port
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    smoke = BrowserSmoke(args)
    try:
        exit_code = smoke.run()
    except NotRun as error:
        smoke.report.not_run_reason = str(error)
        print(f"[NORUN] {error}", file=sys.stderr, flush=True)
        smoke.exit_code = 3
        exit_code = 3
    except Exception:  # noqa: BLE001
        smoke.report.fatal = traceback.format_exc(limit=6)
        print("[FAIL] unexpected browser driver error:\n" + smoke.report.fatal, file=sys.stderr, flush=True)
        smoke.exit_code = 1
        exit_code = 1
    finally:
        smoke.shutdown()

    report_path = smoke.write_report()
    counts = smoke.report.counts()
    verdict = "NOT RUN" if smoke.report.not_run_reason else ("PASS" if counts["FAIL"] == 0 else "FAIL")
    print(
        f"\nverdict: {verdict}\n"
        f"summary: {', '.join(f'{key}={value}' for key, value in counts.items())}\n"
        f"report:  {report_path}\n"
        f"screens: {len(smoke.report.screenshots)} in {smoke.artifacts}",
        flush=True,
    )
    print(
        "honesty: this run does not judge audio quality and does not exercise GPU synthesis.",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
