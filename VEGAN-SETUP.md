# Vegan + macro-banding fork of Recipe Dredger

## What changed vs upstream

| File | Change |
|---|---|
| `vegan_filter.py` | **New.** Vegan gate, full macro estimator, Mealie write-back. |
| `dredger.py` | 4 small hunks: import the module; capture the Mealie slug on import; vegan check + tagging in the main loop and in the retry queue. |
| `sites_vegan.json` | **New.** ~45 fully-vegan blogs instead of the 149 mixed ones. |
| `Dockerfile` | Also copies `vegan_filter.py`. |
| `docker-compose.yml` | Builds locally instead of pulling the upstream image; mounts `sites_vegan.json`. |

Nothing upstream was removed, so `git pull` conflicts stay small.

## How a recipe is judged

1. Page is fetched and verified as a recipe (unchanged upstream logic).
2. Schema.org JSON-LD ingredients are read.
3. **Vegan gate** — each ingredient line has known false-positive phrases stripped
   ("vegan butter", "peanut butter", "butter beans", "almond milk", "flax egg",
   "eggplant", "chickpea", "beefsteak tomato", "king oyster mushroom",
   "nutritional yeast", "honeydew"…) and is then scanned for ~150 animal terms
   including honey, gelatine, whey, ghee, fish sauce and Worcestershire.
   A hit means reject, not import.
4. **Macros** — calories, protein, fat, carbs and fibre. Uses the site's own
   nutrition block when it publishes one (calories + protein minimum).
   Otherwise estimates from ingredients against a ~300-entry table covering
   proteins, legumes, grains, flours, nuts, oils, plant milks, sweeteners,
   vegetables and fruit — converting cups/tbsp/cans/blocks/cloves to grams and
   scaling dry legumes and grains up by 2.8×, then dividing by servings.
   Spices, salt, water and vinegar count as recognised-but-zero.
5. **Coverage guard** — if fewer than `MIN_COVERAGE` (default 70%) of the
   ingredient lines are recognised, no numbers are published; the recipe gets
   `macros-unknown` instead of a figure that would be wrong.
6. **Written back to Mealie** — the macros go into the recipe's built-in
   nutrition panel (so they show on the recipe page and feed meal-plan totals),
   and band tags go on for filtering, since Mealie's sidebar filters on tags
   rather than numbers:

   | Tag | Default threshold, per serving |
   |---|---|
   | `protein-high` / `-med` / `-low` | ≥20 g / 10–20 g / <10 g |
   | `carb-high` / `-med` / `-low` | ≥50 g / 20–50 g / <20 g |
   | `fibre-high` / `-med` / `-low` | ≥8 g / 4–8 g / <4 g |
   | `fat-*`, `calorie-*` | off by default — add to `BAND_TAGS` |

   Estimated recipes also get `macros-estimated`, so you can tell a published
   figure from a calculated one when filtering. All thresholds are `.env` settings.

Filter in Mealie with the tag sidebar, or save a cookbook on
`vegan AND protein-high` so it stays current as more get imported.

## New .env settings

See the block at the bottom of `.env.example`. The ones worth knowing:

```
VEGAN_ONLY=true                 # false = import non-vegan too
WRITE_NUTRITION=true            # false = tags only, don't touch the nutrition panel
BAND_TAGS=protein,carb,fibre    # add fat,calorie if you want those tags too
MIN_COVERAGE=0.7                # how much of the ingredient list must be recognised
```

## Deploy (Docker VM, 192.168.1.185)

```bash
mkdir -p /opt/recipe-dredger && cd /opt/recipe-dredger
# copy the files from this zip into here, then:
cp .env.example .env
nano .env      # set MEALIE_URL, MEALIE_API_TOKEN, keep DRY_RUN=true for now
docker compose build
docker compose up          # dry run — check the log
```

Get the Mealie token from Mealie → your user → **Manage API Tokens**.

When the dry run looks right, set `DRY_RUN=false` in `.env` and:

```bash
docker compose up
```

Start small the first time — add `--limit 5` by overriding the command:

```bash
docker compose run --rm mealie-recipe-dredger python dredger.py --limit 5
```

Weekly cron (Sunday 3am):

```
0 3 * * 0 cd /opt/recipe-dredger && docker compose up >> /var/log/dredger.log 2>&1
```

## Caveats worth knowing

- Estimated macros are good enough for banding and rough meal planning, not
  for precise tracking — ingredient weights are inferred from cup and spoon
  measures, and brands vary. `macros-estimated` marks every one of them, and
  the log line shows the coverage percentage behind each estimate.
- Where a site publishes its own nutrition, that's used as-is and no estimate
  is made — so those recipes carry the blog's numbers, for better or worse.
- A handful of site URLs in `sites_vegan.json` may have moved; a site with no
  reachable sitemap is skipped silently, so dead entries cost nothing.
- The vegan gate needs JSON-LD ingredients. Pages without them are imported
  and tagged `macros-unknown` rather than dropped — on a vegan-only site list
  that's the safer default. Set them to reject by editing `analyse()` in
  `vegan_filter.py` (the `if not node:` branch).
