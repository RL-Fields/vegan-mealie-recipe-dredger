"""Backfill macros, per-100g figures, tags and categories on recipes already
in Mealie.

Works entirely from what Mealie already stores — name, ingredients, yield and
any nutrition — so it does not re-crawl the source sites and is not subject to
crawl delays or blocked scrapers. A few hundred recipes takes a minute or two.

  docker compose run --rm mealie-recipe-dredger \
      python maintenance/backfill_existing.py --dry-run   # report only
  docker compose run --rm mealie-recipe-dredger \
      python maintenance/backfill_existing.py             # apply
  ... --limit 20                                          # first 20 only

Existing tags and categories you added yourself are kept. Only tags this tool
manages (vegan, the macro bands, macros-*) are replaced, so re-running never
leaves a recipe holding both protein-med and protein-high.
"""

import argparse
import os
import sys

import requests

# Run from anywhere: the modules we need live one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vegan_filter import (
    MEALIE_URL, MEALIE_API_TOKEN, MIN_COVERAGE, PER_100G_MIN_COVERAGE,
    BAND_BASIS, BANDS_100G, NOTE_TITLE, Verdict,
    band_tags, cuisine_for, dish_types, estimate_macros, per_100g,
    published_macros, _plausible, _nutrition_payload, _macro_note,
    _ensure_tag, _ensure_category, _servings,
)

H = {"Authorization": f"Bearer {MEALIE_API_TOKEN}"}

# Tags this script owns and will replace; anything else on the recipe is kept.
MANAGED_PREFIXES = ('protein-', 'carb-', 'fat-', 'fibre-', 'calorie-', 'macros-')
MANAGED_EXACT = ('vegan',)


def is_managed(tag_name: str) -> bool:
    name = (tag_name or '').lower()
    return name in MANAGED_EXACT or name.startswith(MANAGED_PREFIXES)


def ingredients_of(recipe: dict):
    """Mealie stores ingredients as objects; get the human-readable line."""
    out = []
    for item in recipe.get('recipeIngredient') or []:
        if isinstance(item, str):
            line = item
        elif isinstance(item, dict):
            line = (item.get('display') or item.get('originalText')
                    or item.get('note') or '')
            if not line and item.get('food'):
                food = item['food']
                line = food.get('name', '') if isinstance(food, dict) else str(food)
                qty, unit = item.get('quantity'), item.get('unit')
                unit_name = (unit.get('name') if isinstance(unit, dict) else unit) or ''
                line = f"{qty or ''} {unit_name} {line}".strip()
        else:
            continue
        if line:
            out.append(line)
    return out


def servings_of(recipe: dict) -> float:
    n = recipe.get('recipeServings')
    try:
        if n and float(n) > 0:
            return float(n)
    except (TypeError, ValueError):
        pass
    return _servings({'recipeYield': recipe.get('recipeYield')})


def assess(recipe: dict) -> Verdict:
    """Same judgement as an import, but from Mealie's own stored data."""
    ingredients = ingredients_of(recipe)
    servings = servings_of(recipe)
    url = recipe.get('orgURL') or ''

    node = {
        'name': recipe.get('name') or '',
        'recipeYield': recipe.get('recipeYield'),
        'nutrition': recipe.get('nutrition') or {},
        'recipeIngredient': ingredients,
    }

    macros = published_macros(node)
    estimated = False
    est_macros, coverage, total_grams = estimate_macros(ingredients, servings)

    if macros is None:
        macros = est_macros
        estimated = macros is not None
        if macros is not None and (coverage < MIN_COVERAGE or not _plausible(macros)):
            macros = None

    tags = ['vegan']
    hundreds = None
    if macros is None:
        tags.append('macros-unknown')
    else:
        if BAND_BASIS in ('serving', 'both'):
            tags += band_tags(macros)
        hundreds = per_100g(macros, servings, total_grams, coverage)
        if hundreds and BAND_BASIS in ('per100', 'both'):
            tags += band_tags(hundreds, BANDS_100G, '-100g')
        if estimated:
            tags.append('macros-estimated')

    cuisine = cuisine_for(node, url, ingredients)
    categories = ([cuisine] if cuisine else []) + dish_types(node, url)
    serving_g = (round(total_grams / servings)
                 if total_grams and servings > 0 and coverage >= PER_100G_MIN_COVERAGE
                 else None)

    return Verdict(vegan=True, macros=macros, estimated=estimated,
                   coverage=coverage, tags=tags, cuisine=cuisine,
                   categories=categories, per100=hundreds,
                   serving_grams=serving_g)


