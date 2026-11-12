"""Behaviour tests for both components.  Run:  python -m unittest discover tests -v"""
import os
import re
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wep_recon import (ACTION_COLUMN, DETAIL_COLUMN, ActionApplier, BrokerageReport, OrderBook, OrderMatcher,
                       Period, PipelineInputs, ReconciliationPipeline, TransactionAnnotator, TransactionHistory)
from wep_recon.normalize import client_key
from wep_recon.tables import read_table

PERIOD = Period(pd.Timestamp('2025-01-01'), pd.Timestamp('2025-12-31'))


def broker(*legs):
    """legs: (date, symbol, side, qty, gross_value, brokerage)."""
    rows = []
    for n, (date, sym, sd, qty, gross, fee) in enumerate(legs):
        buy = sd == 'buy'
        rows.append({'Sauda Date': pd.Timestamp(date).strftime('%d/%m/%Y'), 'Scrip Code': sym, 'Scrip Name': sym,
                     'Buy Quantity': qty if buy else 0, 'Buy Value': gross + fee if buy else 0,
                     'Sell Quantity': 0 if buy else qty, 'Sell Value': 0 if buy else gross - fee,
                     'Brokerage': fee, 'broker_source_row': n + 5})
    return BrokerageReport(pd.DataFrame(rows), 'TEST CLIENT', 'X1', PERIOD)


def orders(*lines):
    """lines: (order_id, datetime, symbol, side, qty)."""
    raw = pd.DataFrame([dict(order_id=o, created_at=t, customer='C1', customer_name='Test Client', nse_code=s,
                             stock_name=s, buy_sell=sd, requested_qty=str(q), req_type='adhoc')
                        for o, t, s, sd, q in lines])
    return OrderBook(OrderBook.normalise(raw), 'C1', 'TEST CLIENT')


def history(*rows):
    """rows: (order_id, date, symbol, side, qty, price, status[, tx_id])."""
    records = []
    for n, (oid, date, sym, sd, qty, price, status, *tx) in enumerate(rows, start=1):
        records.append({'Sl No.': n, 'Request Id': oid, 'Request Date': pd.Timestamp(date).strftime('%d.%m.%Y'),
                        'Request Time': '10:00:00', 'Transaction Id': tx[0] if tx else f'TX{n}', 'Stock Id': f'{sym}-EQ',
                        'Order Placed By': 'api', 'Share Value': price, 'CMP': price, 'Action': sd, 'transactiontype': sd,
                        'Request Type': 'adhoc', 'Quantity': qty, 'Product Type': '-',
                        'Amount': round(qty * price, 2), 'Status': status})
    if not records:
        return TransactionHistory(pd.DataFrame(columns=TransactionHistory.REQUIRED))
    return TransactionHistory(pd.DataFrame(records))


def annotate(o, b, h):
    matches = OrderMatcher().match(o, b, PERIOD)
    return TransactionAnnotator().annotate(h, o, b, PERIOD, matches).frame


ORDER = ('ID-1', '2025-03-03 10:00', 'ABC', 'buy', 10)
LEG = ('2025-03-03', 'ABC', 'buy', 10, 1000.0, 4)


class BrokerageTests(unittest.TestCase):
    def test_net_value_removes_brokerage_from_buy_and_adds_it_back_to_sell(self):
        legs = broker(('2025-03-03', 'ABC', 'buy', 10, 1000.0, 4), ('2025-03-04', 'ABC', 'sell', 10, 1100.0, 4)).legs
        self.assertEqual(legs.net_value.tolist(), [1000.0, 1100.0])
        self.assertEqual(legs.net_price.tolist(), [100.0, 110.0])


