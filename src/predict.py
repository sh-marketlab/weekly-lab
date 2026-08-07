"""
Step 4 / Step 5 的機器部分：週一抓實際結果 → 自動判對錯 → 累積偏誤統計。

自動判定只處理「可觀測」的欄位（方向、廣度、波動度…）。
敘事型判斷（背離是否兌現、大魔王意圖）留給你自己標，
因為那些正是這套練習真正要訓練的東西，不該外包給程式。
"""
from __future__ import annotations

import json
import statistics as st
from datetime import date, timedelta

import yaml

import common as C
import sources as S

PRED_PATH = C.DATA / "predictions.json"


def cfg():
    return yaml.safe_load((C.CONFIG / "predictions.yaml").read_text(encoding="utf-8"))


def load() -> list[dict]:
    if PRED_PATH.exists():
        try:
            return json.loads(PRED_PATH.read_text(encoding="utf-8")).get("records", [])
        except Exception:  # noqa: BLE001
            return []
    return []


def save(records: list[dict]):
    PRED_PATH.write_text(
        json.dumps(C.clean({"records": records}), ensure_ascii=False, indent=2),
        encoding="utf-8")


# ── 週一探針 ─────────────────────────────────────────────────────────
def monday_metrics(as_of: date) -> dict | None:
    """
    as_of 是週一。全部相對「上週五收盤」計算。
    抓不到就回 None，週一的批改就整批跳過，不要用半套資料去打分。
    """
    c = cfg()
    syms = sorted(set(c["probes"].values()) | set(c["mega_caps"]))
    ohlc = S.yahoo_ohlc(syms, period="1mo")
    if not ohlc:
        return None

    def col(sym, field):
        s = (ohlc.get(field) or {}).get(sym)
        if s is None:
            return None
        s = [(d, v) for d, v in s if d <= as_of and v == v]
        return s

    def ret(sym, field="Close"):
        """該標的：as_of 當日 vs 前一交易日。"""
        s = col(sym, field)
        if not s or len(s) < 2 or s[-1][0] != as_of:
            return None
        return s[-1][1] / s[-2][1] - 1

    def gap(sym):
        o, cl = col(sym, "Open"), col(sym, "Close")
        if not o or not cl or len(cl) < 2 or o[-1][0] != as_of:
            return None, None
        prev_close, mon_open, mon_close = cl[-2][1], o[-1][1], cl[-1][1]
        return mon_open / prev_close - 1, mon_close / mon_open - 1

    p = c["probes"]
    spx = ret(p["spx"])
    g, intra = gap(p["spx"])
    ndx, btc = ret(p["ndx"]), ret(p["btc"])
    stp, utl = ret(p["staples"]), ret(p["utils"])
    rut, vix = ret(p["rut"]), ret(p["vix"])

    megas = [x for x in (ret(m) for m in c["mega_caps"]) if x is not None]
    risk_hi = [x for x in (ndx, btc) if x is not None]
    risk_lo = [x for x in (stp, utl) if x is not None]

    m = {
        "spx_d": spx, "spx_gap": g, "spx_intraday": intra,
        "breadth": None if (rut is None or spx is None) else rut - spx,
        "mega_rel": None if (not megas or spx is None) else st.mean(megas) - spx,
        "risk_app": None if (not risk_hi or not risk_lo)
                    else st.mean(risk_hi) - st.mean(risk_lo),
        "vix_chg": vix,
        "gold": ret(p["gold"]), "dxy": ret(p["dxy"]),
        "hyg": ret(p["hyg"]), "tlt": ret(p["tlt"]),
        "as_of": as_of,
    }
    return m if spx is not None else None


# ── 自動判定 ─────────────────────────────────────────────────────────
def _actual(rule: dict, m: dict):
    t = rule.get("type")
    if t == "manual":
        return None
    if t == "band3":
        v = m.get(rule["metric"])
        if v is None:
            return None
        return rule["above"] if v >= rule["hi"] else rule["below"] if v <= rule["lo"] else rule["mid"]
    if t == "sign2":
        v = m.get(rule["metric"])
        if v is None or v == 0:
            return None
        return rule["pos"] if v > 0 else rule["neg"]
    if t == "quadrant":
        g, i = m.get(rule["gap"]), m.get(rule["intraday"])
        if g is None or i is None:
            return None
        return ("開高" if g >= 0 else "開低") + ("走高" if i >= 0 else "走低")
    return None


