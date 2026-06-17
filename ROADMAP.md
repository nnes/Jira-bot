# ROADMAP — Lộ trình phát triển Multi-Model Jira Agent

> Lộ trình build **tăng dần** theo triết lý *walking skeleton trước, feature sau*. Dựng vòng I/O chạy được rồi đắp dần từng model/integration. Jira & Confluence **mock-first** (in JSON preview) rồi mới đấu server thật.
>
> Tham chiếu kiến trúc & quy tắc: [`CLAUDE.md`](CLAUDE.md) · spec: [`AGENT_SPEC.md`](AGENT_SPEC.md), [`JIRA_TICKET_SPEC.md`](JIRA_TICKET_SPEC.md), [`change-requirement-template.md`](change-requirement-template.md).

## Dependency

```
Phase 0 ─► 1 ─► 2 ─► 3 ─► 4 ─┬─► 5 (Jira thật) ─┐
                             └─► 6 (Confluence) ─┴─► 7 (Hardening)
```
Phase 5 và 6 có thể làm song song sau khi xong Phase 4.

## Mốc demo-able
- **Hết Phase 1**: bot chạy, echo trên Emulator.
- **Hết Phase 4**: demo trọn luồng chat → ticket draft (mock Jira).
- **Hết Phase 5**: tạo ticket thật trên Jira.
- **Hết Phase 7**: đủ guardrails, production-ready.

---

## Phase 0 — Skeleton & Config
**Mục tiêu:** Package boot được, có health check.

**Files chính:**
- `requirements.txt`
- `app/config.py` — pydantic-settings, load `.env`, expose `Settings` + client factories
- `app/main.py` — FastAPI app + `GET /health`
- `main.py` (root) — entry mỏng gọi `uvicorn.run("app.main:app", ...)`

**Test:** `python main.py` → `curl localhost:8000/health` trả `200`.

---

## Phase 1 — Bot I/O Echo
**Mục tiêu:** Chứng minh vòng I/O với Bot Framework Emulator **trước khi có LLM**.

**Files chính:**
- `app/bot/adapter.py` — botbuilder `CloudAdapter` (App ID/Password **blank** cho local)
- `app/bot/handler.py` — `ActivityHandler` echo lại tin nhắn
- Wire `POST /api/messages` trong `app/main.py`

**Test:** Mở Emulator → Open Bot `http://localhost:8000/api/messages` (để trống App ID/Password) → nhắn `"hi"` → nhận `"echo: hi"`.

---

## Phase 2 — LLM client + Orchestrator chat
**Mục tiêu:** Bot trả lời bằng `minimax/minimax-m2.5`; có fallback sang `gpt-oss-20b` khi lỗi.

**Files chính:**
- `app/llm/client.py` — factory OpenAI-compatible client (`base_url=LLM_BASE_URL`, `api_key=LLM_API_KEY`)
- `app/llm/registry.py` — map role → model name (`ORCHESTRATOR_MODEL`, `RERANKER_MODEL`, `GENERATOR_MODEL`, `FALLBACK_MODEL`)
- `app/prompts/orchestrator.py` — system prompt (tone người thật, slot-filling)
- Sửa `app/bot/handler.py` gọi LLM thay vì echo; retry với `FALLBACK_MODEL` nếu primary lỗi

**Test:** Chat trong Emulator, bot phản hồi tự nhiên, có ngữ cảnh; tắt thử primary model → tự failover sang fallback.

---

## Phase 3 — LangGraph state + Slot-filling
**Mục tiêu:** Intent recognition + thu thập slot đa lượt, giữ state theo `conversation.id`.

**Files chính:**
- `app/graph/state.py` — `AgentState` (TypedDict): messages, slots, confluence_data, ticket_json
- `app/graph/builder.py` — compile `StateGraph` (orchestrator node)
- `app/graph/nodes/orchestrator.py` — logic intent + slot-filling
- `app/bot/conversation_store.py` — lưu/khôi phục state theo `conversation.id`

**Test:** Nhắn mô tả thiếu thông tin → bot hỏi bổ sung (system/service, action, type...) → giữ ngữ cảnh qua nhiều lượt.

---

## Phase 4 — Ticket Generator + Schema (mock Jira)
**Mục tiêu:** Sinh ticket JSON theo template, **chưa** đẩy lên Jira.

**Files chính:**
- `app/schemas/ticket.py` — pydantic Epic/Story/Task theo `JIRA_TICKET_SPEC.md`
- `app/prompts/generator.py` — system prompt `google/gemma-4-31b-it` (fintech reasoning, strict schema adherence, Gherkin AC)
- `app/graph/nodes/generator.py` — node sinh ticket
- Nối edge orchestrator → generator

**Test:** Đủ slot → bot in ticket draft đúng 3 phần (Context / Requirement [Product|Technical|Configuration] / Acceptance Criteria Gherkin) + JSON preview; hỏi **Project Key** và **Product Domain** (user nhập).

---

## Phase 5 — Jira thật + Guards
**Mục tiêu:** Tạo Epic/Story/Task thật; enforce security guardrails.

**Files chính:**
- `app/integrations/jira/client.py` — Jira Server REST: **chỉ read + create**
- `app/integrations/jira/guards.py` — enforce: **NO delete**; UPDATE → cờ confirmation
- `app/core/errors.py` — custom exceptions
- `app/core/retry.py` — exponential backoff
- Thay mock bằng client thật (giữ flag bật/tắt mock)

**Test:**
- Tạo Epic → tạo Story link vào Epic đó.
- Thử lệnh xóa → **bị chặn**.
- UPDATE → bot **hỏi xác nhận** trước khi thực thi.
- Verify Epic tồn tại trước khi link.
- Clarification loops: Epic Link, Assignee, Sprint (Active vs Next), Story Points.

---

## Phase 6 — Confluence + Reranker
**Mục tiêu:** Enrich context khi user cung cấp Confluence link.

**Files chính:**
- `app/integrations/confluence/reader.py` — Confluence REST **READ-ONLY**
- `app/graph/nodes/reranker.py` — `qwen3-reranker-8b`: rerank/enrich nội dung PRD/System Design
- `app/graph/routing.py` — conditional edge: có Confluence link? → reranker, else → generator

**Test:** Chat kèm Confluence link → ticket có thêm technical context; link hỏng/không truy cập được → bỏ qua bước rerank, báo user, vẫn chạy tiếp với context chat.

---

## Phase 7 — Compliance & Hardening
**Mục tiêu:** PII guardrail, logging, error handling, tests.

**Files chính:**
- `app/core/pii.py` — detect/mask plain card number, PIN, password
- `app/core/logging.py` — cấu hình logger theo `LOG_LEVEL`
- `tests/` — `test_routing.py`, `test_jira_guards.py`, `test_pii.py`
- Polish error handling toàn luồng

**Test:**
- `pytest` pass.
- Nhập số thẻ giả → **bị mask** trong ticket.
- Tắt Jira → bot báo timeout thân thiện, **không tạo ticket dở dang**.
