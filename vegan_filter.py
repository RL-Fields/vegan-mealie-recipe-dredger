"""
vegan_filter.py — vegan gate + full macro estimation for Recipe Dredger.

Drop this file next to dredger.py. It does three things:

1. analyse(url, soup) -> Verdict
   Pulls the Schema.org JSON-LD recipe block off the page, rejects recipes
   containing animal products, and works out per-serving macros — calories,
   protein, fat, carbs, fibre — from published nutrition where the site has
   it, otherwise estimated from the ingredient list.

2. Verdict.tags — Mealie tags: vegan, plus band tags per macro
   (protein-high, carb-low, fibre-high …) so you can filter in the sidebar,
   since Mealie can't filter on numeric nutrition.

3. Verdict.cuisine — region of origin (Indian, Mexican, Italian …), taken
   from the recipe's own recipeCuisine field, else inferred from marker
   ingredients and title words, else from the source blog.

4. apply_to_mealie(session, slug, verdict) — writes the macros into Mealie's
   built-in nutrition fields, attaches the tags, and sets the cuisine as a
   Mealie category, in one PATCH.

Env vars:
  VEGAN_ONLY=true                     reject non-vegan recipes
  TAG_RECIPES=true                    apply band tags
  WRITE_NUTRITION=true                fill Mealie's nutrition panel
  BAND_TAGS=protein,carb,fibre        which macros get band tags
                                      (options: protein,carb,fat,calorie,fibre)
  MIN_COVERAGE=0.7                    fraction of ingredient lines that must be
                                      recognised before an estimate is trusted
  PROTEIN_HIGH=20  PROTEIN_MED=10     g per serving
  CARB_HIGH=50     CARB_MED=20        g per serving
  FAT_HIGH=25      FAT_MED=10         g per serving
  FIBRE_HIGH=8     FIBRE_MED=4        g per serving
  CAL_HIGH=700     CAL_MED=400        kcal per serving
  SET_CUISINE=true                    set region of origin as a Mealie category
  CUISINE_MIN_SCORE=2                 marker score needed to infer a cuisine
"""

import json
import os
import re
import logging
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("dredger.vegan")


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


MEALIE_URL = os.getenv('MEALIE_URL', 'http://localhost:9000').rstrip('/')
MEALIE_API_TOKEN = os.getenv('MEALIE_API_TOKEN', '')
VEGAN_ONLY = os.getenv('VEGAN_ONLY', 'true').lower() == 'true'
TAG_RECIPES = os.getenv('TAG_RECIPES', 'true').lower() == 'true'
WRITE_NUTRITION = os.getenv('WRITE_NUTRITION', 'true').lower() == 'true'
BAND_TAGS = [b.strip().lower() for b in
             os.getenv('BAND_TAGS', 'protein,carb,fibre').split(',') if b.strip()]
MIN_COVERAGE = _f('MIN_COVERAGE', 0.7)
SET_CUISINE = os.getenv('SET_CUISINE', 'true').lower() == 'true'
CUISINE_MIN_SCORE = _f('CUISINE_MIN_SCORE', 2)

# Band thresholds, per serving. (macro key, tag prefix, med, high)
BANDS = [
    ('protein', 'protein', _f('PROTEIN_MED', 10), _f('PROTEIN_HIGH', 20)),
    ('carb',    'carb',    _f('CARB_MED', 20),    _f('CARB_HIGH', 50)),
    ('fat',     'fat',     _f('FAT_MED', 10),     _f('FAT_HIGH', 25)),
    ('fibre',   'fibre',   _f('FIBRE_MED', 4),    _f('FIBRE_HIGH', 8)),
    ('kcal',    'calorie', _f('CAL_MED', 400),    _f('CAL_HIGH', 700)),
]

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
    r'\bwhey\b', r'\bcasein\b',
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
        for safe in SAFE_RE:
            line = safe.sub(' ', line)
        for rx, pattern in ANIMAL_RE:
            if rx.search(line):
                return False, f"{raw.strip()[:60]} ({pattern})"
    return True, None


# ---------------------------------------------------------------------------
# 2. MACRO TABLE — per 100 g: (kcal, protein, fat, carbs, fibre)
#    Legumes and grains are COOKED values; dry measures are scaled up below.
# ---------------------------------------------------------------------------

