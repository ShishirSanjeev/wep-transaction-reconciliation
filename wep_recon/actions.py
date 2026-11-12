"""Stage: label every transaction row with the action needed to reconcile it.

The annotated file keeps the transaction data's own columns and adds two:

* ``Reconciliation Action`` - one of the labels below.
* ``Action Detail``         - what changes and why (old -> new values, broker evidence).

For ``reconcile - ...`` and ``missing`` rows the transaction columns already hold the
*corrected* values, so the applier needs nothing but this file and the stale data.

Labels
------
no change                      keep the stale row as it is
reconcile - <issues>           replace the stale row with this row (issues: qty / price / date mismatch).
                               A reconciled Quantity of 0 means the row is an excess fill and is removed.
missing                        broker-confirmed recommendation with no successful record: add this row
duplicate                      remove the stale row (source-marked Duplicate, or repeated success)
review - not broker confirmed  successful row not backed by a good order; kept unchanged, needs a human
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from . import normalize as nz
from .matching import MatchResult
from .sources import BrokerageReport, OrderBook, Period, TransactionHistory

ACTION_COLUMN = 'Reconciliation Action'
DETAIL_COLUMN = 'Action Detail'
DATE_FORMAT = '%d.%m.%Y'  # matches the transaction export's 'Request Date'
FAILED_STATUSES = {'failed', 'failure', 'cancelled', 'canceled', 'rejected'}


class Action(str, Enum):
    NO_CHANGE = 'no change'
    RECONCILE = 'reconcile'
    MISSING = 'missing'
    DUPLICATE = 'duplicate'
    REVIEW = 'review - not broker confirmed'

    @classmethod
    def parse(cls, label) -> 'Action':
        label = nz.text(label).lower()
        if label.startswith(cls.RECONCILE.value):
            return cls.RECONCILE
        for action in cls:
            if label == action.value:
                return action
        raise ValueError(f'Unknown {ACTION_COLUMN}: "{label}"')


class Issue(str, Enum):
    QTY = 'qty mismatch'
    PRICE = 'price mismatch'
    DATE = 'date mismatch'


def reconcile_label(issues) -> str:
    return f'{Action.RECONCILE.value} - ' + ', '.join(i.value for i in issues)


@dataclass(frozen=True)
class PriceTolerance:
    """Allowed gap between recorded amount and broker execution value (broker values are rounded to Rs 1)."""
    absolute: float = 5.0
    relative: float = 0.001

    def allowance(self, value: float) -> float:
        return max(self.absolute, self.relative * abs(value))


@dataclass
class AnnotatedTransactions:
    frame: pd.DataFrame          # transaction columns + action columns + internal '_line_id'
    transaction_columns: list

    def export_frame(self) -> pd.DataFrame:
        return self.frame[[*self.transaction_columns, ACTION_COLUMN, DETAIL_COLUMN]]

    def summary(self) -> pd.Series:
        return self.frame[ACTION_COLUMN].value_counts().rename('rows')


def _fmt(value) -> str:
    value = nz.tidy_number(value)
    return '-' if value is None else f'{value:,}' if isinstance(value, int) else f'{value:,.2f}'


class TransactionAnnotator:
    def __init__(self, tolerance: PriceTolerance | None = None):
        self.tolerance = tolerance or PriceTolerance()

    # ------------------------------------------------------------------ public
    def annotate(self, history: TransactionHistory, orders: OrderBook, broker: BrokerageReport,
                 period: Period, matches: MatchResult) -> AnnotatedTransactions:
        n = history.norm
        out = history.raw.copy()
        out[ACTION_COLUMN] = None
        out[DETAIL_COLUMN] = ''
        out['_line_id'] = n.line_id
        out['_position'] = out.index.astype(float)

        repeats = self._repeated_successes(n, period)
        successes = []
        for i, row in n.iterrows():
            if pd.isna(row.date):
                self._set(out, i, Action.REVIEW.value, 'Request Date unreadable; cannot place in period')
            elif not period.contains(pd.Series([row.date])).iloc[0]:
                self._set(out, i, Action.NO_CHANGE.value, f'Outside reconciliation period ({period})')
            elif row.status == 'duplicate':
                self._set(out, i, Action.DUPLICATE.value, 'Marked Duplicate in source; remove')
            elif i in repeats:
                self._set(out, i, Action.DUPLICATE.value,
                          f'Repeats successful row Sl No. {repeats[i]} (same id, date, scrip, side, qty, price, amount); remove')
            elif row.status != 'success':
                self._set(out, i, Action.NO_CHANGE.value, f'{row.status.capitalize() or "Blank"} status; not an execution')
            else:
                successes.append(i)

        known_lines = set(orders.lines.line_id)
        for line_id, rows in n.loc[successes].groupby('line_id', sort=False):
            if matches.is_good(line_id):
                self._reconcile_line(out, rows, matches.good_orders.loc[line_id])
            else:
                reason = ('Recommendation exists but no exact same/next-day broker execution'
                          if line_id in known_lines else 'No matching recommendation line in order data')
                for i in rows.index:
                    self._set(out, i, Action.REVIEW.value, reason)

        recorded = set(n.loc[successes, 'line_id'])
        missing = [self._missing_row(history, n, good) for line_id, good in matches.good_orders.iterrows()
                   if line_id not in recorded]
        if missing:
            out = pd.concat([out, pd.DataFrame(missing)], ignore_index=True)
        out = out.sort_values('_position', kind='stable').reset_index(drop=True)
        return AnnotatedTransactions(out.drop(columns='_position'), history.columns)

    # ------------------------------------------------------------------ rules
    @staticmethod
    def _repeated_successes(n: pd.DataFrame, period: Period) -> dict:
        """Later copies of an identical successful execution -> Sl No. of the first copy."""
        signature = ['order_id', 'tx_id', 'date', 'symbol', 'side', 'quantity', 'rate', 'value']
        eligible = n[n.status.eq('success') & n.tx_id.ne('') & period.contains(n.date)]
        repeats = {}
        for _, group in eligible.groupby(signature, dropna=False, sort=False):
            for i in group.index[1:]:
                repeats[i] = group.sl_no.iloc[0]
        return repeats

    def _reconcile_line(self, out, rows: pd.DataFrame, good: pd.Series):
        target = float(good.quantity)
        recorded_qty = float(rows.quantity.sum())
        issues = []
        if abs(recorded_qty - target) > nz.EPSILON:
            issues.append(Issue.QTY)
        if self._price_wrong(rows, good):
            issues.append(Issue.PRICE)
        if not rows.date.eq(good.broker_date).all():
            issues.append(Issue.DATE)
        evidence = (f'Broker: {_fmt(good.broker_quantity)} @ Rs {good.net_price:,.2f} = Rs {_fmt(good.net_value)} '
                    f'on {good.broker_date:%d-%b-%Y} (rows {good.broker_source_rows})')
        if Issue.QTY in issues and self._corporate_action_adjusted(rows, good):
            ratio = recorded_qty / target
            note = (f'Corporate action adjusted: recorded qty is {ratio:g}x the broker qty at 1/{ratio:g} the price, '
                    'and the amount matches the broker; left as recorded. ')
            issues = [i for i in issues if i is Issue.DATE]
            if not issues:
                for i in rows.index:
                    self._set(out, i, Action.NO_CHANGE.value, note + evidence)
                return
            evidence = note + evidence
            target = recorded_qty  # keep recorded quantities; only the date is corrected below
        if not issues:
            for i in rows.index:
                self._set(out, i, Action.NO_CHANGE.value, 'Matches recommendation and broker. ' + evidence)
            return

        label = reconcile_label(issues)
        price = round(float(good.net_price), 2)
        for (i, row), new_qty in zip(rows.iterrows(), self._allocate(rows.quantity.tolist(), target)):
            rate = price if Issue.PRICE in issues or pd.isna(row.rate) else float(row.rate)
            changes = {}
            if Issue.QTY in issues or Issue.PRICE in issues:
                changes = {'Quantity': new_qty, 'Share Value': rate, 'Amount': round(new_qty * rate, 2)}
            if Issue.DATE in issues:
                changes['Request Date'] = good.broker_date.strftime(DATE_FORMAT)
            diffs = []
            for column, new in changes.items():
                old = out.at[i, column]
                if column == 'Request Date':
                    if nz.text(old) != new:
                        diffs.append(f'{column} {nz.text(old)} -> {new}')
                        out.at[i, column] = new
                    continue
                old_number = nz.to_numbers([old]).iloc[0]
                if pd.isna(old_number) or abs(float(old_number) - float(new)) >= 0.005:
                    diffs.append(f'{column} {_fmt(old_number)} -> {_fmt(new)}')
                    out.at[i, column] = nz.tidy_number(new)
            note = '; '.join(diffs) or 'Line total corrected on another fill of this order'
            if new_qty == 0:
                note = 'Excess fill beyond recommended quantity; remove. ' + note
            self._set(out, i, label, f'{note}. {evidence}')

    def _corporate_action_adjusted(self, rows: pd.DataFrame, good: pd.Series) -> bool:
        """Quantity scaled by a split/bonus ratio while the recorded amount still equals the broker value."""
        recorded_qty, recorded_value = float(rows.quantity.sum()), float(rows.value.sum())
        if recorded_qty <= 0 or pd.isna(recorded_value):
            return False
        ratio = recorded_qty / float(good.quantity)
        whole_ratio = min(abs(ratio - round(ratio)), abs(1 / ratio - round(1 / ratio))) < 1e-6
        return whole_ratio and abs(recorded_value - good.net_value) <= self.tolerance.allowance(good.net_value)

    def _price_wrong(self, rows: pd.DataFrame, good: pd.Series) -> bool:
        bad_row = (rows.rate.isna() | rows.value.isna() | rows.rate.le(0)
                   | (rows.rate * rows.quantity - rows.value).abs().gt(0.02)).any()
        if bad_row or rows.quantity.sum() <= 0:
            return True
        average = rows.value.sum() / rows.quantity.sum()
        return abs(average - good.net_price) * float(good.quantity) > self.tolerance.allowance(good.net_value)

    @staticmethod
    def _allocate(quantities: list, target: float) -> list:
        """Adjust the last fill(s) so the line totals the broker-confirmed quantity."""
        result = [float(q) if pd.notna(q) else 0.0 for q in quantities]
        delta = target - sum(result)
        if delta > 0:
            result[-1] += delta
        for j in range(len(result) - 1, -1, -1):
            if delta >= 0:
                break
            cut = min(result[j], -delta)
            result[j] -= cut
            delta += cut
        return result

    def _missing_row(self, history: TransactionHistory, n: pd.DataFrame, good: pd.Series) -> dict:
        qty = float(good.quantity)
        price = round(float(good.net_price), 2)
        attempts = n[n.line_id.eq(good.line_id)]
        failed = attempts[attempts.status.isin(FAILED_STATUSES)]
        tx_id = 'RECON-' + hashlib.sha256(f'{good.line_id}|{good.broker_date:%Y-%m-%d}'.encode()).hexdigest()[:20]
        row = {c: None for c in history.columns}
        row.update({
            'Request Id': good.order_id, 'Request Date': good.broker_date.strftime(DATE_FORMAT),
            'Transaction Id': tx_id, 'Stock Id': nz.text(good.get('nse_code')) or good.symbol,
            'Order Placed By': 'reconciliation', 'Share Value': price, 'Action': good.side,
            'transactiontype': good.side, 'Request Type': nz.text(good.get('req_type')) or 'adhoc',
            'Quantity': nz.tidy_number(qty), 'Product Type': '-', 'Amount': round(qty * price, 2), 'Status': 'Success'})
        if len(failed):
            why = f'Webhook reported {nz.join_unique(failed.status)} (Sl No. {nz.join_unique(failed.sl_no)}) but broker confirms execution'
        elif len(attempts):
            why = f'Only {nz.join_unique(attempts.status)} records; broker confirms execution'
        else:
            why = 'No webhook record; broker confirms execution (likely executed outside the gateway)'
        row[ACTION_COLUMN] = Action.MISSING.value
        row[DETAIL_COLUMN] = (f'{why}: {_fmt(qty)} @ Rs {good.net_price:,.2f} on {good.broker_date:%d-%b-%Y} '
                              f'(broker rows {good.broker_source_rows}); recommended {good.order_time:%d-%b-%Y %H:%M}. '
                              'Request Time unknown.')
        row['_line_id'] = good.line_id
        row['_position'] = self._position_for(n, good)
        return row

    @staticmethod
    def _position_for(n: pd.DataFrame, good: pd.Series) -> float:
        """Place an added row right after its order bundle, or by date if the bundle is absent."""
        bundle = n.index[n.order_id.eq(good.order_id)]
        if len(bundle):
            return float(bundle.max()) + 0.5
        later = n.index[n.date.notna() & n.date.le(good.broker_date)]
        return float(later.min()) - 0.5 if len(later) else float(len(n))

    @staticmethod
    def _set(out, i, label: str, detail: str):
        out.at[i, ACTION_COLUMN] = label
        out.at[i, DETAIL_COLUMN] = detail
