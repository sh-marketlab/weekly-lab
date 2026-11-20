"""
量價背離（由 flow.py 呼叫）

單日四種（門檻在 config/divergence.yaml）：
  虛漲     漲 ≥ 1%、相對量 < 0.8                      漲得沒人跟，預期偏空
  量縮跌   跌 ≥ 1%、相對量 < 0.8                      賣壓減輕，預期偏多
  承接     跌 ≥ 0.5%、相對量 ≥ 1.5、收盤位置 ≥ 0.3     大量下跌卻收高檔，有人在下面接，預期偏多
  出貨     漲 ≥ 0.5%、相對量 ≥ 1.5、收盤位置 ≤ −0.3    大量上漲卻收低檔，有人趁高賣，預期偏空

週層級：
  量能衰退（漲）  週漲 ≥ 1%，但最後兩天平均相對量 ≤ 前兩天 × 0.7    漲勢後繼無力，預期偏空
  量能衰退（跌）  週跌 ≥ 1%，但最後兩天平均相對量 ≤ 前兩天 × 0.7    賣壓逐漸用完，預期偏多

紀錄：用過去一年的資料找出每一次背離，追蹤 5／10／20／40 個交易日後
相對同板塊其他個股的超額報酬，看「符合預期方向」的比例。全部由價格資料重算，不需要另存。

輸出 docs/divergence.json（給 flow.html 的「背離」模式）。
"""
from __future__ import annotations

import json
import math
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
RV_WINDOW = 20

TYPES = {
    # 代碼: (名稱, 預期方向 +1 偏多／−1 偏空, 說明)
    "fake_up": ("虛漲", -1, "漲但量縮：漲得沒人跟"),
    "quiet_down": ("量縮跌", +1, "跌但量縮：賣壓減輕"),
    "absorb": ("承接", +1, "大量下跌卻收在高檔：有人在下面接"),
    "distrib": ("出貨", -1, "大量上漲卻收在低檔：有人趁高賣"),
    "fade_up": ("量能衰退・漲", -1, "整週上漲但量一天比一天少：漲勢後繼無力"),
    "fade_down": ("量能衰退・跌", +1, "整週下跌但量一天比一天少：賣壓逐漸用完"),
}


def load_cfg():
    return yaml.safe_load((ROOT / "config" / "divergence.yaml").read_text(encoding="utf-8"))


def _r(x, d=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, d)


# ── 判斷規則（flow.py 也用這兩個函式，網頁與紀錄才會一致）──────────────
def day_code(r_pct, rv, clv, cfg):
    """r_pct：漲跌幅（%）。回傳背離代碼或 None"""
    c = cfg["day"]
    if r_pct is None or rv is None or np.isnan(r_pct) or np.isnan(rv):
        return None
    has_clv = clv is not None and not np.isnan(clv)
    if abs(r_pct) >= c["min_move"] and rv >= c["high_rv"] and has_clv:
        if r_pct < 0 and clv >= c["clv"]:
            return "absorb"
        if r_pct > 0 and clv <= -c["clv"]:
            return "distrib"
    if abs(r_pct) >= c.get("lowvol_min_move", c["min_move"]) and rv < c["low_rv"]:
        return "fake_up" if r_pct > 0 else "quiet_down"
    return None


def day_codes_frame(ret_pct, rv, clv, cfg):
    """day_code 的向量版（整張表一次算），規則必須跟 day_code 一致"""
    c = cfg["day"]
    out = pd.DataFrame(None, index=ret_pct.index, columns=ret_pct.columns, dtype=object)
    ok = ret_pct.notna() & rv.notna()
    if clv is not None:
        big = ok & clv.notna() & (ret_pct.abs() >= c["min_move"]) & (rv >= c["high_rv"])
        out[big & (ret_pct < 0) & (clv >= c["clv"])] = "absorb"
        out[big & (ret_pct > 0) & (clv <= -c["clv"])] = "distrib"
    low = ok & out.isna() & (ret_pct.abs() >= c.get("lowvol_min_move", c["min_move"])) & (rv < c["low_rv"])
    out[low & (ret_pct > 0)] = "fake_up"
    out[low & (ret_pct < 0)] = "quiet_down"
    return out


def week_code(rets_pct, rvs, cfg):
    """一週的每日漲跌幅（%）與相對量清單。回傳 fade_up／fade_down 或 None"""
    c = cfg["week"]
    pairs = [(r, v) for r, v in zip(rets_pct, rvs)
             if r is not None and v is not None and not np.isnan(r) and not np.isnan(v)]
    if len(pairs) < c["min_days"]:
        return None
    wret = (np.prod([1 + r / 100 for r, _ in pairs]) - 1) * 100
    if abs(wret) < c["min_move"]:
        return None
    first = np.mean([v for _, v in pairs[:2]])
    last = np.mean([v for _, v in pairs[-2:]])
    if first > 0 and last <= first * c["fade_ratio"]:
        return "fade_up" if wret > 0 else "fade_down"
    return None