MACROS = {
    # --- concentrated protein ---
    'seitan': (370, 75, 2, 14, 1),
    'vital wheat gluten': (370, 75, 2, 14, 0.6),
    'protein powder': (380, 75, 5, 8, 3),
    'soy protein': (335, 80, 1, 7, 5),
    'tvp': (327, 52, 1.2, 34, 18),
    'textured vegetable protein': (327, 52, 1.2, 34, 18),
    'textured soy protein': (327, 52, 1.2, 34, 18),
    'soy curl': (340, 47, 10, 26, 12),
    'tofu': (144, 15, 9, 3, 2),
    'firm tofu': (144, 15, 9, 3, 2),
    'extra firm tofu': (165, 17, 10, 4, 2),
    'smoked tofu': (170, 18, 10, 3, 2),
    'silken tofu': (55, 6, 3, 2, 0.2),
    'tempeh': (192, 19, 11, 8, 5),
    'edamame': (121, 11, 5, 9, 5),
    'natto': (212, 18, 11, 13, 5),
    # --- meat alternatives ---
    'vegan sausage': (230, 18, 14, 8, 3),
    'vegan mince': (180, 17, 8, 7, 3),
    'vegan burger': (220, 17, 11, 15, 4),
    'veggie burger': (220, 15, 11, 15, 4),
    'vegan chicken': (200, 20, 8, 12, 3),
    'vegan bacon': (300, 12, 22, 12, 2),
    'jackfruit': (95, 1.7, 0.6, 23, 1.5),
    # --- legumes (cooked) ---
    'lentil': (116, 9, 0.4, 20, 8),
    'red lentil': (116, 9, 0.4, 20, 8),
    'green lentil': (116, 9, 0.4, 20, 8),
    'brown lentil': (116, 9, 0.4, 20, 8),
    'puy lentil': (116, 9, 0.4, 20, 8),
    'chickpea': (164, 9, 2.6, 27, 8),
    'garbanzo': (164, 9, 2.6, 27, 8),
    'black bean': (132, 9, 0.5, 24, 9),
    'kidney bean': (127, 9, 0.5, 23, 7),
    'pinto bean': (143, 9, 0.7, 26, 9),
    'cannellini': (139, 9, 0.5, 25, 6),
    'white bean': (139, 9, 0.5, 25, 6),
    'borlotti': (139, 9, 0.5, 25, 6),
    'butter bean': (115, 7, 0.4, 20, 7),
    'lima bean': (115, 7, 0.4, 20, 7),
    'black eyed pea': (116, 8, 0.5, 21, 6),
    'split pea': (118, 8, 0.4, 21, 8),
    'green pea': (81, 5, 0.4, 14, 5),
    'baked bean': (94, 5, 0.4, 17, 4),
    'bean': (130, 8, 0.6, 23, 7),
    'refried bean': (110, 6, 2, 16, 5),
    'hummus': (166, 8, 10, 14, 6),
    # --- nuts & seeds ---
    'almond': (579, 21, 50, 22, 12.5),
    'cashew': (553, 18, 44, 30, 3.3),
    'walnut': (654, 15, 65, 14, 6.7),
    'peanut': (567, 26, 49, 16, 8.5),
    'pecan': (691, 9, 72, 14, 9.6),
    'pistachio': (560, 20, 45, 28, 10),
    'hazelnut': (628, 15, 61, 17, 10),
    'macadamia': (718, 8, 76, 14, 9),
    'brazil nut': (659, 14, 67, 12, 7.5),
    'pine nut': (673, 14, 68, 13, 3.7),
    'nut': (600, 17, 55, 20, 8),
    'chia': (486, 17, 31, 42, 34),
    'flax': (534, 18, 42, 29, 27),
    'linseed': (534, 18, 42, 29, 27),
    'hemp seed': (553, 32, 49, 9, 4),
    'hemp heart': (553, 32, 49, 9, 4),
    'pumpkin seed': (559, 30, 49, 11, 6),
    'sunflower seed': (584, 21, 51, 20, 9),
    'sesame': (573, 18, 50, 23, 12),
    'poppy seed': (525, 18, 42, 28, 20),
    'seed': (550, 22, 45, 22, 12),
    'peanut butter': (588, 25, 50, 20, 6),
    'almond butter': (614, 21, 56, 19, 10),
    'cashew butter': (587, 18, 49, 28, 2),
    'tahini': (595, 17, 54, 21, 9),
    'coconut butter': (650, 7, 65, 24, 16),
    'desiccated coconut': (660, 7, 65, 24, 16),
    'coconut flake': (660, 7, 65, 24, 16),
    'shredded coconut': (660, 7, 65, 24, 16),
    # --- grains, flours, pasta, bread ---
    'rice': (130, 2.7, 0.3, 28, 0.4),
    'brown rice': (123, 2.7, 1, 26, 1.6),
    'quinoa': (120, 4.4, 1.9, 21, 2.8),
    'couscous': (112, 3.8, 0.2, 23, 1.4),
    'bulgur': (83, 3, 0.2, 19, 4.5),
    'barley': (123, 2.3, 0.4, 28, 3.8),
    'millet': (119, 3.5, 1, 23, 1.3),
    'farro': (170, 6, 1, 34, 5),
    'buckwheat': (92, 3.4, 0.6, 20, 2.7),
    'oat': (389, 13, 7, 66, 10),
    'rolled oat': (389, 13, 7, 66, 10),
    'porridge oat': (389, 13, 7, 66, 10),
    'oat flour': (389, 13, 7, 66, 10),
    'pasta': (371, 13, 1.5, 75, 3),
    'spaghetti': (371, 13, 1.5, 75, 3),
    'whole wheat pasta': (348, 14, 2.5, 72, 9),
    'wholewheat pasta': (348, 14, 2.5, 72, 9),
    'lentil pasta': (350, 25, 2, 55, 10),
    'chickpea pasta': (350, 20, 5, 55, 12),
    'edamame pasta': (360, 40, 6, 32, 20),
    'noodle': (350, 12, 2, 71, 3),
    'rice noodle': (364, 6, 0.6, 82, 2),
    'soba': (336, 14, 0.7, 71, 5),
    'flour': (364, 10, 1, 76, 2.7),
    'plain flour': (364, 10, 1, 76, 2.7),
    'wholemeal flour': (340, 13, 2.5, 72, 11),
    'whole wheat flour': (340, 13, 2.5, 72, 11),
    'chickpea flour': (387, 22, 7, 58, 11),
    'gram flour': (387, 22, 7, 58, 11),
    'besan': (387, 22, 7, 58, 11),
    'almond flour': (571, 21, 50, 21, 11),
    'coconut flour': (400, 18, 13, 60, 39),
    'cornflour': (381, 0.3, 0.1, 91, 1),
    'cornstarch': (381, 0.3, 0.1, 91, 1),
    'polenta': (370, 7, 1.7, 79, 7),
    'cornmeal': (370, 7, 1.7, 79, 7),
    'bread': (265, 9, 3, 49, 3),
    'breadcrumb': (395, 13, 5, 72, 4),
    'tortilla': (310, 8, 7, 52, 3),
    'pita': (275, 9, 1.2, 56, 2),
    'bun': (280, 9, 4, 51, 2),
    'puff pastry': (551, 7, 38, 45, 2),
    'pastry': (450, 6, 25, 50, 2),
    # --- plant milks & dairy alternatives ---
    'soy milk': (43, 3.3, 1.8, 3, 0.5),
    'soya milk': (43, 3.3, 1.8, 3, 0.5),
    'pea milk': (43, 3.3, 2, 2, 0.5),
    'oat milk': (45, 1, 1.5, 7, 0.8),
    'almond milk': (15, 0.5, 1.1, 0.6, 0.3),
    'cashew milk': (25, 0.5, 2, 1.5, 0.2),
    'rice milk': (47, 0.3, 1, 9, 0),
    'hemp milk': (46, 2, 3, 3, 0.5),
    'coconut milk': (197, 2, 21, 3, 2),
    'coconut cream': (330, 3, 35, 6, 2),
    'vegan cream': (200, 1, 20, 4, 0),
    'vegan yogurt': (60, 2, 3, 7, 0.5),
    'soy yogurt': (55, 4, 2, 4, 0.5),
    'coconut yogurt': (130, 1, 11, 7, 1),
    'vegan cheese': (285, 2, 23, 18, 1),
    'vegan cream cheese': (250, 2, 24, 6, 1),
    'vegan butter': (717, 0.5, 80, 0.5, 0),
    'margarine': (717, 0.5, 80, 0.5, 0),
    'vegan mayo': (680, 0.5, 75, 2, 0),
    'nutritional yeast': (385, 50, 5, 36, 20),
    # --- fats & oils ---
    'olive oil': (884, 0, 100, 0, 0),
    'coconut oil': (862, 0, 100, 0, 0),
    'vegetable oil': (884, 0, 100, 0, 0),
    'sunflower oil': (884, 0, 100, 0, 0),
    'rapeseed oil': (884, 0, 100, 0, 0),
    'canola oil': (884, 0, 100, 0, 0),
    'sesame oil': (884, 0, 100, 0, 0),
    'avocado oil': (884, 0, 100, 0, 0),
    'oil': (884, 0, 100, 0, 0),
    'cooking spray': (884, 0, 100, 0, 0),
    # --- sweeteners ---
    'sugar': (387, 0, 0, 100, 0),
    'caster sugar': (387, 0, 0, 100, 0),
    'brown sugar': (380, 0, 0, 98, 0),
    'icing sugar': (389, 0, 0, 100, 0),
    'powdered sugar': (389, 0, 0, 100, 0),
    'coconut sugar': (375, 0, 0, 100, 0),
    'maple syrup': (260, 0, 0, 67, 0),
    'agave': (310, 0, 0, 76, 0),
    'golden syrup': (300, 0, 0, 79, 0),
    'molasses': (290, 0, 0, 75, 0),
    'date syrup': (290, 1, 0, 72, 1),
    'date': (282, 2.5, 0.4, 75, 8),
    'jam': (278, 0.4, 0, 69, 1),
    # --- chocolate, cocoa, baking ---
    'cocoa': (228, 20, 14, 58, 33),
    'cacao': (228, 20, 14, 58, 33),
    'dark chocolate': (546, 5, 31, 61, 7),
    'chocolate chip': (480, 4, 25, 62, 5),
    'chocolate': (500, 5, 28, 60, 6),
    'yeast': (325, 40, 8, 41, 27),
    # --- vegetables ---
    'onion': (40, 1.1, 0.1, 9, 1.7),
    'shallot': (72, 2.5, 0.1, 17, 3),
    'spring onion': (32, 1.8, 0.2, 7, 2.6),
    'garlic': (149, 6, 0.5, 33, 2),
    'ginger': (80, 1.8, 0.8, 18, 2),
    'carrot': (41, 0.9, 0.2, 10, 2.8),
    'celery': (16, 0.7, 0.2, 3, 1.6),
    'tomato': (18, 0.9, 0.2, 3.9, 1.2),
    'tinned tomato': (32, 1.6, 0.3, 7, 1.9),
    'canned tomato': (32, 1.6, 0.3, 7, 1.9),
    'tomato paste': (82, 4, 0.5, 19, 4),
    'tomato puree': (82, 4, 0.5, 19, 4),
    'passata': (35, 1.6, 0.3, 7, 1.5),
    'potato': (77, 2, 0.1, 17, 2.2),
    'sweet potato': (86, 1.6, 0.1, 20, 3),
    'pepper': (31, 1, 0.3, 6, 2.1),
    'bell pepper': (31, 1, 0.3, 6, 2.1),
    'chilli': (40, 1.9, 0.4, 9, 1.5),
    'courgette': (17, 1.2, 0.3, 3.1, 1),
    'zucchini': (17, 1.2, 0.3, 3.1, 1),
    'aubergine': (25, 1, 0.2, 6, 3),
    'mushroom': (22, 3.1, 0.3, 3.3, 1),
    'spinach': (23, 2.9, 0.4, 3.6, 2.2),
    'kale': (49, 4.3, 0.9, 9, 4),
    'chard': (19, 1.8, 0.2, 3.7, 1.6),
    'broccoli': (34, 2.8, 0.4, 7, 2.6),
    'cauliflower': (25, 1.9, 0.3, 5, 2),
    'cabbage': (25, 1.3, 0.1, 6, 2.5),
    'brussels sprout': (43, 3.4, 0.3, 9, 3.8),
    'lettuce': (15, 1.4, 0.2, 2.9, 1.3),
    'rocket': (25, 2.6, 0.7, 3.7, 1.6),
    'arugula': (25, 2.6, 0.7, 3.7, 1.6),
    'cucumber': (15, 0.7, 0.1, 3.6, 0.5),
    'leek': (61, 1.5, 0.3, 14, 1.8),
    'asparagus': (20, 2.2, 0.1, 3.9, 2.1),
    'green bean': (31, 1.8, 0.2, 7, 2.7),
    'corn': (86, 3.3, 1.2, 19, 2),
    'sweetcorn': (86, 3.3, 1.2, 19, 2),
    'butternut': (45, 1, 0.1, 12, 2),
    'squash': (45, 1, 0.1, 12, 2),
    'pumpkin': (26, 1, 0.1, 7, 0.5),
    'beetroot': (43, 1.6, 0.2, 10, 2.8),
    'parsnip': (75, 1.2, 0.3, 18, 4.9),
    'turnip': (28, 0.9, 0.1, 6, 1.8),
    'swede': (37, 1.1, 0.2, 9, 2.3),
    'olive': (145, 1, 15, 4, 3.2),
    'artichoke': (47, 3.3, 0.2, 11, 5.4),
    'sauerkraut': (19, 0.9, 0.1, 4, 2.9),
    'kimchi': (23, 1.7, 0.5, 4, 1.6),
    'seaweed': (45, 6, 0.6, 9, 1),
    'nori': (35, 6, 0.3, 5, 0.3),
    # --- fruit ---
    'banana': (89, 1.1, 0.3, 23, 2.6),
    'apple': (52, 0.3, 0.2, 14, 2.4),
    'orange': (47, 0.9, 0.1, 12, 2.4),
    'lemon': (29, 1.1, 0.3, 9, 2.8),
    'lime': (30, 0.7, 0.2, 11, 2.8),
    'berry': (57, 0.7, 0.3, 14, 2.4),
    'strawberry': (32, 0.7, 0.3, 8, 2),
    'blueberry': (57, 0.7, 0.3, 14, 2.4),
    'raspberry': (52, 1.2, 0.7, 12, 6.5),
    'mango': (60, 0.8, 0.4, 15, 1.6),
    'pineapple': (50, 0.5, 0.1, 13, 1.4),
    'peach': (39, 0.9, 0.3, 10, 1.5),
    'pear': (57, 0.4, 0.1, 15, 3.1),
    'avocado': (160, 2, 15, 9, 7),
    'raisin': (299, 3, 0.5, 79, 3.7),
    'sultana': (299, 3, 0.5, 79, 3.7),
    'cranberry': (308, 0.1, 1.4, 82, 5.7),
    'apricot': (241, 3.4, 0.5, 63, 7),
    'apple sauce': (68, 0.2, 0.2, 18, 1.2),
    'coconut water': (19, 0.7, 0.2, 3.7, 1.1),
    # --- condiments, stocks, misc ---
    'soy sauce': (53, 8, 0, 5, 0.8),
    'tamari': (60, 10, 0.1, 5, 0.8),
    'coconut aminos': (110, 1, 0, 25, 0),
    'miso': (199, 12, 6, 26, 5),
    'vegetable stock': (5, 0.3, 0.1, 0.8, 0),
    'vegetable broth': (5, 0.3, 0.1, 0.8, 0),
    'stock': (5, 0.3, 0.1, 0.8, 0),
    'broth': (5, 0.3, 0.1, 0.8, 0),
    'ketchup': (101, 1.2, 0.1, 26, 0.3),
    'mustard': (66, 4, 4, 5, 3),
    'sriracha': (93, 2, 1, 19, 2),
    'harissa': (110, 3, 6, 10, 4),
    'curry paste': (150, 3, 9, 14, 4),
    'pesto': (450, 5, 45, 6, 2),
    'salsa': (36, 1.5, 0.2, 7, 1.8),
    'peanut sauce': (300, 10, 22, 15, 3),
    'wine': (83, 0.1, 0, 2.6, 0),
    'beer': (43, 0.5, 0, 3.6, 0),
    'tofu press': (144, 15, 9, 3, 2),
    'crisps': (536, 7, 34, 53, 4),
    'popcorn': (387, 13, 4.5, 78, 15),
    'granola': (471, 10, 20, 64, 7),
}

