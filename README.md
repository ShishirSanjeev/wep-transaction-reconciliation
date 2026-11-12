# WEP transaction reconciliation

Reconciles the Winwize **transaction history** against what was actually **recommended** (order data) and what
actually **executed at the broker** (brokerage report), and produces a corrected transaction statement.

## Why

A recommendation is recorded in the transaction history only when the client executes it through the smallcase
gateway, which sends a webhook. Clients sometimes execute a recommendation **on their own**, outside the
gateway. Webhooks also fail, are duplicated, or record the wrong quantity or price. The transaction history
therefore drifts from reality.

This tool takes the broker as the source of truth for *what executed*, and the order data as the source of truth
for *what we recommended*. When the two agree, the trade is our recommendation, whether or not a webhook exists.
The transaction history is then corrected to match.

## How it works

```
order data ─────────┐
brokerage report ───┼─► 1. annotate ─► transactions with action ─┐
transaction history ┘    (wep-annotate)    (review in Excel)       ├─► 2. apply ─► reconciled_statement.csv
transaction history (stale) ──────────────────────────────────────┘    (wep-apply)
```

**Component 1, annotate.** It finds the *good orders*, then labels every transaction row with the action needed.
The output has the transaction file's own columns plus `Reconciliation Action` and `Action Detail`.
For rows that must change, the transaction columns already hold the corrected values.
It can also write a three-source comparison: broker | order | transaction history, one row per good order.

**Component 2, apply.** It takes only the stale transaction file and the action file. It goes through each order
bundle (`Request Id`) and applies the actions, then writes the reconciled statement as CSV, in the original
transaction format.

Splitting the work this way lets a person review or edit the action file before anything is applied.

## Installation

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e .                  # installs the wep-annotate / wep-apply commands
# or just: pip install -r requirements.txt   and use the two scripts in the repo root
```

## Usage

Put the input files in `data/`. That folder is git-ignored; see [Data and privacy](#data-and-privacy).

### 1. Annotate

```bash
wep-annotate --orders data/order_stocks.csv \
    --transactions "data/<Client> - Transaction History.xlsx" \
    --brokerage "data/WWW2-Equity-transaction-report-<timestamp>.xlsx" \
    --out "outputs/<Client> - Transactions with action.xlsx" \
    --three-source "outputs/<Client> - Three source.xlsx"      # optional
```

| Option | Default | Purpose |
|---|---|---|
| `--orders` | required | Winwize order data, one row per order line (`order_stocks.csv`) |
| `--transactions` | required | Client's Winwize transaction history (xlsx or csv) |
| `--brokerage` | required | Client's Angel equity transaction report (`WWW2-…xlsx`) |
| `--out` | `<transactions> - with action.xlsx` | Action file (`.xlsx` or `.csv`) |
| `--three-source` | not written | Optional broker / order / history comparison |
| `--client-id` | looked up by name | Winwize customer id, if the name lookup is ambiguous |
| `--start`, `--end` | broker report's date range | Reconciliation period (inclusive, `YYYY-MM-DD`) |
| `--max-day-lag` | `1` | Calendar days after the recommendation in which execution is accepted |

The client is identified from the brokerage report header (`Client Name`) and checked against the order data.

### 2. Apply

```bash
wep-apply "data/<Client> - Transaction History.xlsx" \
    "outputs/<Client> - Transactions with action.xlsx" \
    -o "outputs/<Client> - reconciled_statement.csv"
```

Without installing, run `python annotate_transactions.py …` and `python apply_actions.py …` with the same arguments.

### From Python

```python
from wep_recon import PipelineInputs, ReconciliationPipeline, ActionApplier
from wep_recon.tables import read_table

pipeline = ReconciliationPipeline(PipelineInputs(orders, transactions, brokerage))
annotated = pipeline.run()
pipeline.matches.good_orders        # confirmed recommendation lines
pipeline.matches.unmatched_orders   # recommendations with no exact broker match (with reason)
pipeline.matches.unmatched_broker   # broker trades not attributed to a recommendation
pipeline.write_actions('actions.xlsx')
pipeline.write_three_source('three_source.xlsx')

