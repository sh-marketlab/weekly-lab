"""
量價資金流：每日「漲跌幅 × 成交量」的自動化版本，輸出 docs/flow.json 給 docs/flow.html。

同時算三種口徑，方便並排比較：
  raw    = 漲跌幅 × 成交股數          （原本手動 Excel 的算法）
  dollar = 漲跌幅 × 收盤價 × 成交股數  （換成美元，大小股票可以相加）
  rel    = 漲跌幅 × 相對成交量 × 100   （相對量 = 當日量 ÷ 前 20 日均量；每檔權重一樣）

注意：這三個都是「量價動能」指標，不是實際的資金流入流出（每筆成交都有買方和賣方）。

另外做一個「週四 vs 週五」的檢驗：統計每加入一天，累計方向被翻轉的比例。
如果週五的翻轉比例明顯高於週二～週四，「週四是假象」才有數據支持。

版本：
  flow.json      原版：flow_universe.yaml 裡沒有 extended: true 的群組（原本 77 檔）
  flow_ext.json  新版：全部群組（含奢侈精品、美國債券）

用法：python src/flow.py
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
WEEKS_DETAIL = 8      # 頁面可切換的週數
WEEKS_HISTORY = 12    # 板塊熱圖的週數
RV_WINDOW = 20        # 相對量的均量天數
DAY_NAMES = ["週一", "週二", "週三", "週四", "週五"]


VIX_STRESS = 20       # 週四收盤 VIX 高於此值 → 該週算「壓力週」


def load_universe(extended: bool = False):
    """回傳 (ticker→板塊, 板塊順序, 群組清單)。yaml 可用 groups（有分組）或舊的 sectors（不分組）。
    extended=False 時略過標記 extended: true 的群組（原版）；True 時全部納入（新版）。"""
    cfg = yaml.safe_load((ROOT / "config" / "flow_universe.yaml").read_text(encoding="utf-8"))
    groups = cfg.get("groups") or [{"name": "", "note": "", "sectors": cfg["sectors"]}]
    sec_of, order, glist = {}, [], []
    for g in groups:
        if g.get("extended") and not extended:
            continue
        glist.append({"name": g["name"], "note": g.get("note", ""), "sectors": [s["name"] for s in g["sectors"]],
                      "extended": bool(g.get("extended"))})
        for s in g["sectors"]:
            order.append(s["name"])
            for t in s["tickers"]:
                sec_of[t] = s["name"]
    return sec_of, order, glist


def fetch(tickers, period="1y"):
    import yfinance as yf
    df = yf.download(tickers, period=period, interval="1d", auto_adjust=True,
                     progress=False, threads=True, group_by="column")
    close = df["Close"].copy()
    vol = df["Volume"].copy()
    if isinstance(close, pd.Series):
        close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
    close.index = pd.to_datetime(close.index).tz_localize(None)
    vol.index = close.index
    return close.dropna(how="all"), vol.reindex(close.dropna(how="all").index)


def _r(x, d=4):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), d)


def _sign(x):
    return 0 if x is None or x == 0 or (isinstance(x, float) and math.isnan(x)) else (1 if x > 0 else -1)


def fetch_vix(period="1y"):
    import yfinance as yf
    df = yf.download("^VIX", period=period, interval="1d", auto_adjust=True, progress=False)
    c = df["Close"]
    if isinstance(c, pd.DataFrame):
        c = c.iloc[:, 0]
    c.index = pd.to_datetime(c.index).tz_localize(None)
    return c.dropna()


def compute(close: pd.DataFrame, vol: pd.DataFrame, sec_of: dict, order: list,
            groups: list | None = None, vix: pd.Series | None = None) -> dict:
    tickers = [t for t in sec_of if t in close.columns and close[t].notna().sum() > RV_WINDOW]
    missing = [t for t in sec_of if t not in tickers]
    close, vol = close[tickers], vol[tickers].astype(float)

    ret = close.pct_change()
    avgv = vol.rolling(RV_WINDOW, min_periods=10).mean().shift(1)
    rv = vol / avgv
    raw = ret * vol
    dollar = ret * close * vol
    rel = ret * rv * 100

    idx = ret.index[1:]
    iso = idx.isocalendar()
    wkkey = [f"{y}-W{w:02d}" for y, w in zip(iso.year, iso.week)]
    days = pd.DataFrame({"date": idx, "wk": wkkey, "dow": idx.dayofweek}, index=idx)
    weeks = list(dict.fromkeys(wkkey))

    def week_block(wk):
        d = days[days.wk == wk]
        dates = list(d.index)
        thu = [x for x in dates if x.dayofweek <= 3]
        complete = any(x.dayofweek == 4 for x in dates)

        def agg(frame, ds, how="sum"):
            if not ds:
                return pd.Series(np.nan, index=frame.columns)
            sub = frame.loc[ds]
            return sub.sum(min_count=1) if how == "sum" else sub.mean()

        def cumret(ds):
            if not ds:
                return pd.Series(np.nan, index=ret.columns)
            return ((1 + ret.loc[ds]).prod(min_count=1) - 1) * 100

        T = {
            "raw4": agg(raw, thu), "raw5": agg(raw, dates),
            "dollar4": agg(dollar, thu), "dollar5": agg(dollar, dates),
            "rel4": agg(rel, thu), "rel5": agg(rel, dates),
            "ret4": cumret(thu), "ret5": cumret(dates),
        }
        trows = []
        for t in tickers:
            trows.append({
                "t": t, "sec": sec_of[t],
                "close": _r(close.loc[dates[-1], t], 2),
                "days": [{"r": _r(ret.loc[x, t] * 100, 2), "v": _r(vol.loc[x, t], 0), "rv": _r(rv.loc[x, t], 2),
                          "dollar": _r(dollar.loc[x, t], 0)} for x in dates],
                **{k: _r(v[t], 4 if k.startswith("rel") or k.startswith("ret") else 0) for k, v in T.items()},
                "flip": _sign(T["dollar4"][t]) != _sign(T["dollar5"][t]) if complete and thu else False,
            })

        # 每日市場合計與家數
        mdays = []
        for x in dates:
            r_ = ret.loc[x]
            mdays.append({
                "date": x.strftime("%Y-%m-%d"), "dow": DAY_NAMES[x.dayofweek],
                "raw": _r(raw.loc[x].sum(), 0), "dollar": _r(dollar.loc[x].sum(), 0),
                "rel": _r(rel.loc[x].mean(), 3), "up": int((r_ > 0).sum()), "down": int((r_ < 0).sum()),
            })

        def market(ds, suffix):
            if not ds:
                return None
            cr = cumret(ds)
            dol = agg(dollar, ds)
            top = dol.abs().sort_values(ascending=False)
            tot_abs = dol.abs().sum()
            return {
                "raw": _r(agg(raw, ds).sum(), 0), "dollar": _r(dol.sum(), 0),
                "rel": _r(agg(rel, ds).mean(), 3),
                "up": int((cr > 0).sum()), "down": int((cr < 0).sum()),
                "top5": [{"t": t, "dollar": _r(dol[t], 0)} for t in top.index[:5]],
                "top5_share": _r(top.iloc[:5].sum() / tot_abs * 100, 1) if tot_abs else None,
            }

        srows = []
        for s in order:
            mem = [t for t in tickers if sec_of[t] == s]
            if not mem:
                continue
            row = {"name": s, "n": len(mem)}
            for k in ("raw4", "raw5", "dollar4", "dollar5"):
                row[k] = _r(T[k][mem].sum(), 0)
            for k in ("rel4", "rel5", "ret4", "ret5"):
                row[k] = _r(T[k][mem].mean(), 3)
            row["up5"] = int((T["ret5"][mem] > 0).sum())
            row["down5"] = int((T["ret5"][mem] < 0).sum())
            row["daily"] = [{"dollar": _r(dollar.loc[x, mem].sum(), 0), "rel": _r(rel.loc[x, mem].mean(), 3),
                             "up": int((ret.loc[x, mem] > 0).sum()), "down": int((ret.loc[x, mem] < 0).sum())}
                            for x in dates]
            srows.append(row)
        for k in ("raw5", "dollar5", "rel5"):
            ranked = sorted([r for r in srows if r[k] is not None], key=lambda r: -r[k])
            for i, r in enumerate(ranked, 1):
                r["rank_" + k] = i

        return {
            "week": wk, "complete": complete,
            "dates": [x.strftime("%Y-%m-%d") for x in dates],
            "dows": [DAY_NAMES[x.dayofweek] for x in dates],
            "market": {"thu": market(thu, "4"), "full": market(dates, "5"), "days": mdays},
            "sectors": srows, "tickers": trows,
        }

    detail = [week_block(w) for w in weeks[-WEEKS_DETAIL:]]

    # 板塊 × 週 熱圖（相對量口徑與金額口徑）
    hist = []
    for w in weeks[-WEEKS_HISTORY:]:
        ds = list(days[days.wk == w].index)
        row = {"week": w, "start": ds[0].strftime("%m/%d"), "sectors": {}}
        for s in order:
            mem = [t for t in tickers if sec_of[t] == s]
            if mem:
                row["sectors"][s] = {"rel": _r(rel.loc[ds, mem].sum().mean(), 3),
                                     "dollar": _r(dollar.loc[ds, mem].sum().sum(), 0)}
        hist.append(row)

    # 週四 vs 週五 檢驗：每加入一天，累計方向（金額口徑）被翻轉的比例
    full_weeks = []
    for w in weeks:
        ds = list(days[days.wk == w].index)
        if [x.dayofweek for x in ds] == [0, 1, 2, 3, 4]:
            full_weeks.append(ds)

    def flip_stats(series_fn, wks=None):
        flips = {d: [0, 0] for d in (1, 2, 3, 4)}
        for ds in (full_weeks if wks is None else wks):
            cum = None
            for i, x in enumerate(ds):
                v = series_fn(x)
                prev = cum
                cum = v if cum is None else cum + v
                if i >= 1:
                    for a, b in zip(np.atleast_1d(prev), np.atleast_1d(cum)):
                        if _sign(a) == 0:
                            continue
                        flips[i][1] += 1
                        flips[i][0] += int(_sign(a) != _sign(b))
        return {DAY_NAMES[d]: {"rate": _r(f[0] / f[1] * 100, 1) if f[1] else None, "n": f[1]} for d, f in flips.items()}

    sec_mem = {s: [t for t in tickers if sec_of[t] == s] for s in order}
    sec_mem = {s: m for s, m in sec_mem.items() if m}
    test = {
        "weeks": len(full_weeks),
        "first_week": full_weeks[0][0].strftime("%Y-%m-%d") if full_weeks else None,
        "ticker": flip_stats(lambda x: dollar.loc[x].fillna(0).values),
        "sector": flip_stats(lambda x: np.array([dollar.loc[x, m].sum() for m in sec_mem.values()])),
        "market": flip_stats(lambda x: dollar.loc[x].sum()),
    }

    # 依 VIX 分組：週四收盤 VIX > VIX_STRESS 為壓力週，比較兩組的週五行為
    if vix is not None and len(vix):
        def regime_stats(wks):
            if not wks:
                return None
            fri_dollar = [dollar.loc[ds[4]].sum() for ds in wks]
            fri_ret = [ret.loc[ds[4]].mean() * 100 for ds in wks]
            mt_ret = [ret.loc[ds[:4]].mean(axis=1).mean() * 100 for ds in wks]
            fri_rv = [rv.loc[ds[4]].median() for ds in wks]
            mt_rv = [rv.loc[ds[:4]].median(axis=1).mean() for ds in wks]
            return {
                "weeks": len(wks),
                "ticker": flip_stats(lambda x: dollar.loc[x].fillna(0).values, wks),
                "market": flip_stats(lambda x: dollar.loc[x].sum(), wks),
                "fri_down_share": _r(np.mean([v < 0 for v in fri_dollar]) * 100, 1),
                "fri_ret": _r(np.mean(fri_ret), 3), "monthu_ret": _r(np.mean(mt_ret), 3),
                "fri_rv": _r(np.nanmean(fri_rv), 2), "monthu_rv": _r(np.nanmean(mt_rv), 2),
            }
        vthu = {}
        for ds in full_weeks:
            v = vix[vix.index <= ds[3]]
            vthu[ds[0]] = float(v.iloc[-1]) if len(v) else None
        stress = [ds for ds in full_weeks if vthu.get(ds[0]) is not None and vthu[ds[0]] > VIX_STRESS]
        calm = [ds for ds in full_weeks if vthu.get(ds[0]) is not None and vthu[ds[0]] <= VIX_STRESS]
        test["regime"] = {"threshold": VIX_STRESS, "stress": regime_stats(stress), "calm": regime_stats(calm),
                          "vix_now": _r(vix.iloc[-1], 2)}

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "asof": idx[-1].strftime("%Y-%m-%d"),
        "universe": len(tickers), "missing": missing,
        "sector_order": [s for s in order if s in sec_mem],
        "groups": [{**g, "sectors": [s for s in g["sectors"] if s in sec_mem]} for g in (groups or [])],
        "weeks": detail[::-1],
        "history": hist,
        "thursday_test": test,
    }


VERSIONS = [
    # (檔名, 是否納入 extended 群組, 頁面標籤)
    ("flow.json", False, "原版"),
    ("flow_ext.json", True, "新版（含奢侈品・美債）"),
]


def main():
    full_sec, _, _ = load_universe(extended=True)
    print(f"抓 {len(full_sec)} 檔…")
    close, vol = fetch(list(full_sec))
    try:
        vix = fetch_vix()
    except Exception as e:  # noqa: BLE001
        print(f"  ! VIX 抓取失敗，略過分組檢驗: {e}")
        vix = None
    for fname, ext, label in VERSIONS:
        sec_of, order, groups = load_universe(extended=ext)
        cols = [t for t in sec_of if t in close.columns]
        out = compute(close[cols], vol[cols], sec_of, order, groups, vix)
        out["version"] = {"key": "ext" if ext else "base", "label": label}
        path = ROOT / "docs" / fname
        path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"完成 {label} → {path}  截至 {out['asof']}，{out['universe']} 檔，缺 {out['missing']}")


if __name__ == "__main__":
    main()