# Ingredients whose contribution rounds to nothing — count as recognised
# so they don't drag the coverage score down.
NEGLIGIBLE = [
    'salt', 'pepper', 'water', 'ice', 'vinegar', 'lemon juice', 'lime juice',
    'baking powder', 'baking soda', 'bicarbonate', 'cream of tartar',
    'extract', 'essence', 'vanilla', 'food colouring', 'food coloring',
    'cumin', 'coriander', 'paprika', 'turmeric', 'cinnamon', 'nutmeg',
    'cardamom', 'clove', 'oregano', 'thyme', 'rosemary', 'basil', 'parsley',
    'cilantro', 'dill', 'sage', 'bay leaf', 'chilli flake', 'chili flake',
    'red pepper flake', 'garam masala', 'curry powder', 'spice', 'seasoning',
    'herb', 'mint', 'chive', 'zest', 'garnish', 'to taste', 'to serve',
    'xanthan', 'agar', 'liquid smoke', 'msg', 'stevia', 'sweetener',
    'asafoetida', 'fenugreek', 'star anise', 'peppercorn', 'mustard seed',
    'nutritional info', 'optional',
]

# Dry legumes/grains hold roughly 2.8x the macros of the cooked weight above.
DRY_KEYS = ('lentil', 'bean', 'chickpea', 'garbanzo', 'split pea', 'quinoa',
            'rice', 'barley', 'bulgur', 'millet', 'farro', 'couscous',
            'buckwheat')
