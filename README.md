# weekly-lab · 每週練習台

把 Step 1–3 的資料收集全部自動化，讓你的時間留給 Step 4（推演）與 Step 5（批改）。

```
config/*.yaml            ← 長期設定：指標定義、板塊、主題成分股、預測欄位
data/manual_input.json   ← 沒有免費 API 的指標，手填在這
data/predictions.json    ← Step 4 預測 + Step 5 批改紀錄（複利的來源）
data/weekly/             ← 每週歸檔，不覆蓋
src/build.py             ← 抓資料 → 算 Delta → 掃背離 → 寫快照
src/predict.py           ← 週一抓實際結果 → 自動判對錯 → 累積偏誤統計
docs/index.html          ← GitHub Pages 儀表板，只讀 docs/latest.json
```

---

## 一、先講壞消息：你原本列的來源有 4 個在排程下會失敗

不是技術問題，是這些站台本來就擋自動化。與其寫一個每隔幾週就壞掉的爬蟲，不如一開始就換源。

| # | 指標 | 你原本的來源 | 這裡改用 | 狀態 |
|---|---|---|---|---|
| 1–4 | Fed 資產 / TGA / RRP / 淨流動性 | FRED | `WALCL` `WDTGAL` `RRPONTSYD` | 自動 |
| 5–7, 9 | HY OAS / SOFR / 2Y / 10Y-2Y | FRED | `BAMLH0A0HYM2` `SOFR` `DGS2` `T10Y2Y` | 自動 |
| 8 | DXY | TradingEconomics | Yahoo `DX-Y.NYB` | 自動 |
| 12 | VIX | Google 搜尋 | FRED `VIXCLS` | 自動 |
| 15 | Initial Claims | TradingEconomics | FRED `ICSA` | 自動 |
| 16 | Michigan Sentiment | TradingEconomics | FRED `UMCSENT` | 自動 |
| 17 | Inflation Rate | TradingEconomics | FRED `CPIAUCSL` 自算 YoY | 自動 |
| 11 | CNN Fear & Greed | CNN | CNN dataviz JSON（非官方） | 半自動 |
| 13 | MOVE | Google 搜尋 | Yahoo `^MOVE`（不穩） | 半自動 |
| 10 | 主權 CDS | worldgovernmentbonds | **無免費 API** | 手填 |
| 14 | Manufacturing PMI | investing.com | **Cloudflare 擋** | 手填 |

TradingEconomics 與 investing.com 在 Cloudflare 後面，GitHub Actions 的 IP 會直接被擋；Google 搜尋結果頁的結構隨時變且會觸發 bot 偵測。這三個不是「寫得夠好就能過」的問題。

Finviz map 也換掉了：`t=futures` 那張圖沒有市值欄位，你要的金額算不出來；而第 3 項是**你自己的主題分類**，Finviz 沒有這個切法。改成直接用 Yahoo 抓成分股，分類權完全在 `config/themes.yaml`。

---

## 二、儀表板的「04 · 設定與手填」分頁

因為 GitHub Pages 是靜態網站，沒有後端可以寫檔。所以設定分頁的運作方式是：**在網頁上編輯 → 產生檔案內容 → 一鍵跳到 GitHub 貼回去**。編輯過程中的值先存在瀏覽器 localStorage，讓你立刻看到結果。

### 手動指標輸入

四個欄位（主權 CDS、PMI、MOVE 備援、Fear&Greed 備援），每個都有日期 + 數值 + 「加入」。填完按「產生 JSON」→「複製」→「在 GitHub 編輯此檔 ↗」→ 貼上 → commit。下次排程就會納入計算。

沒貼回 repo 的話，數字只活在你這台裝置的瀏覽器，**不會進到週快照**。頁首會顯示「本機草稿 N 筆，尚未貼回 repo」提醒你。

### 觀察名單編輯

每個主題底下是可點掉的 chip（青色框＝龍頭，灰色＝3-10 名），旁邊有輸入框可新增，還能選層級。Frontier Watchlist 同樣可增刪。

- **刪除**：立刻反映在「03 · 主題」分頁。廣度差、市值變化合計、上漲家數都會即時重算。
- **新增**：要等下次排程抓到資料才有數字。卡片上會顯示「新增 N 檔待抓取」。
- 改完按「產生 YAML」貼回 `config/themes.yaml`，才會成為長期設定。

「還原成 repo 版本」會丟掉本機修改，回到 `themes.yaml` 的內容。

⚠️ **記得改 `docs/index.html` 第 2 行 JS 的 `REPO` 常數**，否則「在 GitHub 編輯」的連結會指到別人的 repo：

```js
const REPO = "你的帳號/weekly-lab";
```

---

## 三、三個必須先講清楚的計算細節

