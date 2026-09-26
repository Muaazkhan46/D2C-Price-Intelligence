# D2C Price & Demand Intelligence Engine

A daily price, discount and stock tracker for five Indian D2C skincare
brands, built from data I collect myself rather than a static dataset —
plus a natural-language SQL assistant over the resulting database.

**Status:** collecting since 2026-08-19, running daily via Windows Task
Scheduler. **38 days of history** (2026-08-19 to 2026-09-25). Pipeline
complete; all three planned analyses (stockout revenue, demand/restock
signals, price leadership) built and run on the full dataset. Numbers
below are the real script output, not estimates.

---

## The problem

Indian D2C skincare brands discount constantly and manage inventory on
instinct. Three things cost real money and nobody measures them:

1. **Discounting is unmeasured.** Nobody knows whether 30% off sells
   meaningfully more than 15%. If it doesn't, the extra 15% is margin
   given away for nothing.
2. **Competitor moves are noticed late.** A rival runs a sale; you find
   out weeks later when your own numbers dip.
3. **Stockouts are invisible.** A product goes out of stock, the listing
   stays live, nothing looks broken. That demand goes to a competitor.
   A sale that doesn't happen leaves no row in any system.

All three are invisible in a normal sales report. That's the point.

## The approach

Take a daily photograph of the shelf — every price, discount and stock
flag across five brands — and store the sequence.

One photograph is worthless; you can see today's price by opening the
website. The value is entirely in the sequence. Enough photographs let
you say *when* something changed, *how long it lasted*, and *what
happened after*.

**Brands:** Pilgrim, Minimalist, Foxtale, The Derma Co, Dot & Key — via
each brand's public Shopify `/products.json` feed.

**Volume:** ~1,900 variants/day tracked, ~1,213 of them clean (non-test,
non-freebie, non-combo) product variants used in the revenue analysis.

---

## Pipeline

```
Shopify feeds
     |
  collect.py    fetch + save raw JSON, one folder per date
     |          (scheduled daily, retries, plausibility check, logs all)
     v
  data/raw/YYYY-MM-DD/{Brand}.json      <- source of truth, never edited
     |
  parse.py      flatten product->variant, flag, categorise
     |          (re-reads ALL days every run; auto-patches known
     |           unrecoverable gaps - see Data Quality Log)
     v
  data/parsed/variants_daily.csv
     |
  load.py       split into star schema, INSERT OR REPLACE
     v
  data/db/price_intel.db
     |
     +---- stockout_revenue.py    revenue lost to stockouts (churn vs persistent)
     +---- demand_forecast.py     stockout trend, restock cadence, at-risk products
     +---- price_leadership.py    who moves price first, by subcategory
     +---- rag/                   ask the database questions in plain English
```

`explore.py` sits outside the pipeline — a scratchpad for inspecting the
data, kept separate so exploration never means editing production code.
The three analysis scripts and the RAG assistant are all **read-only**:
none of them can write to `price_intel.db` or anything in `data/` (see
Design decisions).

### Schema

```
dim_product                      fact_price_daily
-----------                      ----------------
variant_id      (PK)  <--------  variant_id    (FK) |
product_id                       snapshot_date      | PK
brand                            price
product_title                    compare_at_price
variant_title                    discount_pct
product_type                     available
category
subcategory
sku, grams
is_freebie, is_combo
is_test, is_campaign
is_flash_sale
published_at
first_seen, last_seen
```

**Grain:** one row per variant per day, enforced by the composite primary
key, so a duplicate is rejected rather than silently doubling a day.

---

## Design decisions

**Save raw, parse downstream.** `collect.py` never extracts anything. If
extraction logic is wrong, fix `parse.py` and re-run over every day ever
collected. This paid off repeatedly — the flag rules were revised
multiple times and every revision backfilled the full history for free.

**Flag, don't drop.** Freebies, combos, campaign clones and flash-sale
listings are marked with columns, never deleted. Extraction preserves;
interpretation filters. Changing what counts as a "real product" means
changing a `WHERE` clause, not re-running the pipeline.

**Overwrite the dimension; no SCD Type 2.** Considered slowly changing
dimensions for renamed products and chose against it. Type 2 puts a
date-range condition into every join for the rest of the project, in
exchange for historically accurate titles — and the analysis is on price
and stock, not labels. `first_seen` / `last_seen` capture the dates that
do matter (launch, delist) at no join cost.

