# Hướng dẫn Deploy lên Kubernetes

## Tổng quan

Project đã có sẵn `Dockerfile` production-ready. Để deploy lên k8s cần:
1. Tắt 2 tính năng AgentBase-specific (Memory + Secrets)
2. Build & push image lên container registry
3. Apply 5 file manifest k8s (Secret, ConfigMap, Deployment, Service, Ingress)

---

## Bước 0 — Tắt AgentBase dependencies

Project có 2 tính năng phụ thuộc AgentBase. Trên k8s chỉ cần bỏ trống 2 biến này trong cấu hình:

| Biến | Tác dụng trên AgentBase | Khi bỏ trống trên k8s |
|------|------------------------|----------------------|
| `MEMORY_ID` | LangGraph dùng `AgentBaseMemoryEvents` checkpointer để persist conversation state | Tự động fallback về **in-memory store** (state mất khi pod restart — chấp nhận được cho MVP) |
| `AGENT_IDENTITY_NAME` | App tự lấy API key từ AgentBase Secrets tại runtime | Tự động fallback về đọc trực tiếp từ env var (`.env` hoặc k8s Secret) |

Không cần sửa code — cả hai đã có fallback sẵn trong `app/core/secrets.py` và `app/graph/builder.py`.

> **Nếu cần state persistent qua pod restart/scale:** Deploy thêm Redis và dùng LangGraph Redis checkpointer thay `AgentBaseMemoryEvents`. Đây là bước optional cho Phase sau.

---

## Bước 1 — Build & push image

```bash
# Thay your-registry bằng registry thực tế (Docker Hub, Harbor, ECR, GCR...)
IMAGE=your-registry/jira-agent:v1.0.0

docker build -t $IMAGE .
docker push $IMAGE
```

---

## Bước 2 — Tạo k8s manifests

Tạo thư mục `k8s/` và các file sau:

### `k8s/secret.yaml` — Sensitive credentials

> **Quan trọng:** Không commit file này lên git. Dùng `.gitignore` hoặc Sealed Secrets / Vault nếu cần GitOps.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: jira-agent-secret
  namespace: your-namespace
type: Opaque
stringData:
  LLM_API_KEY: "your-llm-api-key"
  JIRA_API_TOKEN: "your-jira-pat-or-api-token"
  CONFLUENCE_API_TOKEN: "your-confluence-token"
  TEAMS_BOT_APP_ID: "your-azure-bot-app-id"
  TEAMS_BOT_APP_PASSWORD: "your-azure-bot-app-password"
```

### `k8s/configmap.yaml` — Non-sensitive config

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: jira-agent-config
  namespace: your-namespace
data:
  LLM_BASE_URL: "https://your-llm-provider/v1"
  ORCHESTRATOR_MODEL: "minimax/minimax-m2.5"
  RERANKER_MODEL: "qwen/qwen3-reranker-8b"
  GENERATOR_MODEL: "google/gemma-4-31b-it"
  FALLBACK_MODEL: "gpt-oss-20b"

  JIRA_SERVER_URL: "http://jira.internal:8080"
  JIRA_USER_EMAIL: "bot@yourcompany.com"
  JIRA_EPIC_LINK_FIELD: "customfield_10101"
  JIRA_EPIC_NAME_FIELD: "customfield_10103"
  JIRA_STORY_POINTS_FIELD: "customfield_10801"
  JIRA_SPRINT_FIELD: "customfield_10007"

  CONFLUENCE_SERVER_URL: "http://confluence.internal:8090"
  CONFLUENCE_USER_EMAIL: "bot@yourcompany.com"

  APP_HOST: "0.0.0.0"
  APP_PORT: "8080"
  LOG_LEVEL: "INFO"
  DEBUG: "false"

  # Bật endpoint Teams bot — bắt buộc để nhận message từ Azure Bot Service
  ENABLE_MESSAGES_ENDPOINT: "true"
  ENABLE_DIAGNOSTICS: "false"
  ENABLE_DOCS: "false"

  RATE_LIMIT_ENABLED: "true"
  RATE_LIMIT_MAX_REQUESTS: "120"
  RATE_LIMIT_WINDOW_SECONDS: "60"

  # Bỏ trống → dùng in-memory store thay AgentBase Memory
  MEMORY_ID: ""
  # Bỏ trống → đọc secrets từ env var thay AgentBase Identity
  AGENT_IDENTITY_NAME: ""
```

### `k8s/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: jira-agent
  namespace: your-namespace
  labels:
    app: jira-agent
