"""Stage: identify 'good' orders = recommendation lines confirmed by a broker execution.

Rule: same scrip + side + exact requested quantity, executed on the recommendation date
or within ``max_day_lag`` calendar days after it (same day is tried first). Each broker
daily leg is used at most once. If several recommendations compete for the same leg at
the same lag, none is chosen (ambiguous). Webhook data plays no part in this decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .sources import BrokerageReport, OrderBook, Period


@dataclass
class MatchResult:
    good_orders: pd.DataFrame        # one row per confirmed line, indexed by line_id
    unmatched_orders: pd.DataFrame   # recommendation lines with a review_reason
    unmatched_broker: pd.DataFrame   # broker legs not attributed to a recommendation

    def is_good(self, line_id: str) -> bool:
        return line_id in self.good_orders.index


class OrderMatcher:
    def __init__(self, max_day_lag: int = 1):
        if max_day_lag < 0:
            raise ValueError('max_day_lag must be >= 0')
        self.max_day_lag = max_day_lag

    def match(self, orders: OrderBook, broker: BrokerageReport, period: Period) -> MatchResult:
        o = orders.in_period(period).reset_index(drop=True)
        b = broker.legs.reset_index(drop=True)
        blocked = set(o.index[o.line_id.duplicated(keep=False) | ~o.valid])
        by_key: dict[tuple, list[int]] = {}
        for bi, leg in b.iterrows():
            by_key.setdefault((leg.symbol, leg.side, round(leg.broker_quantity, 8), leg.broker_date), []).append(bi)

        used_o, used_b, ambiguous_o, ambiguous_b, pairs = set(), set(), set(), set(), []
        for lag in range(self.max_day_lag + 1):
            claims: dict[int, list[int]] = {}
            for oi, line in o.iterrows():
                if oi in blocked or oi in used_o or oi in ambiguous_o:
                    continue
                key = (line.symbol, line.side, round(float(line.quantity), 8), line.order_date + pd.Timedelta(days=lag))
                for bi in by_key.get(key, []):
                    if bi not in used_b and bi not in ambiguous_b:
                        claims.setdefault(bi, []).append(oi)
            for bi, claimants in claims.items():
                if len(claimants) > 1:
                    ambiguous_o.update(claimants)
                    ambiguous_b.add(bi)
                elif claimants[0] not in used_o:
                    pairs.append((claimants[0], bi, lag))
                    used_o.add(claimants[0])
                    used_b.add(bi)

        good = pd.DataFrame([{**o.loc[oi].to_dict(), **b.loc[bi].to_dict(), 'day_lag': lag} for oi, bi, lag in pairs])
        if good.empty:
            good = pd.DataFrame(columns=[*o.columns, *b.columns, 'day_lag'])
        good = good.set_index('line_id', drop=False).sort_values(['broker_date', 'order_time'])

        unmatched = o[~o.index.isin(used_o)].copy()
        unmatched['review_reason'] = [self._reason(i, o, b, blocked, ambiguous_o) for i in unmatched.index]
        unmatched_b = b[~b.index.isin(used_b)].copy()
        unmatched_b['review_reason'] = ['Ambiguous: several recommendations fit' if i in ambiguous_b
                                        else 'No exact recommendation match' for i in unmatched_b.index]
        return MatchResult(good, unmatched.reset_index(drop=True), unmatched_b.reset_index(drop=True))

    def _reason(self, i, o, b, blocked, ambiguous) -> str:
        if i in blocked:
            return 'Invalid fields or duplicated recommendation line'
        if i in ambiguous:
            return 'Ambiguous: another recommendation competes for the same broker execution'
        line = o.loc[i]
        window = b[b.symbol.eq(line.symbol) & b.side.eq(line.side)
                   & b.broker_date.between(line.order_date, line.order_date + pd.Timedelta(days=self.max_day_lag))]
        if window.empty:
            return 'No broker execution of this scrip/side in the window'
        return 'Broker quantity differs, or execution already used by another recommendation'
