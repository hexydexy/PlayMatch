from datetime import datetime, timedelta, timezone

from app import bulk, service
from app.connectors.base import ConnectorError, RateLimited, RawListing
from app.models import JobRun, PriceCheck, PriceSnapshot

T = datetime(2026, 9, 1, tzinfo=timezone.utc)


def priced(app_id, price=1999):
    return RawListing("steam", str(app_id), "", f"https://store.steampowered.com/app/{app_id}",
                      price, 2999, "USD", steam_app_id=app_id)


class FakeSteam:
    store_id = "steam"

    def __init__(self, prices=None, fail=None):
        self.prices, self.fail, self.calls = prices or {}, fail, []

    def prices_for(self, app_ids, region="US"):
        self.calls.append(list(app_ids))
        if self.fail:
            self.fail(app_ids, len(self.calls))
        return {a: self.prices.get(a) for a in app_ids}


def add_games(s, n):
    return [service.upsert_game(s, f"Game {i}", 1000 + i) for i in range(n)]


def test_games_due_lists_unchecked_first_then_oldest_and_skips_games_without_a_steam_id(db_session):
    g1, g2, g3 = add_games(db_session, 3)
    service.upsert_game(db_session, "Epic Only")
    service.mark_checked(db_session, [g3.id], "steam", "US", T + timedelta(days=2))
    service.mark_checked(db_session, [g2.id], "steam", "US", T)
    service.mark_checked(db_session, [g1.id], "steam", "GB", T)  # another region does not count
    due = bulk.games_due(db_session, "US")
    assert [gid for gid, _, _ in due] == [g1.id, g2.id, g3.id]
    assert due[0][1:] == (1000, "Game 0")


def test_refresh_records_prices_and_marks_every_game_checked(db_session):
    g1, g2 = add_games(db_session, 2)
    fake = FakeSteam({1000: priced(1000, 999)})  # game 1001 has no price (free to play)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=10)
    assert (rep["priced"], rep["no_price"], rep["snapshots"], rep["batches"]) == (1, 1, 1, 1)
    assert db_session.query(PriceSnapshot).one().price_cents == 999
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g1.id, g2.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="ok").count() == 1


def test_an_unchanged_price_is_not_stored_again_on_the_next_run(db_session):
    add_games(db_session, 1)
    fake = FakeSteam({1000: priced(1000)})
    bulk.refresh_steam_prices(db_session, "US", connector=fake)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake)
    assert rep["priced"] == 1 and rep["snapshots"] == 0
    assert db_session.query(PriceSnapshot).count() == 1


def test_games_are_sent_in_batches_of_the_requested_size(db_session):
    add_games(db_session, 5)
    fake = FakeSteam({})
    bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=2)
    assert [len(c) for c in fake.calls] == [2, 2, 1]


def test_a_failing_batch_is_split_in_half_and_retried(db_session):
    add_games(db_session, 4)

    def fail(ids, n):
        if len(ids) > 2:
            raise ConnectorError("too many ids")

    fake = FakeSteam({a: priced(a) for a in range(1000, 1004)}, fail)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=4)
    assert [len(c) for c in fake.calls] == [4, 2, 2]
    assert rep["priced"] == 4 and rep["errors"] == 0


def test_a_single_game_that_keeps_failing_is_skipped_and_not_marked_checked(db_session):
    g0, g1, g2 = add_games(db_session, 3)

    def fail(ids, n):
        if 1001 in ids:
            raise ConnectorError("boom")

    fake = FakeSteam({a: priced(a) for a in (1000, 1001, 1002)}, fail)
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=1)
    assert rep["errors"] == 1 and rep["priced"] == 2
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g0.id, g2.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="error").count() == 1


def test_rate_limit_stops_the_run_and_the_unchecked_games_go_first_next_time(db_session):
    g0, g1, g2 = add_games(db_session, 3)

    def fail(ids, n):
        if n == 2:
            raise RateLimited("steam rate limited (HTTP 429)")

    fake = FakeSteam({a: priced(a) for a in (1000, 1001, 1002)}, fail)
    blocked = set()
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=1, blocked=blocked)
    assert rep["rate_limited"] is True and "steam" in blocked
    assert len(fake.calls) == 2  # the third batch was never attempted
    assert {c.game_id for c in db_session.query(PriceCheck)} == {g0.id}
    assert db_session.query(JobRun).filter_by(store_id="steam", outcome="rate_limited").count() == 1
    assert [gid for gid, _, _ in bulk.games_due(db_session, "US")][:2] == [g1.id, g2.id]


def test_a_store_already_blocked_this_run_is_not_called(db_session):
    add_games(db_session, 2)
    fake = FakeSteam({})
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, blocked={"steam"})
    assert fake.calls == [] and rep["rate_limited"] is True


class ProbeSteam:
    """Returns entries only for the first `limit` ids, like a server that silently truncates."""

    def __init__(self, limit):
        self.limit, self.sizes = limit, []

    def fetch_batch_payload(self, app_ids, region="US"):
        self.sizes.append(len(app_ids))
        return {str(a): {"success": True, "data": []} for a in app_ids[:self.limit]}


def test_probe_returns_100_when_the_server_fully_answers_both_sizes():
    fake = ProbeSteam(limit=120)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 100
    assert fake.sizes == [50, 100]


def test_probe_returns_50_when_the_server_truncates_a_batch_of_100():
    fake = ProbeSteam(limit=70)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 50
    assert fake.sizes == [50, 100]


def test_probe_returns_zero_when_even_the_smallest_size_fails():
    fake = ProbeSteam(limit=10)
    assert bulk.probe_batch_size(fake, list(range(1, 301))) == 0


def test_probe_ignores_sizes_larger_than_the_sample():
    fake = ProbeSteam(limit=1000)
    assert bulk.probe_batch_size(fake, list(range(1, 76))) == 50
    assert fake.sizes == [50]


def test_outage_aborts_after_max_consecutive_failures(db_session):
    add_games(db_session, 30)
    fake = FakeSteam({})  # no prices, connector will fail

    def always_fail(ids, n):
        raise ConnectorError("network timeout")

    fake.fail = always_fail
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=5)

    # Exactly 10 calls: the batch-halving descent eventually hits the ceiling
    assert len(fake.calls) == 10
    assert rep["aborted"] is True
    assert db_session.query(PriceCheck).count() == 0
    # Exactly one JobRun with detail starting with "aborting run"
    aborting_jobs = db_session.query(JobRun).filter(
        JobRun.store_id == "steam",
        JobRun.outcome == "error",
        JobRun.detail.like("aborting run%")
    ).all()
    assert len(aborting_jobs) == 1


def test_poison_id_is_still_isolated_after_abort_logic(db_session):
    add_games(db_session, 50)

    def fail_on_poison(ids, n):
        if 1000 in ids:
            raise ConnectorError("poison id")

    fake = FakeSteam({a: priced(a) for a in range(1000, 1050)})
    fake.fail = fail_on_poison
    rep = bulk.refresh_steam_prices(db_session, "US", connector=fake, batch_size=50)

    assert rep["aborted"] is False
    assert rep["errors"] == 1
    assert rep["priced"] == 49
    checked = {c.game_id for c in db_session.query(PriceCheck)}
    assert len(checked) == 49  # all except the poison id game
