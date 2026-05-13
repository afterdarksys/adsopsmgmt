# adsopsmgmt

After Dark Systems ops platform — change management, host inventory, and infrastructure tooling.

This repo links the component repositories as submodules. Clone with:

```bash
git clone --recurse-submodules https://github.com/afterdarksys/adsopsmgmt.git
```

---

## Ecosystem Overview

### Core Platform (`adsops-utils`)

Change management and compliance system — the main product:

- **Ticket management** with approval workflows (ops, security, risk, change board, AI ops, network engineering, cloud)
- **Multi-industry compliance** — HIPAA, SOX, GLBA, GDPR, Banking Secrecy Act, and custom templates
- **Auth** — OAuth2/OIDC, Google OAuth2, Passkeys/WebAuthn (FIDO2), email/password with TOTP MFA
- **API server** (Go/Gin), **CLI** (`changes`), background workers, database migrations
- **Proto/gRPC definitions** for host, container, k3s, stats, and telemetry

### Host Infrastructure Layer (`adsops-utils/tools/`)

Supporting tools that manage the fleet the change system governs:

| Tool | Description |
|------|-------------|
| `hostctl` | Host inventory — source of truth for host records, SSH config, and key tracking |
| `infractl` | SSH remote management — Docker, k3s, and Prometheus node_exporter |
| `statsagent` | Metrics agent deployed on remote hosts |
| `blackout` | Blackout window management for scheduled maintenance |
| `sysscript` | Sandboxed system script runner |
| `aiserve-deploy` | AI service deployment tooling |
| `dockate-discover` | Container and Docker host discovery |
| `ssh-key-tracker` | SSH key tracking across the fleet |
| `md-ticket-sync` | Markdown-based ticket synchronization |

### Python CLI Wrapper (`adsops-cli`)

`adsops` — unified Python CLI that surfaces hostctl, infractl, stats, and sysscript operations under a single interface.

### Shared Library (`adsyslib`)

Common library shared across After Dark Systems tooling.

---

## Submodules

| Repo | Description |
|------|-------------|
| [adsops-utils](https://github.com/afterdarksys/adsops-utils) | Core platform, API, CLI, and host tools |
| [adsops-cli](https://github.com/straticus1/adsops-cli) | Unified Python CLI wrapper |
| [adsyslib](https://github.com/straticus1/adsyslib) | Shared library |

---

## Architecture

The platform follows a layered model:

```
┌─────────────────────────────────────────┐
│         Change Management API           │  Ticket lifecycle, approvals, audit trail
├─────────────────────────────────────────┤
│            adsops CLI / changes CLI     │  Operator interface
├──────────────┬──────────────────────────┤
│   hostctl    │   infractl               │  Host inventory + remote SSH management
├──────────────┴──────────────────────────┤
│   statsagent / node_exporter            │  Host metrics and observability
└─────────────────────────────────────────┘
```

All privileged operations are gated through the change management ticket and approval workflow before execution.

---

## Quick Start

```bash
# Clone everything
git clone --recurse-submodules https://github.com/afterdarksys/adsopsmgmt.git
cd adsopsmgmt

# Core platform (Go API + CLI)
cd adsops-utils
make deps && make build

# Python CLI
cd ../adsops-cli
pip install -e .

# infractl (standalone Go binary)
cd ../adsops-utils/tools/infractl
make build
```

See each submodule's README for full setup instructions.
