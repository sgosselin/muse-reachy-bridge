# Security model

## What "secure" means here

The bridge's public URL (via Tailscale Funnel / Cloudflare Tunnel) is
**publicly reachable**. Security therefore cannot depend on the URL being
secret. Instead: every API call must carry cryptographic proof of *who* is
calling (Ed25519 request signature), or the owner's panel token. There is no
anonymous access to anything but the panel's static HTML.

## Threat model

| Threat | Mitigation |
|---|---|
| Stranger finds the Funnel URL | 401 — they can't forge a signature without the agent's private key |
| Replay of a captured request | Timestamp window (±120 s default) + server-side nonce cache |
| Agent's laptop/VM compromised | Owner revokes: `rm clients/<name>.pub`, restart — no shared secret to rotate elsewhere |
| Panel token leaks (browser, shoulder-surfing) | Rotate `BRIDGE_PANEL_TOKEN` in `.env`; agent API unaffected (separate credential) |
| Malicious or buggy motion command | Clamped to published safe ranges; e-stop latch; audit log shows who did what |
| Eavesdropping on the wire | Tunnel provides TLS; signatures don't leak the key even over plaintext |
| Clock skew breaks legit calls | NTP-level sync is enough (120 s window); increase `BRIDGE_AUTH_WINDOW` if needed |
| Daemon API changes shape | `/proxy` passthrough; bridge validates nothing about daemon payloads |

## What this does NOT defend against

- **A compromised owner machine.** The bridge runs there; root on that box
  owns the robot. Keep the Mac patched and locked.
- **Tunnel-provider compromise.** TLS terminates at the tunnel edge by design.
  Signatures still authenticate the caller, but use a provider you trust for
  confidentiality.
- **Physical access to the robot.** Anyone who can touch it can move it.
- **The agent itself going rogue.** The audit log records every command with
  the client id — accountability, not prevention. Keep the e-stop handy and
  don't leave Funnel running unattended with a live robot.

## Operational guidance

- Generate the panel token with `openssl rand -hex 16` (setup.sh does this).
- Never commit `.env`, `clients/*.pub`, or `audit.log` (all gitignored).
- The agent's **private** key must never be transmitted — the ceremony is
  public-key-only. If a private key is ever exposed, generate a new keypair
  and replace the `.pub` file.
- `--no-auth` exists for local debugging only. Never expose a `--no-auth`
  bridge to a tunnel.
- Review `audit.log` after remote sessions.
- When done for the day: `tailscale funnel --off` (or stop the bridge),
  return the head to neutral, consider `POST /motors {"mode":"disabled"}`.

## Reporting

This is a prototype by an independent developer, not audited software. If you
find a vulnerability, open a GitHub issue — please don't post working exploits
against other people's bridges.
