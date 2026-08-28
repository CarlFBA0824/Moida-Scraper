"""
Moida Shopify scraper — variations, prices, and GTIN, via ZenRows.

Forked from the shared Vitacost/Moida scraper in the Web-Scraping repo, kept
in its own repo so Moida-specific changes never risk affecting Vitacost's
scraper (and vice versa).

Targets any Moidaus (moidaus.com) Shopify page: a /collections/<handle> page
or a /search?q=... results page - pass a different --collection-url to
target a different collection/search on the same store.

Product discovery defaults to /collections/all (every product on the store)
filtered down by --vendor-filter (default "Medicube"), rather than
/search?q=medicube. Moida's /search endpoint consistently fails through
ZenRows (422 RESP001 "could not get content" after ~150-170s, 0 credits
charged - a real server-side failure, not a fluke) regardless of wait
strategy, while /collections/all/products.json is public, cheap (no JS
rendering needed), and each product entry includes a "vendor" field to
filter on - so walking the whole catalog's lightweight JSON and keeping
only vendor-matching products sidesteps the block entirely.

Plug-and-play, single-file version. Setup:
    pip install requests beautifulsoup4 lxml python-dotenv
    Create a .env file next to this script containing:
        ZENROWS_API_KEY=your_key_here

Run:
    python scrape.py --limit 3     (quick test on 3 products)
    python scrape.py               (full run, default: all Medicube products)
    python scrape.py --vendor-filter "Anua"
    python scrape.py --collection-url https://moidaus.com/collections/awards-moida-2026-mid-year-awards-_event --vendor-filter ""

Results are written to output/<site>_<collection>_<timestamp>.csv and .json,
one row per variation (a product with 3 sizes/flavors produces 3 rows):
    product_name, product_url, variation, sku,
    original_price, sale_price, cart_price, cart_discount,
    currency, gtin

original_price / sale_price come from the product page itself (Shopify's
compare_at_price vs price) - reliable, same technique as everything else.

cart_price is computed directly as sale_price with Moida's "welcome30"
cart-level discount applied (default 30% off) - confirmed live against two
real cart line items (Anua $19.90 -> $13.93, Arencia $16.80 -> $11.76, both
exactly 30% off sale_price). An earlier version simulated an actual
add-to-cart + apply-discount-code + read-cart round trip via Shopify's cart
AJAX API, but that depended on ZenRows correctly forwarding cookies across
three separate requests, which didn't reliably apply the discount in
practice - a direct calculation is simpler and gives the same result.

Use --discount-pct to change the assumed percentage (default 30), or
--skip-cart-price to leave cart_price blank and only report original/sale
price.
"""

import argparse
import csv
import json
import logging
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    zenrows_api_key: str
    zenrows_endpoint: str = "https://api.zenrows.com/v1/"
    # JS render + premium proxy + wait_for on a search results page can
    # legitimately take well over 150s -- a timeout here just means the
    # client gave up before ZenRows finished, not that anything is broken.
    request_timeout_seconds: int = 230
    request_delay_seconds: float = 0.75
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    max_workers: int = 4
    proxy_country: str = "us"


def load_settings() -> Settings:
    api_key = os.getenv("ZENROWS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ZENROWS_API_KEY is not set. Create a .env file next to this script "
            "containing: ZENROWS_API_KEY=your_key_here"
        )
    return Settings(zenrows_api_key=api_key)


# --------------------------------------------------------------------------
# ZenRows client
# --------------------------------------------------------------------------

class ZenRowsError(RuntimeError):
    pass


class ZenRowsClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = requests.Session()

    def fetch(
        self,
        url: str,
        js_render: bool = True,
        premium_proxy: bool = True,
        wait_for: Optional[str] = None,
        wait_ms: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> requests.Response:
        params = {
            "apikey": self._settings.zenrows_api_key,
            "url": url,
            "js_render": "true" if js_render else "false",
        }
        if premium_proxy:
            params["premium_proxy"] = "true"
            params["proxy_country"] = self._settings.proxy_country
        if wait_for:
            params["wait_for"] = wait_for
        if wait_ms:
            params["wait"] = str(wait_ms)

        retries = max_retries if max_retries is not None else self._settings.max_retries
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                response = self._session.get(
                    self._settings.zenrows_endpoint,
                    params=params,
                    timeout=self._settings.request_timeout_seconds,
                )
                if response.status_code == 200:
                    return response
                last_error = ZenRowsError(
                    f"ZenRows returned HTTP {response.status_code} for {url}: "
                    f"{response.text[:300]}"
                )
                logger.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, last_error)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Attempt %d/%d errored for %s: %s", attempt, retries, url, exc)

            if attempt < retries:
                time.sleep(self._settings.retry_backoff_seconds * attempt)

        raise ZenRowsError(f"Failed to fetch {url} after {retries} attempt(s)") from last_error