DRY_FACTOR = 2.8
COOKED_HINTS = ('cooked', 'canned', 'can ', 'tin', 'drained', 'rinsed',
                'leftover', 'pre-cooked', 'precooked', 'jar')
DRY_EXCLUDE = ('pasta', 'noodle', 'flour', 'milk', 'yogurt', 'yoghurt',
               'bread', 'cake', 'chip', 'crisp', 'puff', 'snack', 'syrup',
               'sprout')

# rough grams per cup, by ingredient family
CUP_GRAMS = {
    'spinach': 30, 'kale': 30, 'lettuce': 30, 'rocket': 25, 'arugula': 25,
    'herb': 25, 'basil': 25, 'coriander': 25, 'cilantro': 25, 'parsley': 25,
    'mushroom': 70, 'broccoli': 90, 'cauliflower': 100, 'pepper': 150,
    'berry': 145, 'coconut': 80, 'breadcrumb': 110, 'cocoa': 85,
    'sugar': 200, 'oil': 218, 'syrup': 320, 'date': 150,
    'flour': 125, 'oat': 90, 'rice': 185, 'quinoa': 170, 'lentil': 190,
    'bean': 175, 'chickpea': 165, 'pasta': 100, 'seed': 140, 'nut': 130,
    'butter': 250, 'milk': 240, 'yogurt': 245, 'tofu': 250, 'tempeh': 165,
    'gluten': 136, 'yeast': 60, 'stock': 240, 'broth': 240, 'sauce': 240,
    'default': 150,
}

