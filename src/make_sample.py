#!/usr/bin/env python3
"""用你表格裡的實際數字產生一份 docs/latest.json，讓儀表板在第一次真跑之前就能看。

    python src/make_sample.py
"""
import random
from datetime import date

import yaml

import common as C
from build import TH, manual_slots, scan_divergence

random.seed(7)
AS_OF = date(2026, 8, 6)

RAW = [
    (1, 6748567, 6738190), (2, 929325, 970442), (3, 1.45, 2.15),
    (4, None, None), (5, 2.75, 2.87), (6, 3.64, 3.65), (7, 4.18, 4.22),
    (8, 99.676, 101.11), (9, 0.45, 0.45), (10, 35.97, 35.95),
    (11, 60, 35), (12, 15.81, 20.66), (13, 73.58, 74.18),
    (14, 53.9, 53.8), (15, 199000, 197000), (16, 55.2, 54.4), (17, 3.5, 4.2),
    (18, 3021400, 2989900),
]

SECTOR_CHG = {
    "Gold": .0526, "Silver": .0827, "Copper": .0419, "Palladium": .0719, "Platinum": .0553,
    "S&P 500": .0284, "Nasdaq 100": .0397, "Euro Stoxx 50": .0235, "Russell 2000": .0229,
    "DAX": .0207, "DJIA": .0241, "Nikkei 225": .030,
    "30Y T-Bond": .0081, "10Y T-Note": .0043, "5Y T-Note": .0027, "2Y T-Note": .0009,
    "IT & Telecom": -.0034, "Warehousing": -.0267, "Healthcare": -.0197, "Housing": .0042,
    "Retail": -.0419, "Tourism": -.0361, "Office": -.0459,
    "BTC": .020, "ETH": .014,
    "USD": .0004, "EUR": -.0008, "JPY": .0026, "GBP": -.0021, "CAD": -.0003,
    "CHF": -.0061, "NZD": -.0046, "AUD": -.0013,
    "Crude Oil WTI": -.0752, "Crude Oil Brent": -.047, "Natural Gas": -.0382,
    "Heating Oil": -.0423, "Gasoline RBOB": -.0472,
    "Soybean": -.0045, "Corn": .0023, "Wheat": -.0102, "Sugar": .079,
    "Coffee": -.0043, "Cocoa": .1242,
}

MCAP = {  # 十億美元，概略值，僅供樣板顯示
    "MSFT": 3720, "AAPL": 3410, "NVDA": 4180, "GOOGL": 2510, "AMZN": 2380,
    "META": 1640, "TSLA": 1090, "AVGO": 1330, "AMD": 268, "QCOM": 182,
    "TXN": 176, "AMAT": 158, "LRCX": 131, "MU": 154, "ADI": 118,
    "ORCL": 618, "CRM": 262, "ADBE": 178, "PANW": 128, "INTU": 191,
    "NOW": 208, "CRWD": 106, "PLTR": 342, "BRK-B": 1080, "JPM": 742,
    "V": 668, "MA": 512, "BAC": 366, "WFC": 248, "MS": 226, "SPGI": 158,
    "LLY": 812, "UNH": 288, "JNJ": 392, "ABBV": 348, "MRK": 226,
    "TMO": 198, "ABT": 224, "ISRG": 186, "GE": 254, "CAT": 186,
    "UNP": 138, "RTX": 176, "HON": 142, "ETN": 148, "BA": 138, "UPS": 82,
    "WMT": 786, "PG": 372, "COST": 428, "KO": 302, "PEP": 196, "PM": 268,
    "MDLZ": 88, "CL": 74, "HD": 386, "MCD": 218, "NKE": 108, "LOW": 138,
    "SBUX": 106, "BKNG": 178, "TJX": 142, "MAR": 76, "XOM": 486, "CVX": 296,
    "COP": 122, "LIN": 218, "FCX": 68, "EOG": 72, "SLB": 58, "NUE": 32,
    "NEE": 152, "PLD": 108, "AMT": 102, "EQIX": 88, "SO": 102, "DUK": 92,
    "PSA": 52, "CCI": 42,
}