**Category from title, not `product_type`.** `product_type` is an
internal field each brand abuses differently: Minimalist tags everything
"Skin Care", Pilgrim puts ad campaigns in it ("Liquid Lipstick -
Acquisition - Meta"), and hundreds of rows are blank. Titles are
customer-facing and consistent. Prefer the field the source has an
incentive to keep clean. `subcategory` is only populated for Skincare —
it's `NULL` for Makeup, Haircare, Bodycare, Accessories and Fragrance, so
anything grouped by subcategory (price leadership) only speaks to
Skincare.

**Idempotence throughout.** `collect.py` skips brands already collected
today; `parse.py` rebuilds from scratch; `load.py` uses `INSERT OR
REPLACE`. Running any of them twice gives the same result as once.

**Known-unrecoverable gaps are patched in code, not by hand.** See Data
Quality Log below. A one-off manual fix would be silently undone the
next time `parse.py` rebuilds from raw, since that rebuild is by design
a full rebuild every run. The fix therefore lives inside `parse.py`
itself (`LOST_RAW_PATCHES`), runs automatically forever, and prints a
clearly labelled line so it is never silent or mysterious.

**Every analysis script is read-only by construction, not by discipline.**
`stockout_revenue.py`, `demand_forecast.py` and `price_leadership.py` all
open `price_intel.db` via a `file:...?mode=ro` URI connection — a mistake
that tries to write raises immediately, rather than relying on "just
don't write to it." They only ever write to `reports/`. The RAG assistant
uses the same read-only connection, plus a second, independent
`is_safe_select()` check that rejects anything that isn't a `SELECT`/
`WITH` before it's even run — belt and suspenders, because a
model-generated query is the one kind of SQL in this project that wasn't
written by hand.

---

## Quantified analysis (38 days, 2026-08-19 to 2026-09-25)

This is the answer to the project's three original questions. Each
script is read-only, states its own assumptions in its docstring, and
writes its results to `reports/`.

### 1. Revenue lost to stockouts — `stockout_revenue.py`

**Method:** for every variant-day where `available = 0`, count
`lost_revenue = price × 1 unit/day` (an explicit, adjustable floor
assumption — there's no real sales data here, so this is a conservative
estimate, not a measurement). Variants are split into two groups before
totalling, because they represent different problems:

- **Churn** (1,065 variants) — in stock at least once, cycling in and
  out. This is a genuine stock-management problem and the operationally
  meaningful number.
- **Persistent** (148 variants) — never seen in stock across the whole
  38-day window. Likely dead/discontinued stock rather than a
  restocking failure; reporting it mixed into the churn total would
  overstate "lost sales from poor inventory management."

| Brand | Churn lost revenue | Persistent lost revenue | Combined |
|---|---:|---:|---:|
| Pilgrim | ₹939,731 | ₹1,178,634 | **₹2,118,365** |
| DermaCo | ₹283,140 | ₹633,536 | ₹916,676 |
| Minimalist | ₹128,618 | — | ₹128,618 |
| Dot & Key | ₹62,811 | — | ₹62,811 |
| Foxtale | — | ₹20,710 | ₹20,710 |
| **Total** | **₹1,414,300** | **₹1,832,880** | **₹3,247,180** |

**Headline number: ~₹34.7 lakh estimated lost revenue over 38 days**, at
the conservative floor of 1 unit/variant/day. Pilgrim accounts for 65% of
it — more than DermaCo, Minimalist, Dot & Key and Foxtale combined.

**The rate-vs-volume finding:** DermaCo has the *worst daily stockout
rate* of any brand (27–33% of skincare SKUs out on any given day — see
the demand analysis below), but Pilgrim has the *highest estimated lost
revenue*. Pilgrim stocks out less often, but on more expensive, more
numerous SKUs, and on some that never come back (₹11.8L in persistent
loss alone). Rate and revenue impact are different questions with
different answers — the same shape of finding as the Bengaluru/Guwahati
result in the RTO project.

Top single products by estimated loss: a Pilgrim hair-growth serum
(48172658786533) stuck out of stock for all 38 days at ₹67,379; a set of
seven identical DermaCo foundation shades, each out for all 38 days at
₹34,162 apiece (₹239,134 combined for one discontinued-looking SKU
family). Full lists in `reports/stockout_revenue_top_churn_products.csv`
and `..._top_persistent_products.csv`.

**Methodology note:** an earlier pass in this project manually built a
stricter, gaps-and-islands "resolved spells only" estimate on 36 days of
data (~₹5.8L for Pilgrim), counting only stockouts that had both a
confirmed start *and* confirmed end inside the window. That number is
smaller because it excludes every stockout that was still ongoing at the
end of the window (right-censored) or already ongoing at the start
(left-censored) — which, per the at-risk list below, is most of
Pilgrim's stockout volume. `stockout_revenue.py`'s simpler day-count
method is the one reported here as canonical, because it's the version
that's actually shipped, tested against the full 38-day dataset, and
consistent across the churn/persistent split; the resolved-spells-only
number is a useful lower bound, not a contradiction.

### 2. Demand & restock signals — `demand_forecast.py`

Honest framing up front: 38 days isn't enough for real time-series
forecasting (ARIMA/Prophet need months to find seasonal patterns
reliably). What this measures instead are three signals that *do* hold
up with limited data.

**Stockout rate trend** (first 19 days vs. last 19 days):

| Brand | First half | Second half | Change | Direction |
|---|---:|---:|---:|---|
| Dot & Key | 0.85% | 5.99% | +5.14 pts | worsening |
| DermaCo | 27.58% | 32.54% | +4.96 pts | worsening |
| Pilgrim | 26.40% | 27.50% | +1.10 pts | worsening |
| Foxtale | 0.44% | 0.43% | −0.01 pts | stable |
| Minimalist | 7.19% | 5.77% | −1.42 pts | improving |

Four of five brands are flat or worsening. Minimalist is the only brand
actually getting better at keeping stock — worth revisiting once its
promotional cycle (Finding 1 below) fully plays out, since better
availability during a live discount is itself notable.

**Restock cadence** (average days from going out of stock to coming
back, completed episodes only):

| Brand | Avg. restock days | Completed episodes |
|---|---:|---:|
| DermaCo | 2.8 | 111 |
| Pilgrim | 5.1 | 251 |
| Dot & Key | 9.0 | 20 |
| Minimalist | 10.3 | 16 |

DermaCo has by far the worst *stockout rate* but the *fastest* restocks
— high-frequency, short-duration churn on a wide SKU base. Minimalist
and Dot & Key stock out rarely, but take over a week to recover when
they do. Two different operational patterns that a single "% out of
stock" number would have hidden.

**Currently at risk:** as of 2026-09-25, the longest-running stockouts
are almost entirely Pilgrim products stuck at **38 days out — the full
length of the observation window.** These are left-censored: already out
of stock on day one of collection, so the true stockout length is
unknown and at least 38 days. This is exactly the population the
persistent/churn split above was built to separate out. Full list in
`reports/currently_at_risk.csv`.

### 3. Competitive price leadership — `price_leadership.py`

**Method:** no product-to-product competitor mapping exists in this
data, so leadership is measured at the subcategory level — for each
Skincare subcategory, did a brand change *any* of its prices on a given
day? For every brand pair sharing a subcategory, the nearest matching
move within a ±5-day window gives a signed lag; averaging the lag across
all matched events says who tends to move first. Pairs need ≥15 matched
events to be trusted rather than flagged.

**Result: 0 of 20 brand-pair/subcategory combinations reached the
confidence threshold.** Every single one is reported as `INSUFFICIENT
DATA` — including the closest cases (Cleanser and Moisturizer,
Minimalist–Pilgrim, 13 matched events each; just short of 15). This
is the honest answer, not a null result to hide: it means there isn't
yet a statistically defensible "Brand X leads, Brand Y follows" claim
anywhere in this dataset, and it will take more days of history, not a
different method, to get one.

The *reason* it's inconclusive is itself a finding:

| Subcategory | Pilgrim | Minimalist | DermaCo | Dot & Key | Foxtale |
|---|---:|---:|---:|---:|---:|
| Serum | 11.73/variant | 11.67/variant | 0.00 | 0.00 | 0.02 |
| Moisturizer | 11.14 | 13.00 | 0.05 | 0.12 | 0.03 |
| Sunscreen | 12.64 | 13.00 | 0.00 | 0.67 | 0.00 |
| Toner | 15.00 | 13.00 | 0.00 | 0.00 | 0.00 |

Pilgrim and Minimalist change price 8–15 times per variant over 38 days;
DermaCo and Foxtale are essentially static (0.00–0.03), Dot & Key mostly
static with one exception (Sunscreen, 0.67). A leader/follower signal
needs both sides of a pair to actually move — and three of the five
brands barely do, on listed price. That absence of movement is a
reportable result on its own: those brands most likely compete on
`compare_at_price`/checkout discounts rather than sticker price, which
this feed can't see (see Limitations).

---

## The RAG assistant (`rag/`)

A natural-language interface over `price_intel.db`, so a question like
*"how much revenue did stockouts cost Pilgrim?"* doesn't require writing
SQL by hand.

**Architecture:**

1. `build_index.py` — indexes a curated `knowledge_base.json` (table/
   column docs, a glossary of flags, and worked question→SQL example
   pairs) into a persistent ChromaDB vector store.
2. `query.py` (CLI) / `app.py` (Streamlit) — retrieves the 5 most
   relevant chunks for the question, then asks GLM 4.5 (via OpenRouter)
   to respond in exactly one of two modes:
   - **`SQL:`** for data questions — generates a `SELECT`/`WITH` query
     against the real schema.
   - **`EXPLANATION:`** for methodology questions ("how is lost revenue
     calculated", "why exclude combos") — answered in plain English from
     the knowledge base, not forced into a query that doesn't exist.
3. The generated SQL runs through **two independent safety layers**
   before touching the database: a read-only `file:...?mode=ro`
   connection that structurally cannot write, and a separate
   `is_safe_select()` regex check that rejects anything that isn't a
   clean `SELECT`/`WITH` — including a second scan for `INSERT`,
   `DROP`, `ATTACH`, `PRAGMA` and similar, in case a generated query
   tries to hide one in a comment.

The system prompt also embeds the project's specific query patterns
directly — the default flag-exclusion rule (exclude test/freebie/combo/
campaign/flash-sale unless the question is about one of them) and the
correct two-`ROW_NUMBER()` gaps-and-islands pattern for streak/duration
questions — so the model doesn't have to rediscover conventions this
project already worked out by hand.

**Known limitation:** `knowledge_base.json` was built against an earlier
snapshot (22 days, 1,871 products, 35,943 rows) and is now stale relative
to the live 38-day, ~1,914-variant database. Generated SQL still runs
correctly against live data — the schema and flags haven't changed — but
any row/date counts *stated in the KB's own text* are out of date.
**To do:** re-run `build_index.py` now that the dataset has stabilised.

---

## Early findings (first 11 days: 2026-08-19 to 2026-08-29)

These are the qualitative, mechanism-level findings from before enough
history existed to quantify revenue and trend. They still hold — the
quantified analysis above builds on top of them, it doesn't replace them.

### Three brands, three completely different promotional mechanics

| Brand | Mechanism | Evidence |
|---|---|---|
| **Minimalist** | Genuine time-boxed price cut on real SKUs | All ~85 SKUs dropped 5% on 20 Aug, deepened to 10% on 22 Aug. `compare_at_price` unchanged throughout - MRP held, price fell. |
| **Pilgrim** | Clone the catalogue into separate discounted listings, twice | Cycle 1: 95 variants at ₹7-277, published in 80 seconds on 24 Aug, deleted within 24 hours. Cycle 2: 19 variants on 28 Aug, also deleted within a day. |
| **Pilgrim / Foxtale** | Permanent inflated reference price | 136 products at 48.6% off, unchanged for 11 consecutive days. Foxtale: 7 products at 23.0%, identical every day. |

The third matters analytically: **a discount that never changes is not a
discount, it's a price position.** The measurable signal is *change* in
discount, not discount level.

### Two complete promotional cycles, captured start to finish

Pilgrim's flash-sale behaviour, fully observed twice: launched 24 Aug
14:50-14:52 (95 variants, ₹7/77/177/277 ladder tracking product value),
gone by 25 Aug; launched again 28 Aug (19 variants), gone by 29 Aug. Two
independent one-day cycles is a documented operating rhythm, not a
one-off.

**Note on a related but distinct pattern:** on 28 Aug, ~33 *additional*
Pilgrim makeup variants appeared at `first_seen = 2026-08-28`, at **zero
discount** and prices identical to existing listings — almost certainly
the same products re-issued under new `variant_id`s (a Shopify re-sync),
not new launches. Distinguishing the two: a genuine flash event shows a
price signal; an ID-swap re-issue shows the exact same price as the item
it replaced. This is a real limitation of using `variant_id` as a stable
key (see Limitations).

### Pilgrim manages promotions by bulk create-and-delete

Investigating a 589→501 variant drop on 23 Aug (see Data Quality Log)
showed almost the entire deleted set was promotional scaffolding — ad-
channel clone listings, gift-with-purchase accessories, campaign and
staging listings — not real products. **Pilgrim's clean skincare count
stayed constant across every one of these events**, confirming the
classification isolated the noise as intended.

### Nobody responded to Minimalist's sale

Minimalist went from 1 discounted product to 82 overnight. Over the same
window Pilgrim, Foxtale, DermaCo and Dot & Key held their pricing exactly
constant. No competitive response within 11 days — consistent with the
price-leadership analysis above finding no measurable leader/follower
relationship at 38 days either.

### Catalogue composition differs sharply

Dot & Key: ~39% of skincare SKUs are sunscreen — a specialist position.
Foxtale: roughly half of all listings are gift-with-purchase items, not
products for sale. Pilgrim: skincare is a minority of the catalogue
(makeup and haircare are larger). Comparing brand-level average prices
without categorising first is meaningless — the differences are product
mix, not pricing strategy.

---

## Limitations

**No sales data.** The public feed exposes what is listed, not what is
bought. Every revenue figure above is an estimate under an explicit,
adjustable assumption (1 unit sold/day/variant when in stock) — the
brand-to-brand *ranking* is more trustworthy than the absolute rupee
figure. Catalogue size is not sales.

**Discount visibility is uneven.** DermaCo and Dot & Key rarely populate
`compare_at_price`, so they show near-zero discounts and near-zero price
movement. That's a data limitation, not necessarily a business fact —
this is also the likely explanation for why they show no measurable
price-leadership signal (Finding 3 above): they may compete on
checkout-level discounts this feed can't see.

**`variant_id` is not a perfectly stable key.** Pilgrim has been observed
re-issuing existing products under new `variant_id`s with unchanged
prices. This can register false launches/delistings via `first_seen`/
`last_seen`. The distinguishing signal is price: a genuine launch or
promotion carries a price signal; an ID-swap re-issue does not.

**Left- and right-censoring in the stockout data.** Many of Pilgrim's
longest stockouts (the entire "currently at risk" top of the list) were
already out of stock on day one of collection or still out at the end —
the true duration is unknown in both directions. The persistent/churn
split partially addresses this; a full survival-analysis treatment would
be more rigorous but wasn't necessary to answer the project's questions.

**Rule-based classification.** Category, freebie, combo, campaign and
flash-sale flags are keyword rules with thresholds I chose (e.g.
`MIN_REAL_PRICE = 50`). A small fraction of variants remain unclassified
("Other"), verified as scattered rather than concentrated in one brand.

**RAG knowledge base is stale relative to the live database** (see RAG
section above) — a known, tracked gap, not an unnoticed one.

**Observational, outsider data.** No visibility into margins, marketing
spend or actual inventory. Conclusions are about observable behaviour,
not internal strategy.

---

## Data quality log

### 2026-08-23 — Pilgrim variant count drop: two wrong diagnoses before the right one

Pilgrim's raw variant count fell 589→501 on 23 Aug. This number came up
three times before it was correctly explained.

**Diagnosis 1 (wrong): partial collection.** Reasoned a same-day
`ConnectionError` must have caused the drop — correlation treated as
cause, without checking that the retry had actually succeeded.

**Diagnosis 2 (also wrong): explained by the 25 Aug flash-sale
teardown** (596 − 95 = 501 arithmetic). But the flash sale didn't exist
yet on 23 Aug, so it couldn't explain the earlier date.

**Correct diagnosis: a genuine bulk deletion of promotional listings.**
Confirmed by querying `last_seen = '2026-08-22'` and inspecting *which*
variants disappeared — ad-campaign clones, GWP items, stale staging
listings, not an arbitrary scatter — and by requesting page 2 of the
live feed directly, which returned `{"products":[]}`, confirming the
smaller catalogue was current and real.

**Lesson:** when a number moves, don't explain it from summary
statistics or the most recent coincidence. Look at *which rows* changed.

### 2026-08-29 — Pilgrim's raw file for 2026-08-25 deleted in error, permanently

While debugging an unrelated `SUSPECT` flag for 29 Aug, the wrong file
was deleted: `data/raw/2026-08-25/Pilgrim.json` instead of the intended
29 Aug file. Shopify serves only the current catalogue, so this raw file
is permanently unrecoverable.

**What was not lost:** `parse.py` had already processed this file before
deletion, so the flattened rows existed in `variants_daily.csv` and were
backed up immediately (`variants_daily_backup_aug25.csv`).

**The complication:** `parse.py` rebuilds `variants_daily.csv` entirely
from `data/raw/` every run by design — a one-off manual merge would be
silently overwritten on the very next run.

**The fix:** the restoration logic lives permanently inside `parse.py`
as a self-checking patch (`LOST_RAW_PATCHES` / `apply_lost_raw_patches`).
Each run checks whether the raw folder now exists (raw always wins) and
whether the rows are already present (does nothing if so); only when
both checks fail does it pull the missing rows from the backup and print
`[known gap - auto-restored] Pilgrim/2026-08-25: 501 rows recovered from
backup` — visible, not silent.

**Impact on analysis:** none. Pilgrim's skincare category was unaffected
and the restored rows carry the same values they always had.

**Lesson:** a fix applied once, by hand, to a file that gets regenerated
every run does not stay fixed. Automatic pipelines need automatic,
self-checking patches for permanent gaps — and the patch should always
announce itself, because a quiet fix is indistinguishable from no fix at
all to anyone reading the output later.

### Collector hardening

`collect.py` compares each brand's variant count against the previous
day and logs `ok_suspect` rather than `ok` on a drop over 10%, or when
pagination was cut short. **Known false-positive mode:** it can't
distinguish a broken collection from a real catalogue change — it fired
correctly on both the 23 Aug bulk deletion and the flash-sale teardowns,
all of which were real, not broken. Acceptable: a false alarm costs
thirty seconds of checking; a silent partial costs a day of data
permanently. The general principle, reinforced twice: **"did the fetch
succeed?" and "is the result believable?" are different questions.**

### Minor, unfixed

One row ("App Test Product (Copy)", 2026-08-21) is flagged as both
`is_test` and `is_flash_sale`. Both are excluded from the clean set, so
no reported number is affected.

---

## Still to do

- [x] ~~Stockout spells~~ — done (`stockout_revenue.py`, `demand_forecast.py`)
- [x] ~~Competitive price leadership~~ — done (`price_leadership.py`),
      honest null result at 38 days
- [ ] Re-run `rag/build_index.py` so the knowledge base's stated counts
      match the live 38-day database
- [ ] Comparable-set price benchmarking (e.g. 30ml vitamin C serums
      across brands)
- [ ] Test whether discount depth correlates with stockout frequency —
      the closest available proxy for elasticity
- [ ] Verify the flash-sale mechanism by checking `body_html` for cart
      threshold terms, rather than inferring it
- [ ] Deploy the Streamlit RAG assistant as a hosted demo
- [ ] Power BI dashboard
- [ ] Automated weekly Excel brief

---

## Running it

```bash
pip install requests pandas

python collect.py             # fetch today's snapshot (scheduled daily, 20:00)
python parse.py                # rebuild the flat table from all raw days
python load.py                  # rebuild the SQLite star schema

python stockout_revenue.py     # revenue lost to stockouts, churn vs persistent
python demand_forecast.py      # stockout trend, restock cadence, at-risk products
python price_leadership.py     # who moves price first, by subcategory
```

`collect.py` runs on Windows Task Scheduler with catch-up enabled, so a
missed evening fires on next boot. All three pipeline scripts are safe
to re-run; all three analysis scripts are read-only and only write to
`reports/`.

**RAG assistant:**

```bash
cd rag
pip install -r requirements.txt
# create a .env file with OPENROUTER_API_KEY=...
python build_index.py           # build the vector index (run once, or after schema/KB changes)
python query.py "how much revenue did stockouts cost Pilgrim?"
# or, for the chat UI:
streamlit run app.py
```