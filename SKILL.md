# unoffical_renaiss_price Skill 🐾

**Version: v1.3**

**v1.3 更新重點**
- 已同步 `tcg_pro` 核心能力：`bot.py`、`market_report_vision.py`、`image_generator.py`、`templates/`、`fonts/`
- 新增 Discord Bot 執行模式（斜線指令與討論串流程）
- 新增 `/profile` 錢包收藏海報互動流程（語言選擇 + 模板 + SBT/Card 選擇）
- Wallet 海報模板已納入 `templates/profile/wallet_profile.html`（目前正式模板選項為 Top 1 / Top 3 / Top 10）
- 新增 `jinja2` 依賴（wallet profile 模板渲染需要）

**v1.2 更新重點**
- 新增 Google Gemini 視覺辨識支援 (`GOOGLE_API_KEY`)
- 預設辨識模型改為 Gemini 3 Flash (`gemini-3-flash-preview`)
- 辨識備援順序：Google Gemini -> OpenAI -> MiniMax
- PriceCharting 分級邏輯改為嚴格 PSA：`PSA 10` 不再混入 `CGC/BGS/SGC 10`
- Flow A 外部 JSON 新增 `language` 欄位 (`EN` / `JP` / `Unknown`) 供 SNKRDUNK 平手時 tie-break
- 報告與海報新增 Gemrate PSA 統計（總數、PSA10、PSA9、PSA8以下、滿分率）
- v3 海報樣式更新：PSA 區塊 + Global 區塊共用同一個外層容器

Welcome to **unoffical_renaiss_price**, the high-performance TCG intelligence engine. This guide ensures that both human developers and AI agents can achieve professional results in seconds.

---

## ⚡ Quick Start (The 2-Minute Setup)

### 1. 🔑 API Configuration
Create a `.env` file in the project root to unlock full capabilities:

```env
# Default primary vision provider (recommended)
GOOGLE_API_KEY=your_google_key_here
# Optional: override model name (default is gemini-3-flash-preview)
GEMINI_MODEL=gemini-3-flash-preview

# Essential for Japanese cards & precise text recognition
MINIMAX_API_KEY=your_minimax_key_here

# Used for fallback recognition and report formatting
OPENAI_API_KEY=your_openai_key_here

# Required when running Discord bot mode
DISCORD_BOT_TOKEN=your_discord_bot_token_here
```
> [!IMPORTANT]
> **Native Mode**: If these keys are missing, unoffical_renaiss_price enters "Native Mode," performing basic identification using only file metadata.
>
> Vision fallback order (default): `Google Gemini (gemini-3-flash-preview)` -> `OpenAI` -> `MiniMax`.

### 2. Choose Your Entry Flow
| Flow | Best For | Requirement |
| :--- | :--- | :--- |
| **Flow A (External)** | AI Agents with Vision | Pre-extracted JSON |
| **Flow B (Internal)** | Automation / Simple Bots | Image Path |

> [!IMPORTANT]
> Both flows are first-class and must stay supported:
> - Use **Flow A** when the agent already has card metadata (its own vision system).
> - Use **Flow B** when the agent wants unoffical_renaiss_price to do vision with API keys.
> Never remove one flow as a fallback for the other.

### 3. Runtime Modes
- **CLI/Facade Mode** (default for skills): use `openclaw_facade.py` for JSON or image workflows.
- **Discord Bot Mode**: run `python3 bot.py` to enable slash commands and thread-based interaction in Discord.

**Discord Bot Mode Commands (current)**
- `/manual_analyze`: image-based manual candidate selection + report/poster generation.
- `/profile address:<0x...>`: wallet collection poster wizard in a thread (language -> template -> SBT/cards -> generate).

### 🖼️ Poster Template Versions
- Supported versions: `v1`, `v3` (legacy alias `b3` is accepted and mapped to `v3`)
- Default: `v3` (must be treated as default unless user explicitly requests otherwise)
- CLI override: `--poster_version v1` or `--poster_version v3` (or `b3`)
- JSON mode can optionally include `"poster_version": "v1"` / `"v3"` / `"b3"` in the input metadata.

---

## 🚀 Flow A: For AI Agents (Vision-to-Market)
If you are an AI agent with image analysis capabilities (e.g., Gemini, GPT-4o), follow this **Step-by-Step Logic Flow**:

### 🧠 Step 1: Internal Reasoning (Agent-Only)
When you receive a TCG card image, follow this internal checklist:
1. **Vision Check**: Look for the **Card Name**, **ID Number** (e.g., 001/165), and **Set Code** (e.g., SV1, OP05).
2. **Metadata Extraction**: Do not guess. If you see "Pikachu" and "005/015", that is your core data.
3. **Format**: Construct a metadata JSON using the template below.

