"""Component 1 end to end: load the three sources, match, annotate, write outputs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .actions import AnnotatedTransactions, PriceTolerance, TransactionAnnotator
from .matching import MatchResult, OrderMatcher
from .reports import ThreeSourceReport
from .sources import BrokerageReport, OrderBook, Period, TransactionHistory
from .tables import write_table


@dataclass
class PipelineInputs:
    orders: Path              # order_stocks.csv
    transactions: Path        # '<Client> - Transaction History.xlsx'
    brokerage: Path           # 'WWW2-Equity-transaction-report-*.xlsx'
    client_id: str | None = None   # Winwize customer id; looked up by the broker's client name if omitted
    start: str | None = None       # defaults to the broker report's date range
    end: str | None = None
    max_day_lag: int = 1


class ReconciliationPipeline:
    def __init__(self, inputs: PipelineInputs, matcher: OrderMatcher | None = None,
                 annotator: TransactionAnnotator | None = None):
        self.inputs = inputs
        self.matcher = matcher or OrderMatcher(inputs.max_day_lag)
        self.annotator = annotator or TransactionAnnotator(PriceTolerance())
        self.broker: BrokerageReport | None = None
        self.orders: OrderBook | None = None
        self.history: TransactionHistory | None = None
        self.period: Period | None = None
        self.matches: MatchResult | None = None
        self.annotated: AnnotatedTransactions | None = None

    def run(self) -> AnnotatedTransactions:
        self.broker = BrokerageReport.load(self.inputs.brokerage)
        if not self.broker.client_name:
            raise ValueError('Client name not found in brokerage report header')
        self.period = self._period()
        self.orders = OrderBook.load(self.inputs.orders, self.broker.client_name, self.inputs.client_id)
        self.history = TransactionHistory.load(self.inputs.transactions)
        self.matches = self.matcher.match(self.orders, self.broker, self.period)
        self.annotated = self.annotator.annotate(self.history, self.orders, self.broker, self.period, self.matches)
        return self.annotated

    def _period(self) -> Period:
        default = self.broker.period
        start = pd.Timestamp(self.inputs.start) if self.inputs.start else default and default.start
        end = pd.Timestamp(self.inputs.end) if self.inputs.end else default and default.end
        if start is None or end is None:
            raise ValueError('Broker report has no date range; pass start and end')
        return Period(start, end)

    def write_actions(self, path) -> Path:
        return write_table(self._require().export_frame(), path, 'Transactions with action')

    def write_three_source(self, path) -> Path:
        report = ThreeSourceReport().build(self.matches, self.history, self._require())
        return write_table(report, path, 'Three source comparison')

    def describe(self) -> str:
        a = self._require()
        lines = [f'Client: {self.broker.client_name} ({self.orders.client_id})  Period: {self.period}',
                 f'Good orders: {len(self.matches.good_orders)}  '
                 f'Unconfirmed recommendation lines: {len(self.matches.unmatched_orders)}  '
                 f'Unattributed broker legs: {len(self.matches.unmatched_broker)}',
                 'Actions:']
        lines += [f'  {label:<40} {count}' for label, count in a.summary().items()]
        return '\n'.join(lines)

    def _require(self) -> AnnotatedTransactions:
        if self.annotated is None:
            raise RuntimeError('Call run() first')
        return self.annotated
