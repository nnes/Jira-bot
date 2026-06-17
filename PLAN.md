# Plan: Tạo CLAUDE.md quy tắc phát triển cho Multi-Model Agent

## Context

Dự án `clawathon` là một backend **Multi-Model AI Agent** cho team E-Wallet/Payment Gateway, tự động biến mô tả thô của user thành Jira Ticket chuẩn production. Hiện trạng repo là **greenfield**: chỉ có file spec (`AGENT_SPEC.md`, `JIRA_TICKET_SPEC.md`, `change-requirement-template.md`) và file cấu hình hạ tầng (`Dockerfile`, `docker-compose.yml`, `.env.example`), **chưa có code Python**. File `CLAUDE.md` ở root đang tồn tại nhưng **rỗng**.

Mục tiêu: tạo `CLAUDE.md` (root) làm "hiến pháp" phát triển — để mọi phiên Claude Code sau này tự động nạp và tuân thủ kiến trúc multi-model, security guardrails, quy tắc sinh Jira ticket, và **luồng phát triển/test cục bộ qua Bot Framework Emulator**.

Hai điểm cần giải quyết:
1. **Bot Framework Emulator**: AGENT_SPEC nói "MS Teams" nhưng vòng lặp dev/test cục bộ dùng Bot Framework Emulator (bot expose `POST /api/messages`, Emulator nối tới `http://localhost:8000/api/messages`, App ID/Password để trống khi chạy local). CLAUDE.md phải mô tả rõ luồng này.
2. **`.env.example` lệch spec**: file hiện dùng `OPENAI_API_KEY` + `LLM_MODEL=gpt-4`, thiếu `LLM_BASE_URL`/`LLM_API_KEY` và 3 model name bắt buộc. Cần sửa cho khớp.

## Quyết định đã chốt với user
- **Ngôn ngữ**: Song ngữ — diễn giải tiếng Việt, giữ tiếng Anh cho thuật ngữ kỹ thuật / tên model / biến môi trường.
- **Vị trí**: Ghi đè `CLAUDE.md` ở thư mục gốc (đang rỗng).
- **Env**: Cập nhật luôn `.env.example` cho khớp AGENT_SPEC (thêm `LLM_BASE_URL`, `LLM_API_KEY`, 3 model name; biến Bot Framework local).
- **Cấu trúc code (§4)**: Modular package `app/` (không bắt buộc theo layout phẳng của AGENT_SPEC). Web framework: **FastAPI** + botbuilder `CloudAdapter`.

## Files cần tạo/sửa
1. **`CLAUDE.md`** (root) — tạo mới (ghi đè file rỗng). Nội dung chính bên dưới.
2. **`.env.example`** (root) — cập nhật để khớp multi-model + Bot Framework Emulator.

> Lưu ý: KHÔNG sửa `.env` thật (chứa secret thật của user) trong kế hoạch này — chỉ sửa `.env.example`.

## Cấu trúc nội dung CLAUDE.md (đề xuất)

### 1. Project Overview (song ngữ, ~5 dòng)
Tóm tắt: Multi-Model Agent biến mô tả user → Jira Ticket chuẩn cho domain Fintech (E-wallet, Payment Gateway, Core Banking, Transactions, Security, Reconciliation).

### 2. Tech Stack & Conventions
- Python 3.11+, **LangGraph** (stateful, cyclic graph) là framework chính.
- LLM truy cập qua **OpenAI-compatible client** với `base_url=LLM_BASE_URL`, `api_key=LLM_API_KEY`.
- Bot: **botbuilder** (`botbuilder-core`, `CloudAdapter`) + **FastAPI**.
- Quy ước: type hints bắt buộc, `pydantic`/`TypedDict` cho state, không hardcode secret, đọc config qua `config.py`.

### 3. Multi-Model Architecture & Routing (BẢNG)
| Role | Model | Khi nào dùng |
|------|-------|--------------|
| Orchestrator / Chatbot | `minimax/minimax-m2.5` | Intent recognition, slot-filling, giữ conversation state, chat với user qua bot |
| Context Enricher / Reranker | `qwen/qwen3-reranker-8b` | CHỈ khi user cung cấp Confluence link — semantic rerank nội dung PRD/System Design |
| Fintech Logic / Ticket Generator | `qwen/qwen3-5-27b` | Tổng hợp context (chat + confluence) → suy luận fintech, điền gap, sinh JSON ticket |

