"""
資料來源轉接層。

設計原則：每個 fetcher 都回傳 [(date, value), ...] 的時間序列（由舊到新），
上層再自己決定「本期 / 上期」怎麼取。這樣月更指標和週更指標可以共用同一套邏輯。

失敗時回傳空 list，不丟例外 —— 一個來源掛掉不該讓整份週報產不出來。
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()


# ── FRED ────────────────────────────────────────────────────────────
def fred(series: str, start: date | None = None, retries: int = 3):
    """
    有 FRED_API_KEY 就走官方 API；沒有就退回 fredgraph.csv（免金鑰但較不穩）。
    """
    start = start or (date.today() - timedelta(days=800))
    for attempt in range(retries):
        try:
            if FRED_KEY:
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": series,
                        "api_key": FRED_KEY,
                        "file_type": "json",
                        "observation_start": start.isoformat(),
                    },
                    timeout=30,
                )
                r.raise_for_status()
                obs = r.json().get("observations", [])
                out = []
                for o in obs:
                    if o["value"] in (".", "", None):
                        continue
                    out.append((date.fromisoformat(o["date"]), float(o["value"])))
                return out

            r = requests.get(
                "https://fred.stlouisfed.org/graph/fredgraph.csv",
                params={"id": series, "cosd": start.isoformat()},
                headers={"User-Agent": UA},
                timeout=30,
            )
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["date", "value"]
            df = df[df["value"] != "."].dropna()
            return [
                (pd.to_datetime(d).date(), float(v))
                for d, v in zip(df["date"], df["value"])
            ]
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  ! FRED {series} 失敗: {e}")
                return []
            time.sleep(2 * (attempt + 1))
    return []


def fred_yoy(series: str, **kw):
    """月頻序列轉年增率（%），給 CPI 用。"""
    s = fred(series, **kw)
    if len(s) < 13:
        return []
    out = []
    for i in range(12, len(s)):
        d, v = s[i]
        _, v0 = s[i - 12]
        if v0:
            out.append((d, round((v / v0 - 1) * 100, 2)))
    return out


# ── Yahoo Finance ───────────────────────────────────────────────────
_YF = None


def _yf():
    global _YF
    if _YF is None:
        import yfinance
        _YF = yfinance
    return _YF


def yahoo_history(tickers: list[str], period: str = "3mo") -> pd.DataFrame:
    """一次抓一批，回傳 Close 的 DataFrame（欄=ticker）。"""
    if not tickers:
        return pd.DataFrame()
    try:
        df = _yf().download(
            tickers, period=period, interval="1d",
            auto_adjust=False, progress=False, threads=False, group_by="column",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        close = df["Close"] if "Close" in df.columns.get_level_values(0) else df
        if isinstance(close, pd.Series):
            close = close.to_frame(tickers[0])
        return close.dropna(how="all")
    except Exception as e:  # noqa: BLE001
        print(f"  ! Yahoo 歷史價失敗 {tickers[:3]}...: {e}")
        return pd.DataFrame()


def yahoo_ohlc(tickers: list[str], period: str = "1mo") -> dict:
    """回傳 {"Open": {sym: [(date, v)...]}, "Close": {...}}，給週一批改用。"""
    if not tickers:
        return {}
    try:
        df = _yf().download(
            tickers, period=period, interval="1d",
            auto_adjust=False, progress=False, threads=False, group_by="column",
        )
        if df is None or df.empty:
            return {}
        out = {}
        for field in ("Open", "Close"):
            if field not in df.columns.get_level_values(0):
                continue
            sub = df[field]
            if isinstance(sub, pd.Series):
                sub = sub.to_frame(tickers[0])
            out[field] = {
                sym: [(i.date(), float(v)) for i, v in sub[sym].dropna().items()]
                for sym in sub.columns
            }
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ! Yahoo OHLC 失敗: {e}")
        return {}


def yahoo_marketcaps(tickers: list[str]) -> dict[str, float | None]:
    """
    逐檔抓市值。fast_info 比 .info 快很多也穩很多。
    抓不到就回 None，讓上層把該檔標成缺市值而不是當成 0。
    """
    out: dict[str, float | None] = {}
    for t in tickers:
        mc = None
        try:
            fi = _yf().Ticker(t).fast_info
            mc = fi.get("market_cap") or fi.get("marketCap")
            if not mc:
                shares = fi.get("shares") or fi.get("sharesOutstanding")
                price = fi.get("last_price") or fi.get("lastPrice")
                if shares and price:
                    mc = shares * price
        except Exception:  # noqa: BLE001
            mc = None
        out[t] = float(mc) if mc else None
        time.sleep(0.12)  # 別把 Yahoo 打爆，不然會被暫時封鎖
    return out


# ── CNN Fear & Greed ────────────────────────────────────────────────
def cnn_fear_greed(days: int = 60):
    """
    CNN 的 dataviz JSON endpoint。這不是官方公開 API，隨時可能改，
    所以失敗時 build 會自動退回 manual_input.json 的 fear_greed。
    """
    start = (date.today() - timedelta(days=days)).isoformat()
    try:
        r = requests.get(
            f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        pts = r.json()["fear_and_greed_historical"]["data"]
        return [
            (datetime.utcfromtimestamp(p["x"] / 1000).date(), float(p["y"]))
            for p in pts
        ]
    except Exception as e:  # noqa: BLE001
        print(f"  ! CNN Fear & Greed 失敗: {e}")
        return []


# ── 手動輸入 ─────────────────────────────────────────────────────────
def manual(path) -> dict:
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:  # noqa: BLE001
        return {}


# ── 序列取值 ─────────────────────────────────────────────────────────
def pick(series, as_of: date, freq: str = "weekly"):
    """
    從序列中取「本期 / 上期」。

    weekly/daily → 上期 = as_of 往前 7 天最近的一筆
    monthly      → 上期 = 前一筆不同的觀測值（因為月更資料一週內不會變，
                   照 7 天去比會拿到同一個數字，Delta 永遠是 0）
    """
    s = [(d, v) for d, v in series if d <= as_of]
    if not s:
        return (None, None, None, None)
    cd, cv = s[-1]

    if freq == "monthly":
        for d, v in reversed(s[:-1]):
            if d != cd:
                return (cv, v, cd, d)
        return (cv, None, cd, None)

    cutoff = as_of - timedelta(days=7)
    prior = [(d, v) for d, v in s if d <= cutoff]
    if not prior:
        return (cv, None, cd, None)
    pd_, pv = prior[-1]
    return (cv, pv, cd, pd_)
