"""Command-line entry points: ``wep-annotate`` (component 1) and ``wep-apply`` (component 2)."""
import argparse
from pathlib import Path

from .applier import ActionApplier
from .pipeline import PipelineInputs, ReconciliationPipeline
from .tables import read_table, write_table


def annotate_main(argv=None):
    """Component 1: order data + transaction history + brokerage -> transactions with action."""
    p = argparse.ArgumentParser(prog='wep-annotate', description=annotate_main.__doc__)
    p.add_argument('--orders', required=True, type=Path, help='Winwize order data (order_stocks.csv)')
    p.add_argument('--transactions', required=True, type=Path, help='Client transaction history (xlsx/csv)')
    p.add_argument('--brokerage', required=True, type=Path, help='Client brokerage report (WWW2-...xlsx)')
    p.add_argument('--out', type=Path, help='Output file (.xlsx or .csv); default: next to the transactions file')
    p.add_argument('--three-source', type=Path, help='Also write the broker | order | transaction comparison here')
    p.add_argument('--client-id', help='Winwize customer id, if the name lookup is ambiguous')
    p.add_argument('--start', help='Period start YYYY-MM-DD (default: broker report range)')
    p.add_argument('--end', help='Period end YYYY-MM-DD (default: broker report range)')
    p.add_argument('--max-day-lag', type=int, default=1, help='Calendar days after recommendation to accept (default 1)')
    a = p.parse_args(argv)

    pipeline = ReconciliationPipeline(PipelineInputs(a.orders, a.transactions, a.brokerage, a.client_id,
                                                     a.start, a.end, a.max_day_lag))
    pipeline.run()
    out = a.out or a.transactions.with_name(f'{a.transactions.stem} - with action.xlsx')
    print(pipeline.describe())
    print(f'Wrote {pipeline.write_actions(out)}')
    if a.three_source:
        print(f'Wrote {pipeline.write_three_source(a.three_source)}')


def apply_main(argv=None):
    """Component 2: stale transaction data + action file -> reconciled statement CSV."""
    p = argparse.ArgumentParser(prog='wep-apply', description=apply_main.__doc__)
    p.add_argument('stale', type=Path, help='Stale transaction data (xlsx/csv)')
    p.add_argument('actions', type=Path, help='Transactions with action, from wep-annotate')
    p.add_argument('-o', '--out', type=Path, help='Output CSV (default: reconciled_statement.csv beside the action file)')
    a = p.parse_args(argv)

    result = ActionApplier().apply(read_table(a.stale), read_table(a.actions))
    out = write_table(result.statement, a.out or a.actions.with_name('reconciled_statement.csv'))
    for label, count in sorted(result.counts.items()):
        print(f'  {label:<32} {count}')
    print(f'{len(result.statement)} rows -> {out}')