def _demo_predictions():
    """樣板用的預測示範，只存在 latest.json，不會寫進 data/predictions.json。"""
    import predict as P
    calls = {
        "2026-W30": [("direction","多","多",4),("open_style","開高走低","開高走高",3),
                     ("flow","Risk-on","Risk-on",4),("leader","領先大盤","領先大盤",5),
                     ("breadth","持平","廣度惡化",2),("vol","下降","下降",3)],
        "2026-W31": [("direction","震盪","多",2),("open_style","開低走高","開低走高",3),
                     ("flow","中性","Risk-on",3),("leader","同步","領先大盤",4),
                     ("breadth","廣度轉強","廣度惡化",4),("vol","上升","下降",2)],
    }
    errs = {("2026-W30","open_style"):"wrong_timing",
            ("2026-W30","breadth"):"single_signal",
            ("2026-W31","direction"):"anchoring",
            ("2026-W31","flow"):"fooled_by_news",
            ("2026-W31","leader"):"misread_liquidity",
            ("2026-W31","breadth"):"overfit_narrative"}
    recs = []
    for wk, rows in calls.items():
        items = [{"id": i, "call": c, "actual": a, "confidence": cf,
                  "verdict": "correct" if c == a else "wrong", "auto": True,
                  "rationale": "（樣板）", "error_type": errs.get((wk, i)), "note": ""}
                 for i, c, a, cf in rows]
        items += [{"id": "divergence", "call": "尚未兌現", "actual": None, "confidence": 3,
                   "verdict": None, "auto": False, "rationale": "（樣板）",
                   "error_type": None, "note": ""},
                  {"id": "intent", "call": "部分正確", "actual": None, "confidence": 3,
                   "verdict": None, "auto": False, "rationale": "（樣板）",
                   "error_type": None, "note": ""}]
        recs.append({"week": wk, "created_at": C.utc_stamp(), "items": items,
                     "hypothesis": "（樣板）霸權在拉權值股掩護出貨，同時默默承接跌深的能源。",
                     "scored_at": C.utc_stamp()})
    return P.bundle(recs, C.week_key(AS_OF))


