from datetime import datetime, timedelta, timezone

from app import config, epic_lookup, service
from app.connectors.base import ConnectorError, RateLimited, RawListing
from app.models import JobRun, PriceCheck, PriceSnapshot

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class FakeEpic:
    store_id = "epic"

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result or [], exc, 0

    def search(self, title, region="US"):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def epic_hades(price=1999):
    return RawListing("epic", "hades-epic", "Hades", "https://store.epicgames.com/p/hades",
                      price, 2499, "USD", release_year=2020)


def new_gate(per_minute=30, clock=None):
    return epic_lookup.Gate(per_minute, clock or Clock())


def hades(s):
    return service.upsert_game(s, "Hades", 1145360, 2020)


def check_count(s):
    return s.query(PriceCheck).filter_by(store_id="epic").count()


def test_a_lookup_stores_the_price_and_marks_the_game_checked(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out == {"status": "checked", "checked_at": NOW.isoformat()} and fake.calls == 1
    assert db_session.query(PriceSnapshot).one().price_cents == 1999
    assert check_count(db_session) == 1


def test_a_recent_check_is_fresh_and_makes_no_request(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    service.mark_checked(db_session, [g.id], "epic", "US", NOW - timedelta(hours=1))
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out["status"] == "fresh" and fake.calls == 0


def test_a_check_older_than_the_cooldown_looks_again(db_session):
    g, fake = hades(db_session), FakeEpic([epic_hades()])
    service.mark_checked(db_session, [g.id], "epic", "US", NOW - timedelta(hours=config.EPIC_COOLDOWN_HOURS, minutes=1))
    out = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert out["status"] == "checked" and fake.calls == 1


def test_no_confident_match_is_still_marked_checked_so_the_next_visit_is_fresh(db_session):
    g = hades(db_session)
    other = RawListing("epic", "x", "Completely Different Game", "http://x", 100, 100, "USD")
    fake = FakeEpic([other])
    first = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW)
    assert first["status"] == "checked" and db_session.query(PriceSnapshot).count() == 0
    second = epic_lookup.check_epic(db_session, g, "US", connector=fake, gate=new_gate(), now=NOW + timedelta(minutes=5))
    assert second["status"] == "fresh" and fake.calls == 1


def test_the_global_cap_returns_busy_without_marking_checked_and_refills_over_time(db_session):
    clock = Clock()
    gate = new_gate(per_minute=1, clock=clock)
    g1 = hades(db_session)
    g2 = service.upsert_game(db_session, "Celeste", 504230, 2018)
    fake = FakeEpic([])
    assert epic_lookup.check_epic(db_session, g1, "US", connector=fake, gate=gate, now=NOW)["status"] == "checked"
    busy = epic_lookup.check_epic(db_session, g2, "US", connector=fake, gate=gate, now=NOW)
    assert busy["status"] == "busy" and fake.calls == 1 and check_count(db_session) == 1
    clock.t += 61
    assert epic_lookup.check_epic(db_session, g2, "US", connector=fake, gate=gate, now=NOW)["status"] == "checked"


def test_rate_limiting_blocks_all_lookups_for_fifteen_minutes_then_releases(db_session):
    clock = Clock()
    gate = new_gate(clock=clock)
    g1 = hades(db_session)
    g2 = service.upsert_game(db_session, "Celeste", 504230, 2018)
    limited = FakeEpic(exc=RateLimited("epic rate limited (HTTP 429)"))
    out = epic_lookup.check_epic(db_session, g1, "US", connector=limited, gate=gate, now=NOW)
    assert out["status"] == "unavailable" and check_count(db_session) == 0
    assert db_session.query(JobRun).filter_by(store_id="epic", outcome="rate_limited").count() == 1
    healthy = FakeEpic([])
    assert epic_lookup.check_epic(db_session, g2, "US", connector=healthy, gate=gate, now=NOW)["status"] == "unavailable"
    assert healthy.calls == 0  # blocked: no request was made
    clock.t += 15 * 60 + 1
    assert epic_lookup.check_epic(db_session, g2, "US", connector=healthy, gate=gate, now=NOW)["status"] == "checked"


def test_an_error_is_unavailable_not_marked_checked_and_does_not_block(db_session):
    g = hades(db_session)
    broken = FakeEpic(exc=ConnectorError("epic HTTP 500"))
    gate = new_gate()
    assert epic_lookup.check_epic(db_session, g, "US", connector=broken, gate=gate, now=NOW)["status"] == "unavailable"
    assert check_count(db_session) == 0
    assert epic_lookup.check_epic(db_session, g, "US", connector=broken, gate=gate, now=NOW)["status"] == "unavailable"
    assert broken.calls == 2  # not blocked: the next visit tries again
