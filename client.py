#!/usr/bin/env python3
"""share-my-colab 客户端：零依赖单文件，用法与官方 `colab` CLI 相同，命令在网关所在的机器上执行。

配置（环境变量）：
  COLAB_GW_URL     网关地址，例如 https://example.com/colab（默认 http://127.0.0.1:8790）
  COLAB_GW_TOKEN   网关密钥 cgw_...；也可以写进 COLAB_GW_TOKEN_FILE 指向的文件，
                   或与 client.py 同目录的 .gateway_token
  COLAB_GW_TIMEOUT 单个命令在服务器上的最长运行秒数（默认 600，上限由服务器决定）

用法：
  client.py [客户端选项] <colab 子命令> [参数...]

客户端选项（必须写在子命令前面）：
  --json           输出结构化 JSON（适合 AI Agent）；也可设 COLAB_GW_JSON=1
  --timeout SEC    本次命令在服务器上的最长运行秒数（与子命令自己的 --timeout 无关）
  --url URL        临时指定网关地址
  -h, --help       显示本说明和官方 colab 的命令列表

示例：
  client.py usage
  client.py run --gpu T4 train.py --epochs 3          # 开机 → 运行 → 释放
  client.py new -s gpu1 --gpu T4
  client.py exec -s gpu1 -f train.py                  # 本地脚本自动上传
  echo 'print(1+1)' | client.py exec -s gpu1          # 代码从 stdin 传入
  client.py exec -s gpu1 -f nb.ipynb                  # 结果写回本地 nb_output.ipynb
  client.py ssh -s gpu1 -- nvidia-smi                 # 只支持单次命令
  client.py ssh -s gpu1 -- bash < setup.sh            # 脚本经 stdin 传给远端
  client.py upload -s gpu1 data.csv /content/data.csv
  client.py download -s gpu1 /content/model.pt ./     # 目标是目录时沿用远端文件名
  client.py log -s gpu1 -o history.ipynb
  client.py stop -s gpu1
  client.py --json sessions

本地文件会自动处理：
  上传：run 的脚本、upload 的本地文件、exec -f、install -r
  取回：download 的本地目标、exec/repl --output-image、log -o、exec -f x.ipynb 产生的 x_output.ipynb，
        以及 exec 没给 --output-image 时生成的图片（保存在 ./colab_tmp/）
ssh 只在 stdin 是文件或已有数据的管道时转发 stdin；在 Agent 里调用时建议加 </dev/null。

退出码：
  与 colab 一致（run 会透传脚本的 sys.exit 码）；124 = 超过 --timeout 被网关中断；
  2 = 被网关拒绝或参数错误；1 = 网络或网关错误；130 = 被 Ctrl-C 中断（服务器上的任务会继续跑到结束或超时）。
"""

from __future__ import annotations

import base64
import json
import os
import select
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

CLIENT_VERSION = "3"
DEFAULT_URL = "http://127.0.0.1:8790"

# colab 0.8.0 中需要一个值的选项（其余以 - 开头的都是开关）。
VALUE_OPTS = {
    "-s", "--session", "--gpu", "--tpu", "--timeout", "--env", "-f", "--file", "--output-image",
    "-r", "--requirement", "-n", "--lines", "-t", "--type", "-o", "--output", "--host", "-i", "--identity",
}
SHORT_VALUE_OPTS = {o for o in VALUE_OPTS if len(o) == 2}
# 指向本地文件的选项：in = 上传，out = 取回。
LOCAL_OPTS = {
    "exec": {"-f": "in", "--file": "in", "--output-image": "out"},
    "repl": {"--output-image": "out"},
    "install": {"-r": "in", "--requirement": "in"},
    "log": {"-o": "out", "--output": "out"},
}
# 指向本地文件的位置参数（第几个位置参数）。
LOCAL_POSITIONALS = {"run": {0: "in"}, "upload": {0: "in"}, "download": {1: "out"}}
# 第一个位置参数之后的内容属于脚本参数 / 远端命令，不再解析。
STOP_AFTER_FIRST_POSITIONAL = {"run", "ssh"}


class UsageError(Exception):
    pass