class MatcherTests(unittest.TestCase):
    def test_same_day_first_then_next_day_and_exact_quantity(self):
        o = orders(('ID-1', '2025-03-03 10:00', 'ABC', 'buy', 10), ('ID-2', '2025-03-04 10:00', 'ABC', 'buy', 10))
        m = OrderMatcher().match(o, broker(('2025-03-04', 'ABC', 'buy', 10, 1000.0, 0)), PERIOD)
        self.assertEqual(m.good_orders.order_id.tolist(), ['ID-2'])
        m = OrderMatcher().match(orders(ORDER), broker(('2025-03-04', 'ABC', 'buy', 10, 1000.0, 0)), PERIOD)
        self.assertEqual(m.good_orders.day_lag.tolist(), [1])
        m = OrderMatcher().match(orders(ORDER), broker(('2025-03-03', 'ABC', 'buy', 11, 1100.0, 0)), PERIOD)
        self.assertTrue(m.good_orders.empty)

    def test_competing_recommendations_are_not_guessed(self):
        o = orders(ORDER, ('ID-2', '2025-03-03 11:00', 'ABC', 'buy', 10))
        m = OrderMatcher().match(o, broker(LEG), PERIOD)
        self.assertTrue(m.good_orders.empty)
        self.assertTrue(m.unmatched_orders.review_reason.str.startswith('Ambiguous').all())


class AnnotatorTests(unittest.TestCase):
    def test_clean_execution_is_no_change(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Success')))
        self.assertEqual(out[ACTION_COLUMN].tolist(), ['no change'])

    def test_unrecorded_execution_becomes_missing_row_at_net_price(self):
        out = annotate(orders(ORDER), broker(LEG), history())
        row = out.iloc[0]
        self.assertEqual(row[ACTION_COLUMN], 'missing')
        self.assertTrue(pd.isna(row['Sl No.']))
        self.assertEqual((row.Quantity, row['Share Value'], row.Amount, row.Status), (10, 100.0, 1000.0, 'Success'))
        self.assertTrue(row['Transaction Id'].startswith('RECON-'))

    def test_failed_but_executed_keeps_failed_row_and_adds_missing(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 0, 'Failed')))
        self.assertEqual(out[ACTION_COLUMN].tolist(), ['no change', 'missing'])
        self.assertIn('failed', out.iloc[1][DETAIL_COLUMN])

    def test_quantity_and_price_mismatches_carry_corrected_values(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 5, 100.0, 'Success')))
        self.assertEqual(out.iloc[0][ACTION_COLUMN], 'reconcile - qty mismatch')
        self.assertEqual((out.iloc[0].Quantity, out.iloc[0].Amount), (10, 1000))
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 104.0, 'Success')))
        self.assertEqual(out.iloc[0][ACTION_COLUMN], 'reconcile - price mismatch')
        self.assertEqual((out.iloc[0]['Share Value'], out.iloc[0].Amount), (100, 1000))

    def test_small_rounding_difference_is_tolerated(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.3, 'Success')))
        self.assertEqual(out.iloc[0][ACTION_COLUMN], 'no change')

    def test_date_mismatch(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-04', 'ABC', 'buy', 10, 100.0, 'Success')))
        self.assertEqual(out.iloc[0][ACTION_COLUMN], 'reconcile - date mismatch')
        self.assertEqual(out.iloc[0]['Request Date'], '03.03.2025')

    def test_duplicates_from_status_and_repeated_success(self):
        h = history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Success', 'T'),
                    ('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Success', 'T'),
                    ('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Duplicate', 'T'))
        self.assertEqual(annotate(orders(ORDER), broker(LEG), h)[ACTION_COLUMN].tolist(),
                         ['no change', 'duplicate', 'duplicate'])

    def test_unconfirmed_success_goes_to_review(self):
        out = annotate(orders(ORDER), broker(('2025-03-03', 'ABC', 'buy', 99, 9900.0, 0)),
                       history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Success'),
                               ('ID-9', '2025-03-03', 'XYZ', 'buy', 1, 50.0, 'Success')))
        self.assertEqual(out[ACTION_COLUMN].tolist(), ['review - not broker confirmed'] * 2)

    def test_split_adjusted_record_is_left_alone(self):
        out = annotate(orders(ORDER), broker(LEG), history(('ID-1', '2025-03-03', 'ABC', 'buy', 20, 50.0, 'Success')))
        self.assertEqual(out.iloc[0][ACTION_COLUMN], 'no change')
        self.assertIn('Corporate action', out.iloc[0][DETAIL_COLUMN])

    def test_excess_second_fill_is_zeroed(self):
        h = history(('ID-1', '2025-03-03', 'ABC', 'buy', 10, 100.0, 'Success', 'T1'),
                    ('ID-1', '2025-03-03', 'ABC', 'buy', 4, 100.0, 'Success', 'T2'))
        out = annotate(orders(ORDER), broker(LEG), h)
        self.assertEqual(out.Quantity.tolist(), [10, 0])


