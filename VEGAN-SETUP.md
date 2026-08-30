# Vegan + protein-banding fork of Recipe Dredger

## What changed vs upstream

| File | Change |
|---|---|
| `vegan_filter.py` | **New.** Vegan gate, protein estimator, Mealie tagging. |
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
4. **Protein** — uses the recipe's published `proteinContent` if it has one.
   Otherwise estimates from ingredients using a lookup table
   (seitan, tofu, tempeh, TVP, legumes, nuts, high-protein pastas…),
   converting cups/tbsp/cans/blocks to grams, scaling dry legumes and grains
   up by 2.8×, then dividing by servings.
5. **Tags applied in Mealie:** `vegan`, plus one of `protein-high` (≥20 g/serving),
   `protein-med` (10–20 g), `protein-low` (<10 g) or `protein-unknown`.
   Estimated numbers also get `protein-estimated`, so you can tell a published
   figure from a guess when you filter.

Filter in Mealie with the tag sidebar, or save a cookbook on
`vegan AND protein-high` so it stays current as more get imported.

## New .env settings

```
VEGAN_ONLY=true        # false = import non-vegan too (still tagged)
TAG_RECIPES=true       # false = import without touching tags
PROTEIN_HIGH=20        # g per serving for protein-high
PROTEIN_MED=10         # g per serving for protein-med
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

- The estimator is rough — good enough for banding, not for macro tracking.
  That's what `protein-estimated` marks.
- A handful of site URLs in `sites_vegan.json` may have moved; a site with no
  reachable sitemap is skipped silently, so dead entries cost nothing.
- The vegan gate needs JSON-LD ingredients. Pages without them are imported
  and tagged `protein-unknown` rather than dropped — on a vegan-only site list
  that's the safer default. Set them to reject by editing `analyse()` in
  `vegan_filter.py` (the `if not node:` branch).
