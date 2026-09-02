# Daiki AI Passport Roadmap

## Implementation Checkpoint — 2026-09-02

Implemented and locally validated in the current workspace:
- Keycloak OIDC remains the authentication boundary while Daiki Backend owns application access state: `pending`, `approved`, `suspended`, `rejected`.
- New authenticated identities are persisted as `pending`. Pending users receive restricted interactive text Chat only; full protected AI access still requires `approved` status. `ai-admin` can approve, reject, suspend and reactivate application users, with durable audit records.
- PostgreSQL schema now contains application users, entitlement policies, temporary-grant primitives, usage ledger, access audit log and Daiki-owned API-key metadata.
- Token entitlement enforcement runs before LiteLLM. PostgreSQL is the durable usage source of truth while Redis is limited to atomic in-flight token reservations so completed usage can be reconstructed without double counts.
- Daiki API keys use `dk_` credentials, store only a bounded prefix plus SHA-256 hash, run through the same whitelist/quota path as web users, require explicit scopes, and are forbidden from calling Admin APIs even when owned by an admin.
- Frontend includes account approval state, user whitelist controls, user/API-key quota controls, token usage display and API-key revoke controls.
- Go minimum/runtime builder is pinned to Go 1.25.13 after vulnerability validation; frontend lint/build and offline Kustomize/schema validation pass.

Still pending and must not be inferred complete from the checkpoint above:
- Initial shared inference queue and model alias router are implemented: Redis queue/lease state, priority ordering, stale lease recovery, global/per-principal/workload concurrency, bounded wait, cancellation, and aliases `auto` / `fast` / `balanced` / `deep`. Further tuning/metrics and additional workload execution endpoints remain pending.
- Exact streaming token reconciliation; the current stream path uses a bounded reservation estimate when the provider does not expose normalized usage.
- Explicit Keycloak identity-provider attribution for email vs Google and the finished registration/provider UX.
- Files/RAG, Project Mode persistence, Skills, MCP, image/vision providers and Local LLM integration.
- Deployment/live/E2E evidence on match-infra. The explicit read-only `match-infra` context check timed out on this checkpoint, so live state remains unverified; no VPN stack was installed or modified.

## Phase 1 — Core Platform
- Frontend: Next.js on Vercel
- Backend: Go + Chi on match-infra
- APISIX, Keycloak, LiteLLM, PostgreSQL, Redis
- OIDC/RBAC, API keys, usage, health and observability
- Separate Admin and User experiences
- Self-service registration/login with email/password and OAuth/OIDC providers such as Google
- New registrations are not automatically allowed to use protected AI workloads
- Admin-controlled whitelist/approval is required before a user becomes active
- Explicit account lifecycle: `pending`, `approved`, `suspended`, `rejected`
- Role separation: at minimum `ai-user` and `ai-admin`
- Admin can approve/whitelist, reject, suspend, reactivate and review user access
- Authentication identity is handled by Keycloak; application approval/whitelist state is enforced by Daiki Backend
- Local LLM remains optional until the GPU host is ready

### Identity, Registration & Whitelist Flow
1. User registers with email/password or signs in through an allowed OAuth/OIDC provider such as Google.
2. Keycloak verifies/authenticates the identity.
3. Daiki Backend creates or updates the application user profile with status `pending` when the identity is not yet approved.
4. Pending users can access onboarding/account-status/logout plus a restricted interactive text Chat trial. The trial is forced to the `fast` route and has small token/output quotas plus aggressive Redis rate limits. Models/usage APIs, API keys, files, skills, projects, MCP and other protected features remain locked.
5. Admin reviews the user and explicitly approves/whitelists access.
6. Approved users receive normal `ai-user` access according to roles/scopes.
7. Suspended/rejected users are denied protected workloads even when their Keycloak login remains technically valid.

### Pending Restricted Chat
- Authentication is still required; anonymous chat is not allowed.
- Pending access works only through interactive OIDC login. Daiki API keys do not receive pending-mode access.
- Chat is text-only and forced to the `fast` alias regardless of the client request.
- Default trial allowance is configurable from runtime config; initial values are 8,000 tokens/day, 10 messages/hour, at least 30 seconds between messages, and maximum 512 completion tokens per response.
- PostgreSQL durable usage plus Redis in-flight reservations still enforce token quota.
- Redis enforces the cooldown/hourly request rate and fails closed if the rate limiter is unavailable.
- Pending users cannot access Models, Usage API, API keys, Admin, Files, Projects, Skills, MCP, Knowledge, image/vision generation or other protected workloads.
- `suspended` and `rejected` users do not receive restricted Chat access.
- Admin approval upgrades the same identity to normal entitlement/router/queue policy without changing the account or conversation ownership.

### Admin User Management
- User list with search/filter by status, role, provider and last activity
- Pending approval queue
- Approve / reject / suspend / reactivate actions
- Assign/remove application roles and scopes
- See authentication provider: email, Google or other configured OAuth/OIDC provider
- See created date, approved-by, approved-at and last-login metadata
- Audit every access-control change
- Optional email notification on approval/rejection in a later notification phase