### 📋 Step 2: Metadata Template (Copy & Fill)
Ensure your JSON matches this structure exactly:
```json
{
  "name": "Card Name (e.g., Pikachu V)",
  "number": "Card Number (e.g., 001/185)",
  "set_code": "Set ID (e.g., SV4)",
  "grade": "Common / PSA 10 / Raw",
  "language": "EN / JP / Unknown (for SNKRDUNK tie-break)",
  "category": "Pokemon / One Piece / Union Arena",
  "is_alt_art": false
}
```

### 🛠️ Step 3: Execution (The Handoff)
Pass your extracted data to unoffical_renaiss_price via the CLI. This skips internal vision and goes straight to the market report.

**CLI Command:**
```bash
python3 openclaw_facade.py --mode full --poster_version v3 --json '{"name": "Mewtwo", "number": "150/165", "set_code": "SV1", "grade": "PSA 10", "language": "JP"}'
```

---

## 🤖 Flow B: Auto-Vision (Agent Provides Image, Script Does the Rest)
If you prefer not to write JSON manually, or if you want the script's built-in LLM to analyze the image, you can simply pass the image path directly to the script. The script will automatically parse the image, generate the markdown report, and build the posters.

**CLI Command:**
```bash
python3 openclaw_facade.py "path/to/downloaded/image.jpg" --mode full --poster_version v3
```

*Note: This flow requires at least one of `GOOGLE_API_KEY` / `OPENAI_API_KEY` / `MINIMAX_API_KEY` to be set in `.env`. Default first choice is Gemini 3 Flash.*

---

## 📊 Precision Guide for AI Agents
To get 100% accurate market data, agents should pay attention to these fields:
- **`set_code`**: This is critical for PriceCharting/SNKRDUNK filters. Use codes like `SV1`, `OP01`, `EB01`.
- **`grade`**: Use `"PSA 10"`, `"BGS 9.5"`, or `"Raw"` (default).
- **`language`**: Use `"EN"` / `"JP"` / `"Unknown"`; this is used as SNKRDUNK final tie-break when candidates are otherwise tied.
- **`is_alt_art`**: Set to `true` if the card is a Parallel Art or Special Illustration Rare.

### 📈 PriceCharting Grade Rule (Important)
- `step2_pc.json` is parsed/normalized data, not raw website output.
- Parser uses strict grade mapping:
  - `PSA 10` only from explicit PSA-10 tokens
  - `CGC/BGS/SGC 10` are kept separate and never merged into `PSA 10`
- This avoids false PSA10 prices caused by generic `10` tokens in listing text.

### 🧬 Gemrate / PSA Data (Report + Poster)
- FULL mode includes Gemrate population stats in report text:
  - Total Population
  - PSA 10 count
  - PSA 9 count
  - PSA 8 and below
  - Gem Mint Rate (%)
- v3 market poster includes PSA data cards and global market stats cards.
- Current layout: PSA section + Global section are rendered inside one shared outer stats container.

### 🧪 Debug Artifacts
- `step2_pc_source.md`: raw PriceCharting markdown (source snapshot)
- `step2_pc.json`: parsed PriceCharting records (with normalized grades)
- `step2_snkr.json`, `step2_meta.json`, `step3_report.md`

---

## 👾 Discord Interaction Protocol (CRITICAL FOR UI AGENTS)
When you receive a user's request with an image in Discord, you **MUST** follow these 3 steps exactly.

### ✅ Required Delivery Sequence (No Skips)
You MUST follow this exact order:
1. Create thread and capture `thread_id`.
2. Run unoffical_renaiss_price and parse output JSON.
3. Send `report_text` into that `thread_id`.
4. Send `poster_data.profile` into that `thread_id`.
5. Send `poster_data.market` into that `thread_id`.

Never send only posters without `report_text`.
Never send any report/poster to the parent channel.
If `thread_id` is missing, stop and report an error instead of sending anything.

### 🚨 Transport Rule (Must Follow in This Environment)
For thread messaging, always use `action="send"` with `channelId=<thread_id>`.

**Correct (send text to thread):**
```python
message(
  action="send",
  channel="discord",
  channelId="1480914216608071832",
  message="訊息內容"
)
```

**Correct (send image to thread):**
```python
message(
  action="send",
  channel="discord",
  channelId="1480914216608071832",
  message="圖片說明",
  filePath="~/clawd/圖片.png"
)
```

**Wrong (do not use):**
```python
message(
  action="thread-reply",
  target="主頻道ID",
  threadId="ThreadID"
)
```

