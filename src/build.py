#!/usr/bin/env python3
"""
每週快照建置器。

    python src/build.py --stage thu     # 台灣週五 05:00，抓美股週四收盤
    python src/build.py --stage fri     # 台灣週六 05:00，抓美股週五收盤

產出 data/weekly/{ISO週}-{stage}.json 並複製一份到 docs/latest.json。
"""
from __future__ import annotations

import argparse
import statistics as st
from datetime import date, timedelta

import yaml

import common as C
import predict as P
import sources as S

# ── 門檻集中管理（與 playbook.py 的 TH 同風格，改這裡就好）──────────
TH = {
    "yield_spike_bp": 15,        # 背離 A：2Y 週變動 ≥ +15bp 算飆升
    "gold_surge": 0.02,          # 背離 A：黃金週漲 ≥ 2%
    "breadth_gap": 0.02,         # 背離 B：龍頭平均 − 3-10名平均 ≥ 2pp
    "dxy_surge": 0.01,           # 背離 C：DXY 週漲 ≥ 1%
    "risk_surge": 0.03,          # 背離 C：BTC/科技股週漲 ≥ 3%
    "oas_widen": 0.15,           # 背離 D：HY OAS 週擴大 ≥ 15bp
    "equity_up": 0.005,          # 背離 D：大盤仍收紅
    "stale_days": 10,            # 資料超過這天數沒更新就掛 stale 旗標
}


# ── 第 1 項：17 項總體指標 ───────────────────────────────────────────
def manual_slots() -> list[dict]:
    """列出所有『可以/需要』手動填的指標，給儀表板的設定分頁用。"""
    cfg = yaml.safe_load((C.CONFIG / "indicators.yaml").read_text(encoding="utf-8"))
    out = []
    for ind in cfg["indicators"]:
        src = ind["source"]
        key = None
        if src["type"] == "manual":
            key, mode = src["key"], "required"
        elif ind.get("manual_fallback"):
            key, mode = ind["manual_fallback"], "fallback"
        elif src["type"] == "cnn_fg":
            key, mode = "fear_greed", "fallback"
        if not key:
            continue
        out.append({"id": ind["id"], "name": ind["name"], "key": key, "mode": mode,
                    "unit": ind.get("unit"), "freq": ind.get("freq"),
                    "url": ind.get("manual_url")})
    return out


def build_macro(as_of: date) -> tuple[list[dict], dict, list[str]]:
    cfg = yaml.safe_load((C.CONFIG / "indicators.yaml").read_text(encoding="utf-8"))
    man = S.manual(C.DATA / "manual_input.json")
    rows, cache, pending = [], {}, []

    for ind in cfg["indicators"]:
        src, freq = ind["source"], ind.get("freq", "weekly")
        series, origin = [], ""

        if src["type"] == "fred":
            sid = src["series"]
            series = S.fred_yoy(sid) if src.get("transform") == "yoy_pct" else S.fred(sid)
            origin = f"FRED:{sid}"
        elif src["type"] == "yahoo":
            df = S.yahoo_history([src["ticker"]], period="6mo")
            if not df.empty:
                col = df.columns[0]
                series = [(i.date(), float(v)) for i, v in df[col].dropna().items()]
            origin = f"Yahoo:{src['ticker']}"
            if not series and ind.get("manual_fallback"):
                series = _manual_series(man, ind["manual_fallback"])
                origin = "手動"
        elif src["type"] == "cnn_fg":
            series = S.cnn_fear_greed()
            origin = "CNN Fear & Greed"
            if not series:
                series = _manual_series(man, "fear_greed")
                origin = "手動"
        elif src["type"] == "manual":
            series = _manual_series(man, src["key"])
            origin = "手動"
        elif src["type"] == "derived":
            rows.append({"id": ind["id"], "name": ind["name"], "_defer": True})
            continue

        curr, prev, cd, pd_ = S.pick(series, as_of, freq)
        if curr is None:
            pending.append(f'{ind["id"]} {ind["name"]}')

        delta = None if (curr is None or prev is None) else curr - prev
        rows.append(_row(ind, curr, prev, cd, pd_, delta, origin, as_of))
        if src["type"] == "fred":
            cache[src["series"]] = series

    # #4 淨流動性：三個序列一律對齊到同一個週三，不然是拿蘋果比橘子
    nl = _net_liquidity(cache, as_of)
    for i, r in enumerate(rows):
        if r.get("_defer"):
            ind = next(x for x in cfg["indicators"] if x["id"] == r["id"])
            curr, prev, cd, pd_ = S.pick(nl, as_of, "weekly")
            delta = None if (curr is None or prev is None) else curr - prev
            rows[i] = _row(ind, curr, prev, cd, pd_, delta, "計算：WALCL−TGA−RRP", as_of)

    rows.sort(key=lambda r: r["id"])
    breakdown = _blood_math(rows)
    return rows, breakdown, pending


