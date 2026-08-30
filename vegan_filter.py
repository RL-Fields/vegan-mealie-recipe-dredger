"""
vegan_filter.py — vegan gate + protein banding for Recipe Dredger.

Drop this file next to dredger.py. It does three things:

1. analyse(url, soup) -> Verdict
   Pulls the Schema.org JSON-LD recipe block off the page, checks the
   ingredient list for animal products, and works out protein per serving
   (from published nutrition if present, otherwise estimated from ingredients).

2. Verdict.tags — the Mealie tags to apply: vegan, protein-high/med/low,
   plus protein-estimated when the number was guessed rather than published.

3. tag_recipe(slug, tags) — creates the tags in Mealie if needed and
   attaches them to the imported recipe.

Env vars:
  VEGAN_ONLY=true            reject non-vegan recipes instead of importing them
  PROTEIN_HIGH=20            g per serving for protein-high
  PROTEIN_MED=10             g per serving for protein-med
  TAG_RECIPES=true           apply tags in Mealie after import
"""

import json
import os
import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger("dredger.vegan")

MEALIE_URL = os.getenv('MEALIE_URL', 'http://localhost:9000').rstrip('/')
MEALIE_API_TOKEN = os.getenv('MEALIE_API_TOKEN', '')
VEGAN_ONLY = os.getenv('VEGAN_ONLY', 'true').lower() == 'true'
TAG_RECIPES = os.getenv('TAG_RECIPES', 'true').lower() == 'true'
PROTEIN_HIGH = float(os.getenv('PROTEIN_HIGH', 20))
PROTEIN_MED = float(os.getenv('PROTEIN_MED', 10))

# ---------------------------------------------------------------------------
# 1. VEGAN GATE
# ---------------------------------------------------------------------------

# Phrases that LOOK animal but are not. Stripped from the line before scanning.
SAFE_PHRASES = [
    r'\b(vegan|plant[- ]based|dairy[- ]free|egg[- ]free|non[- ]dairy|meat[- ]free|'
    r'vegetarian|mock|faux|imitation|substitute for|instead of|no\b)\s+[\w\- ]{0,20}',
    r'\b(peanut|almond|cashew|nut|seed|sunflower|apple|cocoa|coconut|shea|tahini)\s+butter\b',
    r'\bbutter(nut|head|milk powder|fly|cup squash)\b',
    r'\bbutter\s+(bean|lettuce)s?\b',
    r'\b(coconut|almond|soy|soya|oat|rice|cashew|hemp|flax|pea|macadamia|walnut|'
    r'hazelnut|quinoa|nut)\s+(milk|cream|yogh?urt|yoghurt|creamer)\b',
    r'\bmilk\s+thistle\b',
    r'\bcream\s+of\s+(tartar|wheat)\b',
    r'\bcreamed\s+(corn|spinach)\b',
    r'\begg\s*plant\b|\baubergine\b',
    r'\b(flax|chia|aquafaba|tofu)\s+egg\b|\begg\s+replacer\b',
    r'\bchick\s*pea\w*\b|\bgarbanzo\b',
    r'\bbeef\s*(steak)?\s+tomato\w*\b',
    r'\b(oyster|king oyster|lion\'?s mane|chicken of the woods|hen of the woods|maitake)\s*(mushroom)?s?\b',
    r'\bcrab\s+apple\w*\b',
    r'\bhoney\s*(dew|crisp|nut squash|comb pattern)\b',
    r'\bnutritional\s+yeast\b',
    r'\bcheese\s*cloth\b',
    r'\bcashew\s+cheese\b|\bnut\s+cheese\b',
    r'\bcream\s+cheese\s+style\b',
    r'\bsea\s*food\s*style\b',
    r'\bfish\s*less\b|\bchick\s*less\b|\bbeef\s*less\b',
    r'\bhamburger\s+(bun|roll)s?\b|\bburger\s+bun\w*\b',
    r'\bsoy\s+curls?\b',
    r'\bcoconut\s+bacon\b',
    r'\bcoconut\s+(oil|sugar|flour|water|aminos|flakes?|shreds?)\b',
    r'\bbutter\s*less\b',
    r'\bcream\w*\s*(texture|consistency|sauce made)\b',
]

