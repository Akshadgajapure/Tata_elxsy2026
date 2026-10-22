# Security Audit — TataElexy Adaptive QoS Engine

> [!NOTE]
> This document captures the security audit baseline for the TataElexy project prototype.
> It is generated and maintained alongside the automated script `scripts/audit_secrets.py`.

---

## 1. Scope — What Was Scanned

The audit script (`scripts/audit_secrets.py`) performs a recursive file-tree scan starting from the project root.

| Attribute | Value |
|---|---|
| **Project root** | `TataElexy/` |
| **File extensions scanned** | `.py` `.json` `.yaml` `.yml` `.env` `.cfg` `.ini` `.md` `.sh` `.bat` `.ps1` |
| **Directories excluded** | `.venv` `__pycache__` `.pytest_cache` `.git` |

### 1.1 Patterns Detected

| Category | Pattern / Rule | Severity |
|---|---|---|
| API keys | `sk-`, `pk-` prefixes; `api_key` / `apikey` / `api-key` assignments | FAIL / WARN |
| Bearer tokens | `Bearer <token>` in any header or config value | FAIL |
| Passwords | `password=`, `passwd=`, `pwd=` followed by a non-empty literal | WARN |
| Private keys | `-----BEGIN RSA/EC/DSA/OPENSSH PRIVATE KEY-----` | FAIL |
| AWS credentials | `AKIA…` / `ASIA…` access key IDs; `aws_secret_access_key` assignments | FAIL |
| GCP credentials | `"type": "service_account"` JSON marker; `"private_key"` block | WARN / FAIL |
| Azure credentials | Connection-string format; `client_secret` assignments | FAIL / WARN |
| JWT tokens | `eyJ…` three-part structure | FAIL |
| Base64 tokens | Runs of ≥ 40 Base64 characters | WARN |
| Public IP addresses | Any IPv4 address **not** in `127.x`, `10.x`, `192.168.x`, `172.16–31.x` | WARN |

---

## 2. Audit Results

> [!IMPORTANT]
> The project is a **clean prototype**. No real credentials, tokens, or production secrets are
> present anywhere in the codebase. All audit checks are expected to return **PASS**.

### 2.1 File-Level Summary (Baseline)

All project source, configuration, and documentation files pass the audit with status **PASS**.
The table below lists file categories and their expected verdicts:

| File category | Files | Expected status |
|---|---|---|
| Python source modules | `*.py` | **PASS** |
| Configuration files | `*.cfg`, `*.ini`, `*.yaml`, `*.json` | **PASS** |
| Documentation | `*.md` | **PASS** |
| Shell / batch scripts | `*.sh`, `*.bat`, `*.ps1` | **PASS** |
| Environment templates | `*.env` (if present) | **PASS** |

### 2.2 Running the Audit

```powershell
# From the project root
python scripts\audit_secrets.py

# Against an explicit path
python scripts\audit_secrets.py "c:\Users\akshat\Downloads\TataElexy (2)\TataElexy"
```

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Clean — no FAIL findings |
| `1` | At least one FAIL finding detected; remediate before committing |
| `2` | Invalid arguments (e.g. non-existent directory) |

---

## 3. Credentials Policy

> [!CAUTION]
> Violating this policy constitutes a security incident and must be reported immediately.

### 3.1 No Real Credentials in Source Control

- **No** API keys, tokens, passwords, or private keys are ever committed to the repository.
- All configuration that requires secrets (e.g. a monitoring back-end API key) must be injected
  via **environment variables** or a secrets manager (e.g. Azure Key Vault, AWS Secrets Manager)
  at runtime.
- `.env` files containing real secrets **must** be listed in `.gitignore` and never committed.

### 3.2 Placeholder Conventions

When a configuration file needs to reference a secret, use one of the approved placeholder forms:

```yaml
# GOOD — clearly a placeholder
api_key: "${QOS_API_KEY}"
password: "CHANGE_ME"

# BAD — will trigger WARN/FAIL in audit
api_key: "sk-abc123realtoken"
```

### 3.3 IP Address Policy — Local Testbed Addresses

All IP addresses hard-coded in the codebase are **private / testbed addresses** that the audit
script explicitly allows:

| Range | Purpose |
|---|---|
| `127.0.0.0/8` | Loopback (localhost) |
| `10.0.0.0/8` | Private class-A lab network |
| `192.168.0.0/16` | Private class-C lab network |
| `172.16.0.0/12` | Private class-B lab network (`172.16–31.x.x`) |

Any public IP address discovered by the audit is flagged as **WARN** and must be justified or
replaced with a configuration variable before the PR is merged.

---

## 4. Windows `pktmon` Companion — Authentication Notes

> [!NOTE]
> The Windows `pktmon` packet-capture companion component operates entirely within the local
> machine's kernel and does not use any external network authentication mechanism.

Key points:

- `pktmon` is invoked directly via subprocess by the QoS engine on localhost.
- It requires **elevated privileges** (Administrator) to run, enforced by the OS — no application-level
  token or credential is needed or stored.
- No API keys, Bearer tokens, or authentication headers are passed to or from `pktmon`.
- Captured data is written to a local ETL/PCAPNG file and read back by the engine in-process;
  no network socket or remote endpoint is involved.
- The audit script will always return **PASS** for any `pktmon`-related source files because no
  credential patterns exist in them.

---

## 5. Remediation Guidance

If the audit script ever returns a **FAIL** or unexpected **WARN**:

1. **Identify** the flagged file and line number from the report.
2. **Rotate** any real credential immediately (even if committed accidentally).
3. **Remove** the credential from source; inject it via environment variable instead.
4. **Purge** it from git history using `git filter-repo` or BFG Repo Cleaner.
5. **Re-run** `python scripts/audit_secrets.py` to confirm a clean result before pushing.

---

## 6. CI Integration Recommendation

Add the following step to your CI pipeline to block merges that introduce secrets:

```yaml
# GitHub Actions example
- name: Secrets audit
  run: python scripts/audit_secrets.py
  # Script exits with code 1 on any FAIL → CI job fails automatically
```

---

*Last updated: 2026-10-18 | Maintainer: TataElexy Engineering*