def err(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 参数改写：把本地路径换成任务目录里的暂存路径
# ---------------------------------------------------------------------------


class Plan:
    def __init__(self) -> None:
        self.args: list[str] = []
        self.files: dict[str, bytes] = {}  # 暂存路径 -> 内容
        self.outputs: dict[str, Path] = {}  # 回传文件的暂存路径 -> 本地保存位置
        self.display: dict[str, str] = {}  # 暂存路径 -> 在输出里显示成的本地路径
        self.uses_file = False
        self.wants_help = False
        self._n = 0

    def _slot(self, kind: str, name: str) -> str:
        self._n += 1
        return f"gw_{kind}{self._n}/{name}"  # 每个文件一个目录：同名文件不冲突，basename 保持不变

    def stage_in(self, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_file():
            raise UsageError(f"本地文件不存在或不是普通文件：{value}")
        rel = self._slot("in", path.name)
        self.files[rel] = path.read_bytes()
        self.display[rel] = value
        if path.suffix == ".ipynb":  # exec -f x.ipynb 会在旁边写 x_output.ipynb
            out_rel = rel[: -len(".ipynb")] + "_output.ipynb"
            local_out = Path(os.path.splitext(str(path))[0] + "_output.ipynb")
            self.outputs[out_rel] = local_out
            self.display[out_rel] = str(local_out)
        return rel

    def stage_out(self, value: str, remote: str | None = None) -> str:
        path = Path(value).expanduser()
        if value.endswith(("/", os.sep)) or path.is_dir():
            if not remote:
                raise UsageError(f"输出路径是一个目录，请写出文件名：{value}")
            path = path / PurePosixPath(remote).name
        rel = self._slot("out", path.name)
        self.outputs[rel] = path
        self.display[rel] = str(path)
        return rel


def build_plan(argv: list[str]) -> Plan:
    plan = Plan()
    if not argv:
        return plan
    cmd, rest = argv[0], argv[1:]
    plan.args.append(cmd)
    local_opts = LOCAL_OPTS.get(cmd, {})
    local_pos = LOCAL_POSITIONALS.get(cmd, {})
    positionals: list[str] = []
    only_positionals = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if only_positionals or tok == "-" or not tok.startswith("-"):
            kind = local_pos.get(len(positionals))
            if kind == "in":
                plan.uses_file = True
                plan.args.append(plan.stage_in(tok))
            elif kind == "out":
                plan.args.append(plan.stage_out(tok, remote=positionals[0] if positionals else None))
            else:
                plan.args.append(tok)
            positionals.append(tok)
            if cmd in STOP_AFTER_FIRST_POSITIONAL:
                plan.args.extend(rest[i + 1:])
                break
            i += 1
            continue
        if tok == "--":
            plan.args.append(tok)
            only_positionals = True
            i += 1
            continue
        if tok in ("-h", "--help"):
            plan.wants_help = True
        # 拆出选项名和值：--opt=value、-fVALUE、-f VALUE、--opt VALUE
        if tok.startswith("--"):
            name, eq, attached = tok.partition("=")
            value: str | None = attached if eq else None
        elif tok[:2] in SHORT_VALUE_OPTS and len(tok) > 2:
            name, value = tok[:2], tok[2:].removeprefix("=")
        else:
            name, value = tok, None
        if name not in VALUE_OPTS:
            plan.args.append(tok)
            i += 1
            continue
        step = 1
        if value is None:
            if i + 1 >= len(rest):  # 缺值，交给 colab 报错
                plan.args.append(tok)
                break
            value, step = rest[i + 1], 2
        kind = local_opts.get(name)
        if kind == "in":
            plan.uses_file = plan.uses_file or name in ("-f", "--file")
            plan.args += [name, plan.stage_in(value)]
        elif kind == "out":
            plan.args += [name, plan.stage_out(value)]
        else:
            plan.args += rest[i:i + step]
        i += step
    return plan


def read_stdin(cmd: str, plan: Plan) -> bytes | None:
    """只有会消费 stdin 的命令才读，避免 Agent 环境里的 stdin 管道永远不关闭导致卡死。"""
    if plan.wants_help or cmd not in ("exec", "repl", "ssh") or (cmd == "exec" and plan.uses_file):
        return None
    stream = sys.stdin
    if stream is None or stream.isatty():
        if cmd in ("exec", "repl"):
            raise UsageError(f"{cmd} 需要代码：用管道传入（echo 'print(1)' | client.py {cmd} -s NAME）或用 exec -f 文件")
        return None
    if cmd == "ssh":
        mode = os.fstat(stream.fileno()).st_mode
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            ready, _, _ = select.select([stream], [], [], 0.5)
            if not ready:
                return None
    return stream.buffer.read()


# ---------------------------------------------------------------------------
# 网络与输出
# ---------------------------------------------------------------------------


def load_token() -> str:
    token = os.environ.get("COLAB_GW_TOKEN", "").strip()
    candidates = [os.environ.get("COLAB_GW_TOKEN_FILE"), str(Path(__file__).resolve().parent / ".gateway_token")]
    for cand in candidates:
        if token:
            break
        if cand and Path(cand).expanduser().is_file():
            token = Path(cand).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise UsageError("缺少网关密钥：export COLAB_GW_TOKEN=cgw_...（或设置 COLAB_GW_TOKEN_FILE）")
    return token


def http_json(url: str, token: str, payload: dict[str, Any] | bytes | None, timeout: float) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
    req = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": f"share-my-colab-client/{CLIENT_VERSION}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        snippet = raw[:200].decode("utf-8", "replace").strip()
        body = {"ok": False, "error": f"网关返回的不是 JSON（HTTP {status}）：{snippet}"}
    return status, body


def safe_local_path(rel: str) -> Path | None:
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    return Path(*p.parts)


def restore_names(text: str, plan: Plan) -> str:
    for staged in sorted(plan.display, key=len, reverse=True):
        text = text.replace(staged, plan.display[staged])
    return text


def save_files(res: dict[str, Any], plan: Plan) -> tuple[list[str], list[str]]:
    saved, problems = [], []
    for rel, b64 in (res.get("files") or {}).items():
        target = plan.outputs.get(rel) or safe_local_path(rel)
        if target is None:
            problems.append(f"忽略可疑的回传路径：{rel!r}")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(b64))
            saved.append(str(target))
        except OSError as e:
            problems.append(f"无法保存 {target}：{e}")
    for item in res.get("skipped_files") or []:
        name = plan.display.get(item.get("path", ""), item.get("path"))
        problems.append(f"服务器没有回传 {name}（{item.get('size', '?')} 字节）：{item.get('reason')}")
    return saved, problems


def print_help(url: str) -> None:
    print((__doc__ or "").strip())
    try:
        token = load_token()
        status, res = http_json(f"{url}/api/cli", token, {"args": ["--help"], "timeout": 60}, 90)
        if status == 200 and res.get("stdout"):
            print("\n官方 colab 命令（经网关可用的见 health 接口；auth/drivemount/whoami/update/edit/pay 不开放）：")
            sys.stdout.write(res["stdout"])
    except (UsageError, OSError):
        pass


def exit_code_of(res: dict[str, Any]) -> int:
    if res.get("timed_out"):
        return 124
    code = res.get("exit_code")
    if not isinstance(code, int):
        return 0 if res.get("ok") else 1
    if code < 0:  # 被信号结束
        return 128 + min(-code, 127)
    return code if code <= 255 else 1


def main(argv: list[str]) -> int:
    json_mode = os.environ.get("COLAB_GW_JSON", os.environ.get("COLAB_PROXY_JSON", "")) == "1"
    url = os.environ.get("COLAB_GW_URL", DEFAULT_URL)
    timeout_env = os.environ.get("COLAB_GW_TIMEOUT", "600")

    # 客户端自己的选项只认子命令之前的部分，避免吞掉脚本参数或远端命令里的 --json/--timeout。
    i = 0
    timeout_raw = timeout_env
    while i < len(argv) and argv[i].startswith("-"):
        name, eq, val = argv[i].partition("=")
        if name == "--json":
            json_mode, i = True, i + 1
        elif name in ("--timeout", "--url"):
            if not eq:
                if i + 1 >= len(argv):
                    err(f"client.py: {name} 需要一个值")
                    return 2
                val, i = argv[i + 1], i + 2
            else:
                i += 1
            if name == "--timeout":
                timeout_raw = val
            else:
                url = val
        elif name in ("-h", "--help") and len(argv) == i + 1:
            print_help(url.rstrip("/"))
            return 0
        else:
            break
    argv = argv[i:]
    url = url.rstrip("/")
    try:
        timeout = int(timeout_raw)
        if timeout <= 0:
            raise ValueError
    except ValueError:
        err(f"client.py: --timeout / COLAB_GW_TIMEOUT 必须是正整数，收到 {timeout_raw!r}")
        return 2
    if not argv:
        print_help(url)
        return 0
    if not url.startswith(("http://", "https://")):
        err(f"client.py: 网关地址必须以 http:// 或 https:// 开头：{url}")
        return 2

    try:
        token = load_token()
        plan = build_plan(argv)
        stdin = read_stdin(argv[0], plan)
    except UsageError as e:
        if json_mode:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            err(f"client.py: {e}")
        return 2
    except OSError as e:
        err(f"client.py: 读取本地文件失败：{e}")
        return 2

    payload: dict[str, Any] = {"args": plan.args, "timeout": timeout}
    if plan.files:
        payload["files"] = {k: base64.b64encode(v).decode("ascii") for k, v in plan.files.items()}
    if stdin is not None:
        payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")

    body = json.dumps(payload).encode("utf-8")
    if len(body) > 8 * 1024 * 1024:  # 大请求先问上限：超限时服务器会直接断开，客户端只能看到 Broken pipe
        try:
            _, health = http_json(f"{url}/api/health", token, None, 30)
            limit = int(health["limits"]["max_body_bytes"])
        except (OSError, KeyError, TypeError, ValueError):
            limit = 0
        if limit and len(body) > limit:
            msg = (f"上传内容编码后 {len(body) / 2**20:.1f} MiB，超过网关上限 {limit / 2**20:.0f} MiB；"
                   "大文件请在 VM 上直接下载（ssh -- wget/gdown …），或请管理员调大 COLAB_GW_MAX_BODY_BYTES")
            if json_mode:
                print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
            else:
                err(f"client.py: {msg}")
            return 2

    try:
        status, res = http_json(f"{url}/api/cli", token, body, timeout + 120)
    except KeyboardInterrupt:
        err("\nclient.py: 已中断。服务器上的任务会继续运行直到结束或超时，可用 `client.py sessions` / `stop` 检查。")
        return 130
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        msg = f"无法连接网关 {url}：{reason}（检查 COLAB_GW_URL 和网络）"
        if json_mode:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            err(f"client.py: {msg}")
        return 1

    if status != 200:
        hints = {401: "（检查 COLAB_GW_TOKEN）", 413: "（文件太大，可改用 ssh/exec 在 VM 上直接下载数据）",
                 429: "（并发任务已满，稍后重试）"}
        msg = f"网关拒绝（HTTP {status}）：{res.get('error')}{hints.get(status, '')}"
        if json_mode:
            print(json.dumps({**res, "http_status": status}, ensure_ascii=False, indent=2))
        else:
            err(f"client.py: {msg}")
        return 2 if status in (400, 403, 413) else 1

    saved, problems = save_files(res, plan)
    if json_mode:
        res.pop("files", None)
        res["stdout"] = restore_names(res.get("stdout", ""), plan)
        res["stderr"] = restore_names(res.get("stderr", ""), plan)
        res["saved_files"] = saved
        if problems:
            res["client_warnings"] = problems
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res.get("stdout_b64"):
            sys.stdout.buffer.write(base64.b64decode(res["stdout_b64"]))  # 二进制输出原样写出
        else:
            sys.stdout.write(restore_names(res.get("stdout", ""), plan))
        sys.stdout.flush()
        if res.get("stderr"):
            sys.stderr.write(restore_names(res["stderr"], plan))
        if saved:
            err(f"[client] 已保存：{', '.join(saved)}")
        for p in problems:
            err(f"[client] 警告：{p}")
    return exit_code_of(res)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:  # 例如 `client.py log | head`
        try:
            sys.stdout = None  # type: ignore[assignment]
        finally:
            os._exit(141)
