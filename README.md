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
- `cart_price` - the final price after simulating add-to-cart + applying the
  `welcome30` discount code, via Shopify's cart AJAX API

**Caveat:** the cart-price simulation is unverified against the live site -
built and mock-tested against numbers from a screenshot ($25.41 -> $17.79 via
welcome30, math checks out), but it depends on ZenRows correctly forwarding
custom headers/cookies to the target site. Run a `--limit 3` smoke test
first and check that `cart_price` actually comes back populated before a
full run.

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
| `--discount-code` | `WELCOME30` | Discount code applied to the simulated cart (pass `""` to skip) |
| `--skip-cart-price` | off | Skip the add-to-cart simulation (saves ~2 ZenRows requests/variant) |
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
3. `cart_price` is captured by simulating add-to-cart (`POST /cart/add.js`),
   applying the discount code (`POST /cart`), then reading the final price
   (`GET /cart.js`).

## Notes

- ZenRows credit usage: roughly 1 request per page discovered + up to 4-5
  requests per product (JSON probe, rendered HTML, add-to-cart, discount,
  cart read). Use `--limit` and `--skip-cart-price` while tuning.
- Respect the site's `robots.txt` and terms of service, and keep request
  concurrency reasonable (`--workers`).
