# CLAUDE.md — Development Rules cho Multi-Model Jira Agent

> File này là "hiến pháp" phát triển của dự án `clawathon`. Mọi phiên Claude Code làm việc trong repo này PHẢI tuân thủ các quy tắc bên dưới. Ngôn ngữ: song ngữ — diễn giải tiếng Việt, giữ nguyên tiếng Anh cho thuật ngữ kỹ thuật, tên model và biến môi trường.

---

## 1. Project Overview

Backend **Multi-Model AI Agent** cho team E-Wallet / Payment Gateway. Agent tự động hóa vòng đời biến mô tả thô, thiếu sót của user thành **Jira Ticket** chuẩn production theo industry standards.

- **Domain**: Fintech — E-wallet, Payment Gateway, Core Banking, Transactions, Security, Reconciliation.
- **Input**: tin nhắn user qua MS Teams bot (dev/test cục bộ qua **Bot Framework Emulator**), kèm Confluence link tùy chọn (PRD / System Design).
- **Output**: Jira Ticket (Epic / Story / Task) đúng schema trong `JIRA_TICKET_SPEC.md` + `change-requirement-template.md`.

Nguồn chân lý (source of truth): `AGENT_SPEC.md`, `JIRA_TICKET_SPEC.md`, `change-requirement-template.md`. Khi có mâu thuẫn, các file spec đó thắng file này — và phải cập nhật lại file này cho khớp.

---

## 2. Tech Stack & Conventions

- **Language**: Python 3.11+.
- **Framework chính**: **LangGraph** (stateful, cyclic multi-agent workflow). Dùng LangChain adapter khi cần.
- **LLM access**: OpenAI-compatible client với `base_url=LLM_BASE_URL`, `api_key=LLM_API_KEY`. KHÔNG hardcode endpoint/model.
- **Bot integration**: `botbuilder-core` + `botbuilder-integration-aiohttp` (hoặc FastAPI + `CloudAdapter`). Expose `POST /api/messages`.
- **Conventions**:
  - Type hints bắt buộc trên mọi hàm public.
  - State dùng `TypedDict` (LangGraph) hoặc `pydantic` models.
  - Mọi config đọc qua `config.py` — KHÔNG đọc `os.environ` rải rác trong code.
  - KHÔNG hardcode secret/token. KHÔNG commit `.env`.
  - Log qua logger chuẩn, tôn trọng `LOG_LEVEL`.

---

## 3. Multi-Model Architecture & Routing

Hệ thống dùng **4 model chuyên biệt**, định tuyến theo vai trò:

| Role | Model | Lý do chọn | Khi nào dùng |
|------|-------|------------|--------------|
| Orchestrator / Chatbot | `minimax/minimax-m2.5` | Tối ưu cho multi-turn conversation, long context, slot-filling tự nhiên | Intent recognition, slot-filling, giữ conversation state, chat back-and-forth với user |
| Context Enricher / Reranker | `qwen/qwen3-reranker-8b` | Model chuyên dụng cho semantic scoring — không thể thay bằng generative LLM | **CHỈ** khi user cung cấp Confluence link — rerank nội dung PRD/System Design để enrich technical context |
| Fintech Logic / Ticket Generator | `google/gemma-4-31b-it` | 31B Instruction-Tuned: bám schema JSON chặt, giảm hallucination, reasoning fintech tốt hơn Qwen 3.5 27B | Tổng hợp unified context (chat + confluence) → suy luận domain fintech, điền gap, sinh JSON ticket đúng schema |
| Orchestrator Fallback | `gpt-oss-20b` | General-purpose backup, tránh single-point-of-failure | Khi `minimax-m2.5` timeout / lỗi — tự động failover |

> **Không dùng trong luồng chính (future):** `whisper-large-v3` — dành cho Phase sau nếu cần nhận **voice message** từ MS Teams.

### State transition graph

```
MS Teams / Bot Framework Emulator message
        │
        ▼
[minimax/minimax-m2.5]  Intent + slot-filling loop  ◄─┐  (hỏi lại user tới khi đủ slot)
        │  (fallback: gpt-oss-20b nếu lỗi)            │
        ├── thiếu thông tin ───────────────────────────┘
        │
        ▼ (đủ context)
[có Confluence link?]
    ├── Có ──► [qwen/qwen3-reranker-8b]  rerank & enrich
    │                  │
    └── Không ─────────┤
                       ▼
[google/gemma-4-31b-it]  Fintech reasoning + sinh ticket JSON
                       │
                       ▼
[Jira API]  Create (Epic/Story/Task)  ── nếu là UPDATE ──► confirmation loop với user
```

