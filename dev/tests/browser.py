"""Optional Playwright harness for the viewer page (dev only; the shipped skill stays standard library only).

Browser tests import this module and subclass ViewerTestCase. Nothing here is needed to run the rest of the suite:
when Playwright is not installed, or no Chrome/Chromium can be launched, every browser test is skipped with the reason
(unittest.SkipTest), and the suite still passes.

Browser used: $LEDGER_CHROME if set, else /usr/bin/google-chrome when present, else Playwright's own Chromium if it is
installed. One headless browser is started per test process (on first use) and closed at exit; each test gets a fresh
browser context (its own storage, viewport and colour scheme), so tests stay fast and independent.

    from dev.tests import browser

    class MyViewTests(browser.ViewerTestCase):
        @classmethod
        def build_ledger(cls, ledger):        # optional: default seeds the demo (seed_demo + finish_demo)
            ...

        def test_something(self):
            page = self.open("#/reviews")      # a ViewerPage, already settled
            self.assertEqual(page.text("h1"), "Reviews")

ViewerPage records page errors, console errors and every request; requests that leave 127.0.0.1 are aborted (so a
test never reaches the network) and listed in page.outside_requests. tearDown fails the test if a page error happened
or anything left 127.0.0.1; console errors are failures unless the test sets page.allow_console_errors (an expected
4xx answer logs "Failed to load resource" in Chrome).
"""
import atexit
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from dev.tests import fixtures

try:  # Playwright is optional: without it every browser test is skipped.
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PlaywrightError
except Exception as _e:  # noqa: BLE001 - any import problem means "not available"
    sync_playwright = None
    PlaywrightError = Exception
    _IMPORT_ERROR = f"Playwright is not installed ({_e.__class__.__name__}: {_e}); pip install playwright"
else:
    _IMPORT_ERROR = None

DEFAULT_CHROME = "/usr/bin/google-chrome"
DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}

# Injected before the page's own script: counts fetches in flight (until the body is read) and the time of the last
# DOM change, so settle() can wait until the view has finished drawing.
_INIT_SCRIPT = r"""
(() => {
  window.__pending = 0; window.__fetches = 0; window.__lastMutation = performance.now();
  const orig = window.fetch.bind(window);
  window.fetch = async (...a) => {
    window.__pending++; window.__fetches++;
    try { const r = await orig(...a); try { await r.clone().arrayBuffer(); } catch (e) {} return r; }
    finally { window.__pending--; window.__lastMutation = performance.now(); }
  };
  const start = () => new MutationObserver(() => { window.__lastMutation = performance.now(); })
    .observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
})();
"""

_SETTLED = "q => window.__pending === 0 && performance.now() - window.__lastMutation > q"

_state = {"pw": None, "browser": None, "reason": None, "tried": False}


def chrome_path():
    """The Chrome binary to use, or None to let Playwright use its own Chromium."""
    p = os.environ.get("LEDGER_CHROME") or DEFAULT_CHROME
    return p if Path(p).is_file() and os.access(p, os.X_OK) else None


def _stop():
    b, pw = _state["browser"], _state["pw"]
    _state["browser"] = _state["pw"] = None
    for f in ((b.close if b else None), (pw.stop if pw else None)):
        if f:
            try:
                f()
            except Exception:  # noqa: BLE001 - best effort at exit
                pass


def get_browser():
    """The shared headless browser, started on first use; None if it cannot be started (see skip_reason())."""
    if _state["browser"] is not None or _state["tried"]:
        return _state["browser"]
    _state["tried"] = True
    if sync_playwright is None:
        _state["reason"] = _IMPORT_ERROR
        return None
    try:
        pw = sync_playwright().start()
    except Exception as e:  # noqa: BLE001
        _state["reason"] = f"Playwright could not start: {e}"
        return None
    _state["pw"] = pw
    exe = chrome_path()
    try:
        kw = {"headless": True, "args": ["--no-first-run", "--disable-extensions"]}
        if exe:
            kw["executable_path"] = exe
        _state["browser"] = pw.chromium.launch(**kw)
    except Exception as e:  # noqa: BLE001 - no Chrome and no Playwright Chromium
        _state["reason"] = (f"no browser could be launched ({exe or 'Playwright Chromium'}): "
                            f"{str(e).splitlines()[0] if str(e) else e!r}")
        _stop()
        return None
    atexit.register(_stop)
    return _state["browser"]


def skip_reason():
    """Why browser tests are skipped, or None when a browser is available."""
    return None if get_browser() is not None else (_state["reason"] or "no browser")


def require_browser():
    """Raise unittest.SkipTest unless browser tests are switched on (LEDGER_BROWSER_TESTS=1) and a browser is
    available. Returns the browser. They are off by default so the everyday suite stays quick; CI runs them in their
    own job."""
    if os.environ.get("LEDGER_BROWSER_TESTS") != "1":
        raise unittest.SkipTest("browser tests skipped: set LEDGER_BROWSER_TESTS=1 to run them")
    b = get_browser()
    if b is None:
        raise unittest.SkipTest(f"browser tests skipped: {_state['reason']}")
    return b


