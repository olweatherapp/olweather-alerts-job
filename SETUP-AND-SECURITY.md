# Alerts job: setup and security (owner steps)

This repo only builds the shared alerts files. It holds no keys. Visitors and the app never touch it.

## One-time setup (you do these; no one else needs your keys)
1. Create a NEW repository (public is fine: the code has no secrets and public repos get unlimited free Actions minutes). Upload only this folder's contents.
2. Cloudflare dashboard: My Profile > API Tokens > Create Custom Token. Give it ONE permission only: Account > Workers KV Storage > Edit, limited to your account. Set an expiry (for example 1 year). Copy it once.
3. In the repo: Settings > Secrets and variables > Actions > New repository secret. Add three secrets: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, EARTHLENS_KV_NAMESPACE_ID. Paste them yourself; do not send them to anyone, including Claude.
4. Settings > Actions > General: "Fork pull request workflows" off or require approval, Workflow permissions "Read repository contents" only.
5. Settings > Code security: turn on secret scanning + push protection, Dependabot alerts, and branch protection on main (require a pull request, no force push).
6. Turn on two-factor authentication on the GitHub account (use a passkey or authenticator app).
7. Run the workflow once by hand (Actions > alerts-snapshot > Run workflow) and check KV has `alerts-compact` and `alerts`.

## What is already protected
- Encryption: GitHub and Cloudflare only talk over TLS 1.2+ (https). Secrets are encrypted at rest by GitHub and masked in logs.
- No `pull_request` trigger, so a stranger's fork can never run the job or read the secrets.
- The built-in token has no permissions; the one step that uses the Cloudflare token is the last step.
- Every action is pinned to a full commit, and nothing is installed from npm, so a hijacked package cannot reach the token.
- The Cloudflare token can only edit KV, nothing else on your account.

## Rotating the token
Create a new token, replace the GitHub secret, delete the old token in Cloudflare. Do this every year, or at once if you ever think it leaked.
