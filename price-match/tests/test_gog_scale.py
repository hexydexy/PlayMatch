from app import gogdb, service
from app.models import Listing, MatchCandidate
from tests.test_core import make_archive


def test_a_catalog_game_with_an_exact_title_auto_matches_even_with_no_release_year(db_session, tmp_path):
    hades = service.upsert_game(db_session, "Hades", 1145360)  # catalog games have no year
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["matched"] == 1
    assert db_session.query(Listing).filter_by(game_id=hades.id, store_id="gog").count() >= 1


def test_a_near_title_with_no_release_year_is_queued_for_review_not_auto_matched(db_session, tmp_path):
    # The matcher's fuzzy auto-match needs both years. Catalog games have none, so near-matches
    # are reviewed rather than linked. This pins that consequence of the design.
    service.upsert_game(db_session, "Hadess", 1145360)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["matched"] == 0 and rep["review_queued"] == 1 and rep["review_dropped"] == 0
    assert db_session.query(MatchCandidate).filter_by(status="pending").count() == 1


def test_the_review_cap_keeps_the_highest_scores(db_session, tmp_path):
    # "Hades" (year 2005 vs GOG's 2020) scores 90 (exact title, year mismatch);
    # "Hadess" scores about 91 (fuzzy). Cap the queue at one: the higher score wins.
    service.upsert_game(db_session, "Hades", 111, 2005)
    service.upsert_game(db_session, "Hadess", 222)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path), review_cap=1)
    assert rep["review_queued"] == 1 and rep["review_dropped"] == 1
    queued = db_session.query(MatchCandidate).one()
    assert queued.score > 90


def test_a_zero_cap_queues_nothing(db_session, tmp_path):
    service.upsert_game(db_session, "Hades", 111, 2005)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path), review_cap=0)
    assert rep["review_queued"] == 0 and rep["review_dropped"] == 1
    assert db_session.query(MatchCandidate).count() == 0


def test_no_cap_queues_everything_as_before(db_session, tmp_path):
    service.upsert_game(db_session, "Hades", 111, 2005)
    service.upsert_game(db_session, "Hadess", 222)
    rep = gogdb.import_archive(db_session, make_archive(tmp_path))
    assert rep["review_queued"] == 2 and rep["review_dropped"] == 0


def test_the_cap_reaches_new_candidates_once_the_top_ones_are_already_queued(db_session, tmp_path):
    service.upsert_game(db_session, "Hades", 111, 2005)   # scores 90 (exact title, year mismatch)
    service.upsert_game(db_session, "Hadess", 222)        # scores about 90.9 (fuzzy, no year)
    archive = make_archive(tmp_path)
    r1 = gogdb.import_archive(db_session, archive, review_cap=1)   # queues Hadess
    r2 = gogdb.import_archive(db_session, archive, review_cap=1)   # must reach Hades, not re-pick Hadess
    assert r1["review_queued"] == 1 and r1["review_dropped"] == 1
    assert r2["review_queued"] == 1 and r2["review_dropped"] == 0
    assert db_session.query(MatchCandidate).count() == 2
    r3 = gogdb.import_archive(db_session, archive, review_cap=1)   # nothing new left
    assert r3["review_queued"] == 0 and r3["review_dropped"] == 0
    assert db_session.query(MatchCandidate).count() == 2