class ViewerPage:
    """One page of the viewer in its own browser context, with error and request recording."""

    def __init__(self, browser, base_url, *, token=None, viewport=None, color_scheme="light", permissions=None):
        self.base_url = base_url
        self.token = token  # sent once, in the #k= fragment of the first load, as serve()'s launch URL does
        host = urlsplit(base_url).hostname
        self.page_errors, self.console_errors, self.requests, self.outside_requests = [], [], [], []
        self.allow_console_errors = False
        self.context = browser.new_context(viewport=viewport or DESKTOP, color_scheme=color_scheme)
        self.context.set_default_timeout(5000)  # fail fast: nothing in the viewer should take seconds
        if permissions:
            self.context.grant_permissions(permissions, origin=base_url.rstrip("/"))

        def guard(route):
            u = urlsplit(route.request.url)
            if u.scheme in ("data", "blob", "about") or u.hostname == host:
                route.continue_()
            else:
                self.outside_requests.append(route.request.url)
                route.abort()

        self.context.route("**/*", guard)
        self.context.add_init_script(_INIT_SCRIPT)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.on("request", lambda r: self.requests.append(r.url))

    # -- navigation
    def goto(self, hash_="#/"):
        """Load the viewer at a hash (full page load) and wait until it has drawn."""
        if self.token:
            self.page.goto(self.base_url + "#k=" + self.token + (hash_ if hash_.startswith("#") else "#/"))
        else:
            self.page.goto(self.base_url + hash_)
        return self.settle()

    def hash(self, hash_):
        """Change location.hash in the loaded page (the router runs) and wait until it has drawn."""
        before = self.js("window.__fetches")
        self.page.evaluate("h => { location.hash = h; }", hash_)
        try:
            self.page.wait_for_function("n => window.__fetches > n", arg=before, timeout=3000)
        except PlaywrightError:
            pass  # same hash or a view that fetches nothing: settle anyway
        return self.settle()

    def after(self, action, timeout=3000):
        """Run action() (a click or key press that should navigate or load), wait until it has started a fetch, then
        settle. Without the wait, settle() could return before the router's hashchange handler has run."""
        before = self.js("window.__fetches")
        action()
        try:
            self.page.wait_for_function("n => window.__fetches > n", arg=before, timeout=timeout)
        except PlaywrightError:
            pass
        return self.settle()

    def settle(self, quiet_ms=40, timeout=8000):
        """Wait until no fetch is in flight and the DOM has been still for quiet_ms."""
        self.page.wait_for_function(_SETTLED, arg=quiet_ms, timeout=timeout, polling=20)
        return self

    # -- reading
    def js(self, expr, arg=None):
        """Evaluate an expression or function source in the page (page globals such as h, put, VIEWS are visible)."""
        return self.page.evaluate(expr, arg) if arg is not None else self.page.evaluate(expr)

    def text(self, selector):
        return self.page.locator(selector).first.inner_text().strip()

    def texts(self, selector):
        return [t.strip() for t in self.page.locator(selector).all_inner_texts()]

    def contents(self, selector):
        """textContent of every match (not affected by CSS such as text-transform, unlike texts())."""
        return [t.strip() for t in self.page.locator(selector).all_text_contents()]

    def count(self, selector):
        return self.page.locator(selector).count()

    def location_hash(self):
        return self.js("location.hash")

    def close(self):
        try:  # let in-flight requests finish, so the server does not log a broken pipe
            self.page.wait_for_function("window.__pending === 0", timeout=2000, polling=20)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.context.close()
        except Exception:  # noqa: BLE001
            pass


class ViewerTestCase(unittest.TestCase):
    """A ledger (by default the demo, seeded once per class) served by fixtures.ServerThread, and a fresh page per
    test. Override build_ledger(ledger) to fill the ledger differently; the return value is stored as cls.ids."""

    ledger = server = ids = None

    @classmethod
    def build_ledger(cls, ledger):
        ids = fixtures.seed_demo(ledger)
        fixtures.finish_demo(ledger)
        return ids

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.browser = require_browser()
        cls.ledger = fixtures.Ledger()
        cls.ids = cls.build_ledger(cls.ledger)
        cls.ledger.commit()
        cls.server = fixtures.ServerThread(cls.ledger).start()

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.stop()
        if cls.ledger is not None:
            cls.ledger.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._pages = []

    def new_page(self, **kw):
        kw.setdefault("token", self.server.token)
        p = ViewerPage(self.browser, self.server.url, **kw)
        self._pages.append(p)
        return p

    def open(self, hash_="#/", **kw):
        """A new page loaded at hash_ and settled."""
        return self.new_page(**kw).goto(hash_)

    def assertClean(self, page, where=""):
        self.assertEqual(page.page_errors, [], f"page errors {where}")
        self.assertEqual(page.outside_requests, [], f"requests left 127.0.0.1 {where}")
        if not page.allow_console_errors:
            self.assertEqual(page.console_errors, [], f"console errors {where}")

    def tearDown(self):
        pages, self._pages = self._pages, []
        try:
            for p in pages:
                self.assertClean(p)
        finally:
            for p in pages:
                p.close()
        super().tearDown()

    # -- writing to the served ledger while the page is open
    def write(self, sql, args=()):
        """Run one statement on the served ledger and commit (the viewer reads the database on every request)."""
        cur = self.ledger.con.execute(sql, args)
        self.ledger.commit()
        return cur
