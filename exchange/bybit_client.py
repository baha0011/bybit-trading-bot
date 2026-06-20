from __future__ import annotations

import pandas as pd
from pybit.unified_trading import HTTP


class BybitMarketClient:
    def __init__(self, testnet: bool = False, api_key: str = '', api_secret: str = '') -> None:
        kwargs = {'testnet': testnet}
        if api_key and api_secret:
            kwargs.update({'api_key': api_key, 'api_secret': api_secret})
        self.session = HTTP(**kwargs)

    def get_klines(self, category: str, symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
        response = self.session.get_kline(
            category=category,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        if response.get('retCode') != 0:
            raise RuntimeError(f"Bybit get_kline error: {response}")

        rows = response['result']['list']
        df = pd.DataFrame(rows, columns=['start_time', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        for col in ['open', 'high', 'low', 'close', 'volume', 'turnover']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['start_time'] = pd.to_datetime(pd.to_numeric(df['start_time']), unit='ms')
        df = df.sort_values('start_time').reset_index(drop=True)
        return df

    def get_ticker(self, category: str, symbol: str) -> dict:
        response = self.session.get_tickers(category=category, symbol=symbol)
        if response.get('retCode') != 0:
            raise RuntimeError(f"Bybit get_tickers error: {response}")
        item = response['result']['list'][0]
        return {
            'last': float(item['lastPrice']),
            'bid': float(item.get('bid1Price') or item['lastPrice']),
            'ask': float(item.get('ask1Price') or item['lastPrice']),
            'volume24h': float(item.get('volume24h') or 0),
            'turnover24h': float(item.get('turnover24h') or 0),
        }
    
    def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> dict:
        response = self.session.get_orderbook(
            category=category,
            symbol=symbol,
            limit=limit,
        )

        result = response.get("result", {})

        bids = result.get("b", [])
        asks = result.get("a", [])

        return {
            "bids": [(float(price), float(size)) for price, size in bids],
            "asks": [(float(price), float(size)) for price, size in asks],
        }
