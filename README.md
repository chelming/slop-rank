# Ranker

Pairwise "this or that" ranking for any list of items, with optional images.
Each person builds their own ranking; there's also a combined view.

## Use your own list

Put your content in a directory (default `./content`, or set `CONTENT_DIR`):

    content/
      items.json
      img/           # optional

`items.json`:

```json
{
  "title": "Pizza Toppings",
  "noun": "topping",
  "nouns": "toppings",
  "color": "#c0392b",
  "icon": "pepperoni",
  "items": [
    { "name": "Pepperoni", "category": "Meat" },
    { "name": "Mushrooms", "category": "Veg", "image": "img/shrooms.png" },
    "Extra Cheese"
  ]
}
```

- Only `items` is required (a bare list works too); an item can be just a string.
- `slug` defaults to the slugified name. **Votes are stored by slug**, so don't
  change a slug once people have voted.
- `image` is optional. If omitted, `img/<slug>.webp|png|jpg|...` is used when it exists.
- `category` is optional; the category picker only appears if any item has one.
- `icon` is the slug of the item whose image becomes the favicon.

See `content.example/` for a minimal setup.

## Run

    pip install -r requirements.txt
    CONTENT_DIR=./content.example DATA_DIR=./data uvicorn main:app --app-dir app --port 8080

Docker Compose (copy `compose.example.yml` to `compose.yml` and adjust):

    mkdir -p data && sudo chown 1000:1000 data   # container runs as uid 1000
    docker compose up -d --build

Then open http://localhost:8080. The image bakes in the sample list from
`content.example/`; the `./content:/content:ro` mount replaces it with yours.
After editing `items.json` or adding images, restart the container
(`docker compose restart ranker`) — no rebuild needed.

Plain Docker:

    docker build -t ranker .
    docker run -p 8080:8080 -v ./content:/content:ro -v ./data:/data ranker

Votes are stored in `$DATA_DIR/rank.db` (SQLite). The app has no authentication:
voters just type a name, so keep it on a trusted network.

## How it ranks

Votes are the only stored state. Ratings are re-derived from the full history on
each request, so undo is a single `DELETE` and changing the model re-scores all
of history.

Ratings come from a **Bradley-Terry** maximum-likelihood fit (MM iteration,
Hunter 2004) with a one-game prior against an average opponent — the prior keeps
undefeated items finite and makes 3-0 outrank 1-0. The fit uses the whole
comparison graph, so beating a item that itself beat strong ones counts for
more, and two items never directly compared are still ordered through shared
opponents.

Elo was the first attempt and was much worse here. It is an online estimator for
continuous play; replayed once over a sparse offline set, every item that went
2-0 got the identical number, because its opponents were all still at the
starting rating when it beat them. On one 200-vote simulated session over 89
items:

| | distinct ratings | items tied | reproduces the votes cast |
|---|---|---|---|
| Elo | 44 / 89 | 66 | 87.0% |
| Bradley-Terry | 87 / 89 | 4 | 97.5% |

Pair selection runs in two phases. **Coverage:** until every item has been
seen `TARGET_PER_FLAVOR` times, offer the least-compared ones — a item with no
comparisons has no position at all. **Resolution:** after that, offer the
closest-rated pair nobody has been asked about, which is where the ranking is
still undecided. Sides are randomised so a item doesn't collect position bias,
and a voter never sees the same pair twice.

There is no skip. A skipped pair had to be recorded as "asked" so it wouldn't
come back, which meant the hardest comparisons — exactly the ones between
close items, exactly where the ranking is undecided — were the ones most
likely to be thrown away.

**The ranking is a strict total order**, 1..N with no shared places. Where the
fit scores two items within `TIE_EPSILON` of each other it falls through a
tiebreak chain, in `_break_ties`:

1. head-to-head, if the two actually met — the most direct evidence there is
2. higher share of wins
3. more comparisons behind that share
4. tougher average opposition
5. name, so the order is stable and never wobbles between requests

Untasted items have no evidence at all, so they sit in their own unranked
section rather than being ordered against real results.