ANIMAL_TERMS = [
    # dairy & eggs
    r'\bmilk\b', r'\bbuttermilk\b', r'\bbutter\b', r'\bghee\b', r'\bcreams?\b',
    r'\bcr[eè]me\s+fra[iî]che\b', r'\bhalf[- ]and[- ]half\b', r'\bdouble cream\b',
    r'\bsour cream\b', r'\bcondensed milk\b', r'\bevaporated milk\b',
    r'\bcheeses?\b', r'\bparmesan\b', r'\bparmigiano\b', r'\bpecorino\b',
    r'\bmozzarella\b', r'\bcheddar\b', r'\bfeta\b', r'\bricotta\b',
    r'\bmascarpone\b', r'\bhalloumi\b', r'\bpaneer\b', r'\bgruy[eè]re\b',
    r'\bbrie\b', r'\bgorgonzola\b', r'\bcotija\b', r'\bqueso\b',
    r'\byogh?urt\b', r'\bskyr\b', r'\bquark\b', r'\bcustard\b', r'\bcurds?\b',
    r'\bwhey\b', r'\bcasein\b', r'\bghee\b',
    r'\beggs?\b', r'\begg\s+(white|yolk)s?\b', r'\bmayonnaise\b', r'\bmayo\b',
    r'\bmeringue\b',
    # meat & poultry
    r'\bchicken\b', r'\bbeef\b', r'\bsteak\b', r'\bpork\b', r'\bbacon\b',
    r'\bham\b', r'\bsausages?\b', r'\blamb\b', r'\bmutton\b', r'\bveal\b',
    r'\bturkey\b', r'\bduck\b', r'\bvenison\b', r'\bmince\b', r'\bbrisket\b',
    r'\bpepperoni\b', r'\bsalami\b', r'\bprosciutto\b', r'\bchorizo\b',
    r'\bpancetta\b', r'\bguanciale\b', r'\bmeatballs?\b', r'\bribs?\b',
    r'\bthighs?\b', r'\bdrumsticks?\b', r'\bpoultry\b', r'\bmeat\b',
    # fish & shellfish
    r'\bfish\b', r'\bsalmon\b', r'\btuna\b', r'\banchov\w+\b', r'\bcod\b',
    r'\bhaddock\b', r'\bsardines?\b', r'\bmackerel\b', r'\btrout\b',
    r'\bprawns?\b', r'\bshrimps?\b', r'\bcrab\b', r'\blobster\b',
    r'\boysters?\b', r'\bmussels?\b', r'\bclams?\b', r'\bsquid\b',
    r'\bcalamari\b', r'\boctopus\b', r'\bscallops?\b', r'\bcaviar\b',
    r'\broe\b', r'\bbonito\b', r'\bkatsuobushi\b', r'\bdashi\b',
    r'\bfish sauce\b', r'\boyster sauce\b', r'\bshellfish\b',
    # fats, stocks, additives
    r'\blard\b', r'\btallow\b', r'\bsuet\b', r'\bschmaltz\b',
    r'\bbone broth\b', r'\bchicken (stock|broth|bouillon)\b',
    r'\bbeef (stock|broth|bouillon)\b',
    r'\bgelatin\w*\b', r'\bcollagen\b', r'\balbumin\b', r'\bisinglass\b',
    r'\bcarmine\b', r'\bcochineal\b', r'\bshellac\b', r'\brennet\b',
    r'\bhoney\b', r'\bbee\s*pollen\b', r'\broyal jelly\b', r'\bpropolis\b',
    r'\bworcestershire\b',
]

SAFE_RE = [re.compile(p, re.I) for p in SAFE_PHRASES]
ANIMAL_RE = [(re.compile(p, re.I), p) for p in ANIMAL_TERMS]


def check_vegan(ingredients: List[str]) -> Tuple[bool, Optional[str]]:
    """Return (is_vegan, offending_ingredient)."""
    for raw in ingredients:
        line = re.sub(r'\s+', ' ', str(raw)).lower()
        # Remove known false-positive phrases first
        for safe in SAFE_RE:
            line = safe.sub(' ', line)
        for rx, pattern in ANIMAL_RE:
            if rx.search(line):
                return False, f"{raw.strip()[:60]} ({pattern})"
    return True, None


# ---------------------------------------------------------------------------
# 2. PROTEIN
# ---------------------------------------------------------------------------