# --------------------------------------------------------------------------
# Collection/search page: discover product URLs
# --------------------------------------------------------------------------

PRODUCT_LINK_PATTERN = re.compile(r"/(products?|p)/[\w\-]+")


def _urls_from_products_json(payload: dict, base_url: str) -> List[str]:
    urls = []
    for product in payload.get("products", []):
        handle = product.get("handle")
        if handle:
            urls.append(f"{base_url}/products/{handle}")
    return urls


def _product_matches_vendor(product: dict, needle: str) -> bool:
    """True if needle appears (case-insensitively, substring) in the
    product's vendor field, title, or any of its tags. Substring rather than
    exact match, and checking title/tags too, so a collab line (e.g. "MOIDA
    X MEDICUBE"), a bracketed brand prefix in the title ("[MEDICUBE] ...")
    whose vendor field is set to something else (e.g. left as the
    storefront's own name for an exclusive item), or a tag-only labeling
    isn't silently dropped - erring toward over-matching rather than missing
    real products, since a few false positives are easy to spot-check but a
    missed product isn't."""
    needle = needle.strip().lower()
    vendor = (product.get("vendor") or "").lower()
    if needle in vendor:
        return True
    title = (product.get("title") or "").lower()
    if needle in title:
        return True
    return any(needle in (tag or "").lower() for tag in (product.get("tags") or []))


def _urls_from_products_json_filtered(payload: dict, base_url: str, vendor_filter: Optional[str]) -> List[str]:
    """Like _urls_from_products_json, but keeps only products matching
    vendor_filter per _product_matches_vendor (or all products if
    vendor_filter is falsy) - lets discovery walk a big catch-all collection
    like /collections/all and keep just the brand we care about."""
    products = payload.get("products", [])
    if vendor_filter:
        products = [p for p in products if _product_matches_vendor(p, vendor_filter)]
    return _urls_from_products_json({"products": products}, base_url)


def _urls_from_html(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if PRODUCT_LINK_PATTERN.search(href):
            full = href if href.startswith("http") else f"{base_url}{href}"
            urls.add(full.split("?")[0])
    return sorted(urls)


def _add_query_params(url: str, **params: Any) -> str:
    """Merges params into url's existing query string instead of blindly
    appending '?...', which would produce a malformed URL (two '?'s) for
    a URL that already has query params, e.g. a Shopify /search?q=... URL."""
    split = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(split.query))
    query.update(params)
    return urllib.parse.urlunsplit(split._replace(query=urllib.parse.urlencode(query)))


