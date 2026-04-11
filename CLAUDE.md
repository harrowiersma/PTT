# PTT Server

Self-hosted Push-to-Talk server for Hytera P50 walkie-talkies. Replaces Hytera's proprietary HyTalk service.

## Architecture
- **Murmur**: Voice backbone (Mumble server). Handles audio routing, Opus codec, channels, TLS.
- **Admin Service**: FastAPI (Python) wrapper for user management, channels, device health.
- **PostgreSQL**: Admin state (users, enrollment, health history).
- **Nginx**: HTTPS reverse proxy for admin dashboard only. Mumble traffic goes directly to Murmur.
- **HamMumble**: Existing Android client on Hytera P50 devices (not custom-built).

## Key decisions
- gRPC preferred over ICE for Murmur control (ICE Python bindings fragile on 3.11+)
- Murmur listens on port 443 for corporate firewall traversal
- Opus tuned for voice-only PTT: 16-24kbps, no positional audio
- PostgreSQL over SQLite for growth path
- P50 configured with background running + non-sleep mode for HamMumble

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming -> invoke office-hours
- Bugs, errors, "why is this broken", 500 errors -> invoke investigate
- Ship, deploy, push, create PR -> invoke ship
- QA, test the site, find bugs -> invoke qa
- Code review, check my diff -> invoke review
- Update docs after shipping -> invoke document-release
- Weekly retro -> invoke retro
- Design system, brand -> invoke design-consultation
- Visual audit, design polish -> invoke design-review
- Architecture review -> invoke plan-eng-review
- Save progress, checkpoint, resume -> invoke checkpoint
- Code quality, health check -> invoke health