statement = ActionApplier().apply(read_table(transactions), annotated.export_frame()).statement
```

## Actions

| `Reconciliation Action` | When | Component 2 does |
|---|---|---|
| `no change` | Matches the order and the broker; or a failed, blank or out-of-period row; or a split-adjusted record | keeps the stale row |
| `reconcile - qty mismatch` / `price mismatch` / `date mismatch` | Order is broker-confirmed but the record is wrong. Labels can combine, e.g. `reconcile - qty mismatch, price mismatch` | replaces the row with the corrected row; drops it if the corrected `Quantity` is 0 (excess fill) |
| `missing` | Broker-confirmed order with no successful record: never recorded, or reported failed | adds a new row (`Transaction Id` = `RECON-…`, `Order Placed By` = `reconciliation`) |
| `duplicate` | Status is `Duplicate`, or an identical repeat of an earlier successful row | removes the row |
| `review - not broker confirmed` | Successful row with no matching good order | keeps the row; needs a manual decision |

`Action Detail` explains each row, e.g. `Quantity 5 -> 11; Amount 550 -> 1,210. Broker: 11 @ Rs 110.00 …`.

## Rules

**Good order.** One recommendation line is one `Request Id` + scrip + side. It is good when a broker trade has the
same scrip, the same side and exactly the `requested_qty`:
- The trade is on the recommendation date or within `--max-day-lag` calendar days after it. Same-day matches take priority.
- Broker rows are summed per trade date + scrip + side, and each such total is used once.
- If two recommendations compete for the same broker trade, neither is chosen; both are listed in `unmatched_orders`.
- Webhook data is never used to decide this.

Symbols are normalised: whitespace is removed, as are the series suffixes `-EQ`, `-BE`, `-BZ`, `-BL`, `-RR` and `-IV`.

**Broker execution value.** Angel's `Buy Value` includes brokerage and `Sell Value` is net of it. The execution
value is therefore `Buy Value − Brokerage` or `Sell Value + Brokerage`, and the price is that value ÷ quantity.
Corrected and added rows use this price.

**Price mismatch.** Flagged when the recorded amount differs from the broker execution value by more than
max(Rs 5, 0.1%), or when a row's `Quantity × Share Value ≠ Amount`, or its price is missing or zero.

**Quantity mismatch with several fills.** The last fill absorbs a shortfall. An excess is trimmed from the
last fills; a fill trimmed to 0 is removed.

**Corporate actions.** If the recorded quantity is a whole multiple of the broker quantity (split/bonus
adjusted) and the amount still matches the broker, the row is left as recorded and labelled in `Action Detail`.

**Safety checks in component 2.** Rows are linked by `Sl No.` and verified against `Transaction Id`. An action
file built from a different transaction file is rejected. Unknown action labels are rejected, as is a stale row
with no action. Values of `no change` and `review` rows are always taken from the stale file.

## Known limitations

- The next-day window counts calendar days, not trading days: a Friday recommendation executed on Monday is not matched.
- Broker rows are matched as daily totals. If the same scrip is traded twice on one day, for two
  recommendations or one recommendation plus the client's own trade, the total matches neither.
- The brokerage report has no execution time, so `Request Time` is blank on added rows.
- Recommendations that were not executed exactly cannot be confirmed (`unmatched_orders`). Partial fills fall in this group.

## Input formats

| Source | Expected |
|---|---|
| Order data | CSV with at least `order_id, created_at, customer, customer_name, nse_code, stock_name, buy_sell, requested_qty` (`req_type` used if present) |
| Transaction history | Columns `Sl No., Request Id, Request Date (dd.mm.yyyy), Request Time, Transaction Id, Stock Id, Order Placed By, Share Value, CMP, Action, transactiontype, Request Type, Quantity, Product Type, Amount, Status`; `Sl No.` unique |
| Brokerage report | Angel export: header lines `Client Code :`, `Client Name :`, `Date Range: YYYY-MM-DD-YYYY-MM-DD`, then a table from `Sauda Date` with `Scrip Code, Scrip Name, Buy/Sell Quantity, Buy/Sell Value, Brokerage`; a `Total` row is ignored |

## Project layout

```
wep_recon/
  sources.py     BrokerageReport, OrderBook, TransactionHistory, Period - load and normalise inputs
  matching.py    OrderMatcher - find good orders
  actions.py     TransactionAnnotator, Action, PriceTolerance - label each transaction
  applier.py     ActionApplier - component 2
  reports.py     ThreeSourceReport - optional comparison sheet
  pipeline.py    ReconciliationPipeline - component 1 end to end
  cli.py         wep-annotate / wep-apply
  normalize.py   value normalisation helpers
  tables.py      csv / xlsx read and write
annotate_transactions.py, apply_actions.py   script wrappers around cli.py
tests/test_wep_recon.py
```

## Tests

```bash
python -m unittest discover tests -v
```

The unit tests use synthetic data. To also run a round trip on real client files (annotate → apply → check that
every good order's recorded quantity equals the broker quantity), point `WEP_DATA_DIR` at a folder containing
`order_stocks.csv`, the `* - Transaction History.xlsx` files and the `WWW2-*.xlsx` reports. Files are paired by
the client name inside them.

```bash
set WEP_DATA_DIR=C:\path\to\data        # PowerShell: $env:WEP_DATA_DIR="C:\path\to\data"
python -m unittest discover tests -v
```

## Data and privacy

The input and output files contain clients' names, holdings and trades. `data/` and `outputs/` and all
`*.xlsx` / `*.csv` files are git-ignored. Keep it that way, and don't put client names or figures in code,
tests or issues.