# grams of protein per 100g of ingredient
PROTEIN_PER_100G = {
    'seitan': 75, 'vital wheat gluten': 75, 'soy protein': 80, 'protein powder': 75,
    'tvp': 52, 'textured vegetable protein': 52, 'soy curls': 47,
    'peanut butter': 25, 'almond butter': 21, 'cashew butter': 18, 'tahini': 17,
    'nutritional yeast': 50, 'hemp seed': 32, 'hemp heart': 32,
    'pumpkin seed': 30, 'peanut': 26, 'almond': 21, 'pistachio': 20,
    'sunflower seed': 21, 'cashew': 18, 'walnut': 15, 'chia': 17, 'flax': 18,
    'sesame': 17, 'pecan': 9, 'hazelnut': 15, 'macadamia': 8,
    'tempeh': 19, 'tofu': 12, 'firm tofu': 15, 'extra firm tofu': 17,
    'silken tofu': 6, 'edamame': 11, 'soybean': 13,
    'lentil': 9, 'red lentil': 9, 'green lentil': 9, 'brown lentil': 9,
    'chickpea': 9, 'garbanzo': 9, 'black bean': 9, 'kidney bean': 9,
    'pinto bean': 9, 'cannellini': 9, 'butter bean': 7, 'white bean': 9,
    'split pea': 8, 'green pea': 5, 'pea': 5, 'bean': 8,
    'quinoa': 4.4, 'oat': 13, 'rolled oat': 13, 'buckwheat': 13,
    'pasta': 13, 'wholewheat pasta': 14, 'whole wheat pasta': 14,
    'lentil pasta': 25, 'chickpea pasta': 20, 'edamame pasta': 40,
    'bread': 9, 'flour': 10, 'wheat flour': 10, 'chickpea flour': 22,
    'gram flour': 22, 'besan': 22, 'rice': 2.7, 'couscous': 4,
    'bulgur': 3, 'barley': 3.5, 'millet': 3.5, 'farro': 7,
    'soy milk': 3.3, 'soya milk': 3.3, 'pea milk': 3.3, 'oat milk': 1,
    'almond milk': 0.5, 'coconut milk': 2, 'cashew milk': 0.5,
    'soy yogurt': 4, 'vegan yogurt': 2, 'vegan cheese': 2, 'vegan sausage': 18,
    'vegan mince': 17, 'veggie burger': 15, 'vegan burger': 17,
    'nutritional': 50,
    'mushroom': 3, 'spinach': 2.9, 'broccoli': 2.8, 'kale': 3,
    'potato': 2, 'sweet potato': 1.6, 'cauliflower': 1.9, 'corn': 3.3,
}

# Dry weights of grains/legumes hold roughly 2.5-3x the protein of the cooked
# weight in the table above. If the line reads as dry (no "cooked"/"canned"),
# scale it up.
DRY_KEYS = ('lentil', 'bean', 'chickpea', 'garbanzo', 'split pea', 'quinoa',
            'rice', 'barley', 'bulgur', 'millet', 'farro', 'couscous')
DRY_FACTOR = 2.8
COOKED_HINTS = ('cooked', 'canned', 'can ', 'tin', 'drained', 'rinsed',
                'leftover', 'pre-cooked', 'precooked')
# Processed forms that are already protein-dense as sold — never scale these
DRY_EXCLUDE = ('pasta', 'noodle', 'flour', 'milk', 'yogurt', 'yoghurt',
               'bread', 'cake', 'chip', 'crisp', 'puff', 'snack', 'syrup')

# rough grams per cup, by ingredient family
CUP_GRAMS = {
    'spinach': 30, 'kale': 30, 'lettuce': 30, 'rocket': 25, 'arugula': 25,
    'herb': 25, 'basil': 25, 'coriander': 25, 'cilantro': 25, 'parsley': 25,
    'mushroom': 70, 'broccoli': 90, 'cauliflower': 100, 'pepper': 150,
    'flour': 125, 'oat': 90, 'rice': 185, 'quinoa': 170, 'lentil': 190,
    'bean': 175, 'chickpea': 165, 'pasta': 100, 'seed': 140, 'nut': 130,
    'butter': 250, 'milk': 240, 'yogurt': 245, 'tofu': 250, 'tempeh': 165,
    'gluten': 136, 'yeast': 60, 'default': 150,
}