UNIT_GRAMS = {
    'g': 1, 'gram': 1, 'grams': 1, 'gr': 1, 'gs': 1,
    'kg': 1000, 'kilogram': 1000, 'kilograms': 1000,
    'oz': 28.35, 'ounce': 28.35, 'ounces': 28.35,
    'lb': 453.6, 'lbs': 453.6, 'pound': 453.6, 'pounds': 453.6,
    'ml': 1, 'millilitre': 1, 'milliliter': 1, 'l': 1000, 'litre': 1000,
    'liter': 1000, 'litres': 1000, 'liters': 1000,
    'tbsp': 15, 'tablespoon': 15, 'tablespoons': 15, 'tbs': 15, 'tb': 15,
    'tsp': 5, 'teaspoon': 5, 'teaspoons': 5,
    'cup': None, 'cups': None, 'c': None,
    'can': 400, 'cans': 400, 'tin': 400, 'tins': 400, 'jar': 350,
    'block': 350, 'blocks': 350, 'package': 350, 'packet': 350, 'pkg': 350,
    'bunch': 100, 'handful': 30, 'slice': 30, 'slices': 30,
    'clove': 5, 'cloves': 5, 'sprig': 2, 'stalk': 40, 'stick': 60,
    'pinch': 0.5, 'dash': 1,
}

FRACTIONS = {'½': 0.5, '⅓': 1/3, '⅔': 2/3, '¼': 0.25, '¾': 0.75,
             '⅛': 0.125, '⅜': 0.375, '⅝': 0.625, '⅞': 0.875, '⅕': 0.2}

QTY_RE = re.compile(
    r'^\s*(?P<qty>\d+\s+\d+/\d+|\d+/\d+|\d*\.?\d+|[½⅓⅔¼¾⅛⅜⅝⅞⅕])?\s*'
    r'(?P<unit>[a-zA-Z]+\.?)?\s*(?P<rest>.*)$'
)

MACRO_KEYS = ('kcal', 'protein', 'fat', 'carb', 'fibre')


def _parse_qty(text: str) -> float:
    text = text.strip()
    if not text:
        return 1.0
    if text in FRACTIONS:
        return FRACTIONS[text]
    if ' ' in text and '/' in text:
        whole, frac = text.split(' ', 1)
        try:
            return float(whole) + _parse_qty(frac)
        except ValueError:
            return _parse_qty(frac)
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


def _lookup(name: str) -> Optional[Tuple]:
    """Longest key match wins, so 'extra firm tofu' beats 'tofu'."""
    best, best_len = None, 0
    for key, vals in MACROS.items():
        if key in name and len(key) > best_len:
            best, best_len = vals, len(key)
    return best


def estimate_macros(ingredients: List[str], servings: float
                    ) -> Tuple[Optional[Dict[str, float]], float]:
    """Per-serving macros and the fraction of ingredient lines recognised."""
    totals = dict.fromkeys(MACRO_KEYS, 0.0)
    considered = 0
    recognised = 0

    for raw in ingredients:
        line = re.sub(r'\(.*?\)', ' ', str(raw)).lower().strip()
        line = line.replace('-', ' ')
        if not line:
            continue
        considered += 1

        if any(n in line for n in NEGLIGIBLE) and _lookup(line) is None:
            recognised += 1
            continue

        m = QTY_RE.match(line)
        if not m:
            continue
        qty = _parse_qty(m.group('qty') or '')
        unit = (m.group('unit') or '').lower().rstrip('.')
        rest = m.group('rest') or ''

        vals = _lookup(rest) or _lookup(line)
        if vals is None:
            continue
        recognised += 1

        if unit in UNIT_GRAMS:
            grams = UNIT_GRAMS[unit]
            if grams is None:
                grams = _cup_grams(rest)
            grams = qty * grams
        elif unit in ('', 'large', 'small', 'medium', 'whole', 'ripe'):
            grams = qty * 100
        else:
            grams = qty * 100

        if (any(k in line for k in DRY_KEYS)
                and not any(h in line for h in COOKED_HINTS)
                and not any(x in line for x in DRY_EXCLUDE)
                and unit in ('cup', 'cups', 'c', 'g', 'gram', 'grams', 'oz',
                             'ounce', 'ounces', 'lb', 'lbs', 'pound', 'pounds',
                             'kg')):
            scale = DRY_FACTOR
        else:
            scale = 1.0

        grams = min(grams, 3000)
        for key, val in zip(MACRO_KEYS, vals):
            totals[key] += grams * val * scale / 100.0

    if recognised == 0 or servings <= 0:
        return None, 0.0

    coverage = recognised / considered if considered else 0.0
    per_serving = {k: round(v / servings, 1) for k, v in totals.items()}
    per_serving['kcal'] = round(per_serving['kcal'])
    return per_serving, round(coverage, 2)



# ---------------------------------------------------------------------------
# 2b. CUISINE / REGION OF ORIGIN
# ---------------------------------------------------------------------------

# Canonical names, and how the messy strings sites publish map onto them.
CUISINE_ALIASES = {
    'indian': 'Indian', 'north indian': 'Indian', 'south indian': 'Indian',
    'punjabi': 'Indian', 'gujarati': 'Indian', 'bengali': 'Indian',
    'sri lankan': 'Sri Lankan', 'nepali': 'Nepali', 'pakistani': 'Pakistani',
    'chinese': 'Chinese', 'sichuan': 'Chinese', 'szechuan': 'Chinese',
    'cantonese': 'Chinese', 'taiwanese': 'Taiwanese',
    'japanese': 'Japanese', 'korean': 'Korean',
    'thai': 'Thai', 'vietnamese': 'Vietnamese', 'filipino': 'Filipino',
    'indonesian': 'Indonesian', 'malaysian': 'Malaysian', 'burmese': 'Burmese',
    'asian': 'Asian', 'east asian': 'Asian', 'southeast asian': 'Asian',
    'mexican': 'Mexican', 'tex mex': 'Mexican', 'tex-mex': 'Mexican',
    'latin': 'Latin American', 'latin american': 'Latin American',
    'peruvian': 'Latin American', 'brazilian': 'Brazilian',
    'caribbean': 'Caribbean', 'jamaican': 'Caribbean', 'cuban': 'Caribbean',
    'italian': 'Italian', 'sicilian': 'Italian', 'tuscan': 'Italian',
    'french': 'French', 'spanish': 'Spanish', 'portuguese': 'Portuguese',
    'greek': 'Greek', 'mediterranean': 'Mediterranean',
    'british': 'British', 'english': 'British', 'scottish': 'British',
    'irish': 'Irish', 'welsh': 'British', 'uk': 'British',
    'american': 'American', 'southern': 'American', 'cajun': 'American',
    'creole': 'American', 'californian': 'American', 'canadian': 'Canadian',
    'german': 'German', 'austrian': 'German', 'swiss': 'German',
    'polish': 'Eastern European', 'russian': 'Eastern European',
    'ukrainian': 'Eastern European', 'hungarian': 'Eastern European',
    'eastern european': 'Eastern European',
    'nordic': 'Nordic', 'scandinavian': 'Nordic', 'swedish': 'Nordic',
    'danish': 'Nordic', 'norwegian': 'Nordic', 'finnish': 'Nordic',
    'turkish': 'Turkish', 'lebanese': 'Middle Eastern',
    'israeli': 'Middle Eastern', 'persian': 'Middle Eastern',
    'iranian': 'Middle Eastern', 'syrian': 'Middle Eastern',
    'middle eastern': 'Middle Eastern', 'moroccan': 'North African',
    'tunisian': 'North African', 'algerian': 'North African',
    'egyptian': 'North African', 'north african': 'North African',
    'ethiopian': 'Ethiopian', 'eritrean': 'Ethiopian',
    'nigerian': 'West African', 'ghanaian': 'West African',
    'senegalese': 'West African', 'west african': 'West African',
    'south african': 'African', 'kenyan': 'African', 'african': 'African',
    'hawaiian': 'Hawaiian', 'australian': 'Australian',
}