def _manual_series(man: dict, key: str):
    """手動輸入格式：{"sovereign_cds": [{"date":"2026-08-06","value":35.97}, ...]}"""
    raw = man.get(key) or []
    out = []
    for e in raw:
        try:
            out.append((date.fromisoformat(e["date"]), float(e["value"])))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out)


def _row(ind, curr, prev, cd, pd_, delta, origin, as_of):
    dp = C.pct(curr, prev)
    verdict = None
    if delta is not None and delta != 0:
        verdict = ind["up_label"] if delta > 0 else ind["down_label"]
    stale = bool(cd and (as_of - cd).days > TH["stale_days"])
    return {
        "id": ind["id"], "name": ind["name"], "axis": ind["axis"],
        "unit": ind.get("unit"), "freq": ind.get("freq"),
        "curr": curr, "prev": prev,
        "curr_date": cd, "prev_date": pd_,
        "delta": delta, "delta_pct": dp,
        "up_bias": ind["up_bias"], "verdict": verdict,
        "note": ind.get("note"), "source": origin, "stale": stale,
        "manual_url": ind.get("manual_url"),
    }


def _net_liquidity(cache, as_of):
    """
    WALCL / WDTGAL 是週三為基準日的週頻，RRPONTSYD 是日頻且單位是「十億」。
    先把 RRP 換成百萬並對齊到 WALCL 的日期，才是可比的淨流動性。
    """
    walcl, tga = cache.get("WALCL", []), dict(cache.get("WDTGAL", []))
    rrp = cache.get("RRPONTSYD", [])
    if not walcl or not tga or not rrp:
        return []
    out = []
    for d, a in walcl:
        t = tga.get(d)
        if t is None:
            near = [(x, v) for x, v in cache.get("WDTGAL", []) if abs((x - d).days) <= 3]
            t = near[-1][1] if near else None
        prior_rrp = [v for x, v in rrp if x <= d]
        if t is None or not prior_rrp:
            continue
        out.append((d, a - t - prior_rrp[-1] * 1000.0))
    return out


def _blood_math(rows):
    """本週淨血量 = ΔFed資產 − ΔTGA − ΔRRP（單位：百萬美元）。"""
    g = {r["id"]: r for r in rows}
    d1, d2, d3 = (g.get(i, {}).get("delta") for i in (1, 2, 3))
    if d1 is None or d2 is None or d3 is None:
        return {"net": None, "components": {}}
    d3m = d3 * 1000.0
    return {
        "net": d1 - d2 - d3m,
        "components": {"fed_assets": d1, "tga": d2, "on_rrp": d3m},
        "reading": "放血（注入）" if (d1 - d2 - d3m) > 0 else "抽血（回收）",
    }


# ── 第 2 項：跨資產板塊 ──────────────────────────────────────────────
def build_sectors(as_of: date) -> list[dict]:
    cfg = yaml.safe_load((C.CONFIG / "sectors.yaml").read_text(encoding="utf-8"))
    tickers = set()
    for items in cfg["groups"].values():
        for it in items:
            tickers.update(it.get("basket") or ([it["ticker"]] if it.get("ticker") else []))

    px = S.yahoo_history(sorted(tickers), period="3mo")
    out = []
    for group, items in cfg["groups"].items():
        for it in items:
            syms = it.get("basket") or [it["ticker"]]
            chgs = [c for c in (_wk_return(px, s, as_of) for s in syms) if c is not None]
            out.append({
                "group": group, "name": it["name"],
                "tickers": syms, "is_basket": bool(it.get("basket")),
                "chg_w": round(st.mean(chgs), 6) if chgs else None,
                "members_ok": len(chgs), "members_total": len(syms),
                "note": it.get("note"), "flow_proxy": it.get("flow_proxy"),
            })
    return out


