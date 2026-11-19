"""
逆勢個股篩選 ＋ 訊號紀錄表（由 flow.py 呼叫，也可單獨跑：python src/contrarian.py）

五個步驟（門檻在 config/contrarian.yaml）：
  1. 第一層（相對量口徑）：個股週數字 > 0、同板塊「其他」個股週數字平均 < 0、個股週均相對量 ≥ 1.5
  2. 連續 2 個完整週都符合才算「確認」；只出現一週的標「首次／未確認」，一樣記錄，用來檢驗確認有沒有用
  3. 自動搜尋訊號週的新聞（Google News RSS ＋ Yahoo 財報日），分成「有消息」與「無明顯消息」
  4. 出場：確認後個股週數字連續 2 週 < 0 → 自動出場訊號；理由被推翻由你在 data/signals_manual.yaml 手填
  5. 第二層確認快照（Yahoo）：放空比例與月變化、機構持股比例、前十大機構增減；ETF 記錄流通份額

另外每檔都附「收盤位置」（CLV，−1 收最低～+1 收最高），用來看盤中是買方還是賣方撐到最後。

輸出：
  docs/contrarian.json   給 docs/contrarian.html
  data/signals.json      訊號紀錄的持久資料（新聞、第二層快照、首次發現日期），每次執行合併更新

注意：這些是量價動能訊號，不是實際的資金淨流入（每筆成交都有買賣雙方）。
"""
from __future__ import annotations

import json
import math
import re
import statistics as st
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
RV_WINDOW = 20
OUT_WEEKS = 12          # 頁面列出最近幾個完整週的篩選結果
MAX_PROVISIONAL_NEWS = 10
HAS_NEWS = "有消息"
NO_NEWS = ("有新聞・無明確催化劑", "未見消息")


# ── 小工具 ───────────────────────────────────────────────────────────
def _r(x, d=4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, d)


def wk_of(ts) -> str:
    y, w, _ = pd.Timestamp(ts).isocalendar()
    return f"{y}-W{w:02d}"


def load_cfg():
    return yaml.safe_load((ROOT / "config" / "contrarian.yaml").read_text(encoding="utf-8"))


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def load_manual():
    p = ROOT / "data" / "signals_manual.yaml"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


# ── 週資料 ───────────────────────────────────────────────────────────
def build_weeks(close, vol, extra):
    """回傳 (週清單, 每週交易日, 週表) 。週表是 dict：欄位 → DataFrame(index=週, columns=ticker)"""
    ret = close.pct_change()
    avgv = vol.rolling(RV_WINDOW, min_periods=10).mean().shift(1)
    rv = vol / avgv
    rel = ret * rv * 100

    clv = None
    ex = extra or {}
    if all(ex.get(k) is not None for k in ("raw", "high", "low")):
        from flow import close_location
        cols = list(close.columns)
        clv = close_location(*(ex[k].reindex(index=close.index, columns=cols) for k in ("raw", "high", "low")))

    idx = ret.index[1:]
    wk = pd.Series([wk_of(x) for x in idx], index=idx)
    weeks = list(dict.fromkeys(wk))
    days = {w: list(wk[wk == w].index) for w in weeks}

    def per_week(fn):
        return pd.DataFrame({w: fn(days[w]) for w in weeks}).T

    tab = {
        "rel": per_week(lambda ds: rel.loc[ds].sum(min_count=1)),
        "rv": per_week(lambda ds: rv.loc[ds].mean()),
        "ret": per_week(lambda ds: ((1 + ret.loc[ds]).prod(min_count=1) - 1) * 100),
        "last": per_week(lambda ds: close.loc[ds[-1]]),
    }
    if clv is not None:
        def wclv(ds):
            c, v = clv.loc[ds], vol.loc[ds]
            m = c.notna() & v.notna() & (v > 0)
            num = (c.where(m) * v.where(m)).sum()
            den = v.where(m).sum()
            return num / den.replace(0, np.nan)
        tab["clv"] = per_week(wclv)
    return weeks, days, tab, clv


def is_complete(w, weeks, days):
    """週五有交易，或後面已經有下一週 → 完整週"""
    return any(d.dayofweek == 4 for d in days[w]) or weeks.index(w) < len(weeks) - 1


def peers_mean(frame, sec_of, tickers):
    """每檔對應「同板塊其他個股」的平均（不含自己）；沒有同伴的板塊回傳 NaN"""
    out = {}
    for t in tickers:
        mates = [p for p in tickers if sec_of[p] == sec_of[t] and p != t]
        out[t] = frame[mates].mean(axis=1) if mates else pd.Series(np.nan, index=frame.index)
    return pd.DataFrame(out)