def grade(record: dict, m: dict) -> dict:
    """把週一實際值填進預測紀錄，能自動判的就判。已手動標過的不覆蓋。"""
    slots = {s["id"]: s for s in cfg()["slots"]}
    for it in record.get("items", []):
        s = slots.get(it["id"])
        if not s:
            continue
        actual = _actual(s.get("resolver", {}), m)
        if actual is None:
            it.setdefault("actual", None)
            it.setdefault("verdict", None)
            it["auto"] = False
            continue
        it["actual"] = actual
        it["verdict"] = "correct" if it.get("call") == actual else "wrong"
        it["auto"] = True
    record["metrics"] = m
    record["scored_at"] = C.utc_stamp()
    return record


# ── 累積統計 ─────────────────────────────────────────────────────────
def stats(records: list[dict]) -> dict:
    c = cfg()
    labels = {s["id"]: s["label"] for s in c["slots"]}
    et_labels = {e["id"]: e["label"] for e in c["error_types"]}

    scored = [r for r in records if r.get("scored_at")]
    per_slot, per_conf, per_err = {}, {}, {}
    total = hit = 0

    for r in scored:
        for it in r.get("items", []):
            v = it.get("verdict")
            if v not in ("correct", "wrong"):
                continue
            ok = v == "correct"
            total += 1
            hit += ok
            d = per_slot.setdefault(it["id"], {"label": labels.get(it["id"], it["id"]),
                                               "n": 0, "hit": 0})
            d["n"] += 1
            d["hit"] += ok
            conf = it.get("confidence")
            if conf:
                k = str(conf)
                cd = per_conf.setdefault(k, {"n": 0, "hit": 0})
                cd["n"] += 1
                cd["hit"] += ok
            et = it.get("error_type")
            if et:
                ed = per_err.setdefault(et, {"label": et_labels.get(et, et), "n": 0})
                ed["n"] += 1

    for d in list(per_slot.values()):
        d["rate"] = round(d["hit"] / d["n"], 4) if d["n"] else None
    for k, d in per_conf.items():
        d["rate"] = round(d["hit"] / d["n"], 4) if d["n"] else None
        # 校準落差：宣稱的信心（1-5 → 20%-100%）減去實際命中率
        d["stated"] = int(k) / 5
        d["gap"] = None if d["rate"] is None else round(d["rate"] - d["stated"], 4)

    lucky = per_err.get("wrong_reason_right_call", {}).get("n", 0)
    unlucky = per_err.get("right_reason_wrong_call", {}).get("n", 0)

    return {
        "weeks_scored": len(scored),
        "n": total, "hit": hit,
        "rate": round(hit / total, 4) if total else None,
        "per_slot": dict(sorted(per_slot.items(), key=lambda kv: kv[1]["n"], reverse=True)),
        "per_confidence": dict(sorted(per_conf.items())),
        "per_error": dict(sorted(per_err.items(), key=lambda kv: kv[1]["n"], reverse=True)),
        # 這兩個數字比命中率更有意義：
        # lucky 高 = 你在用錯的方法拿對的結果，遲早會被打回原形
        # unlucky 高 = 方法沒問題，被外生事件打斷，不需要改判斷框架
        "lucky_calls": lucky, "unlucky_calls": unlucky,
    }


def bundle(records: list[dict], wk: str) -> dict:
    """打包給前端：欄位定義、本週紀錄、上一份已批改、累積統計。"""
    c = cfg()
    by = {r["week"]: r for r in records}
    scored = [r for r in records if r.get("scored_at")]
    return {
        "slots": c["slots"],
        "error_types": c["error_types"],
        "records": records,
        "current": by.get(wk),
        "last_scored": scored[-1] if scored else None,
        "history": [{"week": r["week"], "scored_at": r.get("scored_at"),
                     "hit": sum(1 for i in r.get("items", []) if i.get("verdict") == "correct"),
                     "n": sum(1 for i in r.get("items", []) if i.get("verdict") in ("correct", "wrong"))}
                    for r in scored][-12:],
        "stats": stats(records),
    }


# ── CLI：週一批改 ────────────────────────────────────────────────────
def score_week(as_of: date) -> dict | None:
    """
    as_of = 週一。找出「上一個週五」那份預測（同一個 ISO 週）並批改。
    週一和上週五通常屬於不同 ISO 週，所以往回找最近一份未批改的。
    """
    records = load()
    pending = [r for r in records if not r.get("scored_at")]
    if not pending:
        print("  · 沒有待批改的預測")
        return None

    m = monday_metrics(as_of)
    if not m:
        print("  ! 抓不到週一探針資料，這次不批改（避免用半套資料打分）")
        return None

    target = pending[-1]
    grade(target, m)
    save(records)
    n = sum(1 for i in target["items"] if i.get("verdict") in ("correct", "wrong"))
    ok = sum(1 for i in target["items"] if i.get("verdict") == "correct")
    print(f"  · 已批改 {target['week']}：自動判定 {ok}/{n} 正確"
          f"（另有敘事型欄位待你手動標）")
    return target
