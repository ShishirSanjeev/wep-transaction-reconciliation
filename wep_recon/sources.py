"""Loaders for the three inputs. Each class wraps one source and exposes a normalised view.

Source files are only ever read. Constructors take DataFrames so the classes can be tested
without files; the ``load`` classmethods handle the file formats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import normalize as nz
from .tables import read_table


@dataclass(frozen=True)
class Period:
    """Inclusive reconciliation window."""
    start: pd.Timestamp
    end: pd.Timestamp

    def __post_init__(self):
        if self.start > self.end:
            raise ValueError(f'Period start {self.start:%Y-%m-%d} is after end {self.end:%Y-%m-%d}')

    def contains(self, dates: pd.Series) -> pd.Series:
        return dates.between(self.start, self.end)

    def __str__(self):
        return f'{self.start:%d-%b-%Y} to {self.end:%d-%b-%Y}'


# --------------------------------------------------------------------------- brokerage
class BrokerageReport:
    """Angel 'WWW2-Equity-transaction-report' export for one client.

    The broker's Buy Value includes brokerage and Sell Value is net of it, so the
    execution value is recovered as ``value - brokerage`` (buy) / ``value + brokerage`` (sell).
    Rows are aggregated to one leg per trade date + scrip + side.
    """

    LEG_COLUMNS = ['broker_key', 'broker_date', 'symbol', 'side', 'broker_quantity', 'broker_value',
                   'brokerage', 'net_value', 'net_price', 'broker_scrip_name', 'broker_source_rows']

    def __init__(self, rows: pd.DataFrame, client_name: str = '', client_code: str = '',
                 period: Period | None = None, path: Path | None = None):
        self.rows = rows
        self.client_name = client_name
        self.client_code = client_code
        self.period = period
        self.path = path
        self.legs = self._build_legs(rows)

    @classmethod
    def load(cls, path) -> 'BrokerageReport':
        path = Path(path)
        preamble = pd.read_excel(path, header=None, nrows=20)
        header = next((i for i, row in preamble.iterrows()
                       if any(nz.text(v).lower() == 'sauda date' for v in row)), None)
        if header is None:
            raise ValueError(f'{path.name}: "Sauda Date" header row not found')
        meta = {nz.text(v).split(':', 1)[0].strip().lower(): nz.text(v).split(':', 1)[-1].strip()
                for v in preamble.iloc[:header, 0] if ':' in nz.text(v)}
        period = None
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', meta.get('date range', ''))
        if len(dates) == 2:
            period = Period(pd.Timestamp(dates[0]), pd.Timestamp(dates[1]))
        rows = pd.read_excel(path, header=header, dtype=object)
        rows['broker_source_row'] = rows.index + header + 2  # 1-based Excel row number
        rows = rows[~rows['Sauda Date'].map(nz.text).str.lower().eq('total')]
        name = re.sub(r'\s+', ' ', meta.get('client name', ''))
        return cls(rows, name, meta.get('client code', ''), period, path)

    @classmethod
    def _build_legs(cls, rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty:
            return pd.DataFrame(columns=cls.LEG_COLUMNS)
        b = rows.copy()
        b['broker_date'] = nz.to_dates(b['Sauda Date'], dayfirst=True).values
        b['symbol'] = b['Scrip Code'].map(nz.symbol)
        for col in ['Buy Quantity', 'Sell Quantity', 'Buy Value', 'Sell Value', 'Brokerage']:
            b[col] = nz.to_numbers(b[col]).fillna(0).values
        # A row with both buy and sell quantity carries one brokerage figure: split it by value.
        gross = (b['Buy Value'] + b['Sell Value']).where(lambda s: s > 0)
        legs = []
        for sd in ('buy', 'sell'):
            label = sd.capitalize()
            part = b[b[f'{label} Quantity'].gt(0) & b.broker_date.notna() & b.symbol.ne('')].copy()
            part['leg_brokerage'] = (part['Brokerage'] * part[f'{label} Value'] / gross.loc[part.index]).fillna(0)
            for (date, sym), g in part.groupby(['broker_date', 'symbol']):
                qty = float(g[f'{label} Quantity'].sum())
                value = float(g[f'{label} Value'].sum())
                brokerage = float(g.leg_brokerage.sum())
                net = value - brokerage if sd == 'buy' else value + brokerage
                legs.append(dict(broker_key=f'{date:%Y-%m-%d}|{sym}|{sd}', broker_date=date, symbol=sym, side=sd,
                                 broker_quantity=qty, broker_value=value, brokerage=brokerage, net_value=net,
                                 net_price=net / qty, broker_scrip_name=nz.join_unique(g['Scrip Name']),
                                 broker_source_rows=nz.join_unique(g.broker_source_row)))
        return pd.DataFrame(legs, columns=cls.LEG_COLUMNS)


# --------------------------------------------------------------------------- orders
class OrderBook:
    """Winwize recommendation lines (order_stocks.csv) for one client."""

    REQUIRED = {'order_id', 'created_at', 'customer', 'customer_name', 'nse_code', 'stock_name',
                'buy_sell', 'requested_qty'}

    def __init__(self, lines: pd.DataFrame, client_id: str, client_name: str = ''):
        self.client_id = client_id
        self.client_name = client_name
        self.lines = lines

    @classmethod
    def load(cls, path, client_name: str, client_id: str | None = None) -> 'OrderBook':
        raw = read_table(path)
        missing = cls.REQUIRED - set(raw.columns)
        if missing:
            raise ValueError(f'Order data missing columns: {sorted(missing)}')
        raw['order_source_row'] = raw.index + 2
        by_name = raw.customer_name.map(nz.client_key).eq(nz.client_key(client_name))
        if client_id is None:
            ids = sorted(raw.loc[by_name, 'customer'].map(nz.text).unique())
            if len(ids) != 1:
                raise ValueError(f'Cannot resolve a single customer id for "{client_name}" '
                                 f'(found {ids or "none"}); pass client_id explicitly')
            client_id = ids[0]
        by_id = raw.customer.map(nz.text).eq(client_id)
        if (by_id & ~by_name).any() or (by_name & ~by_id).any():
            raise ValueError(f'Customer name "{client_name}" and id {client_id} disagree in order data')
        return cls(cls.normalise(raw[by_id]), client_id, client_name)

    @staticmethod
    def normalise(raw: pd.DataFrame) -> pd.DataFrame:
        o = raw.copy()
        o['order_id'] = o.order_id.map(nz.text)
        o['order_time'] = pd.to_datetime(o.created_at.map(nz.text), format='mixed', errors='coerce')
        o['order_date'] = o.order_time.dt.normalize()
        o['symbol'] = o.nse_code.map(nz.symbol)
        o['side'] = o.buy_sell.map(nz.side)
        o['quantity'] = nz.to_numbers(o.requested_qty).values
        o['line_id'] = [nz.line_key(a, b, c) for a, b, c in zip(o.order_id, o.symbol, o.side)]
        o['valid'] = (o.order_time.notna() & o.order_id.ne('') & o.symbol.ne('')
                      & o.side.isin(['buy', 'sell']) & o.quantity.gt(0))
        return o.reset_index(drop=True)

    def in_period(self, period: Period) -> pd.DataFrame:
        return self.lines[period.contains(self.lines.order_date)].copy()


# --------------------------------------------------------------------------- transactions
class TransactionHistory:
    """Winwize transaction history (webhook) export. ``raw`` keeps the original columns untouched."""

    REQUIRED = ['Sl No.', 'Request Id', 'Request Date', 'Request Time', 'Transaction Id', 'Stock Id',
                'Order Placed By', 'Share Value', 'CMP', 'Action', 'transactiontype', 'Request Type',
                'Quantity', 'Product Type', 'Amount', 'Status']

    def __init__(self, raw: pd.DataFrame, path: Path | None = None):
        missing = [c for c in self.REQUIRED if c not in raw.columns]
        if missing:
            raise ValueError(f'Transaction data missing columns: {missing}')
        if not raw['Sl No.'].map(nz.text).is_unique:
            raise ValueError('"Sl No." must uniquely identify each transaction row')
        self.raw = raw.reset_index(drop=True)
        self.columns = list(raw.columns)
        self.path = path
        self.norm = self._normalise(self.raw)

    @classmethod
    def load(cls, path) -> 'TransactionHistory':
        return cls(read_table(path), Path(path))

    @staticmethod
    def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
        n = pd.DataFrame(index=raw.index)
        n['sl_no'] = raw['Sl No.'].map(nz.text)
        n['order_id'] = raw['Request Id'].map(nz.text)
        n['tx_id'] = raw['Transaction Id'].map(nz.text)
        n['symbol'] = raw['Stock Id'].map(nz.symbol)
        n['side'] = raw['Action'].map(nz.side)
        n['status'] = raw['Status'].map(nz.side)
        n['date'] = nz.to_dates(raw['Request Date'], dayfirst=True).values
        n['quantity'] = nz.to_numbers(raw['Quantity']).values
        n['rate'] = nz.to_numbers(raw['Share Value']).values
        n['value'] = nz.to_numbers(raw['Amount']).values
        n['line_id'] = [nz.line_key(a, b, c) for a, b, c in zip(n.order_id, n.symbol, n.side)]
        return n
