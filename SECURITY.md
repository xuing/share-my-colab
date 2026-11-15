# Security Model & Architecture

`share-my-colab` is designed to share a single Linux host's authenticated `google-colab-cli` (`colab`) session with trusted family members, collaborators, and their AI Agents **without exposing the host's Google account credentials, personal Google Drive, or host filesystem**.

---

## 1. Threat Model & Trust Boundaries

### Trusted Principals (Authorized Token Holders)
Callers possessing the gateway bearer token (`cgw_...`) are **trusted** to:
- Query shared Colab compute unit (CCU) balance and list active sessions.
- Allocate, run code on, transfer files to/from, and stop Colab VMs under the shared host Google account.
- Inspect shared session logs (`colab log`) and manage shared sessions (`colab sessions`, `colab stop`).

### Protected Assets (Trust Boundary)
Even if a caller (or an AI Agent operating on a caller's behalf—potentially under prompt injection or buggy tool invocation) submits crafted CLI flags, uploaded scripts, or path traversal payloads, the gateway MUST protect:

1. **Host Google OAuth Credentials & Identity**: `~/.config/colab-cli/token.json` (which contains high-privilege scopes such as `cloud-platform`, refresh tokens `1//...`, and access tokens `ya29....`), account email, and GCP permissions.
2. **Host Personal Google Drive**: Preventing `colab drivemount` or implicit `google.colab.drive.mount()` callbacks from authorizing remote Colab VMs against the host's Drive.
3. **Host Filesystem, SSH Keys & Secrets**: Preventing reads or writes outside each job's ephemeral directory (protecting `$HOME`, `~/.ssh`, `/etc/ssh`, `/tmp`, `/mnt`, `COLAB_GW_TOKEN`, `SSH_AUTH_SOCK`, `GOOGLE_APPLICATION_CREDENTIALS`).
4. **Host Software & Process Integrity**: Preventing callers from modifying installed binaries (`colab update`), launching host editors/browsers (`edit`, `pay`, `url --open`), importing uploaded Python files into host Python (`typer.py` shadowing), or leaving persistent background processes running on the host after a job ends.

### Why Simple CLI Blocklists Are Insufficient
Wrapping `colab` with string blacklists fails against parser differentials and hidden side channels:
- **Flag ordering & syntax variants**: `upload -- ~/.config/colab-cli/token.json /content/x`, `--file=/etc/passwd`, `-f/etc/passwd`, or global options like `--auth=adc`.
- **Upstream CLI upgrades**: New subcommands or file-path flags added in future `google-colab-cli` releases bypass static blacklists automatically.
- **Hidden credential hooks**: `colab install` shares `run_automation` machinery that attaches `colab_request_hook`—allowing remote kernel code (even a lingering background thread from a previous `exec`) to request Drive authorization via `drive.mount()`.

---

## 2. Three Independent Defense Layers

`share-my-colab` enforces defense in depth across **three independent layers**, plus process/environment isolation and output redaction. If any single layer has an unexpected gap, the remaining layers still block host compromise.

### Layer 1: Click AST / Parser Parameter Guard (`GUARD_SOURCE`)

Before invoking `colab`, `server.py` enforces two stages of argument validation:

1. **Pre-spawn allowlist check (`run_colab`)**: Rejects any command whose first argument is not in `ALLOWED_COMMANDS` (21 commands) or `("-h", "--help")` with HTTP `403` before creating a job directory or spawning a subprocess.
2. **In-interpreter Click parser validation (`GUARD_SOURCE`)**: Spawns `COLAB_PYTHON` (`-E -P -B -X utf8`) and uses `colab`'s own Typer/Click parser (`typer.main.get_command(app)` from `colab_cli.cli`) with `resilient_parsing=True` to parse the exact `argv` **before** calling `colab_cli.cli.main()` in the **same process**.

Because the gateway uses `colab`'s exact parser rather than a custom regex/getopt re-implementation, flag ordering, `--opt=value`, `-fVALUE`, and `--` end-of-options markers cannot cause a parser differential:
- **Subcommand allowlist (`ALLOWED_COMMANDS`)**: Only `download`, `exec`, `help`, `install`, `log`, `ls`, `new`, `readme`, `repl`, `restart-kernel`, `rm`, `run`, `sessions`, `skill`, `ssh`, `status`, `stop`, `upload`, `url`, `usage`, and `version` are allowed. `auth`, `drivemount`, `whoami`, `update`, `edit`, `pay`, and any unmapped future commands are denied.
- **Global flag rejection**: Every root parameter except `help` (`--auth`, `-c`, `--config`, `--logtostderr`, `--install-completion`, etc.) is rejected if explicitly supplied (`COMMANDLINE`, `ENVIRONMENT`, or `PROMPT`).
- **Forbidden subcommand parameters (`FORBIDDEN_PARAMS`)**: Blocks `ssh` proxy/ssh modes (which hijack stdin/stdout) and `url --open` (which launches a host browser).
- **`ssh` hardening**: Pins `-i` / `--identity` to the gateway's dedicated `.gateway_ssh/id_ed25519` key (`GUARD_SOURCE` verifies `params.get("identity") == policy["ssh_identity"]`) and requires a non-interactive `-- CMD` argument (`ssh -s NAME -- CMD`).
- **Session name validation (`SESSION_RE`)**: Requires `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` because `colab` uses session names to construct `~/.config/colab-cli/history/<session>.jsonl` paths.
- **Local path confinement (`LOCAL_PATH_PARAMS`)**: Every parameter mapped as reading or writing host files (`exec -f/--file`, `exec/repl --output-image`, `run <script>`, `upload <local_path>`, `download <local_path>`, `install -r/--requirement`, `log -o/--output`) must be a relative path strictly inside the ephemeral per-job `work` directory (`inside_work()` checks `os.path.realpath` and `os.path.commonpath`, rejecting `~`, absolute paths, `..`, and symlinks escaping `work`).

Verdicts are sent back over a dedicated `os.pipe()` descriptor (`verdict_fd`); policy denials exit with `126` and return HTTP `403`.

### Layer 2: Python `sys.addaudithook` & Runtime Credential Interception

Inside `GUARD_SOURCE`, before `colab_cli.cli.main()` executes:

1. **Filesystem Audit Hook (`sys.addaudithook(_audit)`)**:
   Intercepts every `"open"` audit event in the `colab` Python process:
   - **Writes** (`w`, `a`, `x`, `+`, or `O_WRONLY`/`O_RDWR`/`O_CREAT`/`O_APPEND`/`O_TRUNC`): Allowed **only** under `WRITE_OK = [WORK, ~/.config/colab-cli, "/dev"]`. Any other write raises `PermissionError(EACCES)`.
   - **Reads under `$HOME`**: Allowed **only** under `READ_OK = WRITE_OK + [sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path, *read_roots (e.g. .gateway_ssh)]`. Any other path under `$HOME` raises `FileNotFoundError(ENOENT)`, rendering host home files invisible even if a future `colab` release introduces an unmapped file-reading option.

2. **Runtime Credential Propagation Blocks (Dual Patch)**:
   - **`ColabRuntime.colab_request_hook`**: Replaced with a no-op property (`lambda self: None, lambda self, value: None`) so commands like `install` cannot honor `drive.mount()` requests coming from the remote kernel or lingering background threads.
   - **`colab_cli.commands.automation.get_credentials`**: Replaced with `_no_credential_propagation`, raising `PermissionError` if any code path attempts to send host OAuth credentials to a VM.

### Layer 3: `bubblewrap` (`bwrap`) Namespace Sandbox & Dedicated SSH Key

When `bwrap` is present and functional (`COLAB_GW_SANDBOX=auto` or `bwrap`), `server.py` wraps every job in a kernel namespace sandbox (`_sandbox_prefix`):
- **Namespaces & lifecycle**: `--die-with-parent`, `--unshare-pid`, `--unshare-ipc`, `--ro-bind / /`, `--dev /dev`, `--proc /proc`.
- **Masked host directories**: Replaces `/home`, `$HOME`, `/root`, `/tmp`, `/var/tmp`, `/mnt`, `/media`, `/srv`, and `/etc/ssh` with empty `--tmpfs` mounts. (Masking `/etc/ssh` also avoids user-namespace `nobody` ownership errors in OpenSSH; `colab` supplies its options via `-o`.)
- **Minimal selective re-mounts**:
  - `--ro-bind` only for Python/`colab` `read_roots` located under masked paths (reconstructing symlink chains such as `uv` Python symlinks via `--symlink`) and `.gateway_ssh/`.
  - `--bind` only for `~/.config/colab-cli` (required for `colab`'s OAuth token and session metadata/history) and the ephemeral per-job `work` directory (`--chdir <work>`).
- **Dedicated SSH identity**: `prepare_runtime()` generates an isolated Ed25519 keypair in `.gateway_ssh/id_ed25519` (`0o700` directory). Host `~/.ssh` and `SSH_AUTH_SOCK` are never mounted or passed into the sandbox.
- **No lingering processes**: Independent PID namespace + `_stop_job` cleanup guarantees child processes cannot outlive job exit or timeout. On timeout/shutdown, `_signal_job` skips `bwrap` itself when sending `SIGINT`/`SIGTERM` so `colab` can execute its `finally` block (e.g., releasing a `run` VM) before process-group `SIGKILL` runs.

---

## 3. Additional Runtime, Process & HTTP Hardening

- **Python Import Isolation (`PYTHONSAFEPATH=1`, `-E -P -B`)**: `colab ssh` uses `python -m colab_cli.cli` as its `ProxyCommand`. Without `PYTHONSAFEPATH=1` and `-P`, Python prepends the current working directory (`work`) to `sys.path`, allowing an uploaded `typer.py` to execute arbitrary Python on the host.
- **Environment Whitelist (`ENV_PASSTHROUGH`)**: Subprocesses inherit only safe locale, timezone, proxy, and TLS CA bundle variables. Secrets and dangerous variables such as `SSH_AUTH_SOCK`, `GOOGLE_APPLICATION_CREDENTIALS`, `COLAB_GW_TOKEN`, and `_COLAB_COMPLETE` are stripped.
- **Output Secret Redaction (`_SECRET_RE` & `_clean_output`)**: Strips ephemeral job directory paths (`work` -> `.`), removes noisy OS keyring warnings (`_KEYRING_NOISE`), and redacts `ya29.<20+ chars>` access tokens, `1//<20+ chars>` refresh tokens, and `colab-runtime-proxy-token=<...>` strings to `REDACTED` in both `stdout` and `stderr`.
- **Audit Log Redaction (`_mask_args`)**: Masks `--env KEY=VALUE` arguments as `--env KEY=***` in server logs.
- **Token Hygiene & Auth**: `.gateway_token` is created atomically with `os.open(..., 0o600)`. Incoming `Authorization: Bearer` tokens are checked in constant time using `hmac.compare_digest` on UTF-8 bytes.
- **Resource Caps**: Bounded concurrency semaphore (`MAX_CONCURRENT_JOBS`), JSON request size cap (`MAX_BODY_BYTES`), returned file byte cap (`MAX_RETURN_BYTES`), head+tail stream capture (`MAX_OUTPUT_BYTES`), per-job disk usage watchdog (`MAX_WORKDIR_BYTES`), and `JOBS_ROOT` directory permissions `0o700`.
- **Web UI Headers**: `GET /` and `GET /index.html` enforce strict `Content-Security-Policy` (`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`) and `X-Frame-Options: DENY`. All responses include `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.

---

## 4. Explicit Non-Goals

`share-my-colab` intentionally does **not** attempt to provide:
1. **Per-user isolation within the shared Colab pool**: All token holders share one `google-colab-cli` configuration (`~/.config/colab-cli`). Any holder can inspect active `sessions`, read session `log` histories (including code and outputs from other sessions), or `stop` any session. Do **not** issue tokens to untrusted public users.
2. **Isolation inside a remote Colab VM**: Once code runs on a Google Colab VM, Google Colab's own VM sandbox, ephemeral `/content` storage, and Terms of Service govern VM execution.
3. **Unauthenticated public DDoS protection**: The gateway is intended to run behind a TLS reverse proxy (or private network/VPN) with trusted token holders rather than exposed naked on the public internet.

---

## 5. Production Hardening Checklist

- [ ] **Require `bubblewrap` sandbox**: Set `COLAB_GW_SANDBOX=bwrap` in production (`.env`) so startup fails fast if kernel namespaces are unavailable instead of silently falling back to `none`.
- [ ] **Terminate TLS in front of the gateway**: Always expose `server.py` behind an HTTPS reverse proxy (e.g., Caddy or Nginx) so `Authorization: Bearer cgw_...` is never transmitted in plaintext.
- [ ] **Restrict bind interface**: Keep `COLAB_GW_HOST=127.0.0.1` (or a private/Docker bridge address firewalled from untrusted networks) so clients cannot bypass the reverse proxy.
- [ ] **Verify file permissions**: Confirm `.gateway_token` (`0600`), `.env` (`0600`), `.gateway_ssh/` (`0700`), `.gateway_ssh/id_ed25519` (`0600`), and `~/.config/colab-cli/` (`0700`) are readable only by the service user.
- [ ] **Keep secrets out of Git & logs**: Verify `.gitignore` excludes `.gateway_token`, `.gateway_ssh/`, `.env`, `gateway.log`, and `.remember/`. Never paste live tokens into shell history or shared docs.
- [ ] **Audit `google-colab-cli` upgrades**: Before upgrading `google-colab-cli`, inspect new commands/options and update `ALLOWED_COMMANDS` / `LOCAL_PATH_PARAMS` in `server.py` accordingly.
- [ ] **Prune session history**: Periodically clean up `~/.config/colab-cli/history/*.jsonl` on the host if long-running sessions produce large output logs.

---

## 6. Reporting Security Vulnerabilities

If you discover a sandbox escape, credential leak, parameter-guard bypass, or authentication issue in `share-my-colab`:
1. **Do not** open a public GitHub issue containing exploit PoCs or leaked credentials.
2. Report privately to the repository maintainers (for example via GitHub Private Security Advisories) with:
   - Affected `share-my-colab` commit/version and `google-colab-cli` version (`GET /api/health`).
   - Whether `bubblewrap` (`sandbox: "bwrap"`) was active.
   - Minimal reproduction steps and expected vs. actual behavior.
