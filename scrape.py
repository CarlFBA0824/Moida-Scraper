"""
Moida Shopify scraper — variations, prices, and GTIN, via ZenRows.

Forked from the shared Vitacost/Moida scraper in the Web-Scraping repo, kept
in its own repo so Moida-specific changes never risk affecting Vitacost's
scraper (and vice versa).

Targets any Moidaus (moidaus.com) Shopify page: a /collections/<handle> page
or a /search?q=... results page - pass a different --collection-url to
target a different collection/search on the same store.

Plug-and-play, single-file version. Setup:
    pip install requests beautifulsoup4 lxml python-dotenv
    Create a .env file next to this script containing:
        ZENROWS_API_KEY=your_key_here

Run:
    python scrape.py --limit 3     (quick test on 3 products)
    python scrape.py               (full run, default: medicube search)
    python scrape.py --collection-url "https://moidaus.com/search?q=medicube&options%5Bprefix%5D=last"
    python scrape.py --collection-url https://moidaus.com/collections/awards-moida-2026-mid-year-awards-_event

Results are written to output/<site>_<collection>_<timestamp>.csv and .json,
one row per variation (a product with 3 sizes/flavors produces 3 rows):
    product_name, product_url, variation, sku,
    original_price, sale_price, cart_price, cart_discount,
    currency, gtin

original_price / sale_price come from the product page itself (Shopify's
compare_at_price vs price) - reliable, same technique as everything else.

cart_price / cart_discount simulate adding the item to a fresh cart via
Shopify's cart AJAX API (POST /cart/add.js), then apply a discount code
(default "WELCOME30", Moida's site-wide auto-applied welcome discount) the
same way the site's own cart-drawer coupon form does (POST /cart with a
"discount" field), then read the final price back via GET /cart.js. This
captures a cart-level discount that never appears anywhere in the product
page's own data - Shopify only calculates it once a discount code is
actually attached to a cart.

Use --discount-code CODE to target a different promo code, or
--discount-code "" to skip applying any code (still runs the add/read
cycle, useful for sites with an automatic no-code discount). Pass
--skip-cart-price to disable the whole cart simulation (saves ~2 extra
ZenRows requests per variation) if you just want original/sale price.

Caveat: the cart-price simulation is UNVERIFIED against the live site (built
and mock-tested against numbers from a screenshot: $25.41 -> $17.79 via
welcome30, math checks out), but real ZenRows header/cookie passthrough
behavior was an assumption, not something confirmed live. Do a --limit 3
smoke test and check the log/output for populated cart_price before a full
run.
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

    def request(
        self,
        url: str,
        method: str = "GET",
        json_body: Optional[dict] = None,
        data_body: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        max_retries: Optional[int] = None,
    ) -> requests.Response:
        """Low-level passthrough request for JSON/form API endpoints (Shopify's
        /cart/add.js, /cart.js, /cart discount form) that need a specific HTTP
        method, body, and/or forwarded headers (e.g. a Cookie to keep one cart
        across calls). Relies on ZenRows' custom_headers passthrough to deliver
        our headers to the target site, not just to ZenRows itself.

        Pass json_body for JSON endpoints, or data_body (a pre-encoded string,
        e.g. form-urlencoded) for endpoints like Shopify's cart discount form -
        not both."""
        params = {
            "apikey": self._settings.zenrows_api_key,
            "url": url,
            "js_render": "false",
            "custom_headers": "true",
        }
        retries = max_retries if max_retries is not None else self._settings.max_retries
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                response = self._session.request(
                    method,
                    self._settings.zenrows_endpoint,
                    params=params,
                    json=json_body,
                    data=data_body,
                    headers=extra_headers or {},
                    timeout=self._settings.request_timeout_seconds,
                )
                if response.status_code == 200:
                    return response
                last_error = ZenRowsError(
                    f"ZenRows returned HTTP {response.status_code} for {method} {url}: {response.text[:300]}"
                )
                logger.warning("Attempt %d/%d failed for %s %s: %s", attempt, retries, method, url, last_error)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Attempt %d/%d errored for %s %s: %s", attempt, retries, method, url, exc)

            if attempt < retries:
                time.sleep(self._settings.retry_backoff_seconds * attempt)

        raise ZenRowsError(f"Failed to {method} {url} after {retries} attempt(s)") from last_error


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
    client: ZenRowsClient, settings: Settings, collection_url: str, max_pages: int = 10
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

        page_urls = _urls_from_products_json(payload, base_url)
        if not page_urls:
            is_shopify = True
            break
        new_urls = set(page_urls) - all_urls
        all_urls.update(page_urls)
        is_shopify = True
        logger.info(
            "products.json page %d: %d products (%d new, %d total)",
            page, len(page_urls), len(new_urls), len(all_urls),
        )
        if not new_urls:
            break
        time.sleep(settings.request_delay_seconds)

    if all_urls:
        return sorted(all_urls), is_shopify

    for page in range(1, max_pages + 1):
        page_url = _add_query_params(collection_url, page=page)
        response = client.fetch(page_url, js_render=True, premium_proxy=True, wait_for="a[href*='product']")
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


def _find_product_jsonld(blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for block in blocks:
        block_type = block.get("@type")
        types = block_type if isinstance(block_type, list) else [block_type]
        if any(t == "Product" for t in types if t):
            return block
    return None


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


def parse_product_jsonld(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    blocks = _extract_jsonld_blocks(soup)
    product = _find_product_jsonld(blocks)
    if not product:
        return {"name": None, "sku": None, "gtin": None, "variants": []}

    top_gtin = _first_gtin(product)
    offers = _normalize_offers(product)

    variants = []
    for offer in offers:
        price_spec = offer.get("priceSpecification") or {}
        variants.append(
            {
                "sku": offer.get("sku"),
                "name": offer.get("name") or product.get("name"),
                "price": offer.get("price") or price_spec.get("price"),
                "currency": offer.get("priceCurrency") or price_spec.get("priceCurrency") or "USD",
                "availability": offer.get("availability"),
                "gtin": _first_gtin(offer) or top_gtin,
            }
        )

    return {
        "name": product.get("name"),
        "sku": product.get("sku"),
        "gtin": top_gtin,
        "variants": variants,
    }


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


def apply_discount_code(
    client: ZenRowsClient, base_url: str, cookie_header: str, discount_code: str
) -> str:
    """Applies a discount code to the current cart by POSTing to /cart with a
    'discount' field - the same request Shopify's cart-drawer coupon form
    (<form is="cart-discount" action="/cart" method="POST"><input
    name="discount" ...></form>) sends. Best-effort: on any failure, just
    returns the original cookie so the caller can still read the cart back
    (without the discount) instead of aborting.
    """
    discount_url = f"{base_url}/cart"
    try:
        response = client.request(
            discount_url,
            method="POST",
            data_body=f"discount={urllib.parse.quote(discount_code)}",
            extra_headers={
                "Cookie": cookie_header,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
            max_retries=1,
        )
    except ZenRowsError as exc:
        logger.info("Applying discount code %s failed: %s", discount_code, exc)
        return cookie_header

    return response.headers.get("Zr-Cookies") or cookie_header


def get_cart_discounted_price(
    client: ZenRowsClient,
    base_url: str,
    variant_id: Any,
    quantity: int = 1,
    discount_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Simulates adding one variant to a fresh cart via Shopify's cart AJAX
    API (POST /cart/add.js), optionally applies a discount code the same way
    the site's own coupon form does (POST /cart with a 'discount' field),
    then reads the final per-unit price back via GET /cart.js - capturing a
    cart-level discount (e.g. Moida's site-wide 'welcome30' code) that never
    appears in the product page's own price data.

    Best-effort: relies on ZenRows forwarding our custom headers/cookies to
    the target site so the same cart persists across calls. Returns None
    (logging why) if anything about that chain doesn't work.
    """
    if not variant_id:
        return None

    add_url = f"{base_url}/cart/add.js"
    try:
        add_response = client.request(
            add_url,
            method="POST",
            json_body={"items": [{"id": int(variant_id), "quantity": quantity}]},
            extra_headers={"Content-Type": "application/json", "Accept": "application/json"},
            max_retries=1,
        )
    except ZenRowsError as exc:
        logger.info("cart/add.js failed for variant %s: %s", variant_id, exc)
        return None

    # ZenRows doesn't surface the target's Set-Cookie in the standard header
    # (requests won't auto-parse it into add_response.cookies) - it forwards
    # it via its own "Zr-Cookies" header, already formatted as a ready-to-use
    # "name=value; name2=value2" Cookie header string.
    cookie_header = add_response.headers.get("Zr-Cookies")
    if not cookie_header:
        logger.info(
            "No cart cookie returned for variant %s; cannot read cart back. "
            "add_response status=%s headers=%s body[:300]=%r",
            variant_id, add_response.status_code, dict(add_response.headers), add_response.text[:300],
        )
        return None

    if discount_code:
        cookie_header = apply_discount_code(client, base_url, cookie_header, discount_code)

    cart_url = f"{base_url}/cart.js"
    try:
        cart_response = client.request(
            cart_url,
            method="GET",
            extra_headers={"Cookie": cookie_header, "Accept": "application/json"},
            max_retries=1,
        )
        cart_json = cart_response.json()
    except (ZenRowsError, ValueError) as exc:
        logger.info("cart.js failed for variant %s: %s", variant_id, exc)
        return None

    for item in cart_json.get("items", []):
        if str(item.get("variant_id")) == str(variant_id):
            discounts = item.get("discounts") or []
            final_price = item.get("final_price")
            original_price = item.get("original_price")
            return {
                "cart_price": (final_price / 100) if isinstance(final_price, (int, float)) else None,
                "cart_original_price": (original_price / 100) if isinstance(original_price, (int, float)) else None,
                "cart_discount_titles": ", ".join(d.get("title", "") for d in discounts if d.get("title")),
            }
    logger.info("Added variant %s not found in cart.js response", variant_id)
    return None


