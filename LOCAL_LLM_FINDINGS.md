# 本機 LLM 實測紀錄：Qwen3.8-27B on A100 80GB

追查「agent 跑半小時還超時」的完整過程與結論。所有數字都是實測，不是估算。

**結論先講（2026-09-29 更新）：回測與研究型對話都可用。** 最早「研究不可用」的結論作廢：
慢的主因是推理混進答案、溫度 0 與壓縮門檻，不是模型能力。現行設定與 A100 建議見文末
「[現行建議](#現行建議2026-09-29)」。下面依時間保留完整追查過程。

---

## 環境

| 項目 | 值 |
|---|---|
| 模型 | `Qwen/Qwen3.8-27B`（BF16） |
| 服務 | vLLM，單張 A100 80GB PCIe |
| 連線 | SSH tunnel，`0.0.0.0:8000 -> localhost:8000` |
| app | Docker，`LANGCHAIN_PROVIDER=openai` 指向 vLLM 的 OpenAI 相容端點 |

---

## 效能量測

### Prefill 不是瓶頸

| prompt tokens | 耗時 | 吞吐 |
|---|---|---|
| 2,060 | 4.4s | 469 tok/s |
| 8,060 | 6.0s | 1,342 tok/s |
| 20,060 | 10.1s | 1,985 tok/s |
| 40,060 | 17.5s | 2,286 tok/s |

### Decode 才是

| max_tokens | 實際輸出 | 耗時 | 吞吐 |
|---|---|---|---|
| 256 | 256 | 12.2s | 21.1 tok/s |
| 2,048 | 2,048 | 82.0s | 25.0 tok/s |

**約 23 tok/s。** 單一請求的 decode 受記憶體頻寬限制：每產生一個 token 要把整個模型權重讀過一遍。27B BF16 約 54GB ÷ A100 約 2TB/s ≈ 理論上限 37 tok/s。23 是合理值。

**A100 再閒也不會更快。** vLLM 的優勢在同時服務多個請求，單人使用發揮不出來。

### 實際 run 的成本

| run | 結果 | output tokens | 推算 decode |
|---|---|---|---|
| 09-07 01:30 | success | 34,864 | 25.3 分 |
| 09-05 14:17 | success | 43,096 | 31.2 分 |

一次「查詢個股前景」產生 3.5 萬 output tokens。**25 分鐘全部是產生時間**，prefill 只佔 1.8 分鐘。

---

## 過程中找到並修好的真 bug

這些都是實際缺陷，跟慢無關，但會讓 run 直接失敗。

### 1. Token 估算對中文低估 2.4 倍

`estimate_tokens` 原本是 `字元數 ÷ 4`。實測服務中的 tokenizer：

| 內容 | 字元/token |
|---|---|
| 英文散文 | 4.49 ← 原假設，只有這個對 |
| 中文散文 | 1.68 |
| 數字 JSON | 1.83 |
| 中英混合 | 1.59 |

台股工作流程是最壞情況：中文新聞、中文會計科目、滿是長數字的財報 JSON。**壓縮機制以為守在 24k，實際送出 60k。**

修法：字元分三類各自計價，再乘 1.4 安全係數。兩個方向代價不對稱 —— 估高只多一次摘要呼叫，估低是整個請求失敗。

### 2. 工具定義每次呼叫都重送，佔 34.6k tokens

```
107 個工具 · 134,041 字元 · 34,631 real tokens
```

加系統提示約 38k 固定開銷。在 65,536 上限的模型上，**超過一半的視窗在對話開始前就沒了**。

這解釋了 `at least 65537 input tokens`：壓縮把訊息守在 24k 是對的，但 24k + 38k = 62k，**那個預算從一開始就不可能成立**。

修法：`VIBE_TRADING_ENABLED_TOOLS` 白名單。26 個研究用工具 = 7,080 tokens，**省 80%**。

### 3. 逾時鏈算術矛盾

```
LLM 單次上限   900s
MAX_RETRIES    2      → 最壞 900 × 3 = 2700s 沒有完成
STALL 看門狗   1800s  ← 先觸發，把還在重試的 run 當殭屍殺掉
```

`run stalled: no LLM completion or tool result for 1849s` —— 1849 就是看門狗，不是真的當掉。

修法：`STALL > LLM × (1 + MAX_RETRIES)`。記關係，不記數字。

### 4. 前端 SSE 逾時 90 秒

畫面停住但 GPU 還在跑，看起來像當機。實際上後端跑完並寫入 `status: success`，只是沒人在看。

修法：`VIBE_TRADING_SSE_TIMEOUT >= VIBE_TRADING_LLM_TIMEOUT_SECONDS`。

### 5. 孤兒請求

Agent 停止後 vLLM 仍在產生 —— 客戶端斷線沒有傳達到，請求跑到 `max_tokens` 為止。觀察到 `num_requests_running` 從 1 累積到 2。

緩解：`--max-model-len 65536` 而非 `auto`（262144），讓孤兒有上限。

---

## 當時的設定（已過時，現行版本見文末）

`agent/.env`（gitignored，此處記錄值）：

```bash
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=Qwen/Qwen3.8-27B
OPENAI_BASE_URL=http://host.docker.internal:8000/v1
OPENAI_API_KEY=vllm-local

VIBE_TRADING_LLM_TIMEOUT_SECONDS=900          # 預設 300
VIBE_TRADING_RUN_STALL_TIMEOUT_SECONDS=3600   # 預設 1800，須 > 900×3
VIBE_TRADING_SSE_TIMEOUT=900                  # 預設 90，須 >= LLM timeout
TOKEN_THRESHOLD=24000                         # 預設 40000
VIBE_TRADING_ANSWER_LANGUAGE=Traditional Chinese (繁體中文)
VIBE_TRADING_ENABLED_TOOLS=<26 個研究用工具，見 taiwan-market skill>
```

vLLM：

```bash
vllm serve Qwen/Qwen3.8-27B \
  --max-model-len 65536 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --gpu-memory-utilization 0.90
```

---

## 判斷：哪些能用

| 任務 | 實測 | 判斷 |
|---|---|---|
| 台股回測 | 2 分 06 秒 | **可用** |
| 收盤掃描 playbook | 輸出結構化、量小 | 應可用（未實測） |
| 因子批次評測 | 不經 LLM | **可用** |
| 研究型對話 | 25–30 分鐘 | **不可用** |

差別是 output token 量：回測輸出幾百 token，研究要幾萬。

## 繼續調的天花板

| 做法 | 預期 | 仍然 |
|---|---|---|
| 關思考模式（`enable_thinking: false`） | 輸出砍半 | 約 12 分鐘 |
| INT4 量化（AWQ/GPTQ） | decode 2–3 倍 | 約 8–10 分鐘 |
| 換更小模型 | 3–4 倍 | 工具呼叫可靠度下降 |

**都到不了「可用」。**

## 建議

研究型任務改用雲端 API，回測與因子評測留在本機：

```bash
LANGCHAIN_PROVIDER=deepseek
LANGCHAIN_MODEL_NAME=deepseek-v4-pro
DEEPSEEK_API_KEY=sk-xxx
```

一次研究約幾塊台幣，25 分鐘變 1–2 分鐘。A100 留給不趕時間的批次工作。

不付費的話，誠實說：**這個 workload 不適合單卡本機模型**，不是設定錯誤。

---

## 2026-09-28 重新分析：3.5 萬 output token 大多是被退回的草稿

上面把慢歸因到 decode 速度，這沒錯，但沒回答「為什麼一個問題要產生 3.5 萬 token」。
重讀 session `a9d9a142b994` 的 `trace.jsonl`（9 次「2330的前景」／「台指期的趨勢」）：

| run | output tokens | 被 grounding 退回的草稿 | 其中退回草稿佔 |
|---|---|---|---|
| 09-05 14:17 | 43,096 | 4 次（it60–63） | 41,384（96%） |
| 09-07 01:30 | 34,864 | 4 次（it76–79） | 34,001（97.5%） |

整個 session 共 16 次 `answer_rejected`。另有兩個 run 以 `maximum context length` 400 結束：
每份被退回的草稿（最長 4.3 萬字元）連同 gate 的回饋都塞回 context，迴圈把 65,536 撐爆。

### 根因：思考內容混進了答案

vLLM 沒開 `--reasoning-parser`，所以 Qwen 的推理過程留在 `content` 裡（it61 的草稿裡找得到 `</think>`）。
Grounding gate 核對的是 `content`，所以連推理文字一起核對：

- `"June 23 high 2535"` → 把日期 23 當成價格，跟 OHLC 範圍 1145–2535 比 → 衝突
- `"Draft 2: 7/1 close"` → 2 與 1 被當成價格
- 模型在下一輪的推理裡檢討「為什麼 23 被擋」，又寫出更多數字 → 再被擋

it78 的草稿已經在逐一列舉 `13 (contains 1), 04 (contains 4) …`，這是死亡螺旋，不是研究。
單次 22,063 token ÷ 23 tok/s ≈ 16 分鐘，**單一呼叫就超過 900 秒的 LLM 逾時**，觸發重試，重試再走一遍。

App 端沒有任何 `<think>` 處理（`grep "think>" agent/src` 為空）；
推理只要走 `reasoning_content` 欄位，`providers/llm.py` 就會把它和答案分開，也不會回送（`send_reasoning_content=False`）。

**預期：** 開 `--reasoning-parser qwen3` 後，gate 只核對真正的答案，退回次數應接近 0，
output 砍到剩「一份草稿 + 推理」，研究型對話可能從 25–30 分鐘降到 5 分鐘以內。
上面「繼續調的天花板」表格是在沒發現這點的前提下算的，要重估。

### 本機實驗計畫（RTX 5090 32GB，GPU 有空時）

27B BF16（54GB）與 FP8（約 27GB + KV cache）都塞不進 32GB，要用 4-bit（AWQ / GPTQ，或 Blackwell 原生 NVFP4，先在 HF 確認有無現成量化版），
權重約 14–15GB。5090 頻寬約 1.8 TB/s，4-bit decode 的理論上限約 120 tok/s，是 A100 BF16 的數倍。

```bash
docker run --gpus all -p 8000:8000 -v hf_cache:/root/.cache/huggingface vllm/vllm-openai:latest \
  --model <Qwen3.8-27B 的 4-bit 版> \
  --max-model-len 65536 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --gpu-memory-utilization 0.90
```

同一個 prompt（`2330的前景`），每組跑 3 次：

| 組 | 變因 |
|---|---|
| A | 不加 `--reasoning-parser`（重現基準） |
| B | 加 `--reasoning-parser qwen3` |
| C | B + `chat_template_kwargs: {enable_thinking: false}` |

量測：`llm_usage.json` 的 `totals.output_tokens`、`trace.jsonl` 的 `answer_rejected` 次數與 `end.degraded`、總耗時。
先做 A/B 就能驗證根因；A 組若在 5090 上仍出現退回迴圈，B 組沒有，就定案。

### 2026-09-29 實測：本機 Ollama（RTX 5090）

```bash
docker run -d --name ollama-qwen --gpus all -p 127.0.0.1:11434:11434 \
  -v ollama:/root/.ollama \
  -e OLLAMA_CONTEXT_LENGTH=65536 -e OLLAMA_FLASH_ATTENTION=1 -e OLLAMA_KV_CACHE_TYPE=q8_0 \
  ollama/ollama:latest                       # 0.34.4
docker exec ollama-qwen ollama pull qwen3.8:27b   # Q4_K_M，17GB

# app 端用環境變數覆蓋，不動 agent/.env
docker compose run --rm -e LANGCHAIN_PROVIDER=ollama -e LANGCHAIN_MODEL_NAME=qwen3.8:27b \
  vibe-trading vibe-trading run -p "2330的前景" --no-rich
```

| 項目 | 結果 |
|---|---|
| decode（熱機） | **89 tok/s**，A100 BF16 的 3.9 倍 |
| VRAM | 24.7GB（含 Windows 桌面約 4GB） |
| 推理分離 | Ollama `/v1` 自動把推理放在 `reasoning` 欄位，不需額外設定 |
| 工具呼叫 | 正常 |
| agent run | **失敗**，15.6 分鐘，`empty_model_response` |

agent run 的數字：

- `answer_rejected` = **0**。推理不再進 `content`，grounding 死亡螺旋消失，根因成立。
- 但 iter 4 與 iter 15 各輸出約 46k token，輸入 + 輸出 = 65,532 / 65,536，**撞上 context 上限**。
  模型一直在想、沒有收尾，最後 content 和 tool call 都空的。
- 15 次呼叫裡壓縮了 8 次，同一份行情被重抓 7 次。

新的疑點：`agent/.env` 設 `LANGCHAIN_TEMPERATURE=0.0`，但 Qwen3.8 官方建議 thinking 模式用
`temperature=1.0, top_p=0.95, top_k=20`，並警告低溫可能無限重複。
openai / ollama 路徑也沒有設 `max_tokens`，推理沒有上限，只能靠撞牆結束。

`TOKEN_THRESHOLD=24000` 是工具定義還佔 34.6k 時定的；白名單後剩約 7k，這個門檻太低，造成反覆壓縮。

重跑第二輪：`LANGCHAIN_TEMPERATURE=1.0`、`TOKEN_THRESHOLD=40000`

```bash
docker compose run -d --name vt-ollama-run2 \
  -e LANGCHAIN_PROVIDER=ollama -e LANGCHAIN_MODEL_NAME=qwen3.8:27b \
  -e LANGCHAIN_TEMPERATURE=1.0 -e TOKEN_THRESHOLD=40000 \
  vibe-trading vibe-trading run -p "2330的前景" --no-rich
```

**成功，1.3 分鐘**，4 次呼叫、輸出 4,084 token，退回 1 次後一次修正通過。

### 2026-09-29 實測：本機 vLLM（RTX 5090）

```bash
docker run -d --name vllm-qwen --gpus all --ipc=host -p 127.0.0.1:8000:8000 \
  -v hf_cache:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model nvidia/Qwen3.8-27B-NVFP4 --served-model-name Qwen/Qwen3.8-27B \
  --max-model-len 131072 --kv-cache-dtype fp8_e4m3 \
  --gpu-memory-utilization 0.90 --max-num-seqs 2 \
  --limit-mm-per-prompt '{"image":0,"video":0}' \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder

# served-model-name 跟 agent/.env 的 LANGCHAIN_MODEL_NAME 一致，.env 不用改
docker compose run --rm vibe-trading vibe-trading run -p "2330的前景" --no-rich
```

模型先下載到 volume：`hf download nvidia/Qwen3.8-27B-NVFP4`（21.3GB）。vLLM 0.30.0，啟動約 2 分鐘。

| 項目 | vLLM NVFP4 | Ollama Q4_K_M |
|---|---|---|
| decode | 60 tok/s | 89 tok/s |
| VRAM | 28.5GB（KV cache 173k token） | 24.7GB |
| 推理分離 | 需 `--reasoning-parser qwen3` | 自動 |
| agent run | **成功，9.9 分鐘**，12 次呼叫、輸出 32k | **成功，1.3 分鐘**，4 次呼叫、輸出 4k |

兩次 run 的差距主要是模型這次查得比較深（17 個工具呼叫），不是引擎本身。
各只有一次樣本，要比較引擎得各跑幾次。

注意：上面兩次 agent run 用的是 09-07 建的舊 image。merge 後 zh-TW 語系檔缺 262 個新 key，
frontend 型別檢查失敗、image 一直建不起來，`docker compose run` 就沿用舊的。補齊 key 後重建才是新程式。

**兩邊都能用。** 關鍵是三件事，跟引擎無關：推理要跟答案分開、溫度照官方設 1.0、壓縮門檻回到 40000。
A100 那次的 25–30 分鐘與「不可用」結論作廢。

### merge 後的新程式：兩個新坑

用新程式、照 `.env` 跑 vLLM，前兩次都失敗：

1. **400 `System message must be at the beginning.`**（7.1 分鐘）
   模型回空內容時，loop 以 `role=system` 補一則提醒；Qwen 的 chat template 只收開頭的 system。
   upstream #1112 修過同類問題但漏了三處，已照同樣方式改成 user 訊息加 `<system>` 標籤。
2. **失憶迴圈**（27 分鐘仍未結束，手動停止）
   upstream 的 microcompact 在估算 token 超過 `TOKEN_THRESHOLD × 0.5` 時，只留最近 3 筆工具結果；
   09-05 起被清掉的結果又允許重新呼叫。估算值（中文係數 × 1.4）一開始就超過 20k，
   所以每一輪都清、每一輪都重抓：33 次呼叫、72 次工具呼叫，同一批 6 個工具輪流重跑。

   修法（只改設定）：vLLM `--max-model-len 131072`（KV cache 放得下 185k），
   `TOKEN_THRESHOLD=90000`，微壓縮延到估算 45k 才啟動。
   **注意：這個門檻要搭配 131k context；Ollama 若維持 65536 就得調回 40000。**

第三次：**成功，6.3 分鐘**，5 次呼叫、microcompact 0 次，EPS 66.26 正確。
但降級：模型把 FinMind 的元自行換算成「億」，gate 對不上工具數據，40 個數字被遮成「（略※）」。

### 過程中發現的台股資料 bug（已修）

`get_financial_statements` 的台股年度資料只取 12 月那筆。FinMind 的損益表是**單季**值
（2330 在 2025 Q4 的 EPS 是 19.51，四季加總 66.26），現金流量表才是年初累計。
Ollama 那次的回答因此把本益比算成 127 倍（實際約 37 倍）；vLLM 那次的回答則自己發現各來源對不上，標註「無法內部自洽」。

修法：年度損益表改為四季加總，缺季的年度直接不報；資產負債表與現金流維持取 12 月。
季度現金流另標 `basis: year_to_date`。FinMind 實測 2025 EPS 66.26、2024 45.26、2023 32.34。

### 降級處理：grounding 與 Qwen 的三個摩擦點（已修）

| 問題 | 症狀 | 修法 |
|---|---|---|
| gate 只認簡體「万／亿」 | 「3,809 億」當成 3809 比對，40 個數字被遮 | 加入「萬、億、兆」 |
| 修正輪沒有工具，但 microcompact 同輪清掉資料 | 模型回「我要重抓資料」，無數字、通過 gate、以 success 放行 | 修正輪不做 microcompact |
| 欄位路徑省略列表索引 | `earnings_trend.eps_avg` 對不到 `earnings_trend[2].eps_avg`，被判「不在該次呼叫」 | 比對時也試去掉索引的路徑；數值仍須相符 |
| 修正輪回一句話放棄草稿 | 4.5k 字草稿變 139 字「讓我先驗算」，以 success 放行 | 回覆短於草稿 1/5 時，改放行遮蔽後的原草稿 |

最後一次端對端（5090、vLLM NVFP4、「2330的前景」）：**17.6 分鐘**，4 次退回，3,033 字報告，22 個數字被遮。
四季 EPS、技術指標、法人評等都正確；被遮的多是 Yahoo 共識 EPS 與目標價。

時間幾乎都花在退回重寫：前三份草稿各 51–81 個問題，主要是
「數字未宣告」（約 25 個）與「推導式 gate 算不出來」（14–39 個）。
這是 27B 模型對 upstream figures 區塊規範的遵循度問題，不是 bug。

---

## 現行建議（2026-09-29）

### 設定

`agent/.env`：

```bash
LANGCHAIN_PROVIDER=openai
LANGCHAIN_MODEL_NAME=Qwen/Qwen3.8-27B
OPENAI_BASE_URL=http://host.docker.internal:8000/v1
LANGCHAIN_TEMPERATURE=1.0                    # Qwen 官方：思考模式 1.0；0.0 會無限推理
TOKEN_THRESHOLD=90000                        # 需搭配 131k context；65k context 用 40000
VIBE_TRADING_LLM_TIMEOUT_SECONDS=900
VIBE_TRADING_RUN_STALL_TIMEOUT_SECONDS=3600  # 須 > LLM timeout × (1 + MAX_RETRIES)
VIBE_TRADING_SSE_TIMEOUT=900                 # 須 >= LLM timeout
```

vLLM 三個必要參數，換機器也不能少：

- `--reasoning-parser qwen3`：少了它，推理進 `content`，grounding 核對推理文字，退回迴圈失控
- `--max-model-len 131072`：少了它，microcompact 每輪清資料，失憶迴圈
- `--served-model-name Qwen/Qwen3.8-27B`：跟 `.env` 的模型名一致

### A100 要用量化版

| | RTX 5090 實測 | A100 BF16 實測 | A100 量化（預估） |
|---|---|---|---|
| 權重 | NVFP4 21.9GB | BF16 約 54GB | FP8 30.9GB／INT4 約 20GB |
| decode | 45–60 tok/s | 23 tok/s | FP8 約 1.7 倍、INT4 約 2–2.5 倍 |
| 「前景」一次 | 17.6 分鐘 | 推估 30 分鐘以上 | 接近 5090 |

A100 沒有 FP4 硬體，NVFP4 版不能用。可選：

- `Qwen/Qwen3.8-27B-FP8`（官方，30.9GB）：Ampere 上走 weight-only FP8，品質最接近原版
- `cyankiwi/Qwen3.8-27B-AWQ-INT4`、`RedHatAI/Qwen3.8-27B-INT4`（社群，約 20GB）：更快，品質要自己驗

A100 80GB 放得下 131k context 的 KV cache，量化後餘裕更多。**以上 A100 量化數字未實測。**

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 --served-model-name Qwen/Qwen3.8-27B \
  --max-model-len 131072 --gpu-memory-utilization 0.90 \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

### 哪類問題快

時間取決於**答案裡有多少數字**，不是分析類型。每個數字都要過 grounding，數字越多越容易被退回重寫。

| 問法 | 數字量 | 實測／預期 |
|---|---|---|
| 「回測 2330 均線交叉 2020–2025」 | 少，照抄結果檔 | 2 分鐘（A100 實測） |
| 「2330 近 60 日均線、RSI、量能，判斷短線趨勢」 | 少，全來自工具 | 快 |
| 「2330 最近四季 EPS 與毛利率」 | 中 | 快 |
| 「2330的前景」 | 50–80 個，含大量推導 | 6–18 分鐘（5090 實測） |

開放式問題拆成幾個窄問題，通常比一次問「前景」快，被遮的數字也少。

---

## 模型行為觀察

正面：

- 動手前會 grep 原始碼驗證前提
- 發現配置無效時明講並提出替代，不硬幹
- 寫完 `signal_engine.py` 先 `py_compile` 再回測
- 讀得懂 skill —— 照指示加了 `position_adjustment: "rebalance"`
- 數字逐字照抄 `metrics.csv`，連 `0.0856467424999996` 都沒自己四捨五入

需要用 prompt 約束：

- 會自己寫額外分析腳本，燒光迭代預算
- 會編造進場價，被 grounding gate 擋下後整份回答降級

有效的兩條約束：

```
- 不要寫額外的分析腳本，跑完回測讀 artifacts/metrics.csv 就回報。
- 報告裡每個數字都必須逐字來自工具回傳。沒取到的就寫「未取得」。
```

加上這兩條之後：1h39m → 11m33s，草稿被拒 4 次 → 0 次。