---

## 4. File / Directory Structure

**Modular package `app/`** phân lớp (separation of concerns) — cải tiến so với layout phẳng của `AGENT_SPEC.md`. Web framework: **FastAPI** + botbuilder `CloudAdapter`.

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
      generator.py         # gemma-4-31b-it: fintech reasoning → ticket JSON

  llm/
    client.py              # factory OpenAI-compatible client (base_url, api_key)
    registry.py            # map role → model name (ORCHESTRATOR/RERANKER/GENERATOR_MODEL)

  prompts/
    orchestrator.py        # system prompt minimax (slot-filling, human tone)
    generator.py           # system prompt gemma-4-31b-it (fintech reasoning, schema adherence)

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

**Nguyên tắc đặt file:**
- Bot I/O (adapter, activity handling) → `app/bot/`.
- Orchestration / model logic (LangGraph nodes, routing, state) → `app/graph/`.
- I/O external API → `app/integrations/<service>/`; guardrails sống cạnh client (`guards.py`).
- Cross-cutting (errors, retry, PII, logging) → `app/core/`.
- Prompt → `app/prompts/`; schema ticket → `app/schemas/`; config → `app/config.py`.

> **Docker**: Dockerfile hiện chạy `python main.py`. Giữ root `main.py` mỏng gọi `uvicorn.run("app.main:app", host=APP_HOST, port=APP_PORT)` để CMD cũ vẫn chạy — hoặc đổi CMD sang `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

---

## 5. Security Guardrails (CRITICAL)

Đây là ràng buộc TUYỆT ĐỐI. Vi phạm = bug nghiêm trọng.

- **Jira**: CHỈ cho phép **Read** và **Create** (Epic, Story, Task). **TUYỆT ĐỐI KHÔNG** thao tác DELETE / destructive. Mọi **UPDATE** phải kích hoạt **confirmation loop** rõ ràng với user trước khi thực thi.
- **Confluence**: **STRICTLY READ-ONLY**. Không create / modify / delete page Confluence dưới bất kỳ hình thức nào.
- **Fintech Compliance**: ticket sinh ra **KHÔNG** được chứa PII giả lập — plain card number, PIN, password. Mask hoặc loại bỏ.
- **Layer enforce**: `app/integrations/jira/guards.py` (no-delete, UPDATE→confirmation), `app/integrations/jira/client.py` + `app/integrations/confluence/reader.py` KHÔNG expose hàm delete/destructive; `app/core/pii.py` lọc PII. Assert/validate trước mỗi call ra external API.

---

## 6. Local Development — Bot Framework Emulator

Vòng lặp dev/test cục bộ dùng **Bot Framework Emulator** (không cần deploy lên Teams thật).

- Bot expose `POST /api/messages` tại `APP_HOST:APP_PORT` (mặc định `0.0.0.0:8000`).
- Có `GET /health` cho healthcheck (đã dùng trong `Dockerfile` / `docker-compose.yml`).

**Kết nối Emulator:**
1. Chạy bot local: `python main.py` (root entry mỏng) hoặc `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
2. Mở **Bot Framework Emulator** → **Open Bot**.
3. Bot URL: `http://localhost:8000/api/messages`.
4. **Để TRỐNG** Microsoft App ID & App Password khi test local.

**App ID / Password:**
- `TEAMS_BOT_APP_ID` và `TEAMS_BOT_APP_PASSWORD` để **trống/blank** khi chạy local qua Emulator.
- Chỉ điền khi deploy lên **Azure Bot Service / MS Teams** thật.

**Conversation state:** key theo `conversation.id` của Activity để giữ ngữ cảnh slot-filling qua nhiều lượt chat.

**Chạy bằng Docker:** `docker compose up` (build từ `Dockerfile`, map port `APP_PORT`). Lưu ý cập nhật CMD sang `uvicorn app.main:app` nếu không giữ root `main.py`.

---