UNIT_GRAMS = {
    'g': 1, 'gram': 1, 'grams': 1, 'gr': 1,
    'kg': 1000, 'kilogram': 1000,
    'oz': 28.35, 'ounce': 28.35, 'ounces': 28.35,
    'lb': 453.6, 'pound': 453.6, 'pounds': 453.6,
    'ml': 1, 'millilitre': 1, 'milliliter': 1, 'l': 1000, 'litre': 1000,
    'tbsp': 15, 'tablespoon': 15, 'tablespoons': 15, 'tbs': 15,
    'tsp': 5, 'teaspoon': 5, 'teaspoons': 5,
    'cup': None, 'cups': None,  # resolved per-ingredient
    'can': 400, 'cans': 400, 'tin': 400, 'tins': 400,
    'block': 350, 'blocks': 350, 'package': 350, 'packet': 350, 'pkg': 350,
}

FRACTIONS = {'½': 0.5, '⅓': 1/3, '⅔': 2/3, '¼': 0.25, '¾': 0.75,
             '⅛': 0.125, '⅜': 0.375, '⅝': 0.625, '⅞': 0.875, '⅕': 0.2}

QTY_RE = re.compile(
    r'^\s*(?P<qty>\d+\s+\d+/\d+|\d+/\d+|\d*\.?\d+|[½⅓⅔¼¾⅛⅜⅝⅞⅕])?\s*'
    r'(?P<unit>[a-zA-Z]+\.?)?\s*(?P<rest>.*)$'
)


def _parse_qty(text: str) -> float:
    text = text.strip()
    if not text:
        return 1.0
    if text in FRACTIONS:
        return FRACTIONS[text]
    if ' ' in text and '/' in text:  # "1 1/2"
        whole, frac = text.split(' ', 1)
        return float(whole) + _parse_qty(frac)
    if '/' in text:
        a, b = text.split('/', 1)
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return 1.0
    try:
        return float(text)
    except ValueError:
        return 1.0


def _cup_grams(name: str) -> float:
    for key, grams in CUP_GRAMS.items():
        if key in name:
            return grams
    return CUP_GRAMS['default']


def _protein_per_100g(name: str) -> Optional[float]:
    # longest match wins, so "extra firm tofu" beats "tofu"
    best, best_len = None, 0
    for key, val in PROTEIN_PER_100G.items():
        if key in name and len(key) > best_len:
            best, best_len = val, len(key)
    return best


def estimate_protein(ingredients: List[str], servings: float) -> Optional[float]:
    """Rough total protein per serving, in grams. None if nothing recognised."""
    total = 0.0
    matched = 0
    for raw in ingredients:
        line = re.sub(r'\(.*?\)', ' ', str(raw)).lower().strip()
        line = line.replace('-', ' ')
        m = QTY_RE.match(line)
        if not m:
            continue
        qty = _parse_qty(m.group('qty') or '')
        unit = (m.group('unit') or '').lower().rstrip('.')
        rest = m.group('rest') or ''

        p100 = _protein_per_100g(rest) or _protein_per_100g(line)
        if p100 is None:
            continue

        if unit in UNIT_GRAMS:
            grams = UNIT_GRAMS[unit]
            if grams is None:  # cups
                grams = _cup_grams(rest)
            grams = qty * grams
        elif unit in ('', 'large', 'small', 'medium'):
            grams = qty * 100  # bare count: assume ~100g each
        else:
            rest = f"{unit} {rest}"
            p100 = _protein_per_100g(rest) or p100
            grams = qty * 100

        # Dry legumes/grains carry far more protein per gram than cooked ones
        if (any(k in line for k in DRY_KEYS)
                and not any(h in line for h in COOKED_HINTS)
                and not any(x in line for x in DRY_EXCLUDE)
                and unit in ('cup', 'cups', 'g', 'gram', 'grams', 'oz', 'ounce',
                             'ounces', 'lb', 'pound', 'pounds', 'kg')):
            p100 = p100 * DRY_FACTOR

        grams = min(grams, 2000)  # sanity clamp
        total += grams * p100 / 100.0
        matched += 1

    if matched == 0 or servings <= 0:
        return None
    return round(total / servings, 1)


def _num(text) -> Optional[float]:
    if text is None:
        return None
    m = re.search(r'\d*\.?\d+', str(text))
    return float(m.group()) if m else None