def main():
    ind_cfg = {i["id"]: i for i in yaml.safe_load(
        (C.CONFIG / "indicators.yaml").read_text(encoding="utf-8"))["indicators"]}

    macro = []
    for i, curr, prev in RAW:
        cfg = ind_cfg[i]
        if i == 4:
            curr = 6748567 - 929325 - 201900
            prev = 6738190 - 970442 - 198500
        d = None if (curr is None or prev is None) else curr - prev
        verdict = None if not d else (cfg["up_label"] if d > 0 else cfg["down_label"])
        macro.append({
            "id": i, "name": cfg["name"], "axis": cfg["axis"], "unit": cfg.get("unit"),
            "freq": cfg.get("freq"), "curr": curr, "prev": prev,
            "curr_date": AS_OF, "prev_date": date(2026, 7, 30),
            "delta": d, "delta_pct": C.pct(curr, prev),
            "up_bias": cfg["up_bias"], "verdict": verdict, "note": cfg.get("note"),
            "source": "樣板資料", "stale": False, "manual_url": cfg.get("manual_url"),
        })

    sec_cfg = yaml.safe_load((C.CONFIG / "sectors.yaml").read_text(encoding="utf-8"))
    sectors = []
    for g, items in sec_cfg["groups"].items():
        for it in items:
            r = SECTOR_CHG.get(it["name"])
            if it.get("basket"):
                base, kind, of = random.uniform(2e10, 4e11), "成分股市值和", f'{len(it["basket"])}/{len(it["basket"])} 檔'
            elif it.get("mcap"):
                base, kind, of = random.uniform(2e11, 2e12), "流通市值", it["ticker"]
            elif it.get("proxy"):
                base, kind, of = random.uniform(5e8, 6e11), "ETF 資產", it["proxy"]
            else:
                base = kind = of = None
            sectors.append({
                "group": g, "name": it["name"],
                "tickers": it.get("basket") or [it.get("ticker")],
                "is_basket": bool(it.get("basket")),
                "chg_w": r, "members_ok": 1, "members_total": 1,
                "base": base, "base_kind": kind, "base_of": of,
                "value_delta": C.value_delta(base, r),
                "net_flow": None if base is None else base * random.gauss(0, .012),
                "note": it.get("note"),
            })

    th_cfg = yaml.safe_load((C.CONFIG / "themes.yaml").read_text(encoding="utf-8"))
    themes = []
    for t in th_cfg["themes"]:
        rows = []
        for tier, syms in (("leader", t["leader"]), ("rank", t["rank"])):
            for s in syms:
                r = round(random.gauss(0.012, 0.035), 4)
                mc = MCAP.get(s, 40) * 1e9
                rows.append({"ticker": s, "tier": tier, "chg_w": r,
                             "mcap": mc, "value_delta": C.value_delta(mc, r)})
        lead = [x["chg_w"] for x in rows if x["tier"] == "leader"]
        rest = [x["chg_w"] for x in rows if x["tier"] == "rank"]
        themes.append({
            "id": t["id"], "name": t["name"], "zh": t["zh"],
            "positioning": t.get("positioning"), "stocks": rows,
            "leader_avg": round(sum(lead) / len(lead), 6),
            "rank_avg": round(sum(rest) / len(rest), 6),
            "breadth_gap": round(sum(lead) / len(lead) - sum(rest) / len(rest), 6),
            "value_delta_total": sum(x["value_delta"] for x in rows),
            "mcap_coverage": f"{len(rows)}/{len(rows)}",
            "advance_decline": f'{sum(1 for x in rows if x["chg_w"] > 0)}/{len(rows)}',
        })

    frontier = []
    for f in th_cfg["frontier"]:
        rows = []
        for s in f["tickers"]:
            r = round(random.gauss(-0.02, 0.06), 4)
            rows.append({"ticker": s, "tier": "watch", "chg_w": r,
                         "mcap": None, "value_delta": None})
        frontier.append({"name": f["name"], "stocks": rows,
                         "avg": round(sum(x["chg_w"] for x in rows) / len(rows), 6)})

    g = {r["id"]: r for r in macro}
    d1, d2, d3 = 10377, -41117, 3400   # ΔWALCL, ΔWDTGAL, ΔWLRRAL（百萬）
    payload = {
        "generated_at": C.utc_stamp(), "iso_week": C.week_key(AS_OF), "stage": "thu",
        "as_of": AS_OF, "as_of_label": f"{C.week_key(AS_OF)} 週四收盤（樣板）",
        "macro": macro,
        "blood": {"net": d1 - d2 - d3,
                  "components": {"fed_assets": d1, "tga": d2, "on_rrp": d3},
                  "reading": "放血（注入）" if d1 - d2 - d3 > 0 else "抽血（回收）"},
        "sectors": sectors, "themes": themes, "frontier": frontier,
        "divergence": scan_divergence(macro, sectors, themes, frontier),
        "pending_manual": [], "th": TH, "is_sample": True,
        "manual_slots": manual_slots(),
        "manual_current": __import__("json").load(
            open(C.DATA / "manual_input.json", encoding="utf-8")),
        "universe": th_cfg,
        "predictions": _demo_predictions(),
        "windows": {"sectors": {"curr": AS_OF, "prev": date(2026, 7, 30)},
                    "themes":  {"curr": AS_OF, "prev": date(2026, 7, 30)}},
    }
    print("✔", C.save_snapshot(payload, C.week_key(AS_OF), "thu"))


if __name__ == "__main__":
    main()
