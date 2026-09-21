import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, fx, metrics, regions, service
from .db import SessionLocal, get_session, init_db
from .matcher import normalize
from .models import Game, MatchCandidate


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as s:
        service.seed_stores(s)
    yield


app = FastAPI(title="PlayMatch price-match", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        label = route.path if route else "unmatched"  # bounded label cardinality
        metrics.REQUESTS.labels(request.method, label, str(status)).inc()
        metrics.LATENCY.labels(label).observe(time.perf_counter() - start)


def _game(s: Session, game_id: int) -> Game:
    g = s.get(Game, game_id)
    if not g:
        raise HTTPException(404, "game not found")
    return g


def _region(code: str | None) -> str:
    try:
        return regions.get(code).code
    except KeyError:
        raise HTTPException(400, f"region not enabled: {code}")


def require_admin(x_admin_token: str | None = Header(default=None)):
    if not config.ADMIN_TOKEN:
        raise HTTPException(403, "admin endpoints are disabled (ADMIN_TOKEN not set)")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(401, "bad admin token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def prometheus(s: Session = Depends(get_session)):
    metrics.update_gauges(service.stats(s))
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)


@app.get("/api/stats")
def stats(s: Session = Depends(get_session)):
    return service.stats(s)


@app.get("/api/regions")
def list_regions():
    return {"default": regions.default().code,
            "enabled": [{"code": r.code, "currency": r.currency, "name": r.name} for r in regions.enabled()]}


@app.get("/api/currencies")
def currencies():
    return {"base": config.BASE_CURRENCY, "supported": fx.SUPPORTED}


@app.get("/api/games")
def search_games(q: str = Query("", max_length=100), limit: int = 20, s: Session = Depends(get_session)):
    stmt = select(Game).order_by(Game.title).limit(min(limit, 50))
    if q:
        stmt = stmt.where(Game.norm_title.contains(normalize(q)))
    return [service.game_dict(g) for g in s.scalars(stmt)]


@app.get("/api/games/{game_id}/prices")
def prices(game_id: int, currency: str | None = None, region: str | None = None,
           s: Session = Depends(get_session)):
    return service.current_prices(s, _game(s, game_id), currency, _region(region))


@app.get("/api/games/{game_id}/history")
def history(game_id: int, currency: str | None = None, region: str | None = None,
            days: int = Query(365, ge=1, le=3650), s: Session = Depends(get_session)):
    return service.history(s, _game(s, game_id), currency, days, _region(region))


# ---- admin: requires X-Admin-Token matching ADMIN_TOKEN ------------------

@app.post("/api/admin/games", dependencies=[Depends(require_admin)])
def add_game(title: str, steam_app_id: int | None = None, release_year: int | None = None,
             s: Session = Depends(get_session)):
    return service.game_dict(service.upsert_game(s, title, steam_app_id, release_year))


@app.post("/api/admin/games/{game_id}/refresh", dependencies=[Depends(require_admin)])
def refresh_game(game_id: int, region: str | None = None, s: Session = Depends(get_session)):
    return service.ingest_game(s, _game(s, game_id), _region(region))


@app.get("/api/admin/review", dependencies=[Depends(require_admin)])
def review_queue(s: Session = Depends(get_session)):
    rows = s.scalars(select(MatchCandidate).where(MatchCandidate.status == "pending")
                     .order_by(MatchCandidate.score.desc()))
    return [{"id": c.id, "game_id": c.game_id, "store_id": c.store_id, "store_title": c.store_title,
             "score": c.score, "url": c.url} for c in rows]


@app.post("/api/admin/review/{cand_id}/{decision}", dependencies=[Depends(require_admin)])
def resolve(cand_id: int, decision: str, s: Session = Depends(get_session)):
    c = s.get(MatchCandidate, cand_id)
    if not c or decision not in ("approve", "reject"):
        raise HTTPException(404, "not found or bad decision")
    if decision == "approve":
        service.approve_candidate(s, c)
    else:
        c.status = "rejected"
        s.commit()
    return {"status": c.status}


# Optional: serve the frontend from the API process for local dev without Docker.
if os.getenv("FRONTEND_DIR"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=os.environ["FRONTEND_DIR"], html=True), name="frontend")