`thread-reply` with `target` routes to the parent channel in this setup and may not deliver inside the thread.  
After creating the thread, always store that thread ID and reuse it as `channelId` for both text and image sends.

### 🚫 Forbidden Behaviors
- Sending posters to main channel.
- Sending posters before sending `report_text`.
- Skipping thread creation and posting directly to parent channel.
- If unoffical_renaiss_price returns both text and posters, sending only posters.

**1. Isolate (Create Thread):**
You MUST NOT reply with the report in the main channel. You MUST use your thread-creation action/tool to create a new thread for the user's request.

For `message(action="thread-create", ...)`, the parameter name is **`threadName`** (not `name`).

**Correct:**
```python
message(
  action="thread-create",
  channel="discord",
  guildId="1469685526427734181",
  threadName="Gastly SV2a 市場報告"
)
```

**Wrong:**
```python
message(
  action="thread-create",
  channel="discord",
  guildId="1469685526427734181",
  name="Gastly SV2a 市場報告"
)
```

**2. Execute (Run openclaw):**
You MUST execute `openclaw_facade.py` in FULL mode. You have TWO ways to do this, choose ONE based on your capabilities:

- **Flow A (External JSON)**: If you extract the card info yourself, run:
  `python3 openclaw_facade.py --mode full --poster_version v3 --json '{"name": "...", "number": "...", "set_code": "...", "grade": "..."}'`

- **Flow B (Image Pass-through)**: If you want the script's native vision to do the work, download the image and pass it:
  `python3 openclaw_facade.py "path/to/downloaded/image.jpg" --mode full --poster_version v3`

- **Optional switch to legacy layout**: add `--poster_version v1` only if user asks for old design.

**Important Note:** The script will always output a JSON containing the textual report AND the absolute paths to the generated poster images. It looks exactly like this:
```json
{
  "report_text": "Markdown string of the market report...",
  "poster_data": {
    "profile": "/path/to/report_xxx_profile.png",
    "market": "/path/to/report_xxx_data.png"
  },
  "status": "success"
}
```

### 🔒 Output Contract (Non-Negotiable)
When your tool returns JSON from unoffical_renaiss_price:
- `report_text` is the **source of truth** for message content. Post it as-is.
- Do **not** rewrite, summarize, translate, or reformat `report_text`.
- Do **not** invent your own report template.
- Always upload both `poster_data.profile` and `poster_data.market`.
- If `poster_data` is empty or missing, treat that as a tool error and report it explicitly.
- If your messaging tool cannot send text+file in one call, send in 3 separate calls in the SAME thread: text -> profile image -> market image.

### 🛑 Immutable Report Policy (HIGHEST PRIORITY)
`report_text` is an immutable payload. You are a transporter, not an editor.

You MUST send the exact string from `report_text`:
- Keep original headings, emojis, punctuation, spacing, and line breaks.
- Do not prepend or append extra commentary before/after report body.
- Do not "optimize" wording, grammar, formatting, or section order.
- Do not convert language (e.g., zh -> en or en -> zh).
- Do not replace with your own markdown template.

Allowed operations are only:
1. Read `report_text` from tool output.
2. Send that exact text to the thread.
3. Send `poster_data.profile` and `poster_data.market`.

If you cannot send the exact `report_text`, stop and report transport error.  
Never send an altered report.

**3. Report (Post in Thread):**
You MUST read the output JSON from the script. Then, send a new message in the created thread where:
- The **message text** is exactly the string found in `"report_text"`.
- You **MUST upload BOTH image files** listed under `"poster_data"` (`profile` and `market` paths). Do not assume the files don't exist; wait for them and USE YOUR LOCAL SYSTEM TOOLS to read and attach those two absolute file paths!
If you forget to upload the posters, the user will be very disappointed.

### 🧪 Final Pre-Send Checklist
Before sending anything, verify:
- I have a valid `thread_id`.
- All sends use `action="send"` and `channelId=<thread_id>`.
- I will send `report_text` first.
- I will send the exact `report_text` string with zero edits.
- I will send both posters after text.
- I am not sending to parent channel.

## 📁 Directory Structure
- `openclaw_facade.py`: CLI facade entry point (supports `--json` flow and image flow).
- `bot.py`: Discord bot entry point (slash commands + thread workflow).
- `market_report_vision.py` / `image_generator.py`: Synced core engine at repo root.
- `templates/` + `fonts/`: Active rendering assets for root runtime.
- `scripts/`: Compatibility runtime path (also synced for facade imports).
- `SKILL.md`: This guide.