Kèm **state transition graph** dạng text:
`Teams/Emulator message ➔ minimax (intent + slot-fill loop) ➔ [nếu có Confluence link] qwen3-reranker ➔ qwen3-5-27b (sinh ticket) ➔ Jira API (create, có confirm nếu update)`

### 4. File / Directory Structure (modular package — cải tiến so với AGENT_SPEC)

Thay layout phẳng của AGENT_SPEC bằng **package `app/` phân lớp** (separation of concerns), framework web = **FastAPI** + botbuilder `CloudAdapter`:

```
app/
  __init__.py
  main.py                  # FastAPI app: POST /api/messages, GET /health; wire bot adapter
  config.py                # pydantic-settings: load env, expose Settings + client factories

  bot/
    adapter.py             # botbuilder CloudAdapter (App ID/Password blank cho local/Emulator)
    handler.py             # ActivityHandler: Activity → gọi graph → reply
    conversation_store.py  # lưu/khôi phục state theo conversation.id (slot-filling đa lượt)

  graph/
    builder.py             # build & compile LangGraph StateGraph (nodes + edges)
    state.py               # AgentState (TypedDict): messages, slots, confluence_data, ticket_json
    routing.py             # conditional edge: có Confluence link? → reranker, else → generator
    nodes/
      orchestrator.py      # minimax-m2.5: intent recognition + slot-filling
      reranker.py          # qwen3-reranker-8b: rerank/enrich (chỉ khi có link)
      generator.py         # qwen3-5-27b: fintech reasoning → ticket JSON

  llm/
    client.py              # factory OpenAI-compatible client (base_url, api_key)
    registry.py            # map role → model name (ORCHESTRATOR/RERANKER/GENERATOR_MODEL)

  prompts/
    orchestrator.py        # system prompt minimax (slot-filling, human tone)
    generator.py           # system prompt qwen3.5 (fintech reasoning, schema adherence)

  schemas/
    ticket.py              # pydantic: Epic/Story/Task JSON theo JIRA_TICKET_SPEC + template

  integrations/
    jira/
      client.py            # Jira Server REST: CHỈ read + create (Epic/Story/Task)
      guards.py            # enforce: NO delete; UPDATE → cờ confirmation
    confluence/
      reader.py            # Confluence REST: READ-ONLY (fetch page content)

  core/
    errors.py              # custom exceptions (JiraError, ConfluenceUnavailable, LLMError…)
    retry.py               # exponential backoff cho external API calls
    pii.py                 # guardrail: detect/mask plain card number, PIN, password
    logging.py             # cấu hình logger theo LOG_LEVEL

tests/                     # test_routing, test_jira_guards, test_pii, …
main.py                    # root entry mỏng: chạy uvicorn app.main:app (giữ Dockerfile CMD hợp lệ)
requirements.txt
```

**Lý do tốt hơn layout phẳng:**
- Tách rõ 4 quan tâm: **bot I/O** (`bot/`), **orchestration** (`graph/`), **external services** (`integrations/`), **cross-cutting** (`core/`).
- Guardrails sống tập trung tại `integrations/jira/guards.py` + `core/pii.py` → dễ audit, khó vi phạm.
- Prompt & schema tách file riêng → versioned, dễ test, không nhúng inline.
- Dễ unit test từng node/guard độc lập (`tests/`).

> Lưu ý Docker: Dockerfile hiện chạy `python main.py`. Giữ root `main.py` mỏng gọi `uvicorn.run("app.main:app", host=APP_HOST, port=APP_PORT)` để CMD cũ vẫn chạy; hoặc đổi CMD sang `uvicorn app.main:app`. Sẽ ghi rõ trong CLAUDE.md §6.

### 5. Security Guardrails (CRITICAL — đặt nổi bật)
- **Jira**: CHỈ Read + Create (Epic/Story/Task). TUYỆT ĐỐI không DELETE. Mọi UPDATE phải qua **confirmation loop** với user.
- **Confluence**: STRICTLY READ-ONLY.
- **Fintech Compliance**: ticket sinh ra KHÔNG được chứa PII giả lập (plain card number, PIN, password).
- Tool layer phải tự enforce: không expose hàm delete/destructive; assert trước khi gọi API.

