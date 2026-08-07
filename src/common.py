"""共用工具：週期鍵、時區、歸檔讀寫、Delta 計算。"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
WEEKLY = DATA / "weekly"
DOCS = ROOT / "docs"

TW = timezone(timedelta(hours=8))


def now_tw() -> datetime:
    return datetime.now(TW)


def week_key(d: date) -> str:
    """ISO 週鍵，例：2026-W32。用 ISO 週是因為它跨年時不會錯亂。"""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def anchor_date(stage: str, ref: datetime | None = None) -> date:
    """
    決定這次快照的『資料截止日』。

    stage='thu' → 台灣週五 07:00 跑，抓的是美股週四收盤 → anchor = 週四
    stage='fri' → 台灣週六 07:00 跑，抓的是美股週五收盤 → anchor = 週五
    stage='mon' → 台灣週二 07:00 跑，抓的是美股週一收盤 → anchor = 週一
    """
    ref = ref or now_tw()
    d = ref.date()
    # 台灣早上 7 點時，美股「昨天」才剛收，所以往回推一天
    d = d - timedelta(days=1)
    want = {"thu": 3, "fri": 4, "mon": 0}[stage]  # Mon=0
    while d.weekday() != want:
        d -= timedelta(days=1)
    return d


def pct(curr, prev):
    if curr is None or prev is None:
        return None
    try:
        if prev == 0:
            return None
        return (curr - prev) / abs(prev)
    except (TypeError, ZeroDivisionError):
        return None


def value_delta(mcap, r):
    """
    市值變化金額。

    r 是本週報酬率、mcap 是『現在』的市值，所以上週市值 = mcap/(1+r)，
        ΔMV = mcap − mcap/(1+r) = mcap × r/(1+r)

    直接用 mcap × r 會高估（漲的時候）或低估（跌的時候），
    r=5% 時誤差約 5%，r=−20% 時誤差高達 25%。
    """
    if mcap is None or r is None:
        return None
    try:
        return mcap * r / (1.0 + r)
    except ZeroDivisionError:
        return None


def clean(o):
    """把 NaN / numpy 型別洗成 JSON 安全的值。"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if hasattr(o, "item"):
        return clean(o.item())
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return o


def save_snapshot(payload: dict, wk: str, stage: str) -> Path:
    """
    每週歸檔，不覆蓋。回頭做 Step 5 批改、或之後想重算 Delta 時要靠這個。
    latest.json 只是給前端讀的指標。
    """
    WEEKLY.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    p = WEEKLY / f"{wk}-{stage}.json"
    body = json.dumps(clean(payload), ensure_ascii=False, indent=2)
    p.write_text(body, encoding="utf-8")
    (DOCS / "latest.json").write_text(body, encoding="utf-8")

    index = sorted(x.name for x in WEEKLY.glob("*.json"))
    (DOCS / "index_weeks.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def load_snapshot(wk: str, stage: str) -> dict | None:
    p = WEEKLY / f"{wk}-{stage}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def prev_snapshot(wk: str, stage: str) -> dict | None:
    """
    找上一份同 stage 的快照。用來算「扣掉價格效應後的淨資金流」——
    那需要兩個時點的資產規模，單次抓取算不出來。
    """
    files = sorted(WEEKLY.glob(f"*-{stage}.json"))
    older = [f for f in files if f.name < f"{wk}-{stage}.json"]
    if not older:
        return None
    try:
        return json.loads(older[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def latest_weekly() -> dict | None:
    """最近一份『週視圖』快照（fri 優先，其次 thu）。週二批改時要沿用它。"""
    for stage in ("fri", "thu"):
        files = sorted(WEEKLY.glob(f"*-{stage}.json"))
        if files:
            try:
                return json.loads(files[-1].read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
    return None


def utc_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