# Marker ingredients and title words. Weight 2 = distinctive enough on its own
# when paired with anything else; weight 1 = suggestive only.
CUISINE_MARKERS = {
    'Indian': [(2, r'\b(garam masala|asafoetida|hing|amchur|curry leaf|curry leaves|'
                   r'ghee substitute|besan|paneer|chana masala|tikka|masala dosa|'
                   r'idli|sambar|rajma|dal makhani|tandoori|biryani|naan|chapati|'
                   r'roti|paratha|jeera|methi|kadhi|poha|upma)\b'),
               (1, r'\b(turmeric|cumin seed|coriander seed|cardamom|mustard seed|'
                   r'basmati|lentil dal|dal|curry|chutney|ginger garlic paste)\b')],
    'Chinese': [(2, r'\b(shaoxing|doubanjiang|sichuan peppercorn|szechuan|'
                    r'chinkiang|black vinegar|hoisin|five spice|wood ear|'
                    r'bok choy|gai lan|mapo|kung pao|lo mein|chow mein|'
                    r'wonton|dumpling wrapper|char siu|dan dan)\b'),
                (1, r'\b(soy sauce|sesame oil|rice wine|scallion|ginger|'
                    r'stir fry|stir-fry|noodle)\b')],
    'Japanese': [(2, r'\b(miso|mirin|sake|dashi kombu|kombu|nori|wasabi|'
                     r'panko|udon|soba|ramen|teriyaki|edamame|shiso|'
                     r'yuzu|katsu|onigiri|tempura|okonomiyaki|matcha)\b'),
                 (1, r'\b(rice vinegar|sushi|japanese)\b')],
    'Korean': [(2, r'\b(gochujang|gochugaru|kimchi|doenjang|bibimbap|'
                   r'tteokbokki|banchan|bulgogi|japchae|perilla)\b'), (1, r'\b(korean)\b')],
    'Thai': [(2, r'\b(lemongrass|galangal|kaffir lime|thai basil|'
                 r'red curry paste|green curry paste|massaman|pad thai|'
                 r'tom yum|tom kha|palm sugar)\b'), (1, r'\b(coconut milk curry|thai)\b')],
    'Vietnamese': [(2, r'\b(pho|banh mi|rice paper|vermicelli noodle|'
                       r'nuoc cham|vietnamese)\b'), (1, r'\b(lemongrass|mint|coriander)\b')],
    'Mexican': [(2, r'\b(tortilla|masa harina|chipotle|adobo|poblano|jalape|'
                    r'ancho|guajillo|tomatillo|salsa verde|enchilada|'
                    r'taco|burrito|quesadilla|tostada|elote|pico de gallo|'
                    r'refried|mole|nopales)\b'),
                (1, r'\b(black bean|lime|cilantro|avocado|cumin)\b')],
    'Caribbean': [(2, r'\b(jerk seasoning|scotch bonnet|allspice|callaloo|'
                      r'plantain|ackee|jamaican|caribbean)\b'), (1, r'\b(coconut|thyme)\b')],
    'Italian': [(2, r'\b(arborio|risotto|passata|pasta e|gnocchi|polenta|'
                    r'lasagne|lasagna|bolognese|puttanesca|cacio|pesto|'
                    r'bruschetta|focaccia|ciabatta|tiramisu|orecchiette|'
                    r'pappardelle|tagliatelle|rigatoni|penne|spaghetti|'
                    r'balsamic|marinara)\b'),
                (1, r'\b(basil|oregano|olive oil|tomato|garlic)\b')],
    'French': [(2, r'\b(ratatouille|baguette|dijon|herbes de provence|'
                   r'tarte tatin|cassoulet|gratin|beurre|croissant|'
                   r'bouillabaisse|provencal|proven|crepe|cr[eê]pe|'
                   r'shallot|tarragon|puy lentil)\b'), (1, r'\b(thyme|bay leaf|white wine)\b')],
    'Spanish': [(2, r'\b(paella|smoked paprika|piment[oó]n|sofrito|'
                    r'romesco|gazpacho|patatas bravas|manchego|saffron rice|'
                    r'spanish)\b'), (1, r'\b(saffron|olive|sherry vinegar)\b')],
    'Greek': [(2, r'\b(greek|tzatziki|spanakopita|dolma|orzo|'
                  r'kalamata|gyro|souvlaki|filo|phyllo)\b'), (1, r'\b(oregano|lemon|olive)\b')],
    'Middle Eastern': [(2, r'\b(za\'?atar|sumac|tahini sauce|labneh|baharat|'
                           r'pomegranate molasses|freekeh|bulgur|falafel|'
                           r'shawarma|baba ganoush|muhammara|fattoush|'
                           r'tabbouleh|halloumi|pita|hummus|dukkah|'
                           r'rose water|pistachio|persian|lebanese)\b'),
                       (1, r'\b(chickpea|parsley|mint|cinnamon)\b')],
    'Turkish': [(2, r'\b(turkish|pide|menemen|borek|b[oö]rek|'
                    r'pul biber|aleppo pepper)\b'), (1, r'\b(yogurt|bulgur)\b')],
    'North African': [(2, r'\b(harissa|ras el hanout|preserved lemon|couscous|'
                          r'tagine|moroccan|merguez|chermoula|'
                          r'north african)\b'), (1, r'\b(cinnamon|apricot|almond|date)\b')],
    'Ethiopian': [(2, r'\b(berbere|injera|teff|niter kibbeh|mitmita|'
                      r'ethiopian|shiro|wat\b)\b'), (1, r'\b(lentil|collard)\b')],
    'West African': [(2, r'\b(jollof|egusi|fufu|suya|scotch bonnet|'
                         r'nigerian|ghanaian|west african|plantain)\b'),
                     (1, r'\b(peanut|palm oil|okra)\b')],
    'British': [(2, r'\b(british|shepherd\'?s pie|cottage pie|toad in the hole|'
                    r'bubble and squeak|crumpet|scone|yorkshire pudding|'
                    r'bangers|mushy pea|marmite|piccalilli|treacle|'
                    r'sticky toffee|eccles|cornish|ploughman|'
                    r'full english|shortbread|flapjack|trifle)\b'),
                (1, r'\b(golden syrup|self raising|swede|parsnip|custard)\b')],
    'Irish': [(2, r'\b(irish|colcannon|champ|soda bread|boxty)\b'), (1, r'\b(potato|cabbage)\b')],
    'American': [(2, r'\b(cornbread|grits|biscuits and gravy|sloppy joe|'
                     r'mac and cheese|jambalaya|gumbo|po\'? ?boy|'
                     r'buffalo sauce|ranch dressing|s\'?mores|'
                     r'pumpkin pie|thanksgiving|bbq sauce|barbecue sauce|'
                     r'cajun|creole|pancake stack|brownie|'
                     r'chocolate chip cookie|meatloaf|coleslaw|'
                     r'sloppy|philly)\b'),
                 (1, r'\b(maple syrup|graham|all purpose flour|cup of)\b')],
    'German': [(2, r'\b(german|sauerkraut|spaetzle|sp[aä]tzle|pretzel|'
                   r'schnitzel|strudel|rye bread|quark)\b'), (1, r'\b(caraway|mustard|dill)\b')],
    'Eastern European': [(2, r'\b(borscht|borsch|pierogi|golabki|kasha|'
                             r'polish|ukrainian|russian|hungarian|'
                             r'paprikash|goulash|blini)\b'), (1, r'\b(beetroot|dill|cabbage)\b')],
    'Nordic': [(2, r'\b(nordic|scandinavian|swedish|danish|norwegian|'
                   r'cardamom bun|rye crisp|smorgas|lingonberry)\b'), (1, r'\b(rye|dill|caraway)\b')],
    'Mediterranean': [(2, r'\b(mediterranean)\b'), (1, r'\b(olive oil|lemon|oregano|chickpea)\b')],
}

