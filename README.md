# 🌱 Vegan Recipe Dredger

Bulk-imports vegan recipes into [Mealie](https://mealie.io), with macros and
categories filled in automatically.

A fork of [D0rk4ce/mealie-recipe-dredger](https://github.com/D0rk4ce/mealie-recipe-dredger),
which crawls food blogs' sitemaps, spots new posts, checks them against your
existing library and imports what's missing. All of that still works exactly as
upstream describes it. This fork adds the part that makes the result usable if
you eat plant-based and care what's in your food:

- **nothing with animal ingredients gets in**, checked ingredient by ingredient
- **every recipe carries macros** — calories, protein, fat, carbs, fibre —
  either the site's own figures or an estimate from the ingredient list
- **every recipe is categorised** by region and by dish type, and tagged by
  macro band, so the library is filterable rather than just large
- **sites that block Mealie's scraper still work**, because the recipe is built
  locally and posted directly

---

## How a recipe gets in

Each candidate URL goes through the same pipeline:

**1 · Verified as a recipe** — unchanged upstream logic: Schema.org JSON-LD or
known recipe CSS classes, with listicles and roundups filtered out.

**2 · Vegan gate** — the ingredient list is checked against ~150 animal terms,
including the ones people forget: honey, gelatine, whey, casein, ghee, fish
sauce, Worcestershire, carmine, isinglass. Known false positives are stripped
first, so vegan butter, peanut butter, butter beans, almond milk, flax eggs,
eggplant, chickpeas, beefsteak tomatoes, king oyster mushrooms and nutritional
yeast all pass cleanly. **A hit means the recipe is rejected, not imported.**

**3 · Macros** — the site's published nutrition is used where it exists.
Otherwise they're estimated from the ingredients: a ~300-entry table of per-100g
values covering proteins, legumes, grains, flours, pasta, nuts, seeds, oils,
plant milks, sweeteners, chocolate, vegetables, fruit and condiments, with
cups/tablespoons/cans/blocks/cloves converted to grams and dry legumes and
grains scaled up 2.8×. Spices, salt, water and vinegar count as
recognised-but-zero.

Two guards stop bad numbers reaching your library. If fewer than
`MIN_COVERAGE` (70%) of the ingredient lines are recognised, nothing is
published and the recipe is tagged `macros-unknown`. And any estimate above
1200 kcal, 100g fat, 120g protein or 200g carbs per serving is discarded —
almost always a recipe whose yield says "makes 1 cup" and got counted as a
single portion. **A wrong number in the nutrition panel is worse than no
number.**

Ingredients with no quantity *and* no unit ("oil for deep frying") contribute
nothing rather than an invented 100g.

**4 · Region of origin** — from the recipe's own `recipeCuisine` where
published, normalised so "Tex-Mex" lands on Mexican and "Sichuan" on Chinese;
otherwise inferred by scoring marker ingredients and title words. Distinctive
markers (garam masala, gochujang, berbere, harissa, doubanjiang, marmite) carry
the decision; weak ones (cilantro, lime, maple syrup) only break ties, so a kale
salad doesn't become Mexican because it has coriander in it. No signal means no
category rather than a guess.

**5 · Dish type** — Breakfast, Main, Side, Starter, Salad, Soup, Stew, Curry,
Pasta, Noodles, Stir-fry, Sandwich, Burger, Pizza, Bowl, Bake, Bread, Baking,
Snack, Dip, Sauce, Dressing, Dessert, Cake, Cookies, Ice Cream, Smoothie, Drink,
Staple, Meal Prep. From the site's `recipeCategory` and `keywords` where
present, otherwise the title and slug. A recipe can hold several — sweet things
never also get Main, dishes that are a meal in themselves pick up Main
automatically, and it caps at three.

**6 · Per 100g** — Mealie's nutrition panel is per-serving only, so the per-100g
figures go in the recipe's Notes, along with the serving weight they're derived
from. Both published and estimated recipes get them, since the ingredient
weights come from the estimator either way. They're only published when at
least 85% of ingredients were recognised — a missed ingredient shrinks the
denominator and inflates every figure. **These are raw weights, before cooking
loss**, so anything that boils down will read lighter per 100g than what ends
up on the plate.

**7 · Written to Mealie** — one PATCH sets the nutrition panel, the categories,
the tags and the note, and flips on `showNutrition` so the panel renders.

---

## What you get in Mealie

**Categories** — region plus dish type, e.g. *Indian + Curry + Main*.

**Tags** — `vegan`, plus a band per macro. Mealie filters on tags rather than
numbers, which is what these are for:

| Tag | Per serving | On by default |
|---|---|---|
| `protein-high` / `-med` / `-low` | ≥20g / 10–20g / <10g | yes |
| `carb-high` / `-med` / `-low` | ≥50g / 20–50g / <20g | yes |
| `fibre-high` / `-med` / `-low` | ≥8g / 4–8g / <4g | yes |
| `fat-high` / `-med` / `-low` | ≥25g / 10–25g / <10g | no |
| `calorie-high` / `-med` / `-low` | ≥700 / 400–700 / <400 kcal | no |

The same bands are also applied to the per-100g figures, suffixed `-100g`
(`protein-high-100g`), with thresholds following food-labelling conventions
rather than portion sizes:

| Tag | Per 100g |
|---|---|
| `protein-*-100g` | high ≥10g / med ≥5g / low <5g |
| `carb-*-100g` | high ≥25g / med ≥10g / low <10g |
| `fibre-*-100g` | high ≥6g / med ≥3g / low <3g |
| `fat-*-100g` | high ≥17.5g / med ≥3g / low <3g |
| `calorie-*-100g` | high ≥250 / med ≥100 / low <100 kcal |

Per-serving answers "is this meal high in protein"; per-100g answers "is this
dish protein-dense", which is portion-independent and immune to a blog getting
its servings count wrong. Peanut butter energy balls are `protein-low` per
serving and `protein-high-100g` — both true, and useful for different
questions. `BAND_BASIS` can be set to `serving` or `per100` if you only want
one set.

**Notes** — a "Macros per 100g" entry with the per-100g breakdown and the
serving weight. Your own notes on a recipe are preserved; only ours is replaced
on re-import.

Plus `macros-estimated` where the numbers were calculated rather than
published, and `macros-unknown` where neither was possible. Every threshold is
an `.env` setting.

A saved cookbook on `vegan AND protein-high AND Indian` stays current as more
recipes arrive.

---



## Setup

```bash
git clone https://github.com/RL-Fields/vegan-mealie-recipe-dredger
cd vegan-mealie-recipe-dredger
cp .env.example .env
nano .env          # MEALIE_URL and MEALIE_API_TOKEN, at minimum
docker compose build
docker compose up  # DRY_RUN=true by default — nothing is imported
```

The API token comes from Mealie under your user → **Manage API Tokens**.

This fork builds the image locally rather than pulling upstream's, since the
filter code lives here. When the dry run looks right:

```bash
sed -i 's/^DRY_RUN=true/DRY_RUN=false/' .env
docker compose run --rm mealie-recipe-dredger python dredger.py --limit 5
```

Start small. `--limit 5` across 46 sites is enough to see the tags, categories
and nutrition land before turning it loose — the default of 50 per site is
~2,300 recipes.

Weekly, once you're happy:

```
0 3 * * 0 cd /opt/recipe-dredger && docker compose up >> /var/log/dredger.log 2>&1
```

### Watching it work

The progress bar hides the per-site logging, and the crawl delay means several
quiet minutes per site. To see the pipeline:

```bash
docker compose run --rm -e LOG_LEVEL=INFO mealie-recipe-dredger \
  python dredger.py --limit 3
```

Each import prints its categories, tags and macros:

```
🏷️  red-lentil-dal: Indian + Curry + Main | vegan, protein-med, carb-high,
    fibre-high, macros-estimated — 402 kcal, P18.4 C56.0 F12.8 Fib16.6 (est 80%)
```

---

## Backfilling recipes you already have

Everything above happens at import, so recipes imported before a given feature
never got it. This brings them up to date — macros, per-100g figures, band
tags, region and dish-type categories:

```bash
docker compose run --rm mealie-recipe-dredger \
  python maintenance/backfill_existing.py --dry-run
```

It works from what Mealie already stores — name, ingredients, yield, any
nutrition — so it never re-crawls the source sites and isn't affected by crawl
delays or blocked scrapers. A few hundred recipes takes a minute or two.

Drop `--dry-run` to apply, or add `--limit 20` to try a handful first. Worth
re-running after any change to thresholds, markers or tags.

Tags and categories you added yourself are preserved: only the tags this tool
manages (`vegan`, the macro bands, `macros-*`) are replaced, so re-running
never leaves a recipe holding both `protein-med` and `protein-high`. Categories
are additive only.

## Configuration

Upstream's settings all still apply — `TARGET_RECIPES_PER_SITE`, `SCAN_DEPTH`,
`CRAWL_DELAY`, `CACHE_EXPIRY_DAYS`, `SYNC_LIBRARY`, `LANGUAGE_FILTER`,
`NOTIFICATION_WEBHOOK_URL` and the rest. This fork adds:

| Setting | Default | Does |
|---|---|---|
| `VEGAN_ONLY` | `true` | Reject recipes containing animal ingredients |
| `WRITE_NUTRITION` | `true` | Fill Mealie's nutrition panel |
| `TAG_RECIPES` | `true` | Apply the tags |
| `SET_CUISINE` | `true` | Apply region and dish-type categories |
| `BAND_TAGS` | `protein,carb,fibre` | Which macros get band tags |
| `MIN_COVERAGE` | `0.7` | Ingredient recognition needed to trust an estimate |
| `CUISINE_MIN_SCORE` | `2` | Marker score needed to infer a region |
| `PROTEIN_HIGH` / `PROTEIN_MED` | `20` / `10` | g per serving |
| `CARB_HIGH` / `CARB_MED` | `50` / `20` | g per serving |
| `FAT_HIGH` / `FAT_MED` | `25` / `10` | g per serving |
| `FIBRE_HIGH` / `FIBRE_MED` | `8` / `4` | g per serving |
| `CAL_HIGH` / `CAL_MED` | `700` / `400` | kcal per serving |

### Adding sites from a text file

Set `EXTRA_SITES_FILE` and the dredger reads extra sites from a plain text file
each run, on top of `sites_vegan.json`. One URL per line; blank lines and
anything after a `#` are ignored, and a bare domain gets `https://` added:

```
# tried these, worth a look
https://newveganblog.com
anotherblog.co.uk        # bare domain is fine
```

Useful when the file lives on a network share, so sites can be added from any
machine without touching the repo. `EXTRA_SITES_DIR` is the folder on the
Docker host that gets mounted into the container at `/extra`:

```
EXTRA_SITES_DIR=/mnt/media/recipes
EXTRA_SITES_FILE=/extra/_sites.txt
```

Duplicates of sites already in `sites_vegan.json` are ignored, and a missing
file is a warning rather than an error, so a run never fails because the share
was unavailable. New sites are named in the log at startup.

### Site list

`sites_vegan.json` replaces upstream's 149 mixed-diet blogs with ~45 fully
vegan ones, grouped general / UK / Indian & Asian / protein-focused. It's
mounted over `sites.json` by the compose file. A site with no reachable sitemap
is skipped silently, so a dead entry costs nothing.

---

## What's changed from upstream

| File | Change |
|---|---|
| `vegan_filter.py` | New. Vegan gate, macro estimator, region and dish-type classification, Mealie write-back, local-import fallback. |
| `dredger.py` | Four small hunks: import the module, capture the slug, run the gate and write-back in the main loop and the retry queue. |
| `sites_vegan.json` | New. Vegan-only site list. |
| `maintenance/backfill_existing.py` | New. Re-applies macros, tags and categories to recipes already in Mealie. |
| `Dockerfile` | Copies `vegan_filter.py`. |
| `docker-compose.yml` | Builds locally; mounts `sites_vegan.json`. |
| `.env.example` | The settings above. |

Nothing upstream was removed, so `git pull upstream main` stays mergeable.

---

## Known limits

- **Estimated macros are for banding and rough planning, not tracking.**
  Weights are inferred from cup and spoon measures and brands vary.
  `macros-estimated` marks every one, and the log line shows the coverage
  behind it.
- **Published nutrition is taken as authoritative**, so a blog with junk figures
  produces junk here. Nothing is cross-checked.
- **The vegan gate needs JSON-LD ingredients.** Pages without them are imported
  and tagged `macros-unknown` rather than dropped — reasonable on a vegan-only
  site list. To reject them instead, edit the `if not node:` branch in
  `analyse()`.
- **Region and dish type are inferred from words**, so an unusual name will be
  missed or occasionally mislabelled. Raise `CUISINE_MIN_SCORE` to be stricter.
- **Existing recipes aren't backfilled.** Everything here applies at import.

---

## Credit and licence

All the crawling, sitemap parsing, deduplication, caching, rate limiting and
retry logic is [D0rk4ce](https://github.com/D0rk4ce)'s work — see
[the upstream README](https://github.com/D0rk4ce/mealie-recipe-dredger) for how
that machinery works and how to configure it. This fork only adds a filter and
a classifier on top. Same licence as upstream.
