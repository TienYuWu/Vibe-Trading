# 台股使用指南（Web UI）

這份文件寫給這個 fork 的使用者，只講**怎麼用**，不講怎麼改。台股相關的設計理由在
`agent/src/skills/taiwan-market/SKILL.md`。

---

## 0. 每次啟動的順序

三個東西要活著，順序不能顛倒：

```powershell
# 1. 遠端 vLLM 的 SSH tunnel（要保持這個視窗開著）
ssh -i C:\Users\NCKU_CSIE_RL_Tien\.ssh\A100_ed25519 -p 31598 `
    -N -L 0.0.0.0:8000:localhost:8000 root@140.116.154.4
```

先測，有回應就不用重開 tunnel（OpenSSH 對已佔用的 port 會誤報成
`bind: Permission denied`，那是 address already in use，不是權限問題）：

```bash
curl.exe -s --max-time 5 http://localhost:8000/v1/models
```

```bash
# 2. 起服務
docker compose up -d

# 3. 開瀏覽器
#    http://127.0.0.1:8899
```

`-L 0.0.0.0:8000` 不是筆誤：容器透過 `host.docker.internal` 連進來，只綁
`127.0.0.1` 的話容器連不到。代價是同網段的機器也看得到這個 port。

**檢查連線**（agent 說連不上 LLM 時第一個跑這個）：

```bash
curl -s localhost:8000/v1/models
```

沒回應就是 tunnel 掉了，重開步驟 1。

> **改過 `agent/` 的程式碼？** UI 不會自動反映：
> ```bash
> docker compose build vibe-trading && docker compose up -d vibe-trading
> ```
> 忘記這步的話 agent 會用舊程式碼，然後理直氣壮地告訴你台股功能不存在。

---

## 1. 頁面對照

| 路徑 | 用途 |
|---|---|
| `/` `/agent` | 對話。所有回測從這裡開始 |
| `/runs/:id` | 單次回測的完整結果 |
| `/reports` | 歷史回測列表 |
| `/compare` | 兩次以上回測並排比較 |
| `/scheduled` | 排程任務（收盤掃描推播在這裡） |
| `/alpha-zoo` | 因子庫、IC/IR 評測 |
| `/portfolio` | 多券商持倉儀表板（台股無連接器，這頁對台股是空的） |
| `/runtime` | 實盤/模擬狀態，唯讀 |
| `/settings` | 通道、資料源優先序 |
| `/options` | 選擇權損益圖、Greeks |
| `/correlation` | 相關性矩陣 |

---

## 2. 跑一次台股回測

在 `/agent` 貼這段。四個限制條件都是必要的，缺一個結果就會不一樣：

```
載入 taiwan-market skill 並遵循它的 config 指引。

跑一次回測：2330.TW、2454.TW、2317.TW，source finmind，
2022-01-01 到 2024-12-31，initial_cash 1000000。
進場：收盤價高於前 55 根的最高收盤價，且高於 200 日均線，
訊號成立的標的等權配置。
出場寫在 config.json 的 stop_rule chandelier (22, 3.0)，
signal_engine.py 裡不要寫任何出場規則。

