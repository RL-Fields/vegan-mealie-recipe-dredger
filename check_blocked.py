"""One-off: which sites does Mealie's own scraper refuse?

For each site it finds a real recipe URL (verified the same way the dredger
verifies), asks Mealie to import it, then deletes the test recipe again.
A site is only called BLOCKED if Mealie refuses two URLs the dredger itself
confirmed are recipes.
"""

import sys
from urllib.parse import urlparse

from dredger import (get_session, SitemapCrawler, StorageManager,
                     RecipeVerifier, load_sites_from_source,
                     MEALIE_URL, MEALIE_API_TOKEN)

H = {"Authorization": f"Bearer {MEALIE_API_TOKEN}"}
FAILS_NEEDED = 2      # Mealie refusals before calling a site blocked
MAX_CANDIDATES = 25   # URLs to sift per site looking for real recipes

session = get_session()
storage = StorageManager()
crawler = SitemapCrawler(session, storage)
verifier = RecipeVerifier(session)

sites = load_sites_from_source(None)
print(f"Testing {len(sites)} sites — one recipe each, deleted afterwards\n")

results = []

for site in sites:
    host = urlparse(site).netloc.replace('www.', '')
    try:
        candidates = crawler.get_urls_for_site(site)
    except Exception as e:
        results.append((host, "ERROR", str(e)[:40]))
        print(f"  {'ERROR':10} {host}  {str(e)[:40]}")
        continue

    if not candidates:
        results.append((host, "NO SITEMAP", ""))
        print(f"  {'NO SITEMAP':10} {host}")
        continue

    verdict, note, fails, checked = None, "", 0, 0

    for cand in candidates[:MAX_CANDIDATES]:
        url = cand.url
        is_recipe, _, _ = verifier.verify_recipe(url)
        if not is_recipe:
            continue
        checked += 1

        try:
            r = session.post(f"{MEALIE_URL}/api/recipes/create/url",
                             headers=H, json={"url": url}, timeout=90)
        except Exception as e:
            verdict, note = "ERROR", str(e)[:40]
            break

        if r.status_code in (200, 201):
            try:
                body = r.json()
                slug = body if isinstance(body, str) else body.get("slug")
                if slug:
                    session.delete(f"{MEALIE_URL}/api/recipes/{slug}",
                                   headers=H, timeout=30)
            except Exception:
                pass
            verdict = "OK"
            break

        if r.status_code == 409:
            verdict, note = "OK", "already in library"
            break

        fails += 1
        note = f"HTTP {r.status_code}"
        if fails >= FAILS_NEEDED:
            verdict = "BLOCKED"
            break

    if verdict is None:
        verdict = "NO RECIPES" if checked == 0 else "BLOCKED"

    results.append((host, verdict, note))
    print(f"  {verdict:10} {host}  {note}")
    sys.stdout.flush()

print("\n" + "=" * 60)
for state in ("BLOCKED", "NO SITEMAP", "NO RECIPES", "ERROR", "OK"):
    hosts = [h for h, v, _ in results if v == state]
    if hosts:
        print(f"\n{state} ({len(hosts)}):")
        for h in hosts:
            print(f"  {h}")