def collect_product_urls(
    client: ZenRowsClient, settings: Settings, collection_url: str, max_pages: int = 10,
    vendor_filter: Optional[str] = None,
) -> Tuple[List[str], bool]:
    collection_url = collection_url.rstrip("/")
    base_url = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(collection_url))
    is_search = "/search" in urllib.parse.urlsplit(collection_url).path
    all_urls: Set[str] = set()
    is_shopify = False

    # Shopify's /search results page has no products.json-style JSON
    # endpoint (that pattern only exists for /collections/<handle>), so
    # don't waste a request probing for it -- go straight to HTML parsing.
    for page in [] if is_search else range(1, max_pages + 1):
        json_url = f"{collection_url}/products.json?limit=250&page={page}"
        try:
            response = client.fetch(json_url, js_render=False, premium_proxy=False, max_retries=1)
            payload = response.json()
        except (ZenRowsError, ValueError) as exc:
            logger.info("products.json unavailable (%s); falling back to HTML parsing", exc)
            break

        is_shopify = True
        raw_count = len(payload.get("products", []))
        if raw_count == 0:
            break

        page_urls = _urls_from_products_json_filtered(payload, base_url, vendor_filter)
        new_urls = set(page_urls) - all_urls
        all_urls.update(page_urls)
        logger.info(
            "products.json page %d: %d products (%d matching%s, %d total matched)",
            page, raw_count, len(new_urls),
            f" vendor={vendor_filter!r}" if vendor_filter else "", len(all_urls),
        )
        if raw_count < 250:
            break  # last page: Shopify returned fewer than the requested limit
        time.sleep(settings.request_delay_seconds)

    if all_urls or is_shopify:
        # is_shopify but no matches is a real, final result (e.g. vendor_filter
        # matched nothing anywhere in the catalog) - don't fall through to
        # HTML parsing, which can't filter by vendor at all.
        return sorted(all_urls), is_shopify

    if vendor_filter:
        logger.warning(
            "vendor_filter=%r requested but falling back to HTML parsing, which can't filter by vendor "
            "-- results below are unfiltered.", vendor_filter,
        )

    for page in range(1, max_pages + 1):
        page_url = _add_query_params(collection_url, page=page)
        # No wait_for selector here: it assumes a specific CSS shape for
        # product links, and ZenRows returned RESP001 ("could not get
        # content") against Moida's search page when relying on it - likely
        # because that selector never appeared. A fixed render delay is a
        # safer bet across different page markups.
        response = client.fetch(page_url, js_render=True, premium_proxy=True, wait_ms=5000)
        page_urls = _urls_from_html(response.text, base_url)
        new_urls = set(page_urls) - all_urls
        if not new_urls:
            logger.info("No new products found on HTML page %d; stopping", page)
            break
        all_urls.update(page_urls)
        logger.info(
            "HTML page %d: %d products (%d new, %d total)",
            page, len(page_urls), len(new_urls), len(all_urls),
        )
        time.sleep(settings.request_delay_seconds)

    return sorted(all_urls), False


# --------------------------------------------------------------------------
# Product page: variations, price, GTIN
# --------------------------------------------------------------------------

GTIN_KEYS = ("gtin13", "gtin14", "gtin12", "gtin8", "gtin")


