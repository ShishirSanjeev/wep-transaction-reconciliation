"""CSV / Excel input and output, chosen by file extension."""
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter


def read_table(path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == '.csv':
        return pd.read_csv(path, dtype=object, keep_default_na=False, na_values=[''])
    return pd.read_excel(path, dtype=object)


def write_table(frame: pd.DataFrame, path, sheet_name: str = 'Sheet1') -> Path:
    """Write CSV or a readable single-sheet xlsx (frozen header, filters, sensible widths)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == '.csv':
        frame.to_csv(path, index=False, encoding='utf-8-sig')
        return path
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        frame.to_excel(writer, index=False, sheet_name=sheet_name)
        sheet = writer.sheets[sheet_name]
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for i, column in enumerate(frame.columns, start=1):
            sample = [str(column)] + [str(v) for v in frame[column].head(300) if v is not None]
            sheet.column_dimensions[get_column_letter(i)].width = min(60, max(10, max(map(len, sample)) + 2))
    return path