class ApplierTests(unittest.TestCase):
    def setUp(self):
        self.stale = history(('ID-1', '2025-03-03', 'ABC', 'buy', 5, 100.0, 'Success'),
                             ('ID-1', '2025-03-03', 'ABC', 'buy', 5, 100.0, 'Duplicate'),
                             ('ID-2', '2025-03-05', 'XYZ', 'buy', 2, 50.0, 'Failed'))
        o = orders(ORDER, ('ID-2', '2025-03-05 10:00', 'XYZ', 'buy', 2))
        self.actions = annotate(o, broker(LEG, ('2025-03-05', 'XYZ', 'buy', 2, 100.0, 1)), self.stale)

    def test_applies_every_action_and_renumbers(self):
        result = ActionApplier().apply(self.stale.raw, self.actions)
        s = result.statement
        self.assertEqual(s.columns.tolist(), TransactionHistory.REQUIRED)
        self.assertEqual(s['Sl No.'].tolist(), [1, 2, 3])
        self.assertEqual(s.Status.tolist(), ['Success', 'Failed', 'Success'])
        self.assertEqual(s.Quantity.tolist(), [10, 2, 2])
        self.assertEqual(dict(result.counts), {'reconcile': 1, 'duplicate': 1, 'no change': 1, 'missing': 1})

    def test_rejects_action_file_from_another_transaction_file(self):
        other = self.stale.raw.copy()
        other.loc[0, 'Transaction Id'] = 'SOMETHING-ELSE'
        with self.assertRaises(ValueError):
            ActionApplier().apply(other, self.actions)
        with self.assertRaises(ValueError):
            ActionApplier().apply(self.stale.raw.iloc[:2], self.actions)


def real_clients():
    """Pair '<name> - Transaction History' files with WWW2 broker reports by client name.

    Point WEP_DATA_DIR at a folder holding order_stocks.csv and the client files (any depth).
    Nothing client-specific is stored in this repository.
    """
    folder = os.environ.get('WEP_DATA_DIR')
    if not folder or not Path(folder).is_dir():
        return None, []
    root = Path(folder)
    orders = next(root.rglob('order_stocks.csv'), None)
    brokers = {}
    for path in root.rglob('WWW2-*.xlsx'):
        try:
            brokers[client_key(BrokerageReport.load(path).client_name)] = path
        except ValueError:
            continue
    pairs = []
    for path in root.rglob('*Transaction History.xlsx'):
        name = re.sub(r'\s*-?\s*transaction\s*history$', '', path.stem, flags=re.I)
        if client_key(name) in brokers:
            pairs.append((path, brokers[client_key(name)]))
    return orders, pairs


ORDERS, CLIENTS = real_clients()


@unittest.skipUnless(ORDERS and CLIENTS, 'set WEP_DATA_DIR to run against real client data')
class RealDataRoundTrip(unittest.TestCase):
    def test_statement_matches_broker_for_every_good_order(self):
        for tx, br in CLIENTS:
            with self.subTest(tx.name):
                p = ReconciliationPipeline(PipelineInputs(ORDERS, tx, br))
                try:
                    actions = p.run().export_frame()
                except ValueError as error:  # e.g. client not present in the order data
                    self.skipTest(str(error))
                stale = read_table(tx)
                statement = ActionApplier().apply(stale, actions).statement
                self.assertEqual(len(statement), len(stale) + actions[ACTION_COLUMN].eq('missing').sum()
                                 - actions[ACTION_COLUMN].eq('duplicate').sum()
                                 - (actions[ACTION_COLUMN].str.startswith('reconcile') & actions.Quantity.eq(0)).sum())
                success = TransactionHistory(statement).norm.query('status == "success"')
                recorded = success.groupby('line_id').quantity.sum()
                good = p.matches.good_orders
                adjusted = actions.loc[actions[DETAIL_COLUMN].str.startswith('Corporate action', na=False), 'Request Id']
                check = good[~good.order_id.isin(adjusted)]
                self.assertTrue((recorded.reindex(check.index).fillna(0) - check.quantity).abs().lt(1e-8).all())


if __name__ == '__main__':
    unittest.main()
