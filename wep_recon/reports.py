"""Optional output: one row per good order line, showing broker | order | transaction history side by side."""
from __future__ import annotations

import pandas as pd

from . import normalize as nz
from .actions import ACTION_COLUMN, AnnotatedTransactions
from .matching import MatchResult
from .sources import TransactionHistory


class ThreeSourceReport:
    def build(self, matches: MatchResult, history: TransactionHistory,
              annotated: AnnotatedTransactions) -> pd.DataFrame:
        n = history.norm
        success = n[n.status.eq('success')]
        actions = annotated.frame.groupby('_line_id')[ACTION_COLUMN].agg(nz.join_unique)
        rows = []
        for line_id, g in matches.good_orders.iterrows():
            attempts = n[n.line_id.eq(line_id)]
            ok = success[success.line_id.eq(line_id)]
            recorded_qty = float(ok.quantity.sum())
            recorded_amount = float(ok.value.sum())
            rows.append({
                # brokerage
                'Broker date': g.broker_date.date(), 'Broker scrip': g.symbol, 'Broker side': g.side,
                'Broker qty': nz.tidy_number(g.broker_quantity), 'Broker net price': round(g.net_price, 2),
                'Broker net value': nz.tidy_number(g.net_value), 'Brokerage': nz.tidy_number(g.brokerage),
                'Broker rows': g.broker_source_rows,
                # winwize order
                'Order ID': g.order_id, 'Recommended at': g.order_time, 'Stock name': g.get('stock_name'),
                'Recommended qty': nz.tidy_number(g.quantity), 'Day lag': int(g.day_lag),
                # transaction history
                'Transaction IDs': nz.join_unique(attempts.tx_id), 'Statuses': nz.join_unique(attempts.status),
                'Recorded success qty': nz.tidy_number(recorded_qty),
                'Recorded success amount': nz.tidy_number(recorded_amount),
                # differences
                'Qty diff (broker - recorded)': nz.tidy_number(g.broker_quantity - recorded_qty),
                'Amount diff (broker - recorded)': round(g.net_value - recorded_amount, 2),
                'Reconciliation action(s)': actions.get(line_id, ''),
            })
        return pd.DataFrame(rows)