def build_payload(session, recipe: dict, verdict: Verdict) -> dict:
    payload = {}

    # Tags: drop our stale ones, keep the user's, add the current set
    kept = [t for t in (recipe.get('tags') or [])
            if isinstance(t, dict) and not is_managed(t.get('name'))]
    ours = [t for t in (_ensure_tag(session, n) for n in verdict.tags) if t]
    have = {t.get('slug') for t in kept}
    payload['tags'] = kept + [t for t in ours if t.get('slug') not in have]

    # Categories: purely additive — never remove one the user chose
    existing = [c for c in (recipe.get('recipeCategory') or [])
                if isinstance(c, dict)]
    names = {(c.get('name') or '').lower() for c in existing}
    new_cats = [c for c in (_ensure_category(session, n)
                            for n in verdict.categories if n.lower() not in names)
                if c]
    if new_cats:
        payload['recipeCategory'] = existing + new_cats

    if verdict.macros:
        payload['nutrition'] = _nutrition_payload(verdict.macros)
        settings = dict(recipe.get('settings') or {})
        settings['showNutrition'] = True
        payload['settings'] = settings

        note = _macro_note(verdict)
        if note:
            kept_notes = [n for n in (recipe.get('notes') or [])
                          if isinstance(n, dict) and n.get('title') != NOTE_TITLE]
            payload['notes'] = kept_notes + [note]

    return payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help="report, change nothing")
    ap.add_argument('--limit', type=int, help="only process this many recipes")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(H)

    slugs, page = [], 1
    while True:
        r = session.get(f"{MEALIE_URL}/api/recipes",
                        params={"page": page, "perPage": 100}, timeout=30)
        r.raise_for_status()
        items = r.json().get('items') or []
        if not items:
            break
        slugs += [i['slug'] for i in items if i.get('slug')]
        page += 1

    if args.limit:
        slugs = slugs[:args.limit]

    print(f"{len(slugs)} recipes to process"
          f"{' (dry run)' if args.dry_run else ''}\n")

    counts = {'updated': 0, 'macros': 0, 'per100': 0, 'categorised': 0, 'failed': 0}

    for i, slug in enumerate(slugs, 1):
        try:
            recipe = session.get(f"{MEALIE_URL}/api/recipes/{slug}", timeout=30).json()
            verdict = assess(recipe)
            payload = build_payload(session, recipe, verdict)

            if verdict.macros:
                counts['macros'] += 1
            if verdict.per100:
                counts['per100'] += 1
            if verdict.categories:
                counts['categorised'] += 1

            cats = ' + '.join(verdict.categories) or '—'
            print(f"[{i}/{len(slugs)}] {slug}\n"
                  f"      {cats} | {', '.join(verdict.tags)}\n"
                  f"      {verdict.summary()}")

            if not args.dry_run:
                p = session.patch(f"{MEALIE_URL}/api/recipes/{slug}",
                                  json=payload, timeout=30)
                if p.status_code in (200, 201):
                    counts['updated'] += 1
                else:
                    counts['failed'] += 1
                    print(f"      ! PATCH returned HTTP {p.status_code}")
        except Exception as e:
            counts['failed'] += 1
            print(f"[{i}/{len(slugs)}] {slug}\n      ! {e}")
        sys.stdout.flush()

    print("\n" + "=" * 50)
    print(f"  recipes seen      {len(slugs)}")
    print(f"  with macros       {counts['macros']}")
    print(f"  with per-100g     {counts['per100']}")
    print(f"  categorised       {counts['categorised']}")
    print(f"  {'would update' if args.dry_run else 'updated'}      {counts['updated'] if not args.dry_run else len(slugs) - counts['failed']}")
    print(f"  failed            {counts['failed']}")


if __name__ == "__main__":
    main()