### User Experience
- Register with email/password
- Login with email/password
- Sign in with Google
- Extensible OAuth/OIDC provider buttons for future providers
- Pending-approval experience after successful registration/login, with restricted Chat available while waiting for whitelist approval
- Account/profile page showing role and current access status
- Clear suspended/rejected state without exposing internal admin details

### Token Quota & Entitlement System
Goal: let Admin control all usage now while keeping the policy/data model ready for future subscription plans without redesigning the inference API.

#### Scope
- Quotas can apply to a user, API key, role, workspace/project, or future subscription plan
- Admin can override any inherited quota for an individual user or API key
- Support `unlimited` as an explicit policy, not a magic large number
- Track input, output and total tokens separately, while enforcing primarily on total usage tokens
- Usage must be attributed to the authenticated user/API key and selected model tier

#### Supported Limit Windows
- Per hour
- Per day
- Per week
- Per month
- Rolling windows where needed
- Custom period/window for future policies
- Lifetime/manual allocation for special accounts or credits

Example policies:
- `unlimited`
- `100000 tokens / hour`
- `1000000 tokens / week`
- `5000000 tokens / month`
- custom limits per user/API key

#### Enforcement
- Check quota before entering inference queue
- Reserve an estimated token budget before execution where practical to reduce race conditions
- Reconcile actual input/output token usage when generation completes or is cancelled
- Deny or pause new inference requests when a hard quota is exhausted
- Return structured quota metadata and reset time to Web/API clients
- Queueing must not consume model tokens; only actual inference/tool/model usage is charged
- Admin operations and authentication flows do not consume AI token quota
- Define explicit policy for tool/MCP calls that internally invoke models so usage is never double-counted

#### Admin Controls
- Set user/API-key quota mode: unlimited or limited
- Choose interval: hour/day/week/month/rolling/custom
- Set token amount
- Set optional model-specific or workload-specific limits
- Reset usage manually
- Grant temporary extra tokens/credits
- Set effective-from / expires-at for temporary policies
- View current usage, remaining quota, reset time and historical usage
- Audit every quota/entitlement change including admin actor, old value and new value

#### User/API Experience
- Show usage and remaining quota in profile/usage page
- Show next reset time
- Warn before reaching configured thresholds such as 80%, 90% and 100%
- OpenAI-compatible API responses should expose machine-readable quota/rate-limit information where possible
- Exhausted users keep account access but protected inference requests are blocked until reset/override

#### Storage & Performance
- PostgreSQL is source of truth for quota policies, assignments, durable usage ledger and audit history
- Redis stores hot counters/reservations for low-latency enforcement
- Redis counters must be reconstructable/reconcilable from durable usage records
- Use atomic Redis operations/Lua or equivalent to prevent concurrent requests from bypassing limits

#### Future Subscription Readiness
- Introduce a generic `plan` / `entitlement` abstraction even before billing exists
- A future subscription plan can define token quota, allowed models, concurrency, priority, file/storage limits, image-generation limits, skill/MCP permissions and API-key limits
- User-specific Admin overrides take precedence according to an explicit policy hierarchy
- Billing/payment provider integration is out of scope for the initial version; Admin remains the authority for assigning plans and limits

## Phase 2 — Queue & Model Router
- Shared inference queue for Web users and API keys
- Global and per-user/per-key concurrency limits
- Priority, fairness, lease timeout, cancellation and disconnect propagation
- Separate workload queues: fast, deep, vision, image, embedding, batch
- Model aliases: auto, fast, balanced, deep
- Capability-aware routing first, complexity routing second
- Easy Q&A/rewrite/summary can route to a fast model such as Qwen 35B-A3B
- Heavy coding/reasoning/agent workloads route to the deep tier

## Phase 3 — Context & Memory Architecture
Goal: keep inference context small while preserving long-running conversation continuity.

### Redis Hot Session Memory
- Recent messages
- Conversation summary
- Active task/session state
- Important session facts
- Model/route preference
- Queue state and rate-limit counters
- Redis is hot/cache memory, not source of truth

Suggested keys:
- `session:{id}:recent`
- `session:{id}:summary`
- `session:{id}:facts`
- `session:{id}:state`

### PostgreSQL Durable Memory
- Complete conversations/messages
- Durable summaries/checkpoints
- Decisions and constraints
- Open tasks
- Usage and audit metadata
- Redis state must be rebuildable from durable storage

### Semantic Memory — PostgreSQL + pgvector
- Embed selected durable memories
- Retrieve only relevant memories for the current request
- Project/workspace scoped memory
- Permission and retention boundaries

### Context Builder
Do not send the full conversation every request. Compose from:
1. System/policy prompt
2. Pinned decisions/constraints
3. Active task/project state
4. Relevant long-term memories
5. Conversation summary
6. Recent verbatim messages
7. Relevant file/RAG chunks
8. Current request

