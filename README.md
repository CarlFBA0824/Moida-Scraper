# Moida Scraper

Scrapes [moidaus.com](https://moidaus.com) (Shopify) product **variations**,
**prices**, and **GTIN**, using [ZenRows](https://www.zenrows.com/) to handle
proxying/JS rendering. Forked from the shared scraper in the `Web-Scraping`
repo into its own repo so Moida-specific changes never risk affecting the
Vitacost scraper, or vice versa.

Works against either a `/collections/<handle>` page or a `/search?q=...`
results page on Moida.

## Moida-specific pricing

Moida applies a site-wide automatic **"welcome30" discount** that only gets
calculated once an item is actually in the cart - it never appears in the
product page's own price data. So this scraper captures three price points
per variation:

- `original_price` - the listed/compare-at price on the product page
- `sale_price` - the page's own (already discounted) price
- `cart_price` - `sale_price` with the welcome30 discount applied

`cart_price` is a direct calculation (`sale_price * (1 - discount_pct/100)`,
default 30%), confirmed against two real cart line items (Anua $19.90 ->
$13.93, Arencia $16.80 -> $11.76, both exactly 30% off). An earlier version
tried to simulate a live add-to-cart + apply-discount-code round trip
instead, but that depended on ZenRows correctly forwarding cookies across
three separate requests and didn't reliably apply the discount in practice
- if Moida ever changes the discount rate, update it with `--discount-pct`.

## Known issue: `/search` pages get blocked

Fetching a `/search?q=...` results page through ZenRows (JS render + premium
proxy) has consistently failed with error `422 RESP001 "Could not get
content"` after ~150-170s, regardless of wait strategy (`wait_for` selector,
`wait_ms`, or a longer client timeout) - and ZenRows doesn't charge credits
for it, confirming it's a real server-side failure, not a fluke. Individual
`/products/<handle>` pages fetch fine through the same setup, so this looks
like a block/rate-limit specific to Moida's `/search` endpoint (possibly a
separate, more heavily bot-protected backend than plain product/collection
pages), not the whole site.

**Workaround:** use a `/collections/<handle>` page instead of `/search` when
one exists for the products you want - it uses the same lightweight
Shopify product-listing pattern that already works.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your ZenRows API key
```

## Usage

Quick smoke test (3 products, check the output before a full run):

```bash
python scrape.py --limit 3
```

Full run (default: the `medicube` search on moidaus.com):

```bash
python scrape.py
```

Target a different search or collection:

```bash
python scrape.py --collection-url "https://moidaus.com/search?q=medicube&options%5Bprefix%5D=last"
python scrape.py --collection-url https://moidaus.com/collections/awards-moida-2026-mid-year-awards-_event
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--collection-url` | Moida medicube search | Full URL of the collection/search page to scrape |
| `--max-pages` | 10 | Max pages to walk when discovering products |
| `--workers` | 4 | Concurrent product page fetches |
| `--limit N` | none | Only scrape the first N products |
| `--output-dir` | `output` | Where CSV/JSON results are written |
| `--discount-code` | `welcome30` | Label recorded in `cart_discount` for the assumed cart discount |
| `--discount-pct` | `30` | Percent off `sale_price` used to compute `cart_price` |
| `--skip-cart-price` | off | Leave `cart_price` blank instead of computing it |
| `--url` | none | Debug mode: scrape a single product URL and print the raw result |

Results are written to `output/<slug>_<timestamp>.csv` and `.json`, one row
per **variation** (a product with 3 flavors/sizes produces 3 rows), with
columns:

`product_name, product_url, variation, sku, original_price, sale_price, cart_price, cart_discount, currency, gtin`

## How it works

1. Collection/search page discovery tries the Shopify `products.json`
   endpoint first (fast, no JS rendering), and falls back to rendering +
   parsing the page's HTML for product links - this fallback is what
   handles `/search` pages, since Shopify has no JSON endpoint for search.
2. Each product page is fetched through ZenRows (JS rendering + premium
   proxy, since retail sites commonly bot-protect product pages), and
   `original_price`/`sale_price`/variation/SKU/GTIN are extracted from the
   page's JSON-LD and the Shopify per-product JSON endpoint.
3. `cart_price` is computed directly from `sale_price` and `--discount-pct`
   (no extra network requests).

## Notes

- ZenRows credit usage: roughly 1 request per page discovered + up to 2
  requests per product (JSON probe + rendered HTML). Use `--limit` while
  tuning.
- Respect the site's `robots.txt` and terms of service, and keep request
  concurrency reasonable (`--workers`).