def scrape_product(
    client: ZenRowsClient,
    settings: Settings,
    product_url: str,
    try_shopify_json: bool = True,
    fetch_cart_price: bool = False,
    discount_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    response = client.fetch(product_url, js_render=True, premium_proxy=True)
    jsonld = parse_product_jsonld(response.text)
    shopify = fetch_shopify_variants(client, product_url) if try_shopify_json else None
    base_url = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(product_url))

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

            cart_price = cart_original_price = cart_discount_titles = None
            if fetch_cart_price:
                cart_result = get_cart_discounted_price(
                    client, base_url, v.get("variant_id"), discount_code=discount_code
                )
                if cart_result:
                    cart_price = cart_result["cart_price"]
                    cart_original_price = cart_result["cart_original_price"]
                    cart_discount_titles = cart_result["cart_discount_titles"]

            rows.append(
                {
                    "product_name": product_name,
                    "product_url": product_url,
                    "variation": v["variation"],
                    "sku": sku,
                    "original_price": original_price,
                    "sale_price": sale_price,
                    "cart_price": cart_price,
                    "cart_discount": cart_discount_titles,
                    "currency": currency,
                    "gtin": gtin,
                }
            )
    elif jsonld["variants"]:
        for v in jsonld["variants"]:
            rows.append(
                {
                    "product_name": product_name,
                    "product_url": product_url,
                    "variation": v.get("name") or "Default",
                    "sku": v.get("sku"),
                    "original_price": v.get("price"),
                    "sale_price": v.get("price"),
                    "cart_price": None,
                    "cart_discount": None,
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

def slug_from_url(url: str) -> str:
    """Derive a short, filesystem-safe label from a collection/search URL's
    host and last path segment (or its query, for a /search URL) - used to
    prefix output filenames so results from different searches/collections
    don't collide or get confused."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.replace("www.", "").split(".")[0]
    segment = parts.path.rstrip("/").rsplit("/", 1)[-1] or "collection"
    if segment == "search" and parts.query:
        query = dict(urllib.parse.parse_qsl(parts.query))
        segment = f"search-{query.get('q', '')}" or segment
    return re.sub(r"[^a-zA-Z0-9_-]", "-", f"{host}_{segment}")[:80]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape a Moidaus (Shopify) collection or search page's product variations, prices, and GTINs via ZenRows."
    )
    parser.add_argument(
        "--collection-url", type=str,
        default="https://moidaus.com/search?q=medicube&options%5Bprefix%5D=last",
        help="Full URL of the collection or search page to scrape (default: Moidaus medicube search)",
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
        "--skip-cart-price", action="store_true",
        help="Don't simulate add-to-cart to capture cart-level discount prices (saves ~2 extra requests/variant)",
    )
    parser.add_argument(
        "--discount-code", type=str, default="WELCOME30",
        help="Discount code to apply to the simulated cart before reading cart_price "
             "(default: WELCOME30). Pass an empty string ('') to skip applying any code.",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = ZenRowsClient(settings)
    fetch_cart_price = not args.skip_cart_price
    discount_code = args.discount_code or None

    if args.url:
        rows = scrape_product(
            client, settings, args.url, try_shopify_json=True,
            fetch_cart_price=fetch_cart_price, discount_code=discount_code,
        )
        print(json.dumps(rows, indent=2))
        return

    logger.info("Collecting product URLs from %s", args.collection_url)
    product_urls, is_shopify = collect_product_urls(client, settings, args.collection_url, max_pages=args.max_pages)
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
                scrape_product, client, settings, url, is_shopify, fetch_cart_price, discount_code
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
    slug = slug_from_url(args.collection_url)
    csv_path = output_dir / f"{slug}_{timestamp}.csv"
    json_path = output_dir / f"{slug}_{timestamp}.json"
    export_csv(all_rows, csv_path)
    export_json(all_rows, json_path)
    logger.info("Wrote %d rows to %s and %s", len(all_rows), csv_path, json_path)


if __name__ == "__main__":
    main()
