from __future__ import annotations

import csv
from pathlib import Path
from datetime import datetime


class TradeStore:
    def __init__(self, path: str = 'storage/trades.csv') -> None:
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)
        self.fieldnames = [
            'time', 'symbol', 'side', 'entry', 'stop_loss', 'take_profit', 'qty', 'notional',
            'score', 'regime', 'bias', 'status', 'reason', 'balance_after'
        ]
        if not self.path.exists():
            with self.path.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()

    def append(self, row: dict) -> None:
        data = {key: row.get(key, '') for key in self.fieldnames}
        data['time'] = data.get('time') or datetime.utcnow().isoformat()
        with self.path.open('a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writerow(data)
