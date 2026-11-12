"""Component 2: apply the annotated actions to the stale transaction data.

Needs only two inputs: the stale transaction file and the action file produced by
``TransactionAnnotator``. Rows are linked by ``Sl No.`` (checked against ``Transaction Id``),
and processed one order bundle (``Request Id``) at a time:

no change / review   -> stale row kept exactly as it is
reconcile - ...      -> replaced by the corrected row from the action file (dropped if Quantity is 0)
missing              -> corrected row added
duplicate            -> stale row dropped

The result keeps the transaction data's columns; ``Sl No.`` is renumbered 1..n.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from . import normalize as nz
from .actions import ACTION_COLUMN, Action

KEY = 'Sl No.'


@dataclass
class ApplyResult:
    statement: pd.DataFrame
    counts: Counter = field(default_factory=Counter)


class ActionApplier:
    def apply(self, stale: pd.DataFrame, actions: pd.DataFrame) -> ApplyResult:
        columns = list(stale.columns)
        self._validate(stale, actions, columns)
        stale_by_key = {nz.text(row[KEY]): row for _, row in stale.iterrows()}

        output, counts = [], Counter()
        for _, bundle in self._bundles(actions):
            for _, row in bundle.iterrows():
                action = Action.parse(row[ACTION_COLUMN])
                counts[action.value] += 1
                kept = self._resolve(action, row, stale_by_key, columns)
                if kept is not None:
                    output.append(kept)

        statement = pd.DataFrame(output, columns=columns)
        statement[KEY] = range(1, len(statement) + 1)
        return ApplyResult(statement, counts)

    @staticmethod
    def _bundles(actions: pd.DataFrame):
        """Yield (Request Id, rows) in the order bundles first appear in the action file."""
        order = actions['Request Id'].map(nz.text)
        for request_id in dict.fromkeys(order):
            yield request_id, actions[order.eq(request_id)]

    @staticmethod
    def _resolve(action: Action, row: pd.Series, stale_by_key: dict, columns: list):
        if action in (Action.NO_CHANGE, Action.REVIEW):
            return stale_by_key[nz.text(row[KEY])][columns]
        if action is Action.DUPLICATE:
            return None
        quantity = nz.to_numbers([row['Quantity']]).iloc[0]
        if action is Action.RECONCILE and quantity == 0:
            return None  # excess fill
        return row[columns]

    @staticmethod
    def _validate(stale: pd.DataFrame, actions: pd.DataFrame, columns: list):
        missing_columns = [c for c in [*columns, ACTION_COLUMN] if c not in actions.columns]
        if missing_columns:
            raise ValueError(f'Action file is missing columns: {missing_columns}')
        stale_keys = stale[KEY].map(nz.text)
        if not stale_keys.is_unique:
            raise ValueError(f'Stale data has repeated "{KEY}" values')
        # Compare plain strings: pandas does not compare str-Enum members element-wise reliably.
        labels = actions[ACTION_COLUMN].map(lambda v: Action.parse(v).value)  # raises on unknown labels
        is_missing = labels.eq(Action.MISSING.value)
        linked = actions[~is_missing]
        linked_keys = linked[KEY].map(nz.text)
        if (is_missing & actions[KEY].map(nz.text).ne('')).any():
            raise ValueError('"missing" rows must not carry a stale Sl No.')

        unknown = sorted(set(linked_keys) - set(stale_keys))
        unannotated = sorted(set(stale_keys) - set(linked_keys))
        if unknown or unannotated or not linked_keys.is_unique:
            raise ValueError('Action file was not produced from this stale transaction file: '
                             f'{len(unknown)} unknown Sl No. (e.g. {unknown[:3]}), '
                             f'{len(unannotated)} stale rows without an action (e.g. {unannotated[:3]}), '
                             f'{int(linked_keys.duplicated().sum())} repeated Sl No.')
        stale_tx = dict(zip(stale_keys, stale['Transaction Id'].map(nz.text)))
        mismatched = [k for k, tx in zip(linked_keys, linked['Transaction Id'].map(nz.text)) if stale_tx[k] != tx]
        if mismatched:
            raise ValueError(f'Transaction Id differs between stale and action file for Sl No. {mismatched[:5]}')