spec:
  replicas: 2
  selector:
    matchLabels:
      app: jira-agent
  template:
    metadata:
      labels:
        app: jira-agent
    spec:
      containers:
      - name: jira-agent
        image: your-registry/jira-agent:v1.0.0
        ports:
        - containerPort: 8080
        envFrom:
        - configMapRef:
            name: jira-agent-config
        - secretRef:
            name: jira-agent-secret
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 30
          timeoutSeconds: 5
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
          timeoutSeconds: 3
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "1Gi"
            cpu: "500m"
      # Graceful shutdown — cho phép request đang xử lý hoàn thành
      terminationGracePeriodSeconds: 30
```

### `k8s/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: jira-agent-svc
  namespace: your-namespace
spec:
  selector:
    app: jira-agent
  ports:
  - port: 80
    targetPort: 8080
    protocol: TCP
  type: ClusterIP
```

### `k8s/ingress.yaml` — Bắt buộc cho MS Teams

MS Teams Bot Service yêu cầu endpoint **HTTPS public** để gửi message vào bot.

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: jira-agent-ingress
  namespace: your-namespace
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "120"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "120"
    # Nếu dùng cert-manager để tự cấp TLS:
    # cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  rules:
  - host: jira-agent.your-domain.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: jira-agent-svc
            port:
              number: 80
  tls:
  - hosts:
    - jira-agent.your-domain.com
    secretName: jira-agent-tls
```

---

## Bước 3 — Apply lên cluster

```bash
NAMESPACE=your-namespace

# Tạo namespace nếu chưa có
kubectl create namespace $NAMESPACE

# Apply theo thứ tự (Secret và ConfigMap trước Deployment)
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml

# Kiểm tra
kubectl rollout status deployment/jira-agent -n $NAMESPACE
kubectl get pods -n $NAMESPACE
kubectl logs -l app=jira-agent -n $NAMESPACE --tail=50
```

---

## Bước 4 — Cập nhật Azure Bot Service

Sau khi Ingress có IP/DNS, vào **Azure Portal → Bot Services → Settings → Messaging endpoint**:

```
https://jira-agent.your-domain.com/api/messages
```

Test bằng Bot Framework Emulator trước khi kết nối Teams thật:
```
http://<ingress-ip>/api/messages   (hoặc dùng domain)
```

---

## Deploy update (các lần sau)

```bash
# Build version mới
docker build -t your-registry/jira-agent:v1.0.1 .
docker push your-registry/jira-agent:v1.0.1

# Rolling update — zero downtime
kubectl set image deployment/jira-agent \
  jira-agent=your-registry/jira-agent:v1.0.1 \
  -n your-namespace

# Theo dõi
kubectl rollout status deployment/jira-agent -n your-namespace

# Rollback nếu cần
kubectl rollout undo deployment/jira-agent -n your-namespace
```

---

## Checklist trước khi go-live

- [ ] `MEMORY_ID` và `AGENT_IDENTITY_NAME` để trống trong ConfigMap
- [ ] `ENABLE_MESSAGES_ENDPOINT=true` trong ConfigMap
- [ ] `ENABLE_DIAGNOSTICS=false` và `ENABLE_DOCS=false` (tắt info disclosure)
- [ ] `EMULATOR_MODE=false` (tuyệt đối không bật trên production)
- [ ] TLS certificate hợp lệ trên Ingress (MS Teams yêu cầu HTTPS)
- [ ] Secret file không commit lên git
- [ ] `TEAMS_BOT_APP_ID` + `TEAMS_BOT_APP_PASSWORD` đã điền đúng (lấy từ Azure Bot Service)
- [ ] Messaging endpoint đã cập nhật trên Azure Portal
- [ ] `kubectl logs` không có ERROR liên quan đến Jira/LLM auth

---

## Nâng cấp: State persistence qua Redis (optional)

Nếu cần conversation state tồn tại qua pod restart hoặc scale nhiều replica:

1. Deploy Redis (hoặc dùng Redis managed service):
```bash
helm install redis bitnami/redis --namespace your-namespace \
  --set auth.enabled=true \
  --set auth.password=your-redis-password
```

2. Thêm vào requirements.txt:
```
langgraph-checkpoint-redis
```

3. Sửa `app/graph/builder.py`:
```python
# Thay AgentBaseMemoryEvents bằng Redis checkpointer
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
checkpointer = AsyncRedisSaver.from_conn_string(settings.redis_url)
```

4. Thêm `REDIS_URL=redis://:password@redis-master:6379/0` vào ConfigMap/Secret.
