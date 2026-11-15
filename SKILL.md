---
name: share-my-colab
description: Operate remote Google Colab GPUs/TPUs safely via share-my-colab's client.py CLI without direct Google account access. Use when provisioning Colab VMs, running Python/notebooks/shell commands on GPUs, syncing files, or checking shared CCU quota.
---

# share-my-colab Agent Skill

Operate remote Google Colab GPUs/TPUs exclusively through `python3 client.py` (never invoke a local `colab` binary directly). All commands execute inside an isolated sandbox on the `share-my-colab` host and return outputs/files to your local workspace.

## 1. Environment & Quick Health Check

Ensure the gateway URL and bearer token are configured before calling `client.py`:

```bash
export COLAB_GW_URL="https://example.com/colab"   # gateway base URL (default: http://127.0.0.1:8790)
export COLAB_GW_TOKEN="cgw_..."                   # or set COLAB_GW_TOKEN_FILE, or place .gateway_token next to client.py
```

Optional environment variables:
- `COLAB_GW_TIMEOUT`: default gateway job timeout in seconds (default `600`, server max usually `3600`).
- `COLAB_GW_JSON=1`: enable structured JSON output by default.

Verify connectivity and check shared quota & active machines first:

```bash
python3 client.py --json usage
python3 client.py --json sessions
```

## 2. Critical Argument Ordering Rule

**Client options MUST be placed BEFORE the subcommand.** Placing `--json`, `--timeout`, or `--url` after the subcommand passes them to `colab` or the remote script instead of `client.py`:

```bash
# CORRECT
python3 client.py --json --timeout 1800 exec -s alice-train -f train.py
python3 client.py --url https://example.com/colab usage

# WRONG (--json and --timeout will be rejected or passed to colab/script)
python3 client.py sessions --json
python3 client.py exec -s alice-train --timeout 1800 -f train.py
```

Note the distinction between timeouts:
- `client.py --timeout SEC ...` controls how long the **gateway** waits before interrupting the CLI command (`exit_code 124`).
- `colab run --timeout SEC ...` (after `run`) controls `colab`'s own idle-output timeout.

## 3. Mental Model & Shared CCU Pool Etiquette

All gateway token holders share **one Google Colab account pool** (same CCU balance, `sessions`, and `log` history).

1. **Prefer `run` for one-shot scripts (cheapest on CCU)**:
   `run` provisions a VM, uploads and runs your script, returns outputs, and **automatically releases the VM** when done:
   ```bash
   python3 client.py --json run --gpu T4 train.py --epochs 3
   ```
   Supported `--gpu` values exposed by the gateway Web UI / CLI include `T4`, `L4`, `G4`, `A100`, `H100` (omit `--gpu` for CPU; add `--high-mem` if needed; check `python3 client.py new --help` or `run --help` for full GPU/TPU options).
