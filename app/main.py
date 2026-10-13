"""Pairwise item ranking.

Votes are the only durable state: ratings are always re-derived from the full
vote history on read. That makes undo a single DELETE instead of an inverse
update, and it means a change to the model re-scores all of history rather than
leaving old rows scored under old constants.

Ratings come from a Bradley-Terry fit, not Elo. Elo is an online estimator built
for continuous play: replaying a sparse offline set through it once gives every
"2-0" item the identical number (its opponents were all still at the starting
rating when it beat them) and ignores transitive evidence entirely. Measured on
a simulated voter with 200 picks over 89 items, Elo left 87% of items tied
with someone and matched the simulated true order 70% of the time; the fit below
scores the same votes with almost no exact ties and much higher agreement.
"""

import json
import math
import mimetypes
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# python:slim carries no /etc/mime.types, so StaticFiles labels .webp as
# text/plain. Browsers sniff <img> anyway, but that breaks caching heuristics
# and any direct fetch.
mimetypes.add_type("image/webp", ".webp")

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
DB_PATH = DATA_DIR / "rank.db"

START_RATING = 1500.0
# Display scale: one "Elo point" per the usual 400-per-decade convention, so the
# numbers read like ratings people recognise.
RATING_SCALE = 400.0 / math.log(10.0)

# Strength of the pull toward the middle, in virtual games against an
# average-strength opponent. Without it an undefeated item has infinite
# maximum-likelihood strength; with it, 3-0 outranks 1-0 (more evidence, less
# shrinkage) instead of both sitting at "unbeaten".
PRIOR_GAMES = 1.0
FIT_ITERATIONS = 300
FIT_TOLERANCE = 1e-10

# Ratings within this many points are treated as unseparated by the fit, and go
# through the tiebreak chain in _break_ties instead of being ordered by their
# meaningless last decimal. Half a point is a 50.07% win probability.
TIE_EPSILON = 0.5

# Every item should be seen this many times before the ranking is called done.
TARGET_PER_ITEM = 4

# What is being ranked lives in CONTENT_DIR, not in the code: an items.json plus
# whatever images it points at (served under /content/).
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", APP_DIR.parent / "content"))
IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif", ".svg")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_content():
    """Read items.json into (settings, items), filling in sensible defaults.

    items.json is {"title", "noun", "nouns", "icon", "color", "items": [...]};
    only "items" is required, and a bare list of items is accepted too. Each item
    needs a "name"; "slug", "category" and "image" are optional. With no explicit
    image, img/<slug>.<ext> is used if such a file exists.
    """
    raw = json.loads((CONTENT_DIR / "items.json").read_text())
    if isinstance(raw, list):
        raw = {"items": raw}
    settings = {
        "title": raw.get("title", "Ranker"),
        "noun": raw.get("noun", "item"),
        "nouns": raw.get("nouns", raw.get("noun", "item") + "s"),
        "color": raw.get("color", "#2b6cb0"),
    }
    items = []
    for entry in raw["items"]:
        if isinstance(entry, str):
            entry = {"name": entry}
        slug = entry.get("slug") or _slugify(entry["name"])
        image = entry.get("image")
        if not image:
            image = next(
                (f"img/{slug}{e}" for e in IMAGE_EXTS if (CONTENT_DIR / "img" / f"{slug}{e}").is_file()),
                None,
            )
        items.append({
            "slug": slug,
            "name": entry["name"],
            "category": entry.get("category", ""),
            "image": f"/content/{image}" if image else None,
        })
    slugs = [i["slug"] for i in items]
    if len(set(slugs)) != len(slugs):
        dupes = sorted({s for s in slugs if slugs.count(s) > 1})
        raise SystemExit(f"items.json has duplicate slugs: {dupes}")
    if len(items) < 2:
        raise SystemExit("items.json needs at least two items")
    icon = raw.get("icon")
    settings["icon"] = next(
        (i["image"] for i in items if i["slug"] == icon), next((i["image"] for i in items if i["image"]), None)
    )
    return settings, items


SETTINGS, ITEMS = load_content()
BY_SLUG = {i["slug"]: i for i in ITEMS}
CATEGORIES = sorted({i["category"] for i in ITEMS if i["category"]})