限制：
- 不要寫額外的分析腳本，跑完回測讀 artifacts/metrics.csv 就回報。
- 報告裡每個數字都必須逐字來自工具回傳。沒取到的就寫「未取得」，不要自己推算。
- 只報告：總報酬、年化報酬、Sharpe、最大回撤、交易次數、勝率、出場原因分布。
```

那三條「限制」不是客套。少了第一條它會自己寫分析腳本燒光迭代預算；少了第二條它會編進場價，然後被 grounding gate 擋下、整份回答降級。

跑完點左側 run 進 `/runs/:id`。

### 結果頁的分頁

| 分頁 | 看什麼 |
|---|---|
| Dashboard | 總覽 |
| Chart | 淨值曲線 |
| Tearsheet | 月報酬熱力圖、前 N 大回撤 |
| Trades | 逐筆交易，**`reason` 欄是出場原因** |
| Positions | 部位結構 |
| Attribution | 績效歸因 |
| Code | agent 產生的 `signal_engine.py` |
| Validation | Monte Carlo 之類的穩健性檢定 |

`reason` 欄的值：

- `stop_chandelier` — 吊燈停損觸發
- `target_rebalance` — 調整到目標權重，**不是停損**
- `signal` — 訊號消失
- `end_of_backtest` — 回測結束強制平倉

---

## 3. 收盤掃描自動推播

### 先設通道

`/settings` → Channels，設定 Telegram（或其他）並啟動。這步沒做，排程只會把結果存在 session 裡，不會推給你。

### 建排程

`/scheduled` → 新增：

| 欄位 | 值 |
|---|---|
| Playbook | `Taiwan Close Scan` |
| 時間 | `14:30` |
| 週期 | 週一到週五 |
| 時區 | `Asia/Taipei` |
| Delivery channel | 你設定好的通道 |
| Delivery target | 該通道的收件目標 |

台股 13:30 收盤，14:30 跑留一小時給資料更新。

**投遞是每個任務各自 opt-in**：不填 channel 就完全不送任何地方。

### 掃描會回報什麼

- 哪些標的當天新成立進場條件
- 每個持倉離吊燈停損價多遠
- 哪些收在漲跌停

最後一段 `## Verdict` 是機器可讀的：`- SYMBOL: STATE - 原因`，STATE 為
`BREAKOUT` / `HOLDING` / `NEAR_STOP` / `LOCKED` / `FLAT`。

---

## 4. 因子評測

`/alpha-zoo`：

- **Browse** — 篩 `equity_tw` 看哪些因子適用台股
- **Bench** — universe 選 `twse50`，跑全 zoo 的 IC/IR 排名

`twse50` 的 IC **只能當因子之間的相對排名看**。成分股是手動維護的單一快照，倖存者偏差比 `sp500` 還重，不是可實現的報酬。

CLI 等價指令：

```bash
docker compose exec vibe-trading \
  vibe-trading alpha bench --zoo alpha101 --universe twse50 --period 2023-2024
```

---

## 5. 三個會咬人的地方

### `position_adjustment` 預設吃掉調倉

預設 `"hold"` 只開倉平倉，**同方向的權重調整全部丟棄**。等權籃子幾乎每次進出都要調權重，所以帳上會停在第一次進場的權重。

同一個策略：

| | `hold`（預設） | `rebalance` |
|---|---|---|
| 總報酬 | +38.1% | +8.6% |
| Sharpe | 1.00 | 0.36 |
| 交易數 | 29 | 57 |

**前者不是比較好的策略，是一個從沒被執行的策略。** 任何權重會變的策略都要在 config 設 `"position_adjustment": "rebalance"`。

`rebalance_count` 報的是**請求數**不是成交數，runner 只在 stderr 警告，metrics 看不出來。回測數字好得不合理時先檢查這個。

### 只有日線

FinMind 免費層是日線。**盤中提醒做不到**，最快是收盤後掃描。任何宣稱盤中觸價的結果都是錯的。

### 台股不能下單

沒有台股券商連接器（元大、永豐、凱基都沒有）。回測和掃描可以，**下單和讀實際持倉不行**，要自己手動下單。

---

## 6. 壞掉時看哪裡

| 症狀 | 原因 |
|---|---|
| agent 說連不上 LLM | tunnel 掉了 → `curl localhost:8000/v1/models` |
| agent 說台股功能不存在 | image 沒重建 → `docker compose build vibe-trading` |
| 回答降級成 fallback | 模型編了數字被 gate 擋下 → prompt 加「數字必須逐字來自工具」 |
| 跑超過 30 分鐘 | 壓縮逾時 → 確認 `agent/.env` 有 `VIBE_TRADING_LLM_TIMEOUT_SECONDS=900` 和 `TOKEN_THRESHOLD=24000` |
| **網頁停住，但 A100 還在跑** | 不是當機。前端 SSE 閒置逾時（預設 90 秒）放棄了，後端照常跑完並寫入結果。先去 `/reports` 找那次 run，多半是 `success`。設 `VIBE_TRADING_SSE_TIMEOUT=900` |
| 排程沒推播 | 任務沒填 delivery channel，或 `/settings` 通道沒啟動 |
| 回測數字好得離譜 | 先看 `position_adjustment`，再看 Code 分頁確認有 `.shift(1)` |

日誌：

```bash
docker compose logs -f vibe-trading
```