2. **Use personal session prefixes for persistent VMs**:
   Always pick a unique session name matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` with your own prefix, e.g. `-s alice-exp1`:
   ```bash
   python3 client.py --json new -s alice-exp1 --gpu T4
   ```
3. **Persistent Jupyter kernel & remote `/content`**:
   - Successive `exec` calls on the same session share the **same running Jupyter kernel** (variables, imports, and GPU memory persist across `exec` calls).
   - Remote VM files live under `/content` (ephemeral; wiped when the VM stops).
4. **Always `stop` persistent sessions when done**:
   Never leave idle GPU sessions burning CCU:
   ```bash
   python3 client.py --json stop -s alice-exp1
   ```
   Do **not** stop sessions belonging to other users unless asked.

## 4. Automatic Local File Staging & Large File Workaround

`client.py` automatically packages local input files as base64 in the request and writes returned outputs back to your specified local paths. **Never manually rewrite paths into gateway scratch directories.**

| Direction | Command / Flag | Behavior |
|---|---|---|
| Upload (input) | `run <script> [args...]` | Uploads `<script>`; forwards `[args...]` to the script |
| Upload (input) | `upload <local_path> <remote_path>` | Uploads `<local_path>` to remote VM path (e.g. `/content/data.csv`) |
| Upload (input) | `exec -s NAME -f <script_or_nb>` | Uploads local `.py` or `.ipynb` file |
| Upload (input) | `install -s NAME -r <req.txt>` | Uploads local `requirements.txt` and installs packages on VM |
| Download (output) | `download -s NAME <remote_path> <local_path>` | If `<local_path>` is a directory or ends with `/`, keeps remote filename |
| Download (output) | `exec -s NAME -f notebook.ipynb` | Automatically downloads executed `notebook_output.ipynb` next to local `notebook.ipynb` |
| Download (output) | `exec` / `repl --output-image <path>` | Saves generated plot/image to `<path>` |
| Download (output) | `log -s NAME -o <out.ipynb>` | Saves session log file to `<out.ipynb>` |
| Auto-fallback | `exec` without `--output-image` | Display/matplotlib images saved automatically under `./colab_tmp/` |

### 64 MiB JSON / Base64 Payload Limit

Both uploads (`COLAB_GW_MAX_BODY_BYTES`) and file returns (`COLAB_GW_MAX_RETURN_BYTES`) are capped at **64 MiB** by default (~48 MiB raw file limit before base64 expansion).

- **Do NOT** upload large datasets, archives, or multi-hundred-MB checkpoints via `client.py upload` or `download`.
- **Instead**, pull datasets directly onto the Colab VM over `ssh`:
  ```bash
  python3 client.py ssh -s alice-exp1 -- wget -q -O /content/dataset.tar.gz "https://..." </dev/null
  ```
  (Or fetch from Hugging Face / cloud storage from inside the VM, and push large checkpoints directly from the VM to external storage or download only compact metrics/logs back through `client.py`.)

## 5. Execution Patterns for Agents

- **Run inline Python via stdin**:
  ```bash
  echo 'import torch; print(torch.cuda.is_available())' | python3 client.py --json exec -s alice-exp1
  ```
- **Run a local Python script or notebook**:
  ```bash
  python3 client.py --json exec -s alice-exp1 -f train.py
  python3 client.py --json exec -s alice-exp1 -f analysis.ipynb
  ```
- **Run a single non-interactive shell command via `ssh`** (always redirect `</dev/null` unless intentionally piping a script so Agent stdin pipes cannot block):
  ```bash
  python3 client.py ssh -s alice-exp1 -- nvidia-smi </dev/null
  python3 client.py ssh -s alice-exp1 -- bash -lc "ls -la /content && df -h" </dev/null
  ```
- **Pipe a shell script to `ssh`**:
  ```bash
  python3 client.py ssh -s alice-exp1 -- bash < setup.sh
  ```
- **Inspect remote files or check subcommand usage**:
  ```bash
  python3 client.py ls -s alice-exp1 /content
  python3 client.py <subcommand> --help
  ```

## 6. Exit Codes & Timeout Recovery

| Exit Code | Meaning & Agent Action |
|---|---|
| `0` | Success (for `run` and `ssh`, passes through the remote script/command exit code). |
| `1` | Network error, `401` invalid token, `429` concurrency limit reached (`MAX_CONCURRENT`, default `4` — wait & retry), or `5xx` gateway error. |
| `2` | Client usage error, missing local file, or gateway policy/size rejection (`400`, `403`, `413`). **Do not retry unchanged** — inspect stderr / JSON `error`. |
| `124` | Gateway `--timeout` exceeded (`"timed_out": true`). See recovery below. |
| `130` | Interrupted locally (`SIGINT`). Note: the command keeps running on the gateway until completion or timeout. |

### Recovering After an `exec` Timeout (`exit_code = 124`)

When `exec` (or `repl`) times out on the gateway, the remote Jupyter kernel may still be executing the timed-out cell. Any new `exec` call will queue behind it and appear hung.
- To inspect what is running: `python3 client.py ssh -s alice-exp1 -- ps aux </dev/null`
- To immediately clear the stuck execution and reuse the session (note: clears all in-memory kernel variables):
  ```bash
  python3 client.py --json restart-kernel -s alice-exp1
  ```
- If the VM itself is stuck or no longer needed, terminate it:
  ```bash
  python3 client.py --json stop -s alice-exp1
  ```

## 7. Strictly Blocked Commands & Patterns (Do NOT Attempt)

The gateway blocks these at both pre-spawn and Click-parser layers (HTTP `403` / exit `2`). Never waste turns trying them:

- **Blocked subcommands**: `auth`, `drivemount`, `whoami`, `update`, `edit`, `pay` (and any unknown subcommands).
- **Allowed subcommands (21 total)**: `download`, `exec`, `help`, `install`, `log`, `ls`, `new`, `readme`, `repl`, `restart-kernel`, `rm`, `run`, `sessions`, `skill`, `ssh`, `status`, `stop`, `upload`, `url`, `usage`, `version`.
- **Blocked global flags before subcommand**: `--auth`, `-c`, `--config`, `--logtostderr`, `--install-completion`, etc.
- **Blocked `ssh` forms**:
  - Bare interactive `ssh -s NAME` without `-- CMD` (rejected).
  - `ssh -i ...` / `--identity` (gateway pins its own dedicated SSH key).
  - `ssh` proxy/ssh internal modes.
- **Blocked `url --open`**: server will not open a local browser.
- **Blocked interactive `repl` / `exec` without code**: `repl` and `exec` (without `-f`) require code via stdin (`echo '...' | client.py exec -s NAME`); interactive TTY loops are not supported.
- **Blocked raw host paths**: passing `/etc/passwd`, `~/.ssh/...`, `../...`, etc. as local file arguments is rejected; pass paths relative to or inside your local workspace and let `client.py` stage them.