CUISINE_RE = {name: [(w, re.compile(p, re.I)) for w, p in pats]
              for name, pats in CUISINE_MARKERS.items()}

# Blogs whose output is overwhelmingly one cuisine — a weak fallback only.
SITE_CUISINE = {
    'veganricha.com': 'Indian',
    'cookwithmanali.com': 'Indian',
    'pipingpotcurry.com': 'Indian',
    'holycowvegan.net': 'Indian',
    'woonheng.com': 'Chinese',
    'okonomikitchen.com': 'Japanese',
    'thefoodietakesflight.com': 'Asian',
    'theplantbasedschool.com': 'Italian',
    'schoolnightvegan.com': 'British',
    'romylondonuk.com': 'British',
    'thelittleblogofvegan.com': 'British',
    'avirtualvegan.com': 'British',
}


def normalise_cuisine(raw) -> Optional[str]:
    """Map a site's recipeCuisine string onto a canonical name."""
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    key = re.sub(r'[^a-z ]', ' ', raw.lower()).strip()
    key = re.sub(r'\s+', ' ', key)
    if key in CUISINE_ALIASES:
        return CUISINE_ALIASES[key]
    for alias, canonical in CUISINE_ALIASES.items():
        if re.search(rf'\b{re.escape(alias)}\b', key):
            return canonical
    return None


def infer_cuisine(title: str, ingredients: List[str]) -> Tuple[Optional[str], int]:
    """Score marker words across the title and ingredient list."""
    text = ' '.join([title or ''] + [str(i) for i in ingredients]).lower()
    scores = {}
    for name, pats in CUISINE_RE.items():
        score = 0
        for weight, rx in pats:
            hits = len(rx.findall(text))
            if hits:
                score += weight * min(hits, 3)
        if score:
            scores[name] = score
    if not scores:
        return None, 0
    best = max(scores, key=scores.get)
    return best, scores[best]


def cuisine_for(node: dict, url: str, ingredients: List[str]) -> Optional[str]:
    published = normalise_cuisine(node.get('recipeCuisine'))
    if published:
        return published

    title = node.get('name') or ''
    guess, score = infer_cuisine(title, ingredients)
    if guess and score >= CUISINE_MIN_SCORE:
        return guess

    host = urlparse(url).netloc.lower().replace('www.', '')
    return SITE_CUISINE.get(host)


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


def published_macros(node: dict) -> Optional[Dict[str, float]]:
    """Macros from the site's own nutrition block, if it has a useful one."""
    n = node.get('nutrition')
    if not isinstance(n, dict):
        return None
    out = {
        'kcal': _num(n.get('calories')),
        'protein': _num(n.get('proteinContent')),
        'fat': _num(n.get('fatContent')),
        'carb': _num(n.get('carbohydrateContent')),
        'fibre': _num(n.get('fiberContent')),
    }
    # Need at least calories and protein to call it published
    if out['kcal'] is None or out['protein'] is None:
        return None
    return {k: v for k, v in out.items() if v is not None}