**1. 「金額變化」不是資金流入流出。**
你表格的公式 `市值 × 漲跌%` 算出的是**市值變化（蒸發/增加）**，不是有多少錢流進流出——股票市場每筆成交都有買有賣，錢不會憑空進出一檔股票。混用會讓你在 Step 4 推出錯的結論。真要看資金流得看 ETF 的**份額變化**（申購/贖回），`sectors.yaml` 的 `flow_proxy` 欄位就是給這個用的。

**2. 公式本身要修正。** 用 `ΔMV = 市值 × r/(1+r)`，不是 `市值 × r`。
因為抓到的市值是**現在**的市值，上週市值 = 現在 ÷ (1+r)。r = −20% 時 naive 算法給 −200，正確是 −250，差 25%。跌得越兇差越大，而跌得兇的時候正是你最需要準確數字的時候。

**3. 債券那一組方向是相反的。**
第 2 項的「30Y/10Y/5Y/2Y」是**期貨價格**變化（你表上的 +0.81% / +0.43% 就是價格），期貨價漲 = 殖利率跌。這跟第 1 項的 #07（2Y 殖利率）方向相反。同一頁上兩個都叫「2Y」但意義相反，UI 和 config 裡都標註了。

---

## 四、排程時間

| stage | Cron (UTC) | 台灣時間 | 抓到的是 | 對應 |
|---|---|---|---|---|
| `thu` | `0 23 * * 4` | 週五 07:00 | 美股**週四**收盤 | Step 1 + 2 |
| `fri` | `0 23 * * 5` | 週六 07:00 | 美股**週五**收盤（完整週） | Step 3 |
| `mon` | `0 23 * * 1` | 週二 07:00 | 美股**週一**收盤 | Step 5 自動批改 |

`mon` 這一趟為什麼是台灣週二早上：美股週一 16:00 ET 收盤＝台灣週二 04:00，所以你在台灣週二起床時才看得到週一的完整結果。

Fed 的 H.4.1（`WALCL`）是週四 16:30 ET 發布，這個時間留了約 2.5 小時給 FRED 收錄，安全。
若還是偶爾抓到舊值（儀表板會標「舊值」），程式不會出錯——它永遠取「≤ 資料截止日的最新一筆」，只是那一項的 Delta 會變 0。

GitHub Actions 的排程在尖峰時段可能延遲數分鐘到十幾分鐘，這對週資料沒有影響。

---

## 五、背離雷達

Step 3 那張原本要手動勾選的掃描表，現在自動判定。門檻集中在 `src/build.py` 的 `TH` dict（跟你 `playbook.py` 同樣的寫法），要調就改那裡。

| 代號 | 觸發條件 | 目前門檻 |
|---|---|---|
| A | 2Y 殖利率飆升 + 黃金暴漲 | +15bp 且 +2% |
| B | 龍頭大漲 + 廣度血洗 | 龍頭均 − 其餘均 ≥ 2pp |
| C | DXY 大漲 + 風險資產同漲 | +1% 且 +3% |
| D | HY OAS 擴大 + 大盤仍收紅 | +15bp 且 +0.5% |
| E | Frontier 重挫 + Infra 默漲 | −5% 且 ≥ 0 |

背離 B 是量化版的「護盤掩護出貨」偵測：每個主題算 `leader_avg − rank_avg`，正值越大代表指數被少數權值股撐著。比用眼睛看「大多數上漲/下跌」精確得多。

你原本的背離 D 是「新聞釋放極致利多 + 信用利差飆升」。「新聞極致利多」機器判不了，改成「信用利差擴大 + 風險資產仍收紅」——保留同一個邏輯核心（Smart Money 與散戶定價背離），但可觀測。新聞那一半留給你在 Step 4 自己填。

---

## 六、Step 4 / Step 5：預測與批改

這是整套練習真正產生複利的地方——不是「這週看對了嗎」，而是「我在哪一類判斷上系統性地失準」。

### 週末寫預測（儀表板「04 · 預測與批改」）

八個欄位，每個要選判斷 + 給 1–5 分信心 + 寫理由。**理由欄位比選項重要**，因為週一批改時真正要問的是「理由對不對」，不是「結果對不對」。填完產生 JSON 貼回 `data/predictions.json`。

| 欄位 | 週一怎麼判 |
|---|---|
| 整體方向 | S&P500 全日漲跌，±0.3% 分界 |
| 早盤手法 | 開盤跳空方向 × 盤中走勢方向 → 開高走低等四象限 |
| 資金流向 | (NDX + BTC)/2 − (XLP + XLU)/2 |
| 龍頭股表現 | 七大權值股平均 − S&P500 |
| 市場廣度 | Russell 2000 − S&P500 |
| 波動度 | VIX 方向 |
| 背離是否兌現 | **機器判不了，你自己標** |
| 大魔王意圖判讀 | **機器判不了，你自己標** |

前六項由台灣週二早上的排程自動判定。這是刻意的設計：自動判定拿掉了事後合理化的空間，你週一打開只能看到紅字或綠字，沒有商量餘地。後兩項留給你，因為敘事判斷正是這套練習要訓練的東西，不該外包給程式。