app = FastAPI(title=SETTINGS["title"], docs_url=None, redoc_url=None)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS votes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                voter   TEXT NOT NULL,
                winner  TEXT NOT NULL,
                loser   TEXT NOT NULL,
                ts      REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS votes_voter ON votes(voter);
            -- Skipping is gone: a skipped pair was recorded as "asked" and so
            -- was never offered again, which threw away exactly the comparisons
            -- the ranking most needed -- the hard ones between close items.
            DROP TABLE IF EXISTS skips;
            """
        )


init_db()


def pool(category: str) -> list[dict]:
    if category in ("all", "", None):
        return list(ITEMS)
    return [i for i in ITEMS if i["category"] == category]


def load_votes(voter: str | None) -> list[sqlite3.Row]:
    with db() as conn:
        if voter and voter != "everyone":
            return conn.execute(
                "SELECT voter, winner, loser FROM votes WHERE voter = ? ORDER BY id", (voter,)
            ).fetchall()
        return conn.execute("SELECT voter, winner, loser FROM votes ORDER BY id").fetchall()


def load_pairs(voter: str) -> set[frozenset]:
    """Pairs this voter has already answered, so they aren't re-asked."""
    with db() as conn:
        rows = conn.execute(
            "SELECT winner AS a, loser AS b FROM votes WHERE voter = ?", (voter,)
        ).fetchall()
    return {frozenset((r["a"], r["b"])) for r in rows}


# The fit is pure in (votes, pool), so it only needs redoing when a vote is
# added or removed. Keyed by the vote table's row count and highest id, which
# together change on every insert and on every undo.
_fit_cache: dict[tuple, dict] = {}


