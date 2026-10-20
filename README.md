# Moida Scraper

Scrapes [moidaus.com](https://moidaus.com) (Shopify) product **variations**,
**prices**, and **GTIN**, using [ZenRows](https://www.zenrows.com/) to handle
proxying/JS rendering. Forked from the shared scraper in the `Web-Scraping`
repo into its own repo so Moida-specific changes never risk affecting the
Vitacost scraper, or vice versa.

Works against either a `/collections/<handle>` page or a `/search?q=...`
results page on Moida.

## Carl's workflow (step-by-step)

This is the actual repeatable process, start to finish, for scraping the
approved-brand list and turning it into a ranked opportunity report.

1. **Pull the latest code.**
   ```powershell
   git pull
   ```

2. **(Optional) Update `approved_brands.txt`** if the approved-brand list
   changed -- one brand name per line, `#` for comments. Commit/push after
   editing so it's not lost.

3. **Run the scrape**, scoped to the approved-brand list (much cheaper than
   a full `/collections/all` run -- see Notes below on credit cost):
   ```powershell
   py scrape.py --vendor-file approved_brands.txt --workers 8
   ```
   (Use `py` instead of `python` if you get a "cannot find the path
   specified" error -- see Troubleshooting below.)

   Watch the log for:
   - `Found N product URLs` near the top -- sanity-check N is in the
     expected ballpark (not drastically lower than your last run with the
     same brand list).
   - Any `WARNING ... stopping discovery early` or `... after retries`
     line -- either means the result is **incomplete**. Don't push an
     incomplete scrape through ScanUnlimited/Keepa -- just rerun it.

   Output lands in `output/moidaus_all-<N>brands_<timestamp>.csv` (and
   `.json`).

4. **Upload the CSV to ScanUnlimited.** On the scan-detail form, set:
   - **Search By**: `UPC`
   - **Product ID**: the `gtin` column
   - **Cost**: the `cart_price` column
   - **Currency**: USD, **Marketplace**: US, **Condition**: New
   - Leave **Custom** blank and **DST Registered?** unchecked

   Download the resulting `ScanUnlimited-<filename>-<id>.csv` export.

5. **Push the same GTINs through Keepa** and download its export
   (`KeepaExport-<date>-ProductViewer.csv`).

6. **Run `master.py`** to merge the scrape + ScanUnlimited + Keepa into one
   ranked report:
   ```powershell
   py master.py --vitacost "output\moidaus_all-<N>brands_<timestamp>.csv" --scanunlimited "output\ScanUnlimited-<...>.csv" --keepa "output\KeepaExport-<date>-ProductViewer.csv" --source-name Moida --output "output\master_moidaus_all-<N>brands_<date>.xlsx"
   ```

7. **Review the output**: the `Master` sheet holds clean, ranked
   opportunities; `Needs Review` holds flagged rows (ambiguous match, bundle
   mismatch, etc.) worth a manual look before acting on them.

### Troubleshooting

- **`Program 'python.exe' failed to run: ... cannot find the path
  specified`** -- `python` isn't linked into PATH on this machine; use `py`
  instead (`py scrape.py ...`, `py master.py ...`).
- **Scrape finishes but row count looks way too low** for the brand list
  size -- check the log for a `WARNING` about discovery stopping early or a
  per-product fetch failing after retries; rerun the scrape rather than
  trusting a result that logged one of those.
- **Running low on ZenRows credits** -- a single product-page fetch
  (JS render + premium proxy) can run ~25 credits, not the ~1-2 the Notes
  section below assumes for a "cheap" request -- check your ZenRows
  dashboard's credit balance before a big run, and prefer the approved-brand
  scoped scrape (`--vendor-file approved_brands.txt`) over a full
  `/collections/all` run.
- **ScanUnlimited match rate looks suspiciously low** (most rows blank
  ASIN) -- double check **Search By** was set to `UPC` on the scan form, not
  ESBN or another code type. Even with that set correctly, a lot of niche
  Korean-domestic brands simply won't match, since their barcode (GS1 Korea
  prefix `880`) often isn't the same code Amazon's US listing uses -- that's
  expected, not a bug.

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

**Workaround:** discovery now defaults to `/collections/all` (every product
on the store) filtered by `--vendor-filter` (default `Medicube`), instead of
`/search`. `/collections/all/products.json` is public and cheap (no JS
rendering), and each product entry includes a `"vendor"` field - so this
walks the whole catalog's lightweight JSON and keeps only vendor-matching
products, sidestepping the `/search` block entirely. Change `--vendor-filter`
to target a different brand, or pass `--vendor-filter none` to keep every
product on a `--collection-url` you point at directly (e.g. a real
`/collections/<handle>` page, if one exists for what you want).

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

Full run (default: every Medicube product on moidaus.com):

```bash
python scrape.py
```

Full catalog scrape (every vendor, all ~2475 products on `/collections/all`):

```bash
python scrape.py --vendor-filter none
```

Target a different brand or collection:

```bash
python scrape.py --vendor-filter "Anua"
python scrape.py --collection-url https://moidaus.com/collections/awards-moida-2026-mid-year-awards-_event --vendor-filter none
```

Confirmed working single-brand collection pages (skip the `/collections/all` vendor-filter walk entirely, since the brand already has its own dedicated collection page):

```bash
python scrape.py --collection-url https://moidaus.com/collections/celimax --vendor-filter none
python scrape.py --collection-url https://moidaus.com/collections/medicube --vendor-filter none
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--collection-url` | `https://moidaus.com/collections/all` | Full URL of the collection page to scrape |
| `--vendor-filter` | `Medicube` | Only keep products whose Shopify `vendor` field matches (case-insensitive); pass `""` to keep everything |
| `--max-pages` | 15 | Max `products.json` pages to walk when discovering products (250 products/page; 15 pages = 3750 products, covering `/collections/all`'s ~2475 products with headroom for catalog growth) |
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

`product_name, product_url, variation, sku, original_price, sale_price, cart_price, cart_discount, currency, gtin, gtin_suspect`

`gtin_suspect` is `"yes"` when `gtin` is non-empty but not a plain 8-14 digit
barcode (e.g. `880SG00001710`) - a handful of Moida listings (bundle boxes,
sachets) put an internal SKU-shaped code in the GTIN field instead of a real
barcode, which would otherwise silently fail to match in downstream
ScanUnlimited/Keepa lookups.

## How it works

1. Collection page discovery walks `products.json` (fast, no JS rendering,
   250 products per page) and keeps only entries matching `--vendor-filter`;
   falls back to rendering + parsing HTML for product links only if a
   collection has no `products.json` endpoint at all (this fallback can't
   filter by vendor).
2. Each product page is fetched through ZenRows (JS rendering + premium
   proxy, since retail sites commonly bot-protect product pages), and
   `original_price`/`sale_price`/variation/SKU/GTIN are extracted from the
   page's JSON-LD and the Shopify per-product JSON endpoint.
3. `cart_price` is computed directly from `sale_price` and `--discount-pct`
   (no extra network requests).

## Matching against Amazon: master.py

After scraping a brand's products and pushing the resulting GTINs through
ScanUnlimited and Keepa, merge all three into one ranked report:

```bash
python master.py --vitacost output/moidaus_kundal_20260831_104821.csv --scanunlimited ScanUnlimited_export.csv --keepa KeepaExport.csv --source-name Moida --output output/master_kundal.xlsx
```

(`--vitacost` is just the flag name kept for compatibility with the sibling
`Web-Scraping` repo's copy of this script - pass any scrape CSV there,
Moida's included.) This is a plain copy of `Web-Scraping`'s `master.py`,
kept here too so both projects can run from one folder. See that repo's
copy for the full matching/flagging logic docs (GTIN matching, sell-price
fallback chain, `no_buy_box`/`bundle_mismatch`/etc. flags) - the logic is
identical, just kept in sync manually between the two repos.

## Merging multiple brand CSVs

Running `scrape.py` once per brand produces separate CSVs. To submit them
to ScanUnlimited/Keepa/master.py as one batch instead of per brand:

```bash
python merge_csv.py output/moidaus_all-Medicube_*.csv output/moidaus_all-Celimax_*.csv --output output/moidaus_combined.csv
```

## Notes

- ZenRows credit usage: roughly 1 cheap request per 250 products walked
  during discovery + up to 2 requests per matched product (JSON probe +
  rendered HTML). Use `--limit` while tuning.
- Respect the site's `robots.txt` and terms of service, and keep request
  concurrency reasonable (`--workers`).