# ── 第 3 點：新聞 ────────────────────────────────────────────────────
def clean_name(name):
    if not name:
        return None
    n = re.sub(r"[,.]?\s+(Inc|Incorporated|Corp|Corporation|Co|Company|Holdings|Holding|Ltd|plc|PLC|N\.V|S\.A|SE|AG|Group|Technologies|Technology)\b\.?", "", name)
    n = re.sub(r"\s+", " ", n).strip(" ,.")
    return n or None


def google_news(query, start, end, limit):
    import requests
    url = ("https://news.google.com/rss/search?q=" + quote(f"{query} after:{start} before:{end}")
           + "&hl=en-US&gl=US&ceid=US:en")
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 weekly-lab"})
    r.raise_for_status()
    return parse_rss(r.text, limit)


def parse_rss(text, limit):
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            date = parsedate_to_datetime(it.findtext("pubDate")).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            date = None
        items.append({"title": title, "source": source, "date": date, "link": it.findtext("link")})
        if len(items) >= limit:
            break
    return items


def earnings_in(ticker, start, end):
    """Yahoo 財報日是否落在區間內；查不到回傳 None"""
    try:
        import yfinance as yf
        ed = yf.Ticker(ticker).get_earnings_dates(limit=16)
        if ed is None or ed.empty:
            return None
        ds = [pd.Timestamp(x).tz_localize(None).normalize() if pd.Timestamp(x).tzinfo else pd.Timestamp(x).normalize()
              for x in ed.index]
        hit = [d for d in ds if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
        return hit[0].strftime("%Y-%m-%d") if hit else False
    except Exception:  # noqa: BLE001
        return None


def classify(headlines, earnings, cats):
    tags = {}
    for h in headlines:
        low = " " + h["title"].lower() + " "
        hit = [c for c, kws in cats.items() if any(k.lower() in low for k in kws)]
        h["tags"] = hit
        for c in hit:
            tags[c] = tags.get(c, 0) + 1
    if earnings:
        tags["財報"] = tags.get("財報", 0) + 1
    if tags:
        return HAS_NEWS, sorted(tags, key=lambda c: -tags[c])
    return (NO_NEWS[0] if headlines else NO_NEWS[1]), []


def search_news(ticker, name, week_days, ncfg):
    start = (week_days[0] - timedelta(days=ncfg.get("days_before", 3))).strftime("%Y-%m-%d")
    end = (week_days[-1] + timedelta(days=1)).strftime("%Y-%m-%d")
    nm = clean_name(name)
    # 一兩個字母的代號（V、MA、PL、BE…）容易搜到無關新聞，有公司名就只用公司名
    if nm and len(ticker) <= 2:
        q = f'"{nm}" stock'
    elif nm:
        q = f'("{ticker}" OR "{nm}") stock'
    else:
        q = f'"{ticker}" stock'
    res = {"window": [start, end], "query": q, "checked": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    try:
        heads = google_news(q, start, end, ncfg.get("max_headlines", 8))
    except Exception as e:  # noqa: BLE001
        res.update(status="查詢失敗", error=str(e)[:160], headlines=[], tags=[])
        return res
    earn = earnings_in(ticker, week_days[0], week_days[-1])
    status, tags = classify(heads, earn, ncfg.get("catalysts", {}))
    res.update(status=status, tags=tags, earnings=earn or None, headlines=heads)
    time.sleep(1.0)   # 對 Google 客氣一點
    return res


# ── 第 5 點：第二層確認 ──────────────────────────────────────────────
def second_layer(ticker):
    snap = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        snap["name"] = info.get("shortName") or info.get("longName")
        qt = info.get("quoteType")
        snap["type"] = qt
        if qt == "ETF":
            snap["shares_out"] = info.get("sharesOutstanding")
            snap["total_assets"] = info.get("totalAssets")
            return snap
        ss, sp = info.get("sharesShort"), info.get("sharesShortPriorMonth")
        snap["short_pct_float"] = _r(info.get("shortPercentOfFloat"), 4)
        snap["short_chg"] = _r((ss - sp) / sp, 4) if ss and sp else None
        snap["inst_pct"] = _r(info.get("heldPercentInstitutions"), 4)
        try:
            ih = tk.institutional_holders
            if ih is not None and not ih.empty and "pctChange" in ih.columns:
                pc = pd.to_numeric(ih["pctChange"], errors="coerce").dropna()
                snap["inst_top_up"] = int((pc > 0).sum())
                snap["inst_top_down"] = int((pc < 0).sum())
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        snap["error"] = str(e)[:160]
    return snap


# ── 主篩選 ───────────────────────────────────────────────────────────
def screen(weeks, days, tab, sec_of, tickers, cfg):
    sc = cfg["screen"]
    rel, rv = tab["rel"][tickers], tab["rv"][tickers]
    prel = peers_mean(rel, sec_of, tickers)
    pret = peers_mean(tab["ret"][tickers], sec_of, tickers)
    base = (rel > 0) & (prel < 0)
    hit1 = base & (rv >= sc["min_rv"])
    hitc = base & (rv >= sc.get("confirm_min_rv", sc["min_rv"]))

    complete = [w for w in weeks if is_complete(w, weeks, days)]
    streak = pd.DataFrame(0, index=complete, columns=tickers)
    prev = pd.Series(0, index=tickers)
    for w in complete:
        cur = pd.Series(np.where((prev >= 1) & hitc.loc[w], prev + 1, np.where(hit1.loc[w], 1, 0)), index=tickers)
        streak.loc[w] = cur
        prev = cur
    return {"rel": rel, "rv": rv, "prel": prel, "pret": pret, "hit1": hit1, "streak": streak,
            "complete": complete}


def item(t, w, S, tab, sec_of, streak=None):
    g = lambda k: _r(tab[k].loc[w, t], 3) if k in tab else None  # noqa: E731
    return {
        "t": t, "sec": sec_of[t],
        "rel": _r(S["rel"].loc[w, t], 2), "peers": _r(S["prel"].loc[w, t], 2),
        "rv": _r(S["rv"].loc[w, t], 2), "clv": g("clv"),
        "ret": _r(tab["ret"].loc[w, t], 2), "peers_ret": _r(S["pret"].loc[w, t], 2),
        **({"streak": int(streak)} if streak is not None else {}),
    }


def perf(t, w0, tab, sec_of, tickers, horizons, complete):
    """從 w0 週五收盤起，各持有週數的報酬與同板塊其他個股平均（相同期間）"""
    out = {}
    i0 = complete.index(w0)
    last = tab["last"]
    mates = [p for p in tickers if sec_of[p] == sec_of[t] and p != t]
    for h in horizons:
        if i0 + h >= len(complete):
            out[str(h)] = None
            continue
        w1 = complete[i0 + h]
        r = last.loc[w1, t] / last.loc[w0, t] - 1
        pr = (last.loc[w1, mates] / last.loc[w0, mates] - 1).mean() if mates else np.nan
        out[str(h)] = {"ret": _r(r * 100, 2), "peers": _r(pr * 100, 2),
                       "excess": _r((r - pr) * 100, 2) if not np.isnan(pr) else None}
    return out


def episodes(S, tab, sec_of, tickers, cfg, weeks, days):
    complete, streak = S["complete"], S["streak"]
    need = cfg["screen"]["confirm_weeks"]
    weak_n = cfg["exit"]["weak_weeks"]
    hz = cfg["track"]["horizons"]
    eps = []
    for t in tickers:
        i = 0
        while i < len(complete):
            if streak.loc[complete[i], t] != 1:
                i += 1
                continue
            start = complete[i]
            j = i
            while j + 1 < len(complete) and streak.loc[complete[j + 1], t] > streak.loc[complete[j], t]:
                j += 1
            run = complete[i:j + 1]
            conf = run[need - 1] if len(run) >= need else None
            entry = conf or start
            # 第 4 點：自動出場訊號
            ex_w, weak = None, 0
            for w in complete[complete.index(entry) + 1:]:
                weak = weak + 1 if S["rel"].loc[w, t] < 0 else 0
                if weak >= weak_n:
                    ex_w = w
                    break
            ex = None
            if ex_w:
                r = tab["last"].loc[ex_w, t] / tab["last"].loc[entry, t] - 1
                mates = [p for p in tickers if sec_of[p] == sec_of[t] and p != t]
                pr = (tab["last"].loc[ex_w, mates] / tab["last"].loc[entry, mates] - 1).mean() if mates else np.nan
                ex = {"week": ex_w, "date": days[ex_w][-1].strftime("%Y-%m-%d"), "ret": _r(r * 100, 2),
                      "excess": _r((r - pr) * 100, 2) if not np.isnan(pr) else None}
            latest = complete[-1]
            if conf:
                status = "已確認・出場訊號" if ex else "已確認・追蹤中"
            else:
                status = "首次・等下週確認" if start == latest else "未確認"
            eps.append({
                "key": f"{t}@{start}", "t": t, "sec": sec_of[t],
                "start": start, "start_date": days[start][-1].strftime("%Y-%m-%d"),
                "weeks": run, "confirmed": conf,
                "confirm_date": days[conf][-1].strftime("%Y-%m-%d") if conf else None,
                "at_start": item(t, start, S, tab, sec_of),
                "perf": perf(t, start, tab, sec_of, tickers, hz, complete),
                "perf_confirm": perf(t, conf, tab, sec_of, tickers, hz, complete) if conf else None,
                "exit": ex, "status": status,
            })
            i = j + 1
    return eps


# ── 統計：各種分組的勝率 ─────────────────────────────────────────────
def stats(eps, h):
    def group(rows, key):
        xs = [((e[key] or {}).get(str(h)) or {}).get("excess") for e in rows if e.get(key)]
        xs = [x for x in xs if x is not None]
        if not xs:
            return {"n": 0}
        return {"n": len(xs), "win": _r(sum(x > 0 for x in xs) / len(xs) * 100, 1),
                "avg": _r(st.mean(xs), 2), "median": _r(st.median(xs), 2)}

    def news_status(e):
        n = e.get("news") or {}
        return (n.get(e["start"]) or {}).get("status")

    rows = [
        ("全部首次訊號（從首次週起算）", group(eps, "perf")),
        ("已確認（從確認週起算）", group([e for e in eps if e["confirmed"]], "perf_confirm")),
        ("未確認（只出現一週）", group([e for e in eps if not e["confirmed"]], "perf")),
        ("有消息", group([e for e in eps if news_status(e) == HAS_NEWS], "perf")),
        ("無明顯消息", group([e for e in eps if news_status(e) in NO_NEWS], "perf")),
        ("訊號週收盤位置偏高（≥ 0.3）", group([e for e in eps if (e["at_start"].get("clv") or -9) >= 0.3], "perf")),
        ("訊號週收盤位置不高（< 0.3）", group([e for e in eps if e["at_start"].get("clv") is not None
                                          and e["at_start"]["clv"] < 0.3], "perf")),
    ]
    return [{"label": k, **v} for k, v in rows]


# ── 主流程 ───────────────────────────────────────────────────────────
def run(close, vol, extra=None, today=None):
    from flow import load_universe
    cfg = load_cfg()
    sec_of, order, _ = load_universe(extended=True)
    tickers = [t for t in sec_of if t in close.columns and close[t].notna().sum() > RV_WINDOW]
    close, vol = close[tickers], vol[tickers].astype(float)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    weeks, days, tab, _ = build_weeks(close, vol, extra)
    S = screen(weeks, days, tab, sec_of, tickers, cfg)
    complete = S["complete"]
    lonely = sorted({s for s in set(sec_of.values()) if sum(1 for t in tickers if sec_of[t] == s) < 2})

    # 合併持久資料
    store_p = ROOT / "data" / "signals.json"
    store = load_json(store_p, {"names": {}, "episodes": {}})
    names = store.get("names", {})
    old = store.get("episodes", {})
    manual = load_manual()
    ncfg = cfg.get("news", {})
    first_run = not old

    eps = episodes(S, tab, sec_of, tickers, cfg, weeks, days)
    window_start = complete[0] if complete else None
    keep_frozen = {k: v for k, v in old.items() if window_start and v.get("start", "") < window_start}

    recent = set(complete[-ncfg.get("backfill_weeks", 12):])
    active_status = ("首次・等下週確認", "已確認・追蹤中")
    latest_key = {}
    for e in eps:
        if e["t"] not in latest_key or e["start"] > latest_key[e["t"]][1]:
            latest_key[e["t"]] = (e["key"], e["start"])
    for e in eps:
        o = old.get(e["key"], {})
        e["first_seen"] = o.get("first_seen") or today
        # 首次發現日距離訊號週五超過 4 天 → 回溯算出來的，不是當時即時看到的
        e["backfill"] = (pd.Timestamp(e["first_seen"]) - pd.Timestamp(e["start_date"])).days > 4
        e["news"] = o.get("news", {})
        e["snapshots"] = o.get("snapshots", [])

        # 第 3 點：訊號週與確認週各搜一次；失敗的下次重試
        for w in [e["start"]] + ([e["confirmed"]] if e["confirmed"] and e["confirmed"] != e["start"] else []):
            cur = e["news"].get(w)
            if cur and cur.get("status") != "查詢失敗":
                continue
            if not ncfg.get("enabled", True):
                continue
            if w not in recent:
                e["news"][w] = {"status": "未查（回溯）"}
                continue
            if e["t"] not in names:
                names[e["t"]] = second_layer(e["t"]).get("name")
            e["news"][w] = search_news(e["t"], names.get(e["t"]), days[w], ncfg)

        # 第 5 點：追蹤中的訊號每週拍一次第二層快照
        if e["status"] in active_status:
            wk_now = wk_of(pd.Timestamp(today))
            if not e["snapshots"] or wk_of(pd.Timestamp(e["snapshots"][-1]["date"])) != wk_now:
                snap = second_layer(e["t"])
                if snap.get("name"):
                    names[e["t"]] = snap["name"]
                e["snapshots"] = (e["snapshots"] + [snap])[-12:]

        # 第 4 點：手填的「理由被推翻」
        # 「代號@週」指定某一筆；只寫「代號」就套用在該檔最新一筆
        m = manual.get(e["key"]) or (manual.get(e["t"]) if latest_key[e["t"]][0] == e["key"] else None) or {}
        if m:
            e["manual"] = {k: (str(v) if v is not None else None) for k, v in m.items()}
            if m.get("thesis_broken"):
                e["status"] = "手動出場"
        e["name"] = names.get(e["t"])

    all_eps = {e["key"]: e for e in eps}
    all_eps.update(keep_frozen)
    ep_list = sorted(all_eps.values(), key=lambda e: (e["start"], e["t"]), reverse=True)

    # 頁面用：最近幾個完整週的篩選結果
    def news_for(t, w):
        for e in ep_list:
            if e["t"] == t and w in e.get("weeks", []):
                return (e.get("news") or {}).get(w)
        return None

    week_rows = []
    for w in complete[-OUT_WEEKS:][::-1]:
        hits = [t for t in tickers if S["hit1"].loc[w, t]]
        week_rows.append({
            "week": w, "dates": [days[w][0].strftime("%m/%d"), days[w][-1].strftime("%m/%d")],
            "items": sorted([{**item(t, w, S, tab, sec_of, S["streak"].loc[w, t]), "news": news_for(t, w)}
                             for t in hits], key=lambda x: -(x["rel"] or 0)),
        })

    # 進行中的這一週（尚未完整）：暫定名單
    prov = None
    if weeks and weeks[-1] not in complete:
        w = weeks[-1]
        base = (S["rel"].loc[w] > 0) & (S["prel"].loc[w] < 0) & (S["rv"].loc[w] >= cfg["screen"]["min_rv"])
        items = []
        for t in [t for t in tickers if base[t]][:MAX_PROVISIONAL_NEWS]:
            it = item(t, w, S, tab, sec_of)
            it["prev_hit"] = bool(complete and S["hit1"].loc[complete[-1], t])
            if ncfg.get("enabled", True):
                if t not in names:
                    names[t] = second_layer(t).get("name")
                it["news"] = search_news(t, names.get(t), days[w], ncfg)
            items.append(it)
        prov = {"week": w, "dates": [d.strftime("%m/%d") for d in days[w]],
                "items": sorted(items, key=lambda x: -(x["rel"] or 0))}

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "asof": close.index[-1].strftime("%Y-%m-%d"),
        "rules": cfg, "lonely_sectors": lonely, "first_run": first_run,
        "provisional": prov, "weeks": week_rows, "signals": ep_list,
        "stats": {str(h): stats(ep_list, h) for h in cfg["track"]["horizons"]},
    }
    (ROOT / "docs" / "contrarian.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    store_p.write_text(json.dumps({"names": names, "episodes": all_eps}, ensure_ascii=False, indent=1,
                                  default=str), encoding="utf-8")
    n_conf = sum(1 for e in ep_list if e.get("confirmed"))
    print(f"逆勢個股：{len(ep_list)} 筆訊號（確認 {n_conf}），本週暫定 {len(prov['items']) if prov else 0} 檔")
    return out


if __name__ == "__main__":
    from flow import fetch, load_universe
    sec, _, _ = load_universe(extended=True)
    c, v, x = fetch(list(sec))
    run(c, v, x)