def _extract_jsonld_blocks(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    blocks = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                blocks.extend(item for item in data["@graph"] if isinstance(item, dict))
            else:
                blocks.append(data)
    return blocks


def _find_jsonld_of_type(blocks: List[Dict[str, Any]], wanted_type: str) -> Optional[Dict[str, Any]]:
    for block in blocks:
        block_type = block.get("@type")
        types = block_type if isinstance(block_type, list) else [block_type]
        if any(t == wanted_type for t in types if t):
            return block
    return None


def _find_product_jsonld(blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return _find_jsonld_of_type(blocks, "Product")


def _first_gtin(obj: Dict[str, Any]) -> Optional[str]:
    for key in GTIN_KEYS:
        value = obj.get(key)
        if value:
            return str(value)
    return None


def _normalize_offers(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    offers = product.get("offers")
    if offers is None:
        return []
    if isinstance(offers, dict):
        nested = offers.get("offers")
        if isinstance(nested, list):
            return [o for o in nested if isinstance(o, dict)]
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _variant_from_offer(
    offer: Dict[str, Any], fallback_name: Optional[str], fallback_gtin: Optional[str],
    fallback_sku: Optional[str] = None,
) -> Dict[str, Any]:
    price_spec = offer.get("priceSpecification") or {}
    return {
        "sku": offer.get("sku") or fallback_sku,
        "name": offer.get("name") or fallback_name,
        "price": offer.get("price") or price_spec.get("price"),
        "currency": offer.get("priceCurrency") or price_spec.get("priceCurrency") or "USD",
        "availability": offer.get("availability"),
        "gtin": _first_gtin(offer) or fallback_gtin,
    }


def parse_product_jsonld(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    blocks = _extract_jsonld_blocks(soup)

    product = _find_product_jsonld(blocks)
    if product:
        top_gtin = _first_gtin(product)
        variants = [
            _variant_from_offer(offer, product.get("name"), top_gtin)
            for offer in _normalize_offers(product)
        ]
        return {"name": product.get("name"), "sku": product.get("sku"), "gtin": top_gtin, "variants": variants}

    # Some Shopify themes emit schema.org's "ProductGroup" instead of a plain
    # "Product" for a product with multiple variants (e.g. color options) -
    # each variant is its own Product-like node inside "hasVariant", carrying
    # its own sku/gtin rather than a flat "offers" list on the top object.
    group = _find_jsonld_of_type(blocks, "ProductGroup")
    if group:
        top_gtin = _first_gtin(group)
        variants = []
        for variant_node in group.get("hasVariant") or []:
            if not isinstance(variant_node, dict):
                continue
            variant_gtin = _first_gtin(variant_node) or top_gtin
            offers = _normalize_offers(variant_node)
            if offers:
                variants.append(
                    _variant_from_offer(
                        offers[0], variant_node.get("name") or group.get("name"), variant_gtin,
                        fallback_sku=variant_node.get("sku"),
                    )
                )
            else:
                variants.append(
                    {
                        "sku": variant_node.get("sku"),
                        "name": variant_node.get("name") or group.get("name"),
                        "price": None,
                        "currency": "USD",
                        "availability": None,
                        "gtin": variant_gtin,
                    }
                )
        return {
            "name": group.get("name"),
            "sku": group.get("productGroupID"),
            "gtin": top_gtin,
            "variants": variants,
        }

    return {"name": None, "sku": None, "gtin": None, "variants": []}


def fetch_shopify_variants(client: ZenRowsClient, product_url: str) -> Optional[Dict[str, Any]]:
    json_url = f"{product_url}.json"
    try:
        response = client.fetch(json_url, js_render=False, premium_proxy=False, max_retries=1)
        payload = response.json()
    except (ZenRowsError, ValueError) as exc:
        logger.info("Shopify product JSON unavailable for %s: %s", product_url, exc)
        return None

    product = payload.get("product")
    if not product:
        return None

    options = product.get("options", [])
    variants = product.get("variants", [])
    result = []
    for variant in variants:
        option_values = []
        for i, opt in enumerate(options, start=1):
            val = variant.get(f"option{i}")
            if val and val != "Default Title":
                option_values.append(f"{opt.get('name')}: {val}")
        result.append(
            {
                "variant_id": variant.get("id"),
                "sku": variant.get("sku"),
                "variation": " / ".join(option_values) or "Default",
                "price": variant.get("price"),
                "compare_at_price": variant.get("compare_at_price"),
                "available": variant.get("available"),
            }
        )
    return {"name": product.get("title"), "variants": result}


def apply_cart_discount(price: Any, discount_pct: float) -> Optional[float]:
    """Applies Moida's assumed flat cart-level discount (e.g. 'welcome30')
    directly to a product-page price, rather than simulating a live
    add-to-cart round trip - confirmed live against real cart line items
    (Anua $19.90 -> $13.93, Arencia $16.80 -> $11.76, both exactly 30% off)."""
    if price is None:
        return None
    try:
        return round(float(price) * (1 - discount_pct / 100), 2)
    except (TypeError, ValueError):
        return None


def scrape_product(
    client: ZenRowsClient,
    settings: Settings,
    product_url: str,
    try_shopify_json: bool = True,
    fetch_cart_price: bool = False,
    discount_code: Optional[str] = None,
    discount_pct: float = 30.0,
) -> List[Dict[str, Any]]:
    # wait_ms gives the page's JS a moment to finish populating JSON-LD
    # before ZenRows captures the HTML - a debug comparison against this
    # exact page (Zero Pore Cooling Mask) showed two live fetches of the
    # same URL disagreeing on whether a variant's GTIN was present at all,
    # which looks like async-rendered data occasionally getting captured
    # before it's ready rather than a genuine data gap.
    response = client.fetch(product_url, js_render=True, premium_proxy=True, wait_ms=3000)
    jsonld = parse_product_jsonld(response.text)
    shopify = fetch_shopify_variants(client, product_url) if try_shopify_json else None
    return build_rows_from_product_data(product_url, jsonld, shopify, fetch_cart_price, discount_code, discount_pct)


def build_rows_from_product_data(
    product_url: str,
    jsonld: Dict[str, Any],
    shopify: Optional[Dict[str, Any]],
    fetch_cart_price: bool = False,
    discount_code: Optional[str] = None,
    discount_pct: float = 30.0,
) -> List[Dict[str, Any]]:
    """Builds the final row(s) from already-fetched JSON-LD + Shopify data.
    Split out from scrape_product so a caller that already has both (e.g.
    --debug mode) can build the same rows without a second live fetch -
    two separate ZenRows fetches of the same URL can render slightly
    differently, which previously made debug output disagree with the
    real scrape for no code reason."""
    product_name = (shopify or {}).get("name") or jsonld.get("name") or product_url
    gtin_by_sku = {v["sku"]: v["gtin"] for v in jsonld["variants"] if v.get("sku")}
    price_by_sku = {v["sku"]: (v["price"], v["currency"]) for v in jsonld["variants"] if v.get("sku")}

    rows: List[Dict[str, Any]] = []

    if shopify and shopify["variants"]:
        for v in shopify["variants"]:
            sku = v["sku"]
            gtin = gtin_by_sku.get(sku) or jsonld.get("gtin")
            sale_price = v.get("price")
            currency = "USD"
            if sale_price is None and sku in price_by_sku:
                sale_price, currency = price_by_sku[sku]
            original_price = v.get("compare_at_price") or sale_price

            cart_price = apply_cart_discount(sale_price, discount_pct) if fetch_cart_price else None
            cart_discount = f"{discount_code} ({discount_pct:g}% off, assumed)" if cart_price is not None else None

            rows.append(
                {
                    "product_name": product_name,
                    "product_url": product_url,
                    "variation": v["variation"],
                    "sku": sku,
                    "original_price": original_price,
                    "sale_price": sale_price,
                    "cart_price": cart_price,
                    "cart_discount": cart_discount,
                    "currency": currency,
                    "gtin": gtin,
                }
            )
    elif jsonld["variants"]:
        for v in jsonld["variants"]:
            sale_price = v.get("price")
            cart_price = apply_cart_discount(sale_price, discount_pct) if fetch_cart_price else None
            cart_discount = f"{discount_code} ({discount_pct:g}% off, assumed)" if cart_price is not None else None
            rows.append(
                {
                    "product_name": product_name,
                    "product_url": product_url,
                    "variation": v.get("name") or "Default",
                    "sku": v.get("sku"),
                    "original_price": sale_price,
                    "sale_price": sale_price,
                    "cart_price": cart_price,
                    "cart_discount": cart_discount,
                    "currency": v.get("currency", "USD"),
                    "gtin": v.get("gtin"),
                }
            )
    else:
        logger.warning("No variation/price/GTIN data found for %s", product_url)
        rows.append(
            {
                "product_name": product_name,
                "product_url": product_url,
                "variation": "Unknown",
                "sku": None,
                "original_price": None,
                "sale_price": None,
                "cart_price": None,
                "cart_discount": None,
                "currency": None,
                "gtin": None,
            }
        )

    return rows


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

FIELDNAMES = [
    "product_name", "product_url", "variation", "sku",
    "original_price", "sale_price", "cart_price", "cart_discount",
    "currency", "gtin",
]


def export_csv(rows: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_json(rows: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def slug_from_url(url: str, vendor_filter: Optional[str] = None) -> str:
    """Derive a short, filesystem-safe label from a collection/search URL's
    host and last path segment (or its query, for a /search URL), plus the
    vendor filter if any - used to prefix output filenames so results from
    different searches/collections/vendors don't collide or get confused."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.replace("www.", "").split(".")[0]
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1] or "collection"
    if segment == "search" and parts.query:
        query = dict(urllib.parse.parse_qsl(parts.query))
        segment = f"search-{query.get('q', '')}" or segment
    if vendor_filter:
        segment = f"{segment}-{vendor_filter}"
    return re.sub(r"[^a-zA-Z0-9_-]", "-", f"{host}_{segment}")[:80]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape a Moidaus (Shopify) collection or search page's product variations, prices, and GTINs via ZenRows."
    )
    parser.add_argument(
        "--collection-url", type=str,
        default="https://moidaus.com/collections/all",
        help="Full URL of the collection or search page to scrape (default: Moidaus /collections/all, "
             "filtered by --vendor-filter -- /search is blocked, see README)",
    )
    parser.add_argument(
        "--vendor-filter", type=str, default="Medicube",
        help="Only keep products where this appears (case-insensitive, substring) in the Shopify "
             "'vendor' field or any tag -- catches collabs/mislabeled vendors too, at the cost of the "
             "occasional false positive. Only applies to a JSON-backed collection page (not the "
             "HTML-parsing fallback). Pass '' to keep every product in --collection-url.",
    )
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None, help="Only scrape the first N products")
    parser.add_argument("--output-dir", type=str, default="output")
    parser.add_argument(
        "--url", type=str, default=None,
        help="Debug mode: scrape a single product URL and print the raw result, skip collection discovery",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="With --url, also print the raw JSON-LD and Shopify per-product JSON before the final row(s)",
    )
    parser.add_argument(
        "--skip-cart-price", action="store_true",
        help="Leave cart_price blank instead of computing sale_price minus the assumed cart discount",
    )
    parser.add_argument(
        "--discount-code", type=str, default="welcome30",
        help="Label recorded in cart_discount for the assumed cart-level discount (default: welcome30)",
    )
    parser.add_argument(
        "--discount-pct", type=float, default=30.0,
        help="Percent off sale_price to compute cart_price as (default: 30, Moida's welcome30 discount)",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = ZenRowsClient(settings)
    fetch_cart_price = not args.skip_cart_price
    discount_code = args.discount_code or None

    if args.url:
        # One fetch, shared by the debug dump and the row-building below -
        # two separate live ZenRows fetches of the same URL can render
        # slightly differently (JS rendering isn't perfectly deterministic),
        # which previously made --debug output disagree with itself for no
        # real reason.
        response = client.fetch(args.url, js_render=True, premium_proxy=True, wait_ms=3000)
        jsonld = parse_product_jsonld(response.text)
        shopify = fetch_shopify_variants(client, args.url)

        if args.debug:
            raw_blocks = _extract_jsonld_blocks(BeautifulSoup(response.text, "lxml"))
            print("--- Raw JSON-LD block types found on the page ---")
            print([b.get("@type") for b in raw_blocks])
            print("--- JSON-LD extraction (schema.org Product/ProductGroup/Offers) ---")
            print(json.dumps(jsonld, indent=2))
            print("--- Shopify per-product JSON (.json endpoint) ---")
            print(json.dumps(shopify, indent=2))
            print("--- Final scraped row(s) ---")

        rows = build_rows_from_product_data(
            args.url, jsonld, shopify,
            fetch_cart_price=fetch_cart_price, discount_code=discount_code, discount_pct=args.discount_pct,
        )
        print(json.dumps(rows, indent=2))
        return

    vendor_filter = args.vendor_filter or None
    logger.info(
        "Collecting product URLs from %s%s",
        args.collection_url, f" (vendor={vendor_filter!r})" if vendor_filter else "",
    )
    product_urls, is_shopify = collect_product_urls(
        client, settings, args.collection_url, max_pages=args.max_pages, vendor_filter=vendor_filter,
    )
    logger.info("Found %d product URLs (shopify-style endpoints: %s)", len(product_urls), is_shopify)

    if not product_urls:
        logger.error("No product URLs found. The collection/search page structure may differ from expected.")
        return

    if args.limit:
        product_urls = product_urls[: args.limit]

    all_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                scrape_product, client, settings, url, is_shopify, fetch_cart_price, discount_code, args.discount_pct
            ): url
            for url in product_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                rows = future.result()
                all_rows.extend(rows)
                logger.info("Scraped %d variation row(s) from %s", len(rows), url)
            except Exception:
                logger.exception("Failed to scrape %s", url)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    slug = slug_from_url(args.collection_url, vendor_filter)
    csv_path = output_dir / f"{slug}_{timestamp}.csv"
    json_path = output_dir / f"{slug}_{timestamp}.json"
    export_csv(all_rows, csv_path)
    export_json(all_rows, json_path)
    logger.info("Wrote %d rows to %s and %s", len(all_rows), csv_path, json_path)


if __name__ == "__main__":
    main()
