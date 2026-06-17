# SECURITY — Production Hardening Guide

Hướng dẫn bảo mật khi deploy `clawathon` lên production. Song ngữ — thuật ngữ kỹ thuật giữ tiếng Anh.

---

## 1. PAT Least-Privilege (Jira & Confluence)

Agent chỉ cần một tập quyền **tối thiểu**. Tạo một **service account riêng** (không dùng tài khoản cá nhân) và cấp đúng các quyền dưới đây. Đây là lớp phòng thủ quan trọng nhất: dù code có lỗi, PAT không có quyền destructive thì không thể xóa dữ liệu.

### 1.1 Jira PAT — quyền CẦN CÓ

| Permission (Jira) | Vì sao cần | Thao tác của agent |
|-------------------|-----------|--------------------|
| **Browse Projects** | Đọc issue, project, board, sprint | fetch ticket, verify Epic, thống kê (JQL search) |
| **Create Issues** | Tạo Epic/Story/Task | luồng tạo ticket |
| **Edit Issues** | Cập nhật field (story points, assignee, priority, summary) | luồng update (có confirmation) |
| **Schedule Issues** / **Manage Sprints** | Gán issue vào sprint qua Agile API (`POST /rest/agile/1.0/sprint/{id}/issue`) | gán/đổi sprint |
| **Assignable User** (cho user được assign) | Để assignee hợp lệ | gán assignee |

### 1.2 Jira PAT — quyền TUYỆT ĐỐI KHÔNG cấp

| Permission | Lý do |
|-----------|-------|
| **Delete Issues** | Agent **không bao giờ** xóa. Không cấp = defense-in-depth ngoài guard code. |
| **Administer Projects / Jira** | Agent không cần admin. |
| **Manage Watchers / Delete Comments / Delete Attachments** | Ngoài phạm vi. |
| **Bulk Change** | Tránh thao tác hàng loạt ngoài ý muốn. |

> Scope PAT (nếu Jira DC hỗ trợ token scopes) hoặc project-role: chỉ gán service account vào **đúng project** mà agent phục vụ (vd PCFBANK), không cấp global.

### 1.3 Confluence PAT — STRICTLY READ-ONLY

| Permission | Cấp? |
|-----------|------|
| **View / Read space** (chỉ các space cần đọc PRD/System Design) | ✅ |
| Add/Edit/Delete page, Add attachment, Space admin | ❌ KHÔNG |

Agent chỉ gọi `GET /rest/api/content/...` — không có code path nào write Confluence (xem `app/integrations/confluence/`).

### 1.4 Defense-in-depth trong code (đã có sẵn)

Ngay cả khi PAT lỡ có quyền rộng hơn, code vẫn chặn:

- `app/integrations/jira/guards.py` — `assert_no_delete()` chặn mọi op destructive; `require_update_confirmation()` bắt UPDATE phải qua xác nhận.
- `app/graph/nodes/orchestrator.py` — phát hiện & từ chối yêu cầu xóa Jira/Confluence ngay từ đầu (không gọi API).
- `app/integrations/confluence/guards.py` — `assert_read_only()`; reader không expose hàm write nào.
- `JiraClient` không có method `delete`/`destroy` nào.

### 1.5 Xoay vòng & lưu trữ token

- **Rotation**: đặt lịch xoay PAT định kỳ (vd 90 ngày).
- **Storage**: inject qua secrets manager (Vault / AWS Secrets Manager / K8s Secrets), **không** để trong `.env` trên host prod, **không** bake vào image (Dockerfile đã bỏ `COPY .env`).
- **Revoke**: nếu nghi lộ, revoke token trên Jira/Confluence ngay.

---

## 2. Rate Limiting

Bot message handler giới hạn **120 request / 60s** (token bucket, cấu hình qua env).

| Tình huống | Hành vi |
|-----------|---------|
| Dưới giới hạn | Xử lý ngay |
| Vượt giới hạn | Đưa vào **hàng đợi**, thông báo user *"đã được đưa vào hàng đợi, dự kiến chờ ~Ns"*, rồi xử lý khi tới lượt |
| Hàng đợi quá dài (> `RATE_LIMIT_MAX_QUEUE_WAIT_SECONDS`) | Từ chối lịch sự: *"Hệ thống đang quá tải, thử lại sau"* |

Cấu hình (env):

```
RATE_LIMIT_ENABLED=true
RATE_LIMIT_MAX_REQUESTS=120
RATE_LIMIT_WINDOW_SECONDS=60
RATE_LIMIT_MAX_QUEUE_WAIT_SECONDS=300
```

Code: `app/core/ratelimit.py` (token bucket, refill `max_requests/window` tokens/s), áp dụng trong `app/bot/handler.py`.

> Lưu ý scaling: limiter là **per-process**. Nếu chạy nhiều replica, mỗi replica có quota riêng (tổng = `replicas × 120`). Muốn giới hạn toàn cục, đặt rate limit ở tầng API gateway/ingress hoặc dùng backend chia sẻ (Redis).

---

## 3. Checklist deploy production

Env cần set (secure-by-default — không set = an toàn):

```
# Surface — production để false hết
ENABLE_MESSAGES_ENDPOINT=false   # Azure Bot Service host bot, không expose endpoint local
ENABLE_DIAGNOSTICS=false         # tắt /api/jira/check, /api/confluence/check (SSRF + info disclosure)
ENABLE_DOCS=false                # tắt Swagger/OpenAPI
DEBUG=false

# Jira/Confluence — qua secrets manager
JIRA_API_TOKEN=...               # service account, least-privilege (mục 1)
CONFLUENCE_API_TOKEN=...         # read-only
USE_MOCK_JIRA=false

# Field IDs đúng của instance (field_sync KHÔNG còn ghi file lúc runtime)
JIRA_EPIC_LINK_FIELD=...
JIRA_EPIC_NAME_FIELD=...
JIRA_STORY_POINTS_FIELD=...
JIRA_SPRINT_FIELD=...
```

Hạ tầng:

- [ ] **TLS**: đặt sau reverse proxy HTTPS (Bot Framework yêu cầu HTTPS).
- [ ] **Non-root container**: Dockerfile chạy user `appuser` (uid 10001) — đã có.
- [ ] **Read-only filesystem**: an toàn vì `field_sync` không còn ghi source; chỉ cần `logs/` writable (mount volume).
- [ ] **Secrets manager**: không dùng `.env` trên host prod.
- [ ] **Network**: chỉ cho Azure Bot Service / API gateway gọi tới; không expose public trực tiếp.
- [ ] **Log**: bảo vệ volume `logs/`; cân nhắc mask PII trước khi log nếu bật DEBUG file logging.
