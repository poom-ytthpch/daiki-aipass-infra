# Daiki AI Passport — UI Design Specification

## Concept
Daiki is presented as a warm, playful little-boy AI companion rather than a dark developer console. The visual language is adapted from the supplied kids-fashion reference: generous warm-white whitespace, strong black editorial type, coral calls-to-action, low-contrast borders, and pastel category bands. The product remains a serious AI workspace; the child-friendly character appears through tone, scale, softness and color rather than toy-like controls.

## Reference anchors
1. **Canvas:** warm white / paper-like surfaces instead of dark gradients.
2. **Typography:** black, confident headlines with compact labels and restrained muted copy.
3. **Primary action:** coral/orange rounded button, never neon or blue-first.
4. **Categories:** blush, baby-blue, mint and peach blocks used to differentiate actions/status areas.
5. **Responsive behavior:** desktop has broad whitespace and grouped navigation; mobile compresses into a clear top rail and stacked content with no horizontal overflow.

## Design tokens
- `--bg: #fbf9f6`
- `--surface: #ffffff`
- `--ink: #201d1a`
- `--muted: #7b746d`
- `--line: #ebe5df`
- `--coral: #ff7657`
- `--coral-dark: #e85f42`
- `--peach: #fde5d6`
- `--blush: #fce7ec`
- `--sky: #e4eff8`
- `--mint: #e5f3e8`
- `--butter: #f8efcf`
- radius: 14px controls, 18px cards, 24px hero surfaces
- shadows: sparse, low-opacity, mostly borders + elevation on hover

## Information architecture
### Normal user
Default post-login route: `/chat`
- Chat
- Plan
- Info
- Settings
- Account

Pending users keep restricted text Chat but may view Plan/Info/Settings/Account because these pages do not expose protected model/API capabilities.

### Administrator
Configured admin identity sees an Admin group plus normal workspace navigation:
- Dashboard (`/admin`)
- Users (`/admin/users`)
- Usage (`/admin/usage`)
- System (`/admin/system`)
- Chat
- Plan
- Info
- Settings
- Account

## Page specifications
### Login
- Brand-first warm-white card.
- Coral primary email sign-in.
- Secondary create-account action.
- Google OAuth as a full-width white button.
- No dark background, no developer jargon above the fold.

### Chat
- Friendly greeting area, not a dashboard.
- Four pastel quick-prompt tiles inspired by the reference category strips.
- Conversation remains the dominant surface.
- Model selection is compact and secondary.
- Pending policy appears as a soft butter notice, not an error state.

### Plan
- Current access / quota / reset information from real backend data.
- Pending accounts show trial limits from `pendingChatPolicy`.
- Approved accounts show durable usage/quota from `/v1/usage`.

### Info
- Plain-language explanation of Daiki, privacy, routing and account approval.
- Use open sections rather than dense technical cards.

### Settings
- Local user preferences only until durable profile settings are implemented.
- Default model and response style are persisted in versioned local storage and consumed by Chat.

### Admin Dashboard
- Pastel KPI tiles: total users, pending, approved, token usage.
- Queue state and service health below.
- Clear links into Users, Usage and System.

### Admin Users
- Table-driven management (do not convert to card grid).
- User, provider, status, last login, approve/suspend/reject, quota.
- Pending status visually emphasized without alarm styling.

### Admin Usage
- Real usage ledger aggregated by user.
- Requests, input, output and total token columns.
- Sort order is highest total token usage first from backend.

### Admin System
- Live service health and queue snapshot.
- Operational labels remain technical but presentation follows the warm design system.

## Responsive contract
- Desktop: fixed 236px left sidebar, max content width 1440px.
- Tablet/mobile: sidebar becomes compact top section, nav is horizontally scrollable, profile moves inline.
- Chat side panel stacks below conversation under 980px.
- Tables scroll horizontally inside their own surface only.
- No page-level horizontal overflow at 375px.

## Accessibility
- Minimum 4.5:1 contrast for body text.
- Visible focus ring uses coral + outline offset.
- Buttons and nav rows >= 40px target height.
- Status uses text + color, never color alone.
- Respect `prefers-reduced-motion`.

## Intentional deviation from reference
The supplied reference uses child/family photography as e-commerce merchandising. Daiki reuses its visual language but does not ship unrelated stock child photography in the application UI. Brand illustration/mascot artwork can be added later as a dedicated asset without changing the layout system.