## 7. Jira Ticket Generation Rules

Tuân thủ `JIRA_TICKET_SPEC.md` + `change-requirement-template.md`.

- **Project Key**: do **user nhập** (hỏi user, không hardcode). **Product Domain**: do **user nhập**.
- **Summary format**: `[System/Service Name] <Action or Capability description...>`
  - Ví dụ: `[E-Banking Service] Add biometric authentication for transactions`.
- **Description** theo 3 phần của template:
  1. **Context** — bối cảnh & hệ sinh thái kỹ thuật.
  2. **Requirement** — hỏi user chọn 1 trong 3: **Product** | **Technical** | **Configuration**.
  3. **Acceptance Criteria** — định dạng **Checklist** (`- [ ] <tiêu chí ngắn gọn>`), bao gồm happy path + edge cases.
- **Defaults**: Priority `P3 (Medium)`, Assignee `Unassigned`, Sprint `Next Sprint`, Story Points `0`.
- **Clarification loops bắt buộc** (Story/Task):
  - **Epic Link**: hỏi Epic ID/Link; **verify Epic tồn tại** qua Jira API trước khi link.
  - **Assignee**: hỏi rõ; user bỏ qua → `Unassigned`.
  - **Sprint**: hỏi *"Active sprint hay Next sprint?"*; mặc định `Next Sprint`.
  - **Story Points**: hỏi estimation; mặc định `0`.

---

## 8. Prompt Strategy

- Prompt đặt trong `app/prompts/*.py`. Versioned, không nhúng inline rải rác.
- **`minimax/minimax-m2.5`** (orchestrator): tone tự nhiên như người thật; tập trung slot-filling; hỏi ĐÚNG thông tin còn thiếu; KHÔNG bịa thông tin user chưa cung cấp. Fallback sang `gpt-oss-20b` khi lỗi.
- **`google/gemma-4-31b-it`** (generator): reasoning sâu domain fintech; bám chặt markdown schema của `change-requirement-template.md`; output JSON hợp lệ đúng `JIRA_TICKET_SPEC.md`; Acceptance Criteria theo **Checklist** (`- [ ]`), không dùng Gherkin.

---

## 9. Error Handling

- **Jira / Bank API timeout**: retry có exponential backoff; báo user rõ ràng; **KHÔNG** tạo ticket dở dang (atomic — thành công trọn vẹn hoặc rollback/abort).
- **Confluence link không truy cập được**: bỏ qua bước rerank; báo user; tiếp tục chỉ với context từ chat.
- **LLM provider lỗi**: trả fallback message thân thiện; log chi tiết theo `LOG_LEVEL`; không crash toàn bộ luồng.

---

## 10. Environment Variables

Cấu hình qua `.env` (tham chiếu `.env.example`). Mọi biến đọc qua `config.py`.

| Biến | Mục đích |
|------|----------|
| `LLM_BASE_URL` | Base URL của OpenAI-compatible LLM provider |
| `LLM_API_KEY` | API key cho LLM provider |
| `ORCHESTRATOR_MODEL` | `minimax/minimax-m2.5` — orchestrator/chatbot |
| `RERANKER_MODEL` | `qwen/qwen3-reranker-8b` — context enricher (Confluence semantic rerank) |
| `GENERATOR_MODEL` | `google/gemma-4-31b-it` — fintech reasoning + ticket generator |
| `FALLBACK_MODEL` | `gpt-oss-20b` — backup orchestrator khi minimax lỗi |
| `JIRA_SERVER_URL` / `JIRA_USER_EMAIL` / `JIRA_API_TOKEN` | Jira Server REST API (read + create) |
| `CONFLUENCE_SERVER_URL` / `CONFLUENCE_USER_EMAIL` / `CONFLUENCE_API_TOKEN` | Confluence REST API (read-only) |
| `TEAMS_BOT_APP_ID` / `TEAMS_BOT_APP_PASSWORD` | **Để trống cho local/Emulator**; chỉ điền khi deploy Teams/Azure |
| `APP_HOST` / `APP_PORT` | Host/port bot (mặc định `0.0.0.0` / `8000`) |
| `LOG_LEVEL` | Mức log (`INFO`, `DEBUG`, ...) |
| `DEBUG` | Bật/tắt debug mode |