### 誤差類型

判錯的欄位要選一個誤差類型（`config/predictions.yaml` 可增修）：漏看某條水管、被新聞敘事帶偏、方向對但時間錯、過度倚賴單一訊號、忽略部位與微結構、錨定上週結論、敘事過度自洽⋯⋯

其中兩個特別重要，也可以標在**答對**的題目上：

- **對但理由錯（運氣）** —— 結果對、過程錯。這比單純看錯更危險，因為會被記憶成能力，然後在下一次用同樣的錯方法下更大的注。
- **理由對但結果錯** —— 推論健全，被外生事件打斷。這一類通常**不需要**修正判斷框架，硬改反而會把對的方法改掉。

### 累積統計看什麼

命中率只是入場券。真正有訊息量的是另外兩個：

**信心校準** —— 你標 5 分把握時，實際命中率是不是接近 100%？表格會顯示「宣稱 vs 實際」的落差。負值＝過度自信，正值＝低估自己。多數人在 4–5 分那兩列會出現大幅負落差，那就是要修的地方。（樣本少於 10 的那一列先別當真。）

**誤差類型分布** —— 如果「忽略部位與微結構」一直排第一，那就不是運氣問題，是你的框架缺了一層。這正是你在 7/17 台股那次診斷對的東西，但診斷對一次不等於下次會記得看。

### 補跑

```bash
python src/build.py --stage mon                       # 批改最近一份未批改的預測
python src/build.py --stage mon --as-of 2026-08-03     # 指定某個週一
```

抓不到週一探針資料時會整批跳過不批改——寧可沒批改，也不要用半套資料打分。

---

## 七、從零開一個新 repo（逐步）

### 步驟 1 · 拿一把 FRED API key（30 秒）

到 <https://fredaccount.stlouisfed.org/apikeys> 註冊並申請，複製那串 32 碼。
不申請也能跑（會退回免金鑰的 `fredgraph.csv`），但容易被限流。

### 步驟 2 · 在 GitHub 上建 repo

<https://github.com/new> → Repository name 填 `weekly-lab` → **Public**（Pages 免費版需要）→ 不要勾任何 README/gitignore → Create。

### 步驟 3 · 把檔案推上去

```bash
cd 你解壓縮的 weekly-lab 資料夾
git init -b main
git add .
git commit -m "初始：每週練習台"
git remote add origin https://github.com/你的帳號/weekly-lab.git
git push -u origin main
```

### 步驟 4 · 設定 Secret

repo 頁面 → **Settings** → 左欄 Secrets and variables → **Actions** → New repository secret
Name: `FRED_API_KEY`，Secret: 步驟 1 那串 → Add secret。

### 步驟 5 · 開 GitHub Pages

**Settings** → 左欄 **Pages** → Source 選 `Deploy from a branch` → Branch 選 `main`、資料夾選 `/docs` → Save。
一兩分鐘後網址是 `https://你的帳號.github.io/weekly-lab/`。

### 步驟 6 · 改 REPO 常數

編輯 `docs/index.html`，把 `const REPO = "sh-marketlab/weekly-lab";` 改成你的帳號，commit。

### 步驟 7 · 給 Actions 寫入權限

**Settings** → **Actions** → General → 最下面 Workflow permissions → 選 **Read and write permissions** → Save。
（沒開這個，排程跑完會 push 失敗。）

### 步驟 8 · 手動跑第一次

**Actions** 分頁 → 左欄 `weekly-lab` → Run workflow → stage 填 `fri` → Run。
跑完 2–4 分鐘（第一次要抓 80 幾檔的市值），回到 Pages 網址就能看到真實資料。

### 步驟 9 · 補上手填的兩項

儀表板 → 「04 · 設定與手填」→ 填主權 CDS 和 PMI（各兩筆，才算得出 Delta）→ 產生 JSON → 複製 → 在 GitHub 編輯 → 貼上 commit。

### 本機開發（可選）

```bash
pip install -r requirements.txt
python src/make_sample.py            # 用你表格的數字產生樣板
python -m http.server -d docs 8000   # 開 localhost:8000
python src/build.py --stage fri      # 真的抓一次
python src/build.py --stage thu --as-of 2026-07-30   # 補跑舊的一週
```

---

## 八、還沒做、但值得做的

1. **ETF 份額變化** → 這才是真的「資金流入流出」，補上之後第 2 項才名副其實。
2. **預測 vs 快照的關聯分析**：現在統計的是「你判斷的準度」，還沒統計「哪些訊號在你判對時剛好都在場」。累積 20 週以上之後，可以回頭問：淨血量為負的那些週，你的方向判斷準度是不是特別高？那會告訴你哪些指標對你個人真正有預測力。
3. **快照 diff**：`build.py --diff 2026-W31 2026-W32`，直接列出兩週之間變化最大的 N 個項目。