### 6. Local Development — Bot Framework Emulator (trọng tâm user yêu cầu)
- Bot expose `POST /api/messages` tại `APP_HOST:APP_PORT` (mặc định `0.0.0.0:8000`).
- Endpoint `GET /health` cho healthcheck (đã có trong Dockerfile/compose).
- **Kết nối Emulator**: mở Bot Framework Emulator → "Open Bot" → URL `http://localhost:8000/api/messages` → **để trống** Microsoft App ID & Password khi test local.
- App ID/Password (`TEAMS_BOT_APP_ID`, `TEAMS_BOT_APP_PASSWORD`) chỉ cần khi deploy lên Azure Bot Service / Teams thật → để trống/blank cho local.
- Conversation state key theo `conversation.id` của Activity để giữ ngữ cảnh slot-filling.
- Lệnh chạy local: `python main.py` hoặc `uvicorn app.main:app --host 0.0.0.0 --port 8000`. Chạy Docker: `docker compose up`.

### 7. Jira Ticket Generation Rules (tham chiếu spec)
- Tuân thủ `JIRA_TICKET_SPEC.md` + `change-requirement-template.md`.
- Project Key: **User nhập**, Product Domain: **User nhập** (hỏi user, không hardcode).
- Summary format: `[System/Service Name] <Action/Capability...>`.
- Description theo 3 phần: Context / Requirement (Product|Technical|Configuration) / Acceptance Criteria (Gherkin).
- Default: Priority `P3 (Medium)`, Assignee Unassigned, Sprint `Next Sprint`, Story Points `0`.
- **Clarification loops bắt buộc**: Epic Link (Story/Task), Assignee, Sprint (Active vs Next), Story Points; verify Epic tồn tại trước khi link.

### 8. Prompt Strategy (tóm tắt + chỗ đặt)
- Prompt sống trong `app/prompts/*.py`. Versioned, không nhúng inline rải rác.
- `minimax-m2.5`: tone người thật, tập trung slot-filling, hỏi đúng thông tin còn thiếu, không bịa.
- `qwen3-5-27b`: reasoning domain fintech, bám sát markdown schema của template, output JSON hợp lệ.

### 9. Error Handling
- Jira/Bank API timeout → retry có backoff, báo user rõ ràng, không tạo ticket một nửa.
- Confluence link không truy cập được → bỏ qua bước rerank, báo user, tiếp tục chỉ với context chat.
- LLM provider lỗi → fallback message, log `LOG_LEVEL`.

### 10. Environment Variables (bảng, khớp .env.example đã sửa)
Liệt kê & giải thích: `LLM_BASE_URL`, `LLM_API_KEY`, model names; `JIRA_*`, `CONFLUENCE_*`; `TEAMS_BOT_APP_ID/PASSWORD` (blank cho local); `APP_HOST/PORT`, `LOG_LEVEL`, `DEBUG`.

## Cập nhật `.env.example` (đề xuất nội dung)
- **Thêm**: `LLM_BASE_URL`, `LLM_API_KEY`, `ORCHESTRATOR_MODEL=minimax/minimax-m2.5`, `RERANKER_MODEL=qwen/qwen3-reranker-8b`, `GENERATOR_MODEL=qwen/qwen3-5-27b`.
- **Bỏ/thay**: `OPENAI_API_KEY`, `LLM_MODEL=gpt-4` (không khớp spec multi-model).
- Giữ: `JIRA_*`, `CONFLUENCE_*`, `APP_HOST/PORT`, `LOG_LEVEL`, `DEBUG`.
- Bot: ghi chú rõ `TEAMS_BOT_APP_ID`/`TEAMS_BOT_APP_PASSWORD` để **trống** khi test bằng Emulator.

## Verification
1. `cat CLAUDE.md` — kiểm tra đủ 10 mục, song ngữ, bảng model & env render đúng.
2. `cat .env.example` — xác nhận có `LLM_BASE_URL`/`LLM_API_KEY` + 3 model name, không còn `LLM_MODEL=gpt-4`.
3. Đối chiếu chéo: mọi guardrail trong AGENT_SPEC §25-28 đều xuất hiện trong CLAUDE.md §5.
4. Xác nhận CLAUDE.md §4 mô tả đúng cấu trúc `app/` modular (graph/nodes, integrations, bot, core) + ghi chú Docker CMD.
5. (Tùy chọn) mở phiên Claude Code mới ở repo này để xác nhận CLAUDE.md được auto-load.
