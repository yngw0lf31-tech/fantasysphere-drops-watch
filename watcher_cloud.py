#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Veille Fantasy Sphere & Oupi — version GitHub Actions (sans Mac).
Lancée toutes les ~5 min par .github/workflows/watch.yml ; l'état
(state.json) est commité dans le repo entre deux exécutions.

Mêmes règles que le bot local (watcher.py sur le Mac) :
  🚨 urgente — un produit devient ACHETABLE (rappel toutes les ~30 min
               tant que ça reste achetable) ;
  ℹ️ info    — page de vente apparue (encore épuisé) = drop imminent,
               produit reparti en rupture, site injoignable ≥ ~20 min.
"""
import html as html_mod
import json
import os
import re
import sys
import time
import unicodedata

from curl_cffi import requests

NTFY = "https://ntfy.sh"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
if not TOPIC:
    sys.exit("NTFY_TOPIC manquant : configure le secret GitHub")

PRODUCTS = [
    {"name": "Display OP-17 EN — Fantasy Sphere",
     "url": "https://en.fantasysphere.net/product/boite-de-24-boosters-op17-one-piece-cg-op-17-en-10042437"},
    {"name": "Display OP-17 EN — Oupi",
     "url": "https://oupi.eu/fr/display-one-piece/7369-display-op-17-boite-de-booster-anglais-one-piece-card-game.html"},
    {"name": "Case 12 displays OP-17 EN — Oupi",
     "url": "https://oupi.eu/fr/case-scelle-de-display/7370-case-scellee-de-12-display-op-17-anglais-one-piece-card-game.html"},
]

REMIND_SECONDS = 1800      # rappel « toujours dispo » (le bot local, lui, rappelle toutes les 5 min)
ERROR_ALERT_AFTER = 4      # runs consécutifs en erreur (~20 min) avant l'info « injoignable »

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

s = requests.Session(impersonate="chrome")

def ascii_safe(t):
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()

def push(title, msg, click, urgent):
    r = s.post(f"{NTFY}/{TOPIC}", data=msg.encode("utf-8"), headers={
        "Title": ascii_safe(title),
        "Priority": "urgent" if urgent else "default",
        "Tags": "rotating_light,shopping_cart" if urgent else "information_source",
        "Click": click}, timeout=20)
    print(f"ntfy {r.status_code} ← {title}")

# ── Analyse des pages (repris du bot local) ───────────────────────────────
RE_TITLE = re.compile(r'<h1 class="product-title">\s*(.*?)\s*</h1>', re.S)
RE_FORM = re.compile(r'<form[^>]*id="formBasketAdd".*?</form>', re.S)
RE_SUBMIT = re.compile(r'<button[^>]*id="submitBasketAdd"[^>]*>')
RE_MAX = re.compile(r'id="inputQuantity"[^>]*\bmax="(\d+)"')
RE_PRICE = re.compile(r'<span class="price">\s*([\d\s.,]+)\s*€')
NOT_SOLD_MARKERS = ("not currently sold", "pas vendu actuellement", "n&#039;est pas vendu")
RE_DATA_PRODUCT = re.compile(r'id="product-details"[^>]*data-product="([^"]*)"')

def analyze_fs(html):
    title_m = RE_TITLE.search(html)
    info = {"status": "UNKNOWN", "title": title_m.group(1).strip() if title_m else "?",
            "max_qty": 0, "price": ""}
    price_m = RE_PRICE.search(html)
    if price_m:
        info["price"] = price_m.group(1).strip() + " €"
    form_m = RE_FORM.search(html)
    if not form_m:
        if any(m in html for m in NOT_SOLD_MARKERS):
            info["status"] = "NOT_SOLD"
        return info
    form = form_m.group(0)
    max_m = RE_MAX.search(form)
    info["max_qty"] = int(max_m.group(1)) if max_m else 0
    submit_m = RE_SUBMIT.search(form)
    hidden = bool(submit_m) and "d-none" in submit_m.group(0)
    info["status"] = ("BUYABLE" if submit_m and not hidden and info["max_qty"] >= 1
                      else "OUT_OF_STOCK")
    return info

def analyze_oupi(html):
    info = {"status": "UNKNOWN", "title": "?", "max_qty": 0, "price": ""}
    dp_m = RE_DATA_PRODUCT.search(html)
    if dp_m:
        try:
            j = json.loads(html_mod.unescape(dp_m.group(1)))
        except ValueError:
            j = {}
        if j:
            info["title"] = (j.get("name") or "?").strip()
            price = j.get("price")
            if isinstance(price, str) and price:
                info["price"] = price
            elif j.get("price_amount"):
                info["price"] = f"{j['price_amount']} € HT"
            qty = int(j.get("quantity") or 0)
            info["max_qty"] = max(qty, 0)
            avail = (j.get("availability") or "").lower()
            info["status"] = ("OUT_OF_STOCK" if avail == "unavailable" or (not avail and qty <= 0)
                              else "BUYABLE")
            return info
    if "schema.org/InStock" in html or "schema.org/PreOrder" in html:
        info["status"] = "BUYABLE"
    elif "schema.org/OutOfStock" in html:
        info["status"] = "OUT_OF_STOCK"
    return info

def check(url):
    r = s.get(url, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    html = r.text
    return analyze_fs(html) if "fantasysphere" in url else analyze_oupi(html)

# ── état + boucle produits ────────────────────────────────────────────────
try:
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}

now = time.time()
for p in PRODUCTS:
    st = state.setdefault(p["url"], {})
    old = st.get("status")
    try:
        info = check(p["url"])
    except Exception as e:
        st["errors"] = st.get("errors", 0) + 1
        print(f"[{p['name']}] erreur ({st['errors']}) : {e}")
        if st["errors"] == ERROR_ALERT_AFTER and not st.get("error_alerted"):
            st["error_alerted"] = True
            push(f"Site injoignable (GitHub) — {p['name']}",
                 f"Impossible de vérifier « {p['name']} » depuis ~20 min "
                 "(depuis le cloud). Possible rush de drop → vérifie à la main.",
                 p["url"], False)
        continue
    st["errors"], st["error_alerted"] = 0, False

    new = info["status"]
    if old and new != old:
        print(f"[{p['name']}] {old} → {new}")

    if new == "BUYABLE":
        first = old != "BUYABLE"
        if first or now - st.get("last_alert", 0) >= REMIND_SECONDS:
            st["last_alert"] = now
            qty = f" — max {info['max_qty']}" if info["max_qty"] >= 1 else ""
            price = f" à {info['price']}" if info["price"] else ""
            push(("DISPO" if first else "Toujours dispo") + f" (GitHub) — {p['name']}",
                 f"« {info['title']} » est en vente{price}{qty}. Fonce !",
                 p["url"], True)
    elif old == "BUYABLE":
        push(f"Fini (GitHub) — {p['name']}",
             f"« {info['title']} » n'est plus achetable ({new}).", p["url"], False)
    elif old == "NOT_SOLD" and new == "OUT_OF_STOCK":
        push(f"Ca bouge (GitHub) — {p['name']}",
             "La page de vente vient d'apparaître (encore épuisé). "
             "Le drop approche, reste prêt !", p["url"], False)

    st["status"] = new
    print(f"[{p['name']}] {new} (max {info['max_qty']}, {info['price'] or 'prix ?'})")

with open(STATE_FILE, "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=1)

if "--hello" in sys.argv:
    lines = [f"{p['name']} : {state.get(p['url'], {}).get('status', 'ERREUR')}"
             for p in PRODUCTS]
    push("Veille cloud OK (GitHub)",
         "Test réussi — GitHub Actions surveille toutes les ~5 min, Mac allumé ou pas :\n"
         + "\n".join(lines), PRODUCTS[0]["url"], False)
