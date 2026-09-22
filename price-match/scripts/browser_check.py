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


def game_id(p, query):
    return p.evaluate("q => fetch('/api/games?q=' + encodeURIComponent(q)).then(r => r.json()).then(g => g[0].id)", query)


def open_game(p, gid):
    """Open a profile. Going through the list first guarantees the hash changes, so the router always runs."""
    p.goto(BASE + "#/")
    p.goto(BASE + f"#/game/{gid}")


def check_profile(p):
    posts = []
    p.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "epic-check" in r.url else None)
    state = {"status": "unavailable"}

    def fulfill(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": state["status"], "checked_at": None}))

    p.route("**/epic-check*", fulfill)
    fresh, hades, old, free = (game_id(p, n) for n in ("Fresh Steam Only", "Hades", "Old Price Game", "Free Game"))

    # A game whose Epic price was never checked: shows "checking", then the outcome.
    open_game(p, fresh)
    p.wait_for_selector("#epic-row")
    check("a placeholder Epic row says it is checking", "Checking" in p.inner_text("#epic-row"), p.inner_text("#epic-row"))
    p.wait_for_function("document.querySelector('#epic-row') && /unavailable/.test(document.querySelector('#epic-row').innerText)")
    check("an unavailable Epic price is explained", "Open this game again later" in p.inner_text("#epic-row"))
    check("exactly one Epic lookup was requested", len(posts) == 1, str(len(posts)))

    state["status"] = "busy"
    open_game(p, fresh)
    p.wait_for_function("document.querySelector('#epic-row') && /busy/.test(document.querySelector('#epic-row').innerText)")
    check("a busy Epic lookup says to try again in a minute", "in a minute" in p.inner_text("#epic-row"))

    # 'checked' re-renders with refreshed data and must not loop.
    state["status"] = "checked"
    before = len(posts)
    open_game(p, fresh)
    p.wait_for_selector("#epic-row")
    p.wait_for_function("!document.querySelector('#epic-row')")
    p.wait_for_timeout(1500)
    check("a checked lookup refreshes the page and does not ask again", len(posts) == before + 1, f"{len(posts) - before} requests")

    # A game whose Epic price was checked recently: no lookup at all.
    before = len(posts)
    open_game(p, hades)
    p.wait_for_selector(".prices")
    p.wait_for_timeout(1200)
    check("a recently checked game makes no Epic request", len(posts) == before and p.locator("#epic-row").count() == 0)

    # Stale Steam price.
    open_game(p, old)
    p.wait_for_selector(".prices")
    check("a price checked over 14 days ago says it may be out of date", "may be out of date" in p.inner_text(".prices"))
    open_game(p, hades)
    p.wait_for_selector(".prices")
    check("a recent price shows its check date and no warning",
          "Checked" in p.inner_text(".prices") and "out of date" not in p.inner_text(".prices"))

    # No prices at all and an Epic lookup pending: the prices section still appears.
    state["status"] = "unavailable"
    open_game(p, free)
    p.wait_for_selector("#epic-row")
    check("a game with no prices still shows the Epic row while checking", p.locator(".prices").count() == 1)

    # Regression: the back link keeps the search and scroll.
    p.goto(BASE + "#/")
    p.wait_for_selector("#grid .game")
    p.fill("#q", "")
    wait_cards(p, 48)   # start from a clean list so the check below is not trivially true
    p.fill("#q", "hades")
    wait_cards(p, 1)
    p.click("#grid .game")
    p.wait_for_selector(".back")
    p.click(".back")
    p.wait_for_selector("#grid .game")
    check("All games returns to the same search", p.input_value("#q") == "hades" and cards(p) == 1)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.add_init_script("""(() => { const f = window.fetch;
            window.fetch = (u, ...a) => String(u).includes('epic-check')
              ? new Promise(r => setTimeout(r, 700)).then(() => f(u, ...a)) : f(u, ...a); })()""")
        check_list(page)
        check_profile(page)
        browser.close()
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