Initial target budgets, subject to load testing:
- Fast tier: ~8K–16K tokens
- Deep tier: ~16K–32K tokens
- Recent messages: ~6K–10K
- Summary: ~1K–2K
- Retrieved memory/RAG: ~1K–4K
- Output budget reserved separately

### Automatic Compaction
- Trigger when recent context crosses budget
- Summarize older messages but never delete durable full history
- Preserve decisions, constraints, facts, unresolved tasks and references as structured memory
- Keep recent messages verbatim
- Avoid repeated summary-of-summary degradation
- Historical retrieval is triggered for requests such as “ก่อนหน้านี้”, “ที่ตกลงไว้”, or “จำได้ไหม”

Redis memory does not replace inference-runtime KV cache; it reduces prompt size and therefore reduces prefill/KV pressure.

## Phase 4 — Project Mode for Large Work
- Project overview and explicit goal
- Task ledger with TODO/DOING/BLOCKED/DONE
- Repository map/index
- Architecture and requirements
- Decisions and constraints registry
- Durable checkpoints
- Test/validation evidence
- Open issues and blockers
- Activity history
- Full conversation/history remains durable

### Project Execution Flow
Current task -> project state -> repository index -> retrieve relevant files/code -> implement -> test -> review -> checkpoint.

### Required Gates
- Planner can split large work into task graph
- Workers execute scoped tasks
- Reviewer checks diff, tests and acceptance criteria
- A UI existing alone is never considered feature completion

## Phase 5 — Files & Multimodal Input
- File upload and library
- PDF, DOCX, XLSX, CSV, TXT/Markdown, JSON
- PNG, JPG, WEBP images
- Presigned upload to MinIO/S3
- File metadata/permissions in PostgreSQL
- Preview and attach files/images to chat
- Extraction/indexing pipeline

## Phase 6 — Knowledge / RAG
- Collections and sources
- Chunking and embeddings
- pgvector retrieval
- Index/reindex status
- Workspace/user access controls

## Phase 7 — Vision & Image Generation
- Vision route when image inputs are present
- OpenAI-compatible image generation/edit API contracts
- Provider abstraction for ComfyUI, FLUX, Stable Diffusion or future providers
- Separate image queue/capacity policy

## Phase 8 — Skills Registry
- Installed / Marketplace / Updates / Create Skill
- System, workspace and user scopes
- Enable/disable and version management
- Declarative permissions for network, files, tools and secrets
- Built-in, HTTP, MCP, workflow and future sandboxed-container skill types
- Verified / Community / Private / Untrusted trust levels
- No arbitrary host execution for untrusted skills
- Secrets stored outside manifests and referenced securely

## Phase 9 — MCP Connectivity
MCP is a first-class capability used by Skills and Project Mode.

### Connectivity
- Internal MCP servers
- External MCP servers
- Streamable HTTP where supported
- stdio only through explicitly managed/sandboxed runtime
- Health checks, reconnect and exponential backoff
- Tool discovery and capability refresh

### Scope
- System scope
- Workspace/project scope
- User scope
- Enable/disable per server and per tool

### Security
- Permission review before connection/install
- Network allowlist
- File read/write scope
- Tool-level permissions
- Secret references instead of plaintext config
- API keys can be scoped to specific MCP tools
- Untrusted MCP gets no arbitrary host execution by default

### Audit
Record:
- requesting user/API key
- project/workspace
- MCP server
- tool name
- start/end time
- result status
- bounded/redacted arguments and result metadata

### Tool Calling Flow
User/API -> Daiki Backend -> Project Context -> Model Router -> Tool Planner -> MCP Manager -> MCP Tool -> Tool Result -> Model -> Answer/continue task.

Example MCP types:
- GitHub
- Files
- Database
- Browser/Search
- Internal SWS tools
- Custom private MCP servers

## Phase 10 — Admin & Operations
- Users and roles
- Pending approval/whitelist queue
- Approve/reject/suspend/reactivate users
- Authentication provider visibility and account status
- Token quota/entitlement management with unlimited and time-window limits
- Per-user/per-API-key overrides, resets, temporary grants and usage history
- Future plan/subscription assignment without requiring billing in the initial version
- API keys/scopes
- Models and Model Router
- Queue status and active leases
- Files/storage
- Skills
- MCP servers/tools/health
- Knowledge/index status
- Image providers
- Usage
- Audit logs
- System health and observability

## Phase 11 — Local LLM Integration
- Verify the existing external VPN route
- Connect OpenAI-compatible local runtime
- Start with runtime appropriate for available hardware
- Swap to vLLM when supported hardware is available
- Frontend/backend API contracts remain stable across runtime/model changes

## Acceptance Principle
All control-plane features should remain healthy when Local LLM is offline. Before GPU integration, the only expected waiting dependency is the Local LLM upstream itself.