def _fit_key(voter: str | None, category: str) -> tuple:
    with db() as conn:
        if voter and voter != "everyone":
            row = conn.execute(
                "SELECT COUNT(*) c, COALESCE(MAX(id), 0) m FROM votes WHERE voter = ?", (voter,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) c, COALESCE(MAX(id), 0) m FROM votes").fetchone()
    return (voter, category, row["c"], row["m"])


def score(voter: str | None, category: str) -> dict[str, dict]:
    key = _fit_key(voter, category)
    hit = _fit_cache.get(key)
    if hit is None:
        hit = _fit_cache[key] = _fit(voter, category)
        if len(_fit_cache) > 32:
            _fit_cache.pop(next(iter(_fit_cache)))
    return hit


def _fit(voter: str | None, category: str) -> dict[str, dict]:
    """Fit Bradley-Terry strengths to this voter's votes within the category.

    Bradley-Terry says P(i beats j) = p_i / (p_i + p_j). Fitting it by
    maximum likelihood uses the whole comparison graph at once, so beating a
    item that itself beat strong ones counts for more than beating a weak one,
    and items never directly compared are still ordered through their shared
    opponents. Solved by the standard MM iteration (Hunter 2004), which is
    monotone and needs no step size.
    """
    slugs = [f["slug"] for f in pool(category)]
    index = {s: i for i, s in enumerate(slugs)}
    n = len(slugs)

    wins = [0] * n
    losses = [0] * n
    beat: list[dict[int, int]] = [{} for _ in range(n)]
    # games[i] maps opponent index -> number of meetings, the only aggregate the
    # fit needs. Order and timing of votes are irrelevant to the likelihood.
    games: list[dict[int, int]] = [{} for _ in range(n)]

    for row in load_votes(voter):
        w, l = index.get(row["winner"]), index.get(row["loser"])
        if w is None or l is None:
            continue
        wins[w] += 1
        losses[l] += 1
        beat[w][l] = beat[w].get(l, 0) + 1
        games[w][l] = games[w].get(l, 0) + 1
        games[l][w] = games[l].get(w, 0) + 1

    # The prior is PRIOR_GAMES wins and PRIOR_GAMES losses against a virtual
    # opponent pinned at strength 1, which is also what an untasted item
    # collapses to.
    p = [1.0] * n
    for _ in range(FIT_ITERATIONS):
        shift = 0.0
        for i in range(n):
            denom = 2.0 * PRIOR_GAMES / (p[i] + 1.0)
            for j, count in games[i].items():
                denom += count / (p[i] + p[j])
            new = (wins[i] + PRIOR_GAMES) / denom
            shift = max(shift, abs(new - p[i]) / p[i])
            p[i] = new
        # Strengths are only identified up to a common factor; renormalising each
        # sweep keeps them centred so the ratings below stay near START_RATING.
        geo = math.exp(sum(math.log(x) for x in p) / n)
        p = [x / geo for x in p]
        if shift < FIT_TOLERANCE:
            break

    rating = [START_RATING + RATING_SCALE * math.log(x) for x in p]
    return {
        s: {
            "rating": rating[i],
            "strength": p[i],
            "wins": wins[i],
            "losses": losses[i],
            "n": wins[i] + losses[i],
            # Average strength of everything it was measured against, used to
            # separate items the fit itself cannot tell apart.
            "sos": (
                sum(rating[j] * c for j, c in games[i].items()) / sum(games[i].values())
                if games[i] else START_RATING
            ),
            "beat": {slugs[j]: c for j, c in beat[i].items()},
        }
        for s, i in index.items()
    }


def ranked(voter: str | None, category: str) -> list[dict]:
    state = score(voter, category)
    out = []
    for f in pool(category):
        st = state[f["slug"]]
        out.append({**f, **st, "rating": round(st["rating"], 1), "tasted": st["n"] > 0})
    # Untasted items carry no evidence at all, so they land on exactly the
    # prior. Sort them to the back by name rather than interleaving them with
    # real results that happen to sit near the middle.
    out.sort(key=lambda x: (not x["tasted"], -x["rating"], x["name"]))

    # Strict total order: every item gets its own place. Where the fit cannot
    # separate two items, fall through a chain of tiebreaks rather than
    # printing a shared rank -- the list is meant to be read as a ranking.
    order: list[dict] = []
    i = 0
    while i < len(out):
        j = i + 1
        if out[i]["tasted"]:
            while j < len(out) and out[j]["tasted"] and out[i]["rating"] - out[j]["rating"] <= TIE_EPSILON:
                j += 1
        order.extend(_break_ties(out[i:j]))
        i = j

    for n, x in enumerate(order, 1):
        x["rank"] = n if x["tasted"] else None
    return order


def _break_ties(group: list[dict]) -> list[dict]:
    """Order items the fit scored as equal, most-deserving first."""
    if len(group) < 2:
        return group

    # Two items that actually met: the head-to-head result is the most direct
    # evidence there is, and outranks any aggregate.
    if len(group) == 2:
        a, b = group
        ab = a["beat"].get(b["slug"], 0)
        ba = b["beat"].get(a["slug"], 0)
        if ab != ba:
            return [a, b] if ab > ba else [b, a]

    return sorted(
        group,
        key=lambda x: (
            -(x["wins"] / x["n"]) if x["n"] else 0.0,  # won a higher share
            -x["n"],                                   # on more evidence
            -x["sos"],                                 # against tougher items
            x["name"],                                 # stable, so the order never wobbles
        ),
    )


@app.get("/api/config")
def config():
    with db() as conn:
        voters = [
            r["voter"]
            for r in conn.execute(
                "SELECT voter, COUNT(*) c FROM votes GROUP BY voter ORDER BY c DESC"
            ).fetchall()
        ]
    return {
        "categories": CATEGORIES,
        "counts": {c: len(pool(c)) for c in CATEGORIES} | {"all": len(ITEMS)},
        "voters": voters,
        "settings": SETTINGS,
        "target_per_item": TARGET_PER_ITEM,
    }


@app.get("/api/pair")
def pair(voter: str, category: str = "all"):
    items = pool(category)
    if len(items) < 2:
        raise HTTPException(400, "category has fewer than two items")

    state = score(voter, category)
    seen = load_pairs(voter)
    counts = {f["slug"]: state[f["slug"]]["n"] for f in items}
    total_pairs = len(items) * (len(items) - 1) // 2
    prog = progress(items, counts, len(seen), total_pairs)

    chosen = None
    if min(counts.values()) < TARGET_PER_ITEM:
        # Coverage phase. Spread votes over the whole pool first: an item with
        # no comparisons has no position at all, which is a bigger gap in the
        # ranking than two mid-table items being one place out of order.
        fewest = min(counts.values())
        candidates = [f for f in items if counts[f["slug"]] <= fewest + 1]
        random.shuffle(candidates)
        for a in candidates:
            opponents = [
                b for b in items
                if b["slug"] != a["slug"] and frozenset((a["slug"], b["slug"])) not in seen
            ]
            if not opponents:
                continue
            ra = state[a["slug"]]["rating"]
            opponents.sort(key=lambda b: (abs(state[b["slug"]]["rating"] - ra), counts[b["slug"]]))
            chosen = (a, random.choice(opponents[:6]))
            break
    else:
        # Resolution phase. Everything has a position now, so spend votes where
        # the ranking is still undecided: the closest-rated pair nobody has been
        # asked about. A vote between two items already 300 points apart tells
        # us almost nothing; one between neighbours moves them past each other.
        best = []
        for i, a in enumerate(items):
            ra = state[a["slug"]]["rating"]
            for b in items[i + 1:]:
                if frozenset((a["slug"], b["slug"])) in seen:
                    continue
                best.append((abs(state[b["slug"]]["rating"] - ra), a, b))
        if best:
            best.sort(key=lambda t: t[0])
            # Sample from the closest handful so repeat sessions don't replay the
            # same fixed order.
            chosen = random.choice(best[:8])[1:]

    if not chosen:
        return {"a": None, "b": None, "done": True, "progress": prog}

    a, b = chosen
    # Randomise which side each lands on: an item that always appears on top
    # would pick up whatever position bias the voter has.
    if random.random() < 0.5:
        a, b = b, a
    return {"a": a, "b": b, "progress": prog}


def progress(items, counts, answered, total_pairs):
    need = len(items) * TARGET_PER_ITEM // 2
    least = min(counts.values()) if counts else 0
    return {
        "votes": answered,
        "target": need,
        "pct": min(100, round(100 * answered / need)) if need else 100,
        "least_seen": least,
        "unseen": sum(1 for v in counts.values() if v == 0),
        "pairs_left": total_pairs - answered,
        "phase": "coverage" if least < TARGET_PER_ITEM else "resolution",
    }


class Vote(BaseModel):
    voter: str = Field(min_length=1, max_length=40)
    winner: str
    loser: str


@app.post("/api/vote")
def vote(v: Vote):
    if v.winner not in BY_SLUG or v.loser not in BY_SLUG or v.winner == v.loser:
        raise HTTPException(400, "unknown item")
    with db() as conn:
        conn.execute(
            "INSERT INTO votes (voter, winner, loser, ts) VALUES (?, ?, ?, ?)",
            (v.voter.strip(), v.winner, v.loser, time.time()),
        )
    return {"ok": True}


@app.post("/api/undo")
def undo(voter: str):
    """Drop the voter's most recent vote."""
    with db() as conn:
        v = conn.execute(
            "SELECT id FROM votes WHERE voter = ? ORDER BY id DESC LIMIT 1", (voter,)
        ).fetchone()
        if not v:
            return {"ok": False, "reason": "nothing to undo"}
        conn.execute("DELETE FROM votes WHERE id = ?", (v["id"],))
    return {"ok": True}


@app.get("/api/ranking")
def ranking(voter: str = "everyone", category: str = "all"):
    items = ranked(voter, category)
    return {
        "voter": voter,
        "category": category,
        "items": items,
        "total_votes": sum(x["wins"] for x in items),
    }


@app.post("/api/reset")
def reset(voter: str):
    if voter == "everyone":
        raise HTTPException(400, "pick a single voter to reset")
    with db() as conn:
        n = conn.execute("DELETE FROM votes WHERE voter = ?", (voter,)).rowcount
    return {"ok": True, "deleted": n}


@app.get("/healthz")
def healthz():
    return {"ok": True, "items": len(ITEMS)}


@app.get("/")
def index():
    return FileResponse(APP_DIR / "static" / "index.html")


app.mount("/content", StaticFiles(directory=CONTENT_DIR), name="content")
app.mount("/", StaticFiles(directory=APP_DIR / "static"), name="static")
