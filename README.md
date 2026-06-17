# Multi-Model Jira Assistant Bot

Agent AI tự động hóa tạo Jira Ticket cho đội Fintech (E-Wallet / Payment Gateway) qua MS Teams.

---

## Vấn đề & Giải pháp

Tạo Jira Ticket tốn thời gian vì yêu cầu thường mơ hồ, thiếu context, không đúng template — dẫn đến viết lại nhiều lần, dev/QC hỏi lại liên tục.

Agent tích hợp trực tiếp vào **MS Teams**, hỏi làm rõ yêu cầu, tự đọc PRD từ Confluence, rồi dùng AI chuyên Fintech sinh ticket JSON đúng chuẩn — Context, Requirement, Acceptance Criteria checklist. Thống kê dự án chỉ dành cho **Project Lead & Admin**.

**Kết quả**: tạo ticket từ 15–20 phút → dưới 3 phút, đúng chuẩn ngay lần đầu.

---

## Kiến trúc đa mô hình

| Model | Vai trò |
|-------|---------|
| `minimax/minimax-m2.5` | Orchestrator — hội thoại, slot-filling |
| `qwen/qwen3-reranker-8b` | Reranker — đọc & chọn lọc nội dung Confluence |
| `google/gemma-4-31b-it` | Generator — suy luận Fintech, sinh ticket JSON |
| `gpt-oss-20b` | Fallback khi orchestrator lỗi |

---

## ⚠️ Lưu ý — AgentBase đang chạy Mock Mode

Bot deploy trên **GreenNode AgentBase** hiện trả về **dữ liệu mock** cho Jira & Confluence (`USE_MOCK_JIRA=true`, `USE_MOCK_CONFLUENCE=true`).

**Lý do:** `jira.zalopay.vn` và `confluence.zalopay.vn` nằm trên **mạng nội bộ ZaloPay** — AgentBase (cloud) không thể kết nối trực tiếp.

**Muốn test với Jira thật** → chạy local + kết nối **VPN ZaloPay**, đặt `USE_MOCK_JIRA=false` trong `.env`.

---

## Chạy Local (Bot Framework Emulator)

### 1. Cài đặt

```bash
git clone <repo-url> && cd clawathon
pip install -r requirements.txt
cp .env.example .env
```

### 2. Cấu hình `.env` tối thiểu

```env
# LLM
LLM_BASE_URL=https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1
LLM_API_KEY=your-api-key
ORCHESTRATOR_MODEL=minimax/minimax-m2.5
GENERATOR_MODEL=google/gemma-4-31b-it
RERANKER_MODEL=qwen/qwen3-reranker-8b
FALLBACK_MODEL=gpt-oss-20b

# Jira & Confluence (cần VPN ZaloPay nếu dùng thật)
JIRA_SERVER_URL=https://jira.zalopay.vn
JIRA_API_TOKEN=your-jira-pat
CONFLUENCE_SERVER_URL=https://confluence.zalopay.vn
CONFLUENCE_API_TOKEN=your-confluence-pat
USE_MOCK_JIRA=true           # đổi false nếu đang bật VPN
USE_MOCK_CONFLUENCE=true     # đổi false nếu đang bật VPN

# Emulator local — để trống App ID/Password
TEAMS_BOT_APP_ID=
TEAMS_BOT_APP_PASSWORD=
EMULATOR_MODE=true
APP_PORT=8080

# Bật endpoint bot
ENABLE_MESSAGES_ENDPOINT=true
ENABLE_DIAGNOSTICS=true
ENABLE_DOCS=true

# Giả lập identity khi test
TEAMS_TEST_USER_EMAIL=yourname@zalopay.vn
```

### 3. Khởi động

```bash
python main.py
```

### 4. Kết nối Bot Framework Emulator

1. Mở **Bot Framework Emulator** → **Open Bot**
2. Bot URL: `http://localhost:8080/api/messages`
3. Để trống App ID & Password → **Connect**

### 5. Test thử

```
Tôi cần tạo story cho tính năng xác thực sinh trắc học khi thanh toán
```

Bot sẽ hỏi làm rõ (project key, Epic, assignee, sprint, story points) rồi sinh ticket JSON.

**Có Confluence link:**
```
Tạo ticket onboarding merchant. PRD: https://confluence.zalopay.vn/pages/viewpage.action?pageId=12345
```

### Chạy bằng Docker

```bash
docker compose up --build
```

---

## Deploy Production (MS Teams)

### Bước 1 — Đăng ký Azure Bot

1. Azure Portal → **Azure Bot** → tạo mới
2. Lấy **App ID** và tạo **Client Secret**

### Bước 2 — Cập nhật `.env` production

```env
TEAMS_BOT_APP_ID=your-azure-app-id
TEAMS_BOT_APP_PASSWORD=your-azure-app-password
TEAMS_BOT_TENANT_ID=your-tenant-id
EMULATOR_MODE=false
USE_MOCK_JIRA=false
USE_MOCK_CONFLUENCE=false
APP_PORT=8080
ENABLE_MESSAGES_ENDPOINT=true
ENABLE_DIAGNOSTICS=false
ENABLE_DOCS=false
```

### Bước 3 — Deploy lên AgentBase

```bash
# Login CR
bash .claude/skills/agentbase/scripts/cr.sh credentials docker-login

# Build & push
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/clawathon:v1.0.0 .
docker push vcr.vngcloud.vn/<repo>/clawathon:v1.0.0

# Update runtime
bash .claude/skills/agentbase/scripts/runtime.sh update <runtime-id> \
  --image "vcr.vngcloud.vn/<repo>/clawathon:v1.0.0" \
  --flavor "runtime-s2-general-2x4" \
  --env-file .env \
  --from-cr
```

Lấy endpoint URL:
```bash
bash .claude/skills/agentbase/scripts/runtime.sh endpoints list <runtime-id>
```

### Bước 4 — Kết nối Azure Bot

Azure Portal → Bot → **Configuration** → **Messaging endpoint**:
```
https://<endpoint-url>/api/messages
```

### Bước 5 — Thêm vào MS Teams

Azure Portal → Bot → **Channels** → **Microsoft Teams** → Enable → cài vào Teams.

---

## Biến môi trường quan trọng

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `USE_MOCK_JIRA` | `false` | `true` → sinh JSON mock, không gọi Jira thật |
| `USE_MOCK_CONFLUENCE` | `false` | `true` → trả PRD mẫu thay vì fetch Confluence |
| `EMULATOR_MODE` | `false` | `true` → tắt JWT auth cho Bot Framework Emulator |
| `TEAMS_TEST_USER_EMAIL` | `""` | Giả lập email user khi test local |
| `ENABLE_MESSAGES_ENDPOINT` | `false` | Bật `POST /api/messages` (cần `true` khi local) |
| `APP_PORT` | `8000` | Port server (AgentBase yêu cầu `8080`) |
| `LOG_LEVEL` | `INFO` | Mức log: `DEBUG`, `INFO`, `WARNING` |

---

## Bảo mật

- **Jira**: chỉ Read & Create. DELETE bị chặn. Mọi UPDATE phải xác nhận 2 bước.
- **Confluence**: Read-only tuyệt đối.
- **Thống kê**: chỉ Project Lead và Admin mới truy cập được.
- **PII**: nội dung ticket được quét và mask trước khi gửi cho LLM.
- **Secrets**: không bake vào Docker image, load qua `.env` hoặc AgentBase identity lúc runtime.