def band_tags(macros: Dict[str, float]) -> List[str]:
    tags = []
    for key, prefix, med, high in BANDS:
        if prefix not in BAND_TAGS or key not in macros:
            continue
        val = macros[key]
        if val >= high:
            tags.append(f"{prefix}-high")
        elif val >= med:
            tags.append(f"{prefix}-med")
        else:
            tags.append(f"{prefix}-low")
    return tags


@dataclass
class Verdict:
    vegan: bool = True
    reason: Optional[str] = None
    macros: Optional[Dict[str, float]] = None
    estimated: bool = False
    coverage: float = 0.0
    tags: List[str] = field(default_factory=list)
    cuisine: Optional[str] = None

    def summary(self) -> str:
        if not self.macros:
            return "no macros"
        m = self.macros
        src = f"est {int(self.coverage * 100)}%" if self.estimated else "published"
        return (f"{m.get('kcal', '?')} kcal, P{m.get('protein', '?')} "
                f"C{m.get('carb', '?')} F{m.get('fat', '?')} "
                f"Fib{m.get('fibre', '?')} ({src})")


def analyse(url: str, soup) -> Verdict:
    node = extract_recipe_jsonld(soup)
    if not node:
        return Verdict(vegan=True, tags=['vegan', 'macros-unknown'])

    raw_ings = node.get('recipeIngredient') or node.get('ingredients') or []
    if isinstance(raw_ings, str):
        raw_ings = [raw_ings]
    ingredients = [i for i in raw_ings if isinstance(i, str)]

    is_vegan, offender = check_vegan(ingredients)
    if not is_vegan:
        return Verdict(vegan=False, reason=f"Non-vegan ingredient: {offender}")

    servings = _servings(node)
    macros = published_macros(node)
    estimated = False
    coverage = 1.0

    if macros is None:
        macros, coverage = estimate_macros(ingredients, servings)
        estimated = macros is not None
        if macros is not None and coverage < MIN_COVERAGE:
            logger.debug(f"   Low ingredient coverage ({coverage}) for {url}")
            macros = None

    tags = ['vegan']
    if macros is None:
        tags.append('macros-unknown')
    else:
        tags += band_tags(macros)
        if estimated:
            tags.append('macros-estimated')

    cuisine = cuisine_for(node, url, ingredients) if SET_CUISINE else None

    return Verdict(vegan=True, macros=macros, estimated=estimated,
                   coverage=coverage, tags=tags, cuisine=cuisine)


# ---------------------------------------------------------------------------
# 4. MEALIE WRITE-BACK
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
        if r.status_code == 409:
            _tag_cache.clear()
            _load_tags(session)
            return _tag_cache.get(name.lower())
    except Exception as e:
        logger.debug(f"Tag create failed for {name}: {e}")
    return None


_category_cache = {}


def _load_categories(session):
    if _category_cache:
        return
    try:
        r = session.get(f"{MEALIE_URL}/api/organizers/categories",
                        headers=_headers(), params={"perPage": 200}, timeout=20)
        if r.status_code == 200:
            for item in r.json().get('items', []):
                _category_cache[item['name'].lower()] = item
    except Exception as e:
        logger.debug(f"Category list failed: {e}")


def _ensure_category(session, name: str) -> Optional[dict]:
    _load_categories(session)
    if name.lower() in _category_cache:
        return _category_cache[name.lower()]
    try:
        r = session.post(f"{MEALIE_URL}/api/organizers/categories",
                         headers=_headers(), json={"name": name}, timeout=20)
        if r.status_code in (200, 201):
            cat = r.json()
            _category_cache[name.lower()] = cat
            return cat
        if r.status_code == 409:
            _category_cache.clear()
            _load_categories(session)
            return _category_cache.get(name.lower())
    except Exception as e:
        logger.debug(f"Category create failed for {name}: {e}")
    return None


def _nutrition_payload(macros: Dict[str, float]) -> Dict[str, str]:
    """Mealie stores nutrition values as strings."""
    mapping = {
        'kcal': 'calories',
        'protein': 'proteinContent',
        'fat': 'fatContent',
        'carb': 'carbohydrateContent',
        'fibre': 'fiberContent',
    }
    return {field: str(macros[key])
            for key, field in mapping.items() if key in macros}


def _settings_with_nutrition(session, slug: str) -> Optional[dict]:
    """Mealie hides the nutrition panel unless the recipe's showNutrition flag
    is set, so read the current settings and flip just that one."""
    try:
        r = session.get(f"{MEALIE_URL}/api/recipes/{slug}",
                        headers=_headers(), timeout=20)
        if r.status_code != 200:
            return {"showNutrition": True}
        settings = r.json().get('settings') or {}
    except Exception as e:
        logger.debug(f"Could not read settings for {slug}: {e}")
        return {"showNutrition": True}
    settings['showNutrition'] = True
    return settings


def apply_to_mealie(session, slug: str, verdict: 'Verdict') -> bool:
    """Attach tags and nutrition to an imported Mealie recipe."""
    if not slug:
        return False

    payload = {}

    if TAG_RECIPES and verdict.tags:
        tag_objs = [t for t in (_ensure_tag(session, n) for n in verdict.tags) if t]
        if tag_objs:
            payload['tags'] = tag_objs

    if WRITE_NUTRITION and verdict.macros:
        payload['nutrition'] = _nutrition_payload(verdict.macros)
        payload['settings'] = _settings_with_nutrition(session, slug)

    if SET_CUISINE and verdict.cuisine:
        cat = _ensure_category(session, verdict.cuisine)
        if cat:
            payload['recipeCategory'] = [cat]

    if not payload:
        return False

    try:
        r = session.patch(f"{MEALIE_URL}/api/recipes/{slug}",
                          headers=_headers(), json=payload, timeout=20)
        if r.status_code in (200, 201):
            label = ', '.join(verdict.tags)
            if verdict.cuisine:
                label = f"{verdict.cuisine} | {label}"
            logger.info(f"   🏷️  {slug}: {label} — {verdict.summary()}")
            return True
        logger.warning(f"   Write-back failed for {slug}: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"   Write-back error for {slug}: {e}")
    return False


# Backwards-compatible alias
def tag_recipe(session, slug: str, verdict: 'Verdict') -> bool:
    return apply_to_mealie(session, slug, verdict)
