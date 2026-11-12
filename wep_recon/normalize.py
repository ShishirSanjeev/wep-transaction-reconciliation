"""Small, pure helpers for normalising values coming from the three sources."""
import re

import pandas as pd

_SERIES_SUFFIX = re.compile(r'-(EQ|BE|BZ|BL|RR|IV)$')
EPSILON = 1e-8


def text(value) -> str:
    """Return a stripped string; blank for NaN/None."""
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def client_key(value) -> str:
    """Name used to compare clients across sources ('JOHN  DOE' == 'John Doe')."""
    return re.sub('[^A-Z0-9]', '', text(value).upper())


def symbol(value) -> str:
    """Exchange symbol without whitespace or trading-series suffix (GOLDBEES-EQ -> GOLDBEES)."""
    return _SERIES_SUFFIX.sub('', re.sub(r'\s+', '', text(value).upper()))


def side(value) -> str:
    return text(value).lower()


def to_dates(values, *, dayfirst: bool) -> pd.Series:
    """Parse mixed date strings ('05.11.2026', '15/10/2026', Excel dates) to midnight timestamps."""
    as_text = pd.Series(values).map(text).str.replace('.', '/', regex=False)
    return pd.to_datetime(as_text, format='mixed', dayfirst=dayfirst, errors='coerce').dt.normalize()


def to_numbers(values) -> pd.Series:
    return pd.to_numeric(pd.Series(values), errors='coerce')


def line_key(order_id, sym, sd) -> str:
    """One recommendation line = one order (Request Id) + scrip + side."""
    return f'{text(order_id)}|{sym}|{sd}'


def join_unique(values) -> str:
    return ' | '.join(sorted({text(v) for v in values if text(v)}))


def tidy_number(value):
    """Write whole numbers as int so Excel/CSV output looks like the source."""
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return int(round(value)) if abs(value - round(value)) < EPSILON else round(value, 2)
