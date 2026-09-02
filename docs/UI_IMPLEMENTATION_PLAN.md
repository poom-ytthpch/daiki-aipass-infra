# Daiki AI Passport — UI Implementation Plan / Agent Prompt

## Goal
Refactor the complete Daiki frontend into a warm child-friendly AI workspace based on `docs/UI_DESIGN_SPEC.md`, while preserving real authentication, quota, queue, usage and admin contracts.

## Non-negotiable behavior
1. `/` redirects authenticated normal users to `/chat` and admins to `/admin`.
2. Normal user navigation is Chat / Plan / Info / Settings / Account.
3. Admin navigation additionally exposes Dashboard / Users / Usage / System.
4. Pending users remain restricted to Fast text Chat limits; Plan/Info/Settings/Account remain viewable.
5. Admin authorization is server-enforced. UI visibility alone is never security.
6. The configured admin email is auto-approved by backend and may access admin APIs even before a refreshed Keycloak role token; Keycloak role assignment remains synchronized for consistency.
7. Admin dashboard/usage must use durable backend data, not fabricated counters.
8. Keep Google/email login/register working on public production domain.

## Backend work
- Add `ADMIN_EMAIL` config.
- Auto-approve configured admin identity on login.
- Permit configured admin identity in `adminOnly` in addition to `ai-admin` role.
- Add aggregate admin stats to `/v1/admin/summary`.
- Add `/v1/admin/usage` backed by `usage_ledger` aggregation per user.
- Keep all existing whitelist/quota/audit behavior.

## Frontend work
- Rewrite `AppShell` navigation and warm visual system.
- Add active navigation component using `usePathname` only at the client leaf.
- `/` role redirect server component.
- Refactor Login, Chat, Account and Settings.
- Add Plan and Info.
- Add admin Dashboard / Users / Usage / System pages and API proxy for admin usage.
- Keep table semantics for users/usage.
- Use lucide-react for consistent production icons.

## Infra work
- `admin-email` in `daiki-ai-passport-secrets`.
- Inject `ADMIN_EMAIL` into backend/frontend/bootstrap Job.
- Bootstrap assigns `ai-user` + `ai-admin` to matching Keycloak user when present.
- Pin immutable backend/frontend SHA image tags after CI publish.

## Validation
Frontend: lint, production build, linux/amd64 Docker build.
Backend: gofmt, go test ./..., go vet ./..., build, focused security/static checks available in repo.
Infra: Kustomize render, kubeconform, shellcheck for changed scripts, gitleaks source scans.
Live: rollout readiness, public login page, `/` role redirect, backend health, Keycloak/Google path, admin APIs with configured admin, mobile + desktop visual acceptance.

## Visual QA checklist
- Warm-white rather than dark canvas.
- Coral primary actions.
- Black editorial headings.
- Pastel peach/blush/sky/mint sections reused consistently.
- No gradients/glows from the old UI.
- Sidebar remains compact and readable.
- Chat is dominant for normal users.
- Admin tables are dense enough for operations without looking like a developer console.
- 375px viewport has no page overflow.
