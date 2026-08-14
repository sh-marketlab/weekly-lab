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
    "reserve_gap": 25000,        # 背離 F：Δ淨流動性 與 Δ準備金 相差 ≥ 250 億（百萬美元）
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
    cache["WLRRAL"] = S.fred("WLRRAL")
    nl, comps = _net_liquidity(cache, as_of)
    nl_cd = nl_pd = None
    for i, r in enumerate(rows):
        if r.get("_defer"):
            ind = next(x for x in cfg["indicators"] if x["id"] == r["id"])
            curr, prev, nl_cd, nl_pd = S.pick(nl, as_of, "weekly")
            delta = None if (curr is None or prev is None) else curr - prev
            rows[i] = _row(ind, curr, prev, nl_cd, nl_pd, delta,
                           "計算：WALCL−WDTGAL−WLRRAL", as_of)

    rows.sort(key=lambda r: r["id"])
    breakdown = _blood_math(comps, nl_cd, nl_pd)
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
    淨流動性 = WALCL − WDTGAL − WLRRAL

    三條全部是 H.4.1 的「Wednesday Level」、單位都是百萬美元，
    所以是同一個時點的資產負債表快照，不需要任何單位換算或日期插補。

    RRP 這一項刻意用 WLRRAL（資產負債表上的逆回購負債總額）而不是
    RRPONTSYD（隔夜逆回購工具餘額），兩者差很多：

      RRPONTSYD  只含「暫時性公開市場操作」的隔夜逆回購，也就是貨幣基金
                 停車的那個工具。2022–23 年曾破 2.5 兆，現在已排乾到 ~15 億，
                 拿它當扣除項等於沒扣。日頻、單位十億。
      WLRRAL     資產負債表上的逆回購負債全額，額外包含「外國官方機構」
                 （FIMA）的逆回購，這一塊約 2000 億且會動。週三、單位百萬。

    真正從準備金裡被抽走的是 WLRRAL，所以恆等式該用它。
    RRPONTSYD 仍保留為第 3 項指標，因為它是另一個訊號
    （貨幣市場資金是否在釋放），不是同一件事。

    回傳 (序列, 各期組成)，組成拿來算血量計，確保兩者永遠對得起來。
    """
    walcl = cache.get("WALCL", [])
    tga = dict(cache.get("WDTGAL", []))
    rrp = dict(cache.get("WLRRAL", []))
    if not walcl or not tga or not rrp:
        return [], {}

    def near(book, d):
        """H.4.1 偶爾有假日順延，容許 ±3 天對齊。"""
        if d in book:
            return book[d]
        cand = [(abs((x - d).days), v) for x, v in book.items() if abs((x - d).days) <= 3]
        return min(cand)[1] if cand else None

    out, comps = [], {}
    for d, a in walcl:
        t, r = near(tga, d), near(rrp, d)
        if t is None or r is None:
            continue
        out.append((d, a - t - r))
        comps[d] = {"walcl": a, "tga": t, "rrp": r}
    return out, comps


def _blood_math(comps, curr_d, prev_d):
    """
    本週淨血量 = ΔWALCL − ΔTGA − ΔRRP（單位：百萬美元）。

    直接吃 _net_liquidity 算好的組成，而且用的是第 4 項指標選定的
    同一組日期，所以血量計的加總必定等於第 4 項的 Delta。
    先前是拿第 3 項（RRPONTSYD，日頻、對齊到 as_of）去湊，
    日期跟 WALCL 的週三對不上，兩邊差了幾百萬。
    """
    a, b = comps.get(curr_d), comps.get(prev_d)
    if not a or not b:
        return {"net": None, "components": {}}
    d1 = a["walcl"] - b["walcl"]
    d2 = a["tga"] - b["tga"]
    d3 = a["rrp"] - b["rrp"]
    net = d1 - d2 - d3
    return {
        "net": net,
        "components": {"fed_assets": d1, "tga": d2, "on_rrp": d3},
        "dates": {"curr": curr_d, "prev": prev_d},
        "reading": "放血（注入）" if net > 0 else "抽血（回收）",
    }


# ── 第 2 項：跨資產板塊 ──────────────────────────────────────────────
def build_sectors(as_of: date, wk: str, stage: str) -> tuple[list[dict], dict]:
    cfg = yaml.safe_load((C.CONFIG / "sectors.yaml").read_text(encoding="utf-8"))

    px_syms, base_syms = set(), set()
    for items in cfg["groups"].values():
        for it in items:
            syms = it.get("basket") or ([it["ticker"]] if it.get("ticker") else [])
            px_syms.update(syms)
            if it.get("basket"):
                base_syms.update(it["basket"])
            elif it.get("proxy"):
                base_syms.add(it["proxy"])
                px_syms.add(it["proxy"])
            elif it.get("mcap"):
                base_syms.add(it["ticker"])

    px = S.yahoo_history(sorted(px_syms), period="3mo")
    caps = S.yahoo_marketcaps(sorted(base_syms))

    # 上一份同 stage 的快照，用來還原「扣掉價格效應後的淨資金流」
    prev = C.prev_snapshot(wk, stage) or {}
    prev_parts = {r["name"]: r.get("base_parts") for r in prev.get("sectors", [])}

    out = []
    for group, items in cfg["groups"].items():
        for it in items:
            syms = it.get("basket") or [it["ticker"]]
            chgs = [c for c in (_wk_return(px, x, as_of) for x in syms) if c is not None]
            r = round(st.mean(chgs), 6) if chgs else None

            if it.get("basket"):
                parts = {x: caps.get(x) for x in it["basket"]}
                vals = [v for v in parts.values() if v]
                base = sum(vals) if vals else None
                kind, kind_of = "成分股市值和", f'{len(vals)}/{len(it["basket"])} 檔'
            elif it.get("mcap"):
                base = caps.get(it["ticker"])
                parts = {it["ticker"]: base}
                kind, kind_of = "流通市值", it["ticker"]
            elif it.get("proxy"):
                base = caps.get(it["proxy"])
                parts = {it["proxy"]: base}
                kind, kind_of = "ETF 資產", it["proxy"]
            else:
                base = kind = kind_of = None
                parts = {}

            flow, flow_note = _net_flow(parts, prev_parts.get(it["name"]), px, as_of)

            out.append({
                "group": group, "name": it["name"],
                "tickers": syms, "is_basket": bool(it.get("basket")),
                "chg_w": r, "members_ok": len(chgs), "members_total": len(syms),
                "base": base, "base_kind": kind, "base_of": kind_of,
                "base_parts": parts,
                "value_delta": C.value_delta(base, r),
                "net_flow": flow, "flow_note": flow_note,
                "note": it.get("note"),
            })

    window = _price_window(px, as_of)
    return out, window


def _net_flow(now_parts, prev_parts, px, as_of):
    """
    淨資金流 = Σ 各標的（本週規模 − 上週規模 × (1 + 該標的自己的報酬)）

    扣掉價格效應後剩下的才是份額變化（ETF 申購贖回、股票增減資）。
    回傳 (金額, 說明)；算不出來時金額為 None，說明會告訴你為什麼。

    兩個曾經算錯的地方：

    1. 每個標的必須用「自己的」報酬去除價格效應。
       先前是拿整組的等權平均報酬去除市值加總（＝市值加權），
       兩種加權定義不同，即使股數完全沒變也會生出幾十億的假資金流。
       proxy 的情況更明顯：拿期貨報酬去除 ETF 規模，兩者本來就不等。

    2. 規模若與上週「完全相同」，代表來源沒更新，不是「零流入」。
       規模 = 份額 × 價格，價格動了規模必動；一模一樣只可能是資料沒刷新。
       這時若照算，flow 會退化成 −(價格效應)，看起來像每項都在流出，
       而且與市值變化等量反號 —— 那是假象，所以直接不出數字。
    """
    if not now_parts or not prev_parts:
        return None, "需要兩週快照"

    total, used, frozen = 0.0, 0, 0
    for sym, now in now_parts.items():
        prev = prev_parts.get(sym)
        if not now or not prev:
            continue
        ri = _wk_return(px, sym, as_of)
        if ri is None:
            continue
        if now == prev and abs(ri) > 1e-6:
            frozen += 1          # 價格有動、規模沒動 → 來源沒刷新
            continue
        total += now - prev * (1.0 + ri)
        used += 1

    if not used:
        return None, ("來源規模未更新" if frozen else "資料不足")
    if frozen:
        return total, f"部分來源未更新（{frozen} 檔已排除）"
    return total, None


def _price_window(px, as_of):
    """回報這批價格實際用到的兩個交易日，讓儀表板標得出「本期 vs 上期」。"""
    if px is None or px.empty:
        return {"curr": None, "prev": None}
    idx = [d.date() for d in px.index if d.date() <= as_of]
    if len(idx) < 6:
        return {"curr": idx[-1] if idx else None, "prev": None}
    return {"curr": idx[-1], "prev": idx[-6]}


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
def build_themes(as_of: date) -> tuple[list[dict], list[dict], dict]:
    cfg = yaml.safe_load((C.CONFIG / "themes.yaml").read_text(encoding="utf-8"))
    syms = set()
    for t in cfg["themes"] + cfg["frontier"]:
        syms.update(t.get("leader", []) + t.get("rank", []))

    syms = sorted(syms)
    px = S.yahoo_history(syms, period="3mo")
    mcaps = S.yahoo_marketcaps(syms)

    def stock(sym, tier):
        r = _wk_return(px, sym, as_of)
        mc = mcaps.get(sym)
        return {"ticker": sym, "tier": tier, "chg_w": r,
                "mcap": mc, "value_delta": C.value_delta(mc, r)}

    def group(t):
        """themes 與 frontier 共用，避免兩邊的計算邏輯日後漂移。"""
        rows = [stock(x, "leader") for x in t.get("leader", [])] + \
               [stock(x, "rank") for x in t.get("rank", [])]
        lead = [x["chg_w"] for x in rows if x["tier"] == "leader" and x["chg_w"] is not None]
        rest = [x["chg_w"] for x in rows if x["tier"] == "rank" and x["chg_w"] is not None]
        vds = [x["value_delta"] for x in rows if x["value_delta"] is not None]
        chgs = [x["chg_w"] for x in rows if x["chg_w"] is not None]
        up = sum(1 for x in rows if (x["chg_w"] or 0) > 0)
        return {
            "id": t["id"], "name": t["name"], "zh": t.get("zh"),
            "positioning": t.get("positioning"), "stocks": rows,
            "leader_avg": round(st.mean(lead), 6) if lead else None,
            "rank_avg": round(st.mean(rest), 6) if rest else None,
            # 正值 = 龍頭跑贏其餘成分股 → 護盤掩護出貨的量化訊號
            "breadth_gap": round(st.mean(lead) - st.mean(rest), 6) if lead and rest else None,
            "avg": round(st.mean(chgs), 6) if chgs else None,
            "value_delta_total": sum(vds) if vds else None,
            "mcap_coverage": f"{len(vds)}/{len(rows)}",
            "advance_decline": f"{up}/{len(rows)}",
        }

    themes = [group(t) for t in cfg["themes"]]
    frontier = [group(f) for f in cfg["frontier"]]
    return themes, frontier, _price_window(px, as_of)


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

    # 背離 F：第 4 項（近似公式）與第 18 項（直接觀測）的缺口
    nl_d = m.get(4, {}).get("delta")
    res_d = m.get(18, {}).get("delta")
    gap_res = None if (nl_d is None or res_d is None) else nl_d - res_d

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
        {"code": "F", "title": "淨流動性與實際準備金背離",
         "truth": "有第四條水管在動 —— 通貨、其他負債或資本項，不在 WALCL−TGA−RRP 的視野內",
         "hit": flag(None if gap_res is None else abs(gap_res) >= TH["reserve_gap"]),
         "evidence": {"Δ淨流動性": None if nl_d is None else round(nl_d),
                      "Δ準備金": None if res_d is None else round(res_d),
                      "缺口": None if gap_res is None else round(gap_res)}},
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

    # 週二那一趟只做批改。它不該把你週六看到的「本週視圖」蓋掉——
    # 週末推演用的是上週五收盤那份，週一的數字是拿來對答案的，不是換一份新的。
    if args.stage == "mon":
        print("  · Step 5 批改")
        P.score_week(as_of)
        base = C.latest_weekly()
        if not base:
            print("  ! 找不到既有的週視圖快照，先跑一次 --stage fri")
            return
        base["predictions"] = P.bundle(P.load(), base["iso_week"])
        base["graded_at"] = C.utc_stamp()
        base["monday_as_of"] = as_of
        p = C.save_snapshot(base, wk, "mon")
        print(f"✔ 已更新批改結果，週視圖沿用 {base['iso_week']} {base['stage']}")
        print(f"  {p}")
        return

    print("  · 第 1 項 總體指標")
    macro, blood, pending = build_macro(as_of)
    print("  · 第 2 項 跨資產板塊")
    sectors, sec_window = build_sectors(as_of, wk, args.stage)
    print("  · 第 3 項 主題個股")
    themes, frontier, thm_window = build_themes(as_of)
    universe = yaml.safe_load((C.CONFIG / "themes.yaml").read_text(encoding="utf-8"))
    print("  · 背離雷達")
    div = scan_divergence(macro, sectors, themes, frontier)

    payload = {
        "generated_at": C.utc_stamp(),
        "iso_week": wk, "stage": args.stage, "as_of": as_of,
        "as_of_label": f'{wk} {"週四" if args.stage == "thu" else "週五"}收盤',
        "macro": macro, "blood": blood,
        "sectors": sectors, "themes": themes, "frontier": frontier,
        "windows": {"sectors": sec_window, "themes": thm_window},
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