def _wk_return(px, sym, as_of):
    """本期收盤 vs 五個交易日前收盤。找不到就回 None，不要用 0 假裝有資料。"""
    if px.empty or sym not in px.columns:
        return None
    s = px[sym].dropna()
    s = s[s.index.date <= as_of]
    if len(s) < 6:
        return None
    try:
        return float(s.iloc[-1] / s.iloc[-6] - 1)
    except Exception:  # noqa: BLE001
        return None


# ── 第 3 項：主題個股 + 金額 ─────────────────────────────────────────
def build_themes(as_of: date) -> tuple[list[dict], list[dict]]:
    cfg = yaml.safe_load((C.CONFIG / "themes.yaml").read_text(encoding="utf-8"))
    syms = set()
    for t in cfg["themes"]:
        syms.update(t["leader"] + t["rank"])
    for f in cfg["frontier"]:
        syms.update(f["tickers"])

    syms = sorted(syms)
    px = S.yahoo_history(syms, period="3mo")
    mcaps = S.yahoo_marketcaps(syms)

    def stock(sym, tier):
        r = _wk_return(px, sym, as_of)
        mc = mcaps.get(sym)
        return {"ticker": sym, "tier": tier, "chg_w": r,
                "mcap": mc, "value_delta": C.value_delta(mc, r)}

    themes = []
    for t in cfg["themes"]:
        rows = [stock(s, "leader") for s in t["leader"]] + \
               [stock(s, "rank") for s in t["rank"]]
        lead = [x["chg_w"] for x in rows if x["tier"] == "leader" and x["chg_w"] is not None]
        rest = [x["chg_w"] for x in rows if x["tier"] == "rank" and x["chg_w"] is not None]
        vds = [x["value_delta"] for x in rows if x["value_delta"] is not None]
        up = sum(1 for x in rows if (x["chg_w"] or 0) > 0)
        themes.append({
            "id": t["id"], "name": t["name"], "zh": t["zh"],
            "positioning": t.get("positioning"), "stocks": rows,
            "leader_avg": round(st.mean(lead), 6) if lead else None,
            "rank_avg": round(st.mean(rest), 6) if rest else None,
            # 正值 = 龍頭跑贏其餘成分股 → 護盤掩護出貨的量化訊號
            "breadth_gap": round(st.mean(lead) - st.mean(rest), 6) if lead and rest else None,
            "value_delta_total": sum(vds) if vds else None,
            "mcap_coverage": f"{len(vds)}/{len(rows)}",
            "advance_decline": f"{up}/{len(rows)}",
        })

    frontier = []
    for f in cfg["frontier"]:
        rows = [stock(s, "watch") for s in f["tickers"]]
        chgs = [x["chg_w"] for x in rows if x["chg_w"] is not None]
        frontier.append({
            "name": f["name"], "stocks": rows,
            "avg": round(st.mean(chgs), 6) if chgs else None,
        })
    return themes, frontier