# ---------------------------------------------------------------------------
# 3. JSON-LD EXTRACTION
# ---------------------------------------------------------------------------

def extract_recipe_jsonld(soup) -> Optional[dict]:
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get('@type')
                types = t if isinstance(t, list) else [t]
                if 'Recipe' in types:
                    return node
                for key in ('@graph', 'mainEntity', 'itemListElement'):
                    if key in node:
                        stack.append(node[key])
    return None


def _servings(node: dict) -> float:
    y = node.get('recipeYield')
    if isinstance(y, list):
        y = y[0] if y else None
    n = _num(y)
    return n if n and n > 0 else 4.0


@dataclass
class Verdict:
    vegan: bool = True
    reason: Optional[str] = None
    protein: Optional[float] = None
    estimated: bool = False
    tags: List[str] = field(default_factory=list)


def analyse(url: str, soup) -> Verdict:
    node = extract_recipe_jsonld(soup)
    if not node:
        # No structured data: can't verify. Let it through untagged, but flagged.
        return Verdict(vegan=True, tags=['protein-unknown'])

    raw_ings = node.get('recipeIngredient') or node.get('ingredients') or []
    if isinstance(raw_ings, str):
        raw_ings = [raw_ings]
    ingredients = [i for i in raw_ings if isinstance(i, str)]

    is_vegan, offender = check_vegan(ingredients)
    if not is_vegan:
        return Verdict(vegan=False, reason=f"Non-vegan ingredient: {offender}")

    servings = _servings(node)
    protein, estimated = None, False

    nutrition = node.get('nutrition') or {}
    if isinstance(nutrition, dict):
        protein = _num(nutrition.get('proteinContent'))

    if protein is None:
        protein = estimate_protein(ingredients, servings)
        estimated = protein is not None

    tags = ['vegan']
    if protein is None:
        tags.append('protein-unknown')
    else:
        if protein >= PROTEIN_HIGH:
            tags.append('protein-high')
        elif protein >= PROTEIN_MED:
            tags.append('protein-med')
        else:
            tags.append('protein-low')
        if estimated:
            tags.append('protein-estimated')

    return Verdict(vegan=True, protein=protein, estimated=estimated, tags=tags)


# ---------------------------------------------------------------------------
# 4. MEALIE TAGGING
# ---------------------------------------------------------------------------

_tag_cache = {}


def _headers():
    return {"Authorization": f"Bearer {MEALIE_API_TOKEN}"}


def _load_tags(session):
    if _tag_cache:
        return
    try:
        r = session.get(f"{MEALIE_URL}/api/organizers/tags",
                        headers=_headers(), params={"perPage": 200}, timeout=20)
        if r.status_code == 200:
            for item in r.json().get('items', []):
                _tag_cache[item['name'].lower()] = item
    except Exception as e:
        logger.debug(f"Tag list failed: {e}")


def _ensure_tag(session, name: str) -> Optional[dict]:
    _load_tags(session)
    if name.lower() in _tag_cache:
        return _tag_cache[name.lower()]
    try:
        r = session.post(f"{MEALIE_URL}/api/organizers/tags",
                         headers=_headers(), json={"name": name}, timeout=20)
        if r.status_code in (200, 201):
            tag = r.json()
            _tag_cache[name.lower()] = tag
            return tag
        if r.status_code == 409:  # already exists, refresh cache
            _tag_cache.clear()
            _load_tags(session)
            return _tag_cache.get(name.lower())
    except Exception as e:
        logger.debug(f"Tag create failed for {name}: {e}")
    return None


def tag_recipe(session, slug: str, tags: List[str]) -> bool:
    """Attach tags to an imported Mealie recipe. Returns True on success."""
    if not (TAG_RECIPES and slug and tags):
        return False
    tag_objs = [t for t in (_ensure_tag(session, n) for n in tags) if t]
    if not tag_objs:
        return False
    try:
        r = session.patch(f"{MEALIE_URL}/api/recipes/{slug}",
                          headers=_headers(), json={"tags": tag_objs}, timeout=20)
        if r.status_code in (200, 201):
            logger.info(f"   🏷️  Tagged {slug}: {', '.join(tags)}")
            return True
        logger.warning(f"   Tagging failed for {slug}: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"   Tagging error for {slug}: {e}")
    return False