# ── 紀錄與追蹤 ───────────────────────────────────────────────────────
def run(close, vol, extra=None):
    from flow import close_location, load_universe
    cfg = load_cfg()
    sec_of, _, _ = load_universe(extended=True)
    tickers = [t for t in sec_of if t in close.columns and close[t].notna().sum() > RV_WINDOW]
    close, vol = close[tickers], vol[tickers].astype(float)

    ret = close.pct_change() * 100
    rv = vol / vol.rolling(RV_WINDOW, min_periods=10).mean().shift(1)
    ex = extra or {}
    clv = None
    if all(ex.get(k) is not None for k in ("raw", "high", "low")):
        clv = close_location(*(ex[k].reindex(index=close.index, columns=tickers) for k in ("raw", "high", "low")))

    idx = close.index
    pos = {d: i for i, d in enumerate(idx)}
    hz = cfg["track"]["days"]

    # 事先算好每個持有天數的「個股報酬」與「同板塊其他個股平均報酬」（不含自己）
    fwd, peer = {}, {}
    secs = pd.Series(sec_of)[tickers]
    for n in hz:
        f = close.shift(-n) / close - 1
        fwd[n] = f
        ssum = f.T.groupby(secs).transform("sum").T
        scnt = f.notna().T.groupby(secs).transform("sum").T
        peer[n] = (ssum - f.fillna(0)) / (scnt - f.notna().astype(int)).replace(0, np.nan)

    def follow(t, d):
        """從 d 收盤起，持有 n 個交易日：個股報酬、超額（減同板塊其他個股平均）"""
        out = {}
        for n in hz:
            r, pr = fwd[n].loc[d, t], peer[n].loc[d, t]
            if pd.isna(r):
                out[str(n)] = None
                continue
            out[str(n)] = {"ret": _r(r * 100), "excess": _r((r - pr) * 100) if not pd.isna(pr) else None}
        return out

    events = []
    # 單日
    codes = day_codes_frame(ret, rv, clv, cfg).iloc[RV_WINDOW + 1:]
    stacked = codes.stack()
    stacked = stacked[stacked.map(lambda v: isinstance(v, str))]
    for (d, t), c in stacked.items():
        events.append({"date": d.strftime("%Y-%m-%d"), "t": t, "sec": sec_of[t], "type": c,
                       "r": _r(ret.loc[d, t]), "rv": _r(rv.loc[d, t]),
                       "clv": None if clv is None else _r(clv.loc[d, t]),
                       "c": _r(close.loc[d, t]), "f": follow(t, d)})
    # 週層級（只算完整週：週五有交易，或後面已經有下一週）
    iso = idx.isocalendar()
    wk = pd.Series([f"{y}-W{w:02d}" for y, w in zip(iso.year, iso.week)], index=idx)
    wlist = list(dict.fromkeys(wk))
    for j, w in enumerate(wlist):
        ds = list(wk[wk == w].index)
        if not (any(x.dayofweek == 4 for x in ds) or j < len(wlist) - 1):
            continue
        if pos[ds[0]] <= RV_WINDOW:
            continue
        for t in tickers:
            c = week_code(list(ret.loc[ds, t]), list(rv.loc[ds, t]), cfg)
            if c:
                d = ds[-1]
                wret = ((1 + ret.loc[ds, t] / 100).prod() - 1) * 100
                events.append({"date": d.strftime("%Y-%m-%d"), "week": w, "t": t, "sec": sec_of[t], "type": c,
                               "r": _r(wret), "rv_first": _r(rv.loc[ds[:2], t].mean()),
                               "rv_last": _r(rv.loc[ds[-2:], t].mean()), "c": _r(close.loc[d, t]),
                               "f": follow(t, d)})

    # 統計：各類型「符合預期方向」的比例與平均超額
    stats = {}
    for code, (name, direction, note) in TYPES.items():
        row = {"name": name, "dir": direction, "note": note}
        evs = [e for e in events if e["type"] == code]
        row["count"] = len(evs)
        for n in hz:
            xs = [(e["f"].get(str(n)) or {}).get("excess") for e in evs]
            xs = [x for x in xs if x is not None]
            row[str(n)] = {"n": len(xs),
                           "hit": _r(sum((x * direction) > 0 for x in xs) / len(xs) * 100, 1) if xs else None,
                           "avg": _r(st.mean(xs)) if xs else None}
        stats[code] = row

    cutoff = idx[-1] - pd.Timedelta(weeks=cfg["track"]["recent_weeks"])
    recent = sorted([e for e in events if pd.Timestamp(e["date"]) >= cutoff],
                    key=lambda e: (e["date"], e["type"], e["t"]), reverse=True)
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "asof": idx[-1].strftime("%Y-%m-%d"), "since": idx[RV_WINDOW + 1].strftime("%Y-%m-%d"),
        "rules": cfg, "types": {k: {"name": v[0], "dir": v[1], "note": v[2]} for k, v in TYPES.items()},
        "stats": stats, "recent": recent, "total": len(events),
    }
    (ROOT / "docs" / "divergence.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"量價背離：一年 {len(events)} 筆，近 {cfg['track']['recent_weeks']} 週 {len(recent)} 筆")
    return out