# ── Step 3：背離雷達 ─────────────────────────────────────────────────
def scan_divergence(macro, sectors, themes, frontier):
    m = {r["id"]: r for r in macro}
    sec = {s["name"]: s for s in sectors}

    def chg(n):
        return (sec.get(n) or {}).get("chg_w")

    y2 = m.get(7, {}).get("delta")
    gold, dxy, btc = chg("Gold"), chg("USD"), chg("BTC")
    spx, ndx = chg("S&P 500"), chg("Nasdaq 100")
    oas = m.get(5, {}).get("delta")

    gaps = [t["breadth_gap"] for t in themes if t["breadth_gap"] is not None]
    gap = st.mean(gaps) if gaps else None
    infra = next((t for t in themes if t["id"] == 10), {})
    fr = [f["avg"] for f in frontier if f["avg"] is not None]
    fr_avg = st.mean(fr) if fr else None

    def flag(cond):
        return None if cond is None else bool(cond)

    out = [
        {"code": "A", "title": "美債殖利率飆升 + 黃金/防禦股暴漲",
         "truth": "Smart Money 不計代價逃離信用體系，定價地緣危機或通膨爆發",
         "hit": flag(None if (y2 is None or gold is None)
                     else (y2 * 100 >= TH["yield_spike_bp"] and gold >= TH["gold_surge"])),
         "evidence": {"2Y Δbp": None if y2 is None else round(y2 * 100, 1),
                      "Gold %": gold}},
        {"code": "B", "title": "龍頭大漲 + 廣度血洗",
         "truth": "用少數權值股拉指數，護盤掩護出貨，暗中抽離流動性",
         "hit": flag(None if gap is None else gap >= TH["breadth_gap"]),
         "evidence": {"龍頭−其餘 平均差": None if gap is None else round(gap, 4)}},
        {"code": "C", "title": "DXY 強勢大漲 + 風險資產同步暴漲",
         "truth": "全球法幣信任危機，資金進行逃命式搶購",
         "hit": flag(None if (dxy is None or (btc is None and ndx is None))
                     else (dxy >= TH["dxy_surge"] and
                           max(x for x in (btc, ndx) if x is not None) >= TH["risk_surge"])),
         "evidence": {"DXY %": dxy, "BTC %": btc, "NDX %": ndx}},
        {"code": "D", "title": "信用利差飆升 + 風險資產仍收紅",
         "truth": "媒體造神誘多，私下準備關門",
         "hit": flag(None if (oas is None or spx is None)
                     else (oas >= TH["oas_widen"] and spx >= TH["equity_up"])),
         "evidence": {"HY OAS Δ": oas, "S&P %": spx}},
        {"code": "E", "title": "前沿科技重挫 + 基礎設施默漲（風險偏好背離）",
         "truth": "資金從博弈型市場撤退，轉向實體過路費資產",
         "hit": flag(None if (fr_avg is None or infra.get("leader_avg") is None)
                     else (fr_avg <= -0.05 and infra["leader_avg"] >= 0)),
         "evidence": {"Frontier 平均 %": None if fr_avg is None else round(fr_avg, 4),
                      "Infra 龍頭 %": infra.get("leader_avg")}},
    ]
    return out


# ── 主流程 ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["thu", "fri", "mon"], required=True)
    ap.add_argument("--as-of", help="YYYY-MM-DD，手動補跑用")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else C.anchor_date(args.stage)
    wk = C.week_key(as_of)
    print(f"▶ 建置 {wk} / stage={args.stage} / 資料截止 {as_of}")

    if args.stage == "mon":
        print("  · Step 5 批改")
        P.score_week(as_of)

    print("  · 第 1 項 總體指標")
    macro, blood, pending = build_macro(as_of)
    print("  · 第 2 項 跨資產板塊")
    sectors = build_sectors(as_of)
    print("  · 第 3 項 主題個股")
    themes, frontier = build_themes(as_of)
    universe = yaml.safe_load((C.CONFIG / "themes.yaml").read_text(encoding="utf-8"))
    print("  · 背離雷達")
    div = scan_divergence(macro, sectors, themes, frontier)

    payload = {
        "generated_at": C.utc_stamp(),
        "iso_week": wk, "stage": args.stage, "as_of": as_of,
        "as_of_label": f'{wk} {"週四" if args.stage == "thu" else "週五"}收盤',
        "macro": macro, "blood": blood,
        "sectors": sectors, "themes": themes, "frontier": frontier,
        "divergence": div,
        "pending_manual": pending,
        "manual_slots": manual_slots(),
        "manual_current": S.manual(C.DATA / "manual_input.json"),
        "universe": universe,
        "predictions": P.bundle(P.load(), wk),
        "th": TH,
    }
    p = C.save_snapshot(payload, wk, args.stage)
    print(f"✔ 已寫入 {p}")
    if pending:
        print(f"⚠ 需手動補的指標：{', '.join(pending)}")


if __name__ == "__main__":
    main()
