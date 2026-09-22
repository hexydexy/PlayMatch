"""Browser checks for the catalog UI. Needs the app running with the demo data:

    (from price-match/)
    $env:DATABASE_URL = "sqlite:///demo.db"        # bash: export DATABASE_URL=sqlite:///demo.db
    python -m scripts.seed_demo
    $env:FRONTEND_DIR = "../frontend"
    python -m uvicorn app.main:app --port 8765
    # in another terminal:
    python -m scripts.browser_check http://localhost:8765/

Every /epic-check call is intercepted, so nothing here contacts Epic. Exit code 1 if any check fails.
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/"
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def cards(p):
    return p.locator("#grid .game").count()


def wait_cards(p, n):
    p.wait_for_function(f"document.querySelectorAll('#grid .game').length === {n}")


def check_list(p):
    p.goto(BASE)
    p.wait_for_selector("#grid .game")
    check("first page shows 48 games", cards(p) == 48, str(cards(p)))
    check("count line shows the whole catalog", "124 tracked games" in p.inner_text("#count"), p.inner_text("#count"))
    check("Load more is offered", p.locator("#more").count() == 1)
    p.click("#more")
    wait_cards(p, 96)
    check("Load more appends the next 48", cards(p) == 96)
    p.click("#more")
    wait_cards(p, 124)
    check("the last page is partial and the button goes away", cards(p) == 124 and p.locator("#more").count() == 0)

    p.fill("#q", "hades")
    wait_cards(p, 1)
    check("search resets to the first page of matches",
          "1 game matches" in p.inner_text("#count") and p.locator("#more").count() == 0, p.inner_text("#count"))

    # Load more racing with a new search must not mix pages.
    p.fill("#q", "")
    wait_cards(p, 48)
    p.evaluate("""() => { const f = window.fetch;
        window.fetch = (u, ...a) => String(u).includes('offset=48')
          ? new Promise(r => setTimeout(r, 900)).then(() => f(u, ...a)) : f(u, ...a); }""")
    p.click("#more")
    p.fill("#q", "hades")
    wait_cards(p, 1)
    p.wait_for_timeout(1300)  # long enough for the slow second page to arrive
    check("a slow Load more that lost the race is ignored", cards(p) == 1 and "1 game matches" in p.inner_text("#count"),
          f"{cards(p)} cards, {p.inner_text('#count')}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        check_list(page)
        browser.close()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
