#!/usr/bin/env python3
"""share-my-colab（受控 Colab 算力网关）v3：不重写 colab-cli 业务逻辑，把本机 `colab` 安全地开放给受信任的人和 Agent。

宿主机的 Google OAuth 凭证带 cloud-platform 等高危权限，所以网关有三道互相独立的防线：

1. 参数守卫（GUARD_SOURCE）。在 colab 自己的 Python 解释器里，用 colab 自己的 Click 解析器解析
   同一份 argv，按策略检查后，在同一进程里调用 colab 的 main()。网关和 colab 对参数的理解完全
   一致，参数顺序、`--opt=value`、`-fVALUE`、`--` 等写法都无法绕过。
   - 只放行 ALLOWED_COMMANDS；全局参数（-c/--config/--auth/--logtostderr/--install-completion…）
     必须保持默认值；
   - 会读写宿主机文件的参数（LOCAL_PATH_PARAMS）只能是本次任务目录内的相对路径；
   - session 名必须匹配 SESSION_RE（colab 用它拼 history/<name>.jsonl 路径）。
2. Python audit hook。colab 进程里所有 open() 只能写任务目录和 ~/.config/colab-cli；$HOME 下除
   colab 运行所需目录外一律表现为“文件不存在”。将来 colab 新增文件参数也逃不出去。
3. bubblewrap 沙箱（可用时自动启用）。/home、/tmp、/mnt 等换成空 tmpfs，只挂回 colab 的
   Python 环境、~/.config/colab-cli、网关专用 SSH 密钥和本次任务目录；独立 PID 命名空间，
   任务结束或超时后不会残留进程。ssh 看不到宿主机 ~/.ssh 和 ssh-agent。
"""

from __future__ import annotations

import base64
import binascii
import collections
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise SystemExit(f"环境变量 {name} 必须是整数")


MiB = 1024 * 1024
HERE = Path(__file__).resolve().parent
HOME = Path.home()

HOST = os.environ.get("COLAB_GW_HOST", "127.0.0.1")
PORT = _env_int("COLAB_GW_PORT", 8790)
COLAB_BIN = os.environ.get("COLAB_BIN") or shutil.which("colab") or str(HOME / ".local/bin/colab")
SANDBOX_MODE = os.environ.get("COLAB_GW_SANDBOX", "auto").lower()  # auto | bwrap | off

MAX_BODY_BYTES = _env_int("COLAB_GW_MAX_BODY_BYTES", 64 * MiB)  # 单个请求体（含 base64 上传文件）
MAX_RETURN_BYTES = _env_int("COLAB_GW_MAX_RETURN_BYTES", 64 * MiB)  # 回传文件总量
MAX_OUTPUT_BYTES = _env_int("COLAB_GW_MAX_OUTPUT_BYTES", 4 * MiB)  # stdout / stderr 各自保留量
MAX_WORKDIR_BYTES = _env_int("COLAB_GW_MAX_WORKDIR_BYTES", 1024 * MiB)  # 任务目录磁盘上限
DEFAULT_TIMEOUT_SEC = _env_int("COLAB_GW_DEFAULT_TIMEOUT", 600)
MAX_TIMEOUT_SEC = _env_int("COLAB_GW_MAX_TIMEOUT", 3600)
MAX_CONCURRENT_JOBS = _env_int("COLAB_GW_MAX_CONCURRENT", 4)
KILL_GRACE_SEC = _env_int("COLAB_GW_KILL_GRACE", 30)  # 超时后留给 colab 收尾（如 run 释放 VM）的时间

TOKEN_FILE = HERE / ".gateway_token"
CLIENT_FILE = HERE / "client.py"
SKILL_FILE = HERE / "SKILL.md"
SSH_KEY = HERE / ".gateway_ssh" / "id_ed25519"
COLAB_CONFIG_DIR = HOME / ".config" / "colab-cli"
JOBS_ROOT = Path(tempfile.gettempdir()) / f"share-my-colab-{os.getuid()}"
JOB_TMP_SUBDIR = "colab_tmp"  # 任务内 TMPDIR：exec 未指定 --output-image 时图片落在这里并回传

# 白名单而不是黑名单：colab 有隐藏命令（whoami、README、SKILL），升级还可能新增命令，黑名单必然漏。
# 刻意不放行：auth（把宿主机 GCP 凭证注入 VM）、drivemount（挂载个人 Drive）、whoami（邮箱/scope）、
# update（改宿主机程序）、edit（需要 $EDITOR）、pay（打开浏览器）。
ALLOWED_COMMANDS = frozenset(
    {
        "download", "exec", "help", "install", "log", "ls", "new", "readme", "repl", "restart-kernel",
        "rm", "run", "sessions", "skill", "ssh", "status", "stop", "upload", "url", "usage", "version",
    }
)

# 对照 colab_cli 0.8.0 源码，逐个列出会读（in）/写（out）宿主机文件的参数（Click 参数名）。
LOCAL_PATH_PARAMS = {
    "exec": {"file": "in", "output_image": "out"},  # -f 读；.ipynb 还会在同目录写 <stem>_output.ipynb
    "repl": {"output_image": "out"},
    "run": {"script": "in"},
    "upload": {"local_path": "in"},
    "download": {"local_path": "out"},
    "install": {"requirement": "in"},  # 读取后上传到 VM 的 /content
    "log": {"output": "out"},
}
# 不允许调用方设置的参数：ssh 的 ProxyCommand 模式和内部模式会长期占用 stdin/stdout；
# ssh -i 由网关注入专用密钥；url --open 会在服务器上启动浏览器。
FORBIDDEN_PARAMS = {"ssh": ["proxy_mode", "ssh_mode"], "url": ["open_browser"]}
SESSION_RE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"

# 传给 colab 子进程的环境变量白名单。SSH_AUTH_SOCK、GOOGLE_APPLICATION_CREDENTIALS、
# COLAB_GW_TOKEN、_COLAB_COMPLETE（Typer 补全触发器）等一律不传。
ENV_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY",
    "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)

# 在 colab 的 Python 里运行。argv: <policy-json> <colab args...>
GUARD_SOURCE = r'''
import errno, json, os, re, signal, sys

policy = json.loads(sys.argv[1])
argv = sys.argv[2:]
fd = policy["verdict_fd"]
signal.signal(signal.SIGINT, signal.default_int_handler)  # 超时时网关发 SIGINT，让 colab 跑完 finally


def verdict(obj):
    os.write(fd, json.dumps(obj, ensure_ascii=False).encode())
    os.close(fd)


def deny(msg):
    verdict({"ok": False, "error": msg[:2000]})
    sys.exit(126)


def short(value):
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "…"


import typer.main
from colab_cli.cli import app

WORK = os.path.realpath(os.getcwd())


def explicit(ctx, name):
    # typer 0.29 自带一份 click（typer._click），枚举类和顶层 click 的不是同一个对象，所以按名字比较。
    src = ctx.get_parameter_source(name)
    return src is not None and src.name in ("COMMANDLINE", "ENVIRONMENT", "PROMPT")


def inside_work(path):
    real = os.path.realpath(path)
    return real != WORK and os.path.commonpath([real, WORK]) == WORK, real


def check_local_path(opt, value, kind):
    if not isinstance(value, str) or not value:
        deny(f"{opt} 需要一个文件路径")
    if value.startswith("~") or os.path.isabs(value) or ".." in value.replace("\\", "/").split("/"):
        deny(f"{opt} {short(value)}：只能使用网关任务目录内的相对路径。"
             "本地文件请用 client.py，它会自动上传/取回，并把路径改写成任务目录里的位置。")
    ok, real = inside_work(value)
    if not ok:
        deny(f"{opt} {short(value)} 指向任务目录之外")
    if kind == "in" and not os.path.isfile(real):
        deny(f"{opt} {short(value)}：任务目录中没有这个文件（需要随请求一起上传）")
    if kind == "out":
        if os.path.lexists(value) and not os.path.isfile(value):
            deny(f"{opt} {short(value)} 已存在且不是普通文件")
        os.makedirs(os.path.dirname(real), exist_ok=True)


root = typer.main.get_command(app)
try:
    ctx = root.make_context("colab", list(argv), resilient_parsing=True)
    rest = [*ctx._protected_args, *ctx.args]
    for p in root.params:
        if p.name != "help" and explicit(ctx, p.name):
            deny("网关不允许使用全局参数 " + "/".join(p.opts) + "（认证、配置文件与日志由网关统一管理）")
    sub = sctx = None
    if rest:
        name, sub, sub_args = root.resolve_command(ctx, rest)
        if sub is None or sub.name not in policy["allowed"]:
            deny(f"网关不开放子命令 {short(rest[0])}。可用：" + ", ".join(sorted(policy["allowed"])))
        sctx = sub.make_context(name, list(sub_args), parent=ctx, resilient_parsing=True)
except Exception as e:
    if not (hasattr(e, "show") and hasattr(e, "exit_code")):  # 不是 Click 的用法错误：拒绝而不是放行
        deny(f"无法解析参数：{type(e).__name__}: {short(str(e))}")
    verdict({"ok": True})  # 普通用法错误，交给 colab 原样报错（解析阶段失败时 colab 不会执行任何操作）
    e.show()
    sys.exit(e.exit_code)

if sub is not None:
    cmd, params = sub.name, sctx.params
    asking_help = explicit(sctx, "help")
    opts = {p.name: "/".join(p.opts) or p.name for p in sub.params}
    for pname in policy["forbidden"].get(cmd, []):
        if explicit(sctx, pname):
            deny(f"网关不允许 {cmd} {opts.get(pname, pname)}")
    sess = params.get("session")
    if sess is not None and not re.fullmatch(policy["session_re"], sess):
        deny(f"session 名 {short(sess)} 不合法：只能用字母、数字、_ . -，以字母或数字开头，最长 64 个字符")
    for pname, kind in policy["local_paths"].get(cmd, {}).items():
        if params.get(pname) is not None:
            check_local_path(opts.get(pname, pname), params[pname], kind)
    if cmd == "ssh":
        if params.get("identity") != policy["ssh_identity"]:
            deny("网关的 ssh 固定使用网关专用密钥，不接受 -i/--identity")
        if not params.get("cmd") and not asking_help:
            deny("网关里的 ssh 只能执行单次命令，例如：ssh -s my-gpu -- nvidia-smi")
    if not asking_help and not policy["has_stdin"]:
        if cmd == "repl" or (cmd == "exec" and not params.get("file")):
            deny(f"{cmd} 需要代码：通过 stdin 传入（echo 'print(1)' | client.py {cmd} -s NAME），或用 exec -f 文件")

# 第二道防线：colab 进程内的文件访问限制。
def _real(p):
    return os.path.realpath(p)

HOME = _real(os.path.expanduser("~"))
CONFIG = _real(policy["config_dir"])
WRITE_OK = [WORK, CONFIG, "/dev"]
READ_OK = WRITE_OK + [_real(p) for p in [sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path, *policy["read_roots"]] if p]
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _under(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _audit(event, args):
    if event != "open":
        return
    path, mode, flags = args
    if path is None or isinstance(path, int):
        return
    raw = os.fsdecode(path)
    real = _real(raw)
    writing = bool(mode and any(c in mode for c in "wax+")) or bool((flags or 0) & WRITE_FLAGS)
    if writing and not _under(real, WRITE_OK):
        raise PermissionError(errno.EACCES, "share-my-colab: 不允许写入任务目录之外", raw)
    if _under(real, [HOME]) and not _under(real, READ_OK):
        raise FileNotFoundError(errno.ENOENT, "share-my-colab: 网关隐藏了这个路径", raw)


# 第三道补丁：禁止把账号凭证传播到 VM。colab 的 install 与 drivemount/auth 共用 run_automation，
# 它会挂一个钩子：kernel 里任何代码（包括之前 exec 留下的后台线程）调用 drive.mount() 时，
# 宿主机就用自己的 OAuth 凭证把 Drive 授权给这台 VM。两处都打补丁，任何一处失效另一处仍会拒绝。
from colab_cli import runtime as _runtime
from colab_cli.commands import automation as _automation

_runtime.ColabRuntime.colab_request_hook = property(lambda self: None, lambda self, value: None)


def _no_credential_propagation(*_a, **_k):
    raise PermissionError("share-my-colab: 网关不允许把账号凭证传播到 VM（drive.mount / auth）")


_automation.get_credentials = _no_credential_propagation

verdict({"ok": True})
sys.addaudithook(_audit)
sys.argv = ["colab", *argv]
from colab_cli.cli import main
try:
    sys.exit(main())
except (KeyboardInterrupt, SystemExit) as exc:
    # 网关超时/关闭时发来的 SIGINT（typer 会把它转成 SystemExit(130)）。colab 自己的 finally 此时已
    # 执行完（例如 run 已释放 VM）；中断若发生在连接建立阶段，残留的非 daemon websocket 线程会让
    # 解释器一直等下去，所以直接退出。其他退出码照常处理。
    if isinstance(exc, SystemExit) and exc.code != 130:
        raise
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(130)
'''

PROBE_SOURCE = r"""
import json, os, sys
from importlib.metadata import version
import colab_cli.cli
roots = {os.path.realpath(p) for p in [sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path] if p}
print(json.dumps({"version": version("google-colab-cli"), "roots": sorted(r for r in roots if os.path.exists(r))}))
"""

# 会被替换成 REDACTED 的凭证形态：runtime-proxy token（出现在 requests 报错的 URL 里）、
# Google access token（ya29.）和 refresh token（1//）。
_SECRET_RE = re.compile(
    rb"(colab-runtime-proxy-token=)[^&\s'\"]+|(ya29\.)[0-9A-Za-z_\-]{20,}|(?<![\w/])(1//)[0-9A-Za-z_\-]{20,}"
)
_KEYRING_NOISE = (
    b"Warning: Could not read the OAuth token from the OS keyring",
    b"Warning: Could not save the OAuth token to the OS keyring",
)


class Runtime:
    """启动时探测一次的运行环境。"""

    python: str = ""
    colab_version: str = "?"
    read_roots: list[str] = []
    bwrap: str | None = None
    sandbox_binds: list[str] = []
    sandbox_symlinks: list[tuple[str, str]] = []
    hidden_dirs: list[str] = []
    ssh_identity: str | None = None
    token: bytes = b""


RT = Runtime()
JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
ACTIVE_LOCK = threading.Lock()
ACTIVE_JOBS: set[subprocess.Popen] = set()
STOPPING: set[int] = set()  # 正在被中断的任务 pid；保证每个任务只有一个线程发信号（重复的 SIGINT 会打断 colab 的收尾）
PENDING_RESPONSES = [0]  # 正在处理的 API 请求数，关闭时等它们把结果发回
SHUTTING_DOWN = threading.Event()


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 启动准备
# ---------------------------------------------------------------------------


def load_or_create_gateway_token() -> str:
    env_tok = os.environ.get("COLAB_GW_TOKEN", "").strip()
    if env_tok:
        return env_tok
    try:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        tok = ""
    if tok:
        return tok
    tok = "cgw_" + secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # 创建即 0600，没有可读窗口
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    return tok


def colab_python() -> str:
    """colab 入口脚本 shebang 里的解释器；可用 COLAB_PYTHON 覆盖。"""
    if os.environ.get("COLAB_PYTHON"):
        return os.environ["COLAB_PYTHON"]
    with open(COLAB_BIN, "rb") as f:
        first = f.readline().decode("utf-8", "replace").strip()
    if not first.startswith("#!"):
        raise SystemExit(f"{COLAB_BIN} 不是 Python 入口脚本，请设置 COLAB_PYTHON")
    parts = first[2:].split()
    if os.path.basename(parts[0]) == "env":
        found = shutil.which(parts[-1])
        if not found:
            raise SystemExit(f"找不到 {parts[-1]}，请设置 COLAB_PYTHON")
        return found
    return parts[0]


def _outermost(paths: list[str]) -> list[str]:
    """去掉被其他路径包含的路径，保留顺序稳定。"""
    out: list[str] = []
    for p in sorted(set(paths), key=len):
        if not any(p == q or p.startswith(q.rstrip("/") + "/") for q in out):
            out.append(p)
    return out


def _symlink_hops(path: str) -> list[tuple[str, str]]:
    """解析 path 时经过的所有符号链接（链接内容, 链接路径），沙箱里要原样重建。
    例如 uv 的 venv：bin/python -> python/cpython-3.14-linux-x86_64-gnu（链接目录）-> cpython-3.14.10-…"""
    hops: list[tuple[str, str]] = []
    pending = list(Path(path).parts[1:])
    cur = "/"
    seen = 0
    while pending and seen < 64:
        nxt = os.path.join(cur, pending.pop(0))
        if os.path.islink(nxt):
            seen += 1
            target = os.readlink(nxt)
            hops.append((target, nxt))
            pending = list(Path(os.path.join(cur, target)).parts) + pending
            cur = "/"
            if pending and pending[0] == "/":
                pending.pop(0)
        else:
            cur = os.path.normpath(nxt)
    return hops


def prepare_runtime() -> None:
    if not os.path.exists(COLAB_BIN):
        raise SystemExit(f"找不到 colab CLI：{COLAB_BIN}")
    RT.python = colab_python()
    probe = subprocess.run(
        [RT.python, "-E", "-P", "-c", PROBE_SOURCE],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, cwd="/",
    )
    if probe.returncode != 0:
        raise SystemExit(f"无法在 {RT.python} 中导入 colab_cli：\n{probe.stderr}")
    info = json.loads(probe.stdout)
    RT.colab_version = info["version"]
    RT.read_roots = info["roots"]

    if shutil.which("ssh-keygen"):
        if not SSH_KEY.exists():
            SSH_KEY.parent.mkdir(mode=0o700, exist_ok=True)
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "share-my-colab", "-f", str(SSH_KEY)],
                check=True, stdin=subprocess.DEVNULL,
            )
        RT.ssh_identity = str(SSH_KEY)
    else:
        log("警告：没有 ssh-keygen，ssh 子命令不可用")

    # /etc/ssh 也换成空目录：user namespace 里 root 的文件显示为 nobody，OpenSSH 会以
    # “Bad owner or permissions” 拒绝系统配置；colab 已经用 -o 传入 ssh 需要的全部选项。
    hidden = [d for d in ("/home", str(HOME), "/root", "/tmp", "/var/tmp", "/mnt", "/media", "/srv", "/etc/ssh")
              if os.path.isdir(d)]
    RT.hidden_dirs = _outermost(hidden)
    binds = [r for r in RT.read_roots if any(r == h or r.startswith(h + "/") for h in RT.hidden_dirs)]
    if RT.ssh_identity:
        binds.append(str(SSH_KEY.parent))
    RT.sandbox_binds = _outermost(binds)
    hidden_link = lambda link: any(link.startswith(h + "/") for h in RT.hidden_dirs)  # noqa: E731
    RT.sandbox_symlinks = [(t, link) for t, link in _symlink_hops(RT.python) if hidden_link(link)
                           and not any(link == b or link.startswith(b + "/") for b in RT.sandbox_binds)]

    bwrap = shutil.which("bwrap") if SANDBOX_MODE != "off" else None
    if bwrap:
        test = subprocess.run(
            [bwrap, "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
            capture_output=True, stdin=subprocess.DEVNULL,
        )
        if test.returncode == 0:
            RT.bwrap = bwrap
        else:
            log(f"bwrap 不可用：{test.stderr.decode(errors='replace').strip()}")
    if SANDBOX_MODE == "bwrap" and not RT.bwrap:
        raise SystemExit("COLAB_GW_SANDBOX=bwrap，但 bubblewrap 不可用")
    if not RT.bwrap:
        log("警告：未启用 bubblewrap 沙箱，仅依靠参数守卫和 audit hook 两道防线")

    RT.token = load_or_create_gateway_token().encode("utf-8")
    COLAB_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_ROOT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(JOBS_ROOT, 0o700)
    for stale in JOBS_ROOT.iterdir():  # 上次异常退出留下的任务目录
        shutil.rmtree(stale, ignore_errors=True)


# ---------------------------------------------------------------------------
# 运行一个 colab 任务
# ---------------------------------------------------------------------------


class JobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _Capture(threading.Thread):
    """持续读空管道（避免子进程阻塞），内存里只保留开头和结尾各 limit/2 字节。"""

    def __init__(self, stream, limit: int):
        super().__init__(daemon=True)
        self.stream, self.half = stream, max(limit // 2, 1)
        self.head = bytearray()
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_len = 0
        self.dropped = 0

    def run(self) -> None:
        while chunk := self.stream.read1(65536):
            room = self.half - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail.append(chunk)
                self.tail_len += len(chunk)
                while self.tail and self.tail_len - len(self.tail[0]) >= self.half:
                    self.dropped += len(self.tail[0])
                    self.tail_len -= len(self.tail.popleft())
        self.stream.close()

    def value(self) -> tuple[bytes, int]:
        tail = b"".join(self.tail)
        dropped = self.dropped
        if len(tail) > self.half:
            dropped += len(tail) - self.half
            tail = tail[-self.half:]
        if dropped:
            return bytes(self.head) + f"\n…[网关截断了中间 {dropped} 字节]…\n".encode() + tail, dropped
        return bytes(self.head) + tail, 0


def _job_pids(pgid: int) -> list[tuple[int, str]]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        comm = raw[raw.index("(") + 1: raw.rindex(")")]
        fields = raw[raw.rindex(")") + 2:].split()
        if int(fields[2]) == pgid:
            found.append((int(entry), comm))
    return found


def _signal_job(proc: subprocess.Popen, sig: int) -> None:
    """给任务进程组发信号，但跳过 bwrap 自身：bwrap 收到 SIGINT 会立刻退出并连带 SIGKILL 沙箱，
    colab 就来不及收尾（例如 `run` 释放 VM、`exec` 清除 BUSY 状态）。"""
    for pid, comm in _job_pids(proc.pid):
        if comm != "bwrap":
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_job(proc: subprocess.Popen) -> None:
    """SIGINT（colab 走 KeyboardInterrupt → finally 收尾）→ SIGTERM → SIGKILL 整个进程组。
    超时、关闭、请求线程收尾都可能调用；只有第一个调用者发信号，其余的只等待。"""
    with ACTIVE_LOCK:
        owner = proc.pid not in STOPPING
        STOPPING.add(proc.pid)
    if not owner:
        try:
            proc.wait(KILL_GRACE_SEC + 15)
        except subprocess.TimeoutExpired:
            pass
        return
    for sig, grace in ((signal.SIGINT, KILL_GRACE_SEC), (signal.SIGTERM, 5)):
        if proc.poll() is not None:
            break
        _signal_job(proc, sig)
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            pass
    _kill_group(proc)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def _dir_size(root: Path) -> int:
    total = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snap = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = Path(dirpath, n)
            st = p.lstat()
            snap[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return snap


def _collect_outputs(root: Path, before: dict[str, tuple[int, int]]) -> tuple[dict[str, str], list[dict]]:
    files: dict[str, str] = {}
    skipped: list[dict] = []
    total = 0
    for rel, _ in sorted(_snapshot(root).items()):
        p = root / rel
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode) or before.get(rel) == (st.st_size, st.st_mtime_ns):
            continue
        if total + st.st_size > MAX_RETURN_BYTES:
            skipped.append({"path": rel, "size": st.st_size,
                            "reason": f"超过回传总量上限 {MAX_RETURN_BYTES // MiB} MiB（COLAB_GW_MAX_RETURN_BYTES）"})
            continue
        files[rel] = base64.b64encode(p.read_bytes()).decode("ascii")
        total += st.st_size
    return files, skipped


def safe_relpath(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if (
        not rel
        or "\x00" in rel
        or "\\" in rel
        or p.is_absolute()
        or any(part in ("", ".", "..") for part in rel.split("/"))
        or len(rel) > 1024
    ):
        raise JobError(HTTPStatus.BAD_REQUEST, f"非法的上传文件路径：{rel!r}（只能是不含 .. 的相对路径）")
    return p


def _clean_output(data: bytes, work: Path, is_stderr: bool) -> bytes:
    data = data.replace(str(work).encode() + b"/", b"").replace(str(work).encode(), b".")
    data = _SECRET_RE.sub(lambda m: next(g for g in m.groups() if g is not None) + b"REDACTED", data)
    if is_stderr:
        data = b"".join(line for line in data.splitlines(keepends=True) if not line.startswith(_KEYRING_NOISE))
    return data


def _child_env(work: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(
        HOME=str(HOME),
        TMPDIR=str(work / JOB_TMP_SUBDIR),
        # colab 的 ssh 会用 `python -m colab_cli.cli` 做 ProxyCommand，-m 默认把当前目录（任务目录，
        # 里面是调用方上传的文件）放到 sys.path 最前面；不加这一项，上传一个 typer.py 就能在宿主机执行代码。
        PYTHONSAFEPATH="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
    )
    return env


def _sandbox_prefix(work: Path) -> list[str]:
    if not RT.bwrap:
        return []
    cmd = [RT.bwrap, "--die-with-parent", "--unshare-pid", "--unshare-ipc",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for d in RT.hidden_dirs:
        cmd += ["--tmpfs", d]
    for d in RT.sandbox_binds:
        cmd += ["--ro-bind", d, d]
    for target, link in RT.sandbox_symlinks:
        cmd += ["--symlink", target, link]
    cmd += ["--bind", str(COLAB_CONFIG_DIR), str(COLAB_CONFIG_DIR), "--bind", str(work), str(work),
            "--chdir", str(work), "--"]
    return cmd


def _mask_args(args: list[str]) -> str:
    """审计日志里隐藏 --env 的值。"""
    out, hide_next = [], False
    for a in args:
        if hide_next:
            out.append(a.split("=", 1)[0] + "=***")
            hide_next = False
        elif a == "--env":
            out.append(a)
            hide_next = True
        elif a.startswith("--env="):
            out.append("--env=" + a[6:].split("=", 1)[0] + "=***")
        else:
            out.append(a)
    text = " ".join(out)
    return text if len(text) <= 300 else text[:300] + "…"


def run_colab(
    args: list[str],
    files_b64: dict[str, str] | None = None,
    stdin: bytes | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    client: str = "-",
) -> dict[str, Any]:
    """在一次性任务目录里执行 `colab <args>`，返回结构化结果；被拒绝时抛 JobError。"""
    first = args[0] if args else None
    if first is not None and first not in ALLOWED_COMMANDS and first not in ("-h", "--help"):
        raise JobError(
            HTTPStatus.FORBIDDEN,
            f"网关不开放 {first!r}（子命令必须写在最前面，且不能带全局参数）。可用：{', '.join(sorted(ALLOWED_COMMANDS))}",
        )
    if first == "ssh":
        if not RT.ssh_identity:
            raise JobError(HTTPStatus.SERVICE_UNAVAILABLE, "服务器没有 ssh-keygen，ssh 不可用")
        args = ["ssh", "-i", RT.ssh_identity, *args[1:]]  # 调用方再写 -i 会覆盖它，然后被守卫拒绝
    if SHUTTING_DOWN.is_set():
        raise JobError(HTTPStatus.SERVICE_UNAVAILABLE, "网关正在关闭")
    if not JOB_SEMAPHORE.acquire(blocking=False):
        raise JobError(HTTPStatus.TOO_MANY_REQUESTS, f"已有 {MAX_CONCURRENT_JOBS} 个任务在运行，请稍后重试")

    proc: subprocess.Popen | None = None
    verdict_r: int | None = None
    job_dir: Path | None = None
    start = time.monotonic()
    try:
        job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=JOBS_ROOT))
        work = job_dir / "work"
        (work / JOB_TMP_SUBDIR).mkdir(parents=True)
        for rel, b64 in (files_b64 or {}).items():
            target = work / safe_relpath(rel)
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                raise JobError(HTTPStatus.BAD_REQUEST, f"文件 {rel!r} 不是合法的 base64")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        before = _snapshot(work)

        verdict_r, verdict_w = os.pipe()
        policy = {
            "verdict_fd": verdict_w,
            "allowed": sorted(ALLOWED_COMMANDS),
            "forbidden": FORBIDDEN_PARAMS,
            "local_paths": LOCAL_PATH_PARAMS,
            "session_re": SESSION_RE,
            "ssh_identity": RT.ssh_identity,
            "has_stdin": stdin is not None,
            "config_dir": str(COLAB_CONFIG_DIR),
            "read_roots": [str(SSH_KEY.parent)] if RT.ssh_identity else [],
        }
        cmd = [*_sandbox_prefix(work), RT.python, "-E", "-P", "-B", "-X", "utf8", "-c", GUARD_SOURCE,
               json.dumps(policy), *args]
        try:
            proc = subprocess.Popen(
                cmd, cwd=work, env=_child_env(work), pass_fds=(verdict_w,), start_new_session=True,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        finally:
            os.close(verdict_w)
        with ACTIVE_LOCK:
            ACTIVE_JOBS.add(proc)
        out_cap, err_cap = _Capture(proc.stdout, MAX_OUTPUT_BYTES), _Capture(proc.stderr, MAX_OUTPUT_BYTES)
        verdict_cap = _Capture(os.fdopen(verdict_r, "rb"), 1 * MiB)  # 并发读，守卫写多少都不会阻塞
        verdict_r = None
        for cap in (out_cap, err_cap, verdict_cap):
            cap.start()
        with ACTIVE_LOCK:  # 关闭信号恰好在启动期间到达时，main() 的快照里没有这个任务，由自己收尾
            late = SHUTTING_DOWN.is_set()
        if late:
            _stop_job(proc)
        if stdin is not None and proc.stdin is not None:
            def _feed(pipe=proc.stdin) -> None:
                try:
                    pipe.write(stdin)
                except OSError:  # 子进程不读 stdin 就退出了
                    pass
                finally:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            threading.Thread(target=_feed, daemon=True).start()

        killed_reason = None
        deadline = start + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                killed_reason = "timeout"
            else:
                try:
                    proc.wait(timeout=min(2.0, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
                if _dir_size(work) > MAX_WORKDIR_BYTES:
                    killed_reason = "workdir_limit"
            if killed_reason:
                _stop_job(proc)
                break
        _kill_group(proc)  # 清掉 colab 退出后可能残留的子进程（非沙箱模式下尤其重要）
        out_cap.join(10)
        err_cap.join(10)

        verdict_cap.join(10)
        raw_verdict = verdict_cap.value()[0]
        try:
            guard = json.loads(raw_verdict) if raw_verdict else None
        except ValueError:
            guard = None
        if guard is not None and not guard.get("ok"):
            log(f"拒绝 {client}: colab {_mask_args(args)} -> {guard.get('error')}")
            raise JobError(HTTPStatus.FORBIDDEN, guard.get("error") or "被网关策略拒绝")

        stdout, out_dropped = out_cap.value()
        stderr, err_dropped = err_cap.value()
        stdout, stderr = _clean_output(stdout, work, False), _clean_output(stderr, work, True)
        if guard is None:
            log(f"守卫异常 {client}: colab {_mask_args(args)}\n{stderr.decode(errors='replace')}")
            raise JobError(HTTPStatus.INTERNAL_SERVER_ERROR, "网关守卫未能启动 colab，请查看服务器日志")
        if killed_reason == "timeout":
            stderr += f"\n[gateway] 超过 {timeout_sec} 秒，已中断（已先发 SIGINT 让 colab 收尾）。".encode()
            if first in ("exec", "repl"):
                stderr += ("\n[gateway] 注意：远端 kernel 可能仍在执行这段代码，之后的 exec 会排队等它结束；"
                           "要立刻复用请先 restart-kernel -s <名字>（会清空 kernel 里的变量）。").encode()
        elif killed_reason == "workdir_limit":
            stderr += f"\n[gateway] 任务目录超过 {MAX_WORKDIR_BYTES // MiB} MiB，已中断。".encode()
        out_files, skipped = _collect_outputs(work, before)

        exit_code = 124 if killed_reason == "timeout" else proc.returncode
        result: dict[str, Any] = {
            "ok": exit_code == 0 and killed_reason is None,
            "exit_code": exit_code,
            "command": first,
            "duration_sec": round(time.monotonic() - start, 2),
            "timed_out": killed_reason == "timeout",
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
            "files": out_files,
        }
        try:
            stdout.decode("utf-8")
        except UnicodeDecodeError:
            result["stdout_b64"] = base64.b64encode(stdout).decode("ascii")  # 二进制输出按字节回传
        if killed_reason:
            result["killed_reason"] = killed_reason
        if out_dropped or err_dropped:
            result["truncated_bytes"] = {"stdout": out_dropped, "stderr": err_dropped}
        if skipped:
            result["skipped_files"] = skipped
        log(f"{client}: colab {_mask_args(args)} -> exit={exit_code} {result['duration_sec']}s"
            + (f" ({killed_reason})" if killed_reason else ""))
        return result
    finally:
        if verdict_r is not None:
            os.close(verdict_r)
        if proc is not None:
            if proc.poll() is None:
                _stop_job(proc)
            with ACTIVE_LOCK:
                ACTIVE_JOBS.discard(proc)
                STOPPING.discard(proc.pid)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
        JOB_SEMAPHORE.release()


def parse_usage_output(raw: str) -> dict[str, Any]:
    """解析 `colab usage` 的输出。"""
    def grab(pattern: str, cast):
        m = re.search(pattern, raw)
        return cast(m.group(1)) if m else None

    return {
        "balance_ccu": grab(r"Current balance:\s*([0-9.]+)\s*compute units", float),
        "usage_rate_per_hr": grab(r"Usage rate:\s*([0-9.]+)/hr", float),
        "active_assignments": grab(r"Active assignments:\s*(\d+)", int),
        "raw": raw.strip(),
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>share-my-colab · 受控 Colab 算力网关</title>
  <style>
    :root { --bg:#f8fafc; --card:#fff; --text:#0f172a; --muted:#475569; --primary:#2563eb; --primary-h:#1d4ed8; --border:#e2e8f0; }
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg); color: var(--text); max-width: 920px; margin: 0 auto; padding: 24px 16px 48px; line-height: 1.6; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 20px 22px;
      margin-bottom: 16px; box-shadow: 0 2px 10px rgba(15,23,42,.04); }
    .hero { background: linear-gradient(135deg, #fff 0%, #f1f5ff 100%); }
    .head-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
    h1 { margin: 0; font-size: 1.45rem; }
    .subtitle { margin: 2px 0 0; font-size: .95rem; font-weight: 600; color: #334155; }
    h2 { margin: 0 0 8px; font-size: 1.08rem; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .sub { color: var(--muted); font-size: .92rem; margin: 10px 0 0; }
    .pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .pill { font-size: .8rem; background: #fff; border: 1px solid var(--border); border-radius: 999px; padding: 4px 11px; color: #334155; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
    .token-box { display: flex; gap: 8px; align-items: center; }
    label { font-size: .88rem; font-weight: 600; display: block; margin-bottom: 5px; }
    input, select, textarea { width: 100%; padding: 9px 12px; border: 1px solid var(--border); border-radius: 10px;
      font-size: .93rem; font-family: inherit; background: #fff; color: var(--text); }
    textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; min-height: 140px; line-height: 1.5; }
    button, .btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; background: var(--primary);
      color: #fff; border: 1px solid transparent; border-radius: 999px; padding: 9px 18px; font-size: .9rem;
      font-weight: 600; cursor: pointer; text-decoration: none; white-space: nowrap; }
    button:hover, .btn:hover { background: var(--primary-h); }
    button.secondary, .btn.secondary { background: #fff; color: var(--text); border-color: var(--border); }
    button.secondary:hover, .btn.secondary:hover { background: #f8fafc; }
    button.sm { padding: 6px 13px; font-size: .83rem; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    pre { background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 12px; overflow-x: auto;
      font-size: .84rem; line-height: 1.55; white-space: pre-wrap; word-break: break-word; margin: 10px 0 0; }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88em; background: #f1f5f9; padding: 1px 5px; border-radius: 5px; }
    .badge { display: inline-block; padding: 4px 11px; border-radius: 999px; font-size: .8rem; font-weight: 600; }
    .badge.info { background: #dbeafe; color: #1d4ed8; } .badge.ok { background: #dcfce7; color: #166534; }
    .badge.warn { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; } .badge.err { background: #fee2e2; color: #991b1b; }
    .tag { font-size: .76rem; font-weight: 600; padding: 2px 9px; border-radius: 999px; background: #eff6ff; color: #1d4ed8; }
    .hint { margin: 10px 0 0; font-size: .89rem; color: var(--muted); }
    .check-inline { display: inline-flex; align-items: center; gap: 7px; font-size: .88rem; cursor: pointer; font-weight: 500; margin: 0; }
    .check-inline input { width: auto; margin: 0; accent-color: var(--primary); }
    .bar-between { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .sub-details { border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px; background: #f8fafc; margin-top: 12px; }
    details.card { padding: 16px 22px; }
    summary { font-weight: 600; font-size: .92rem; cursor: pointer; color: var(--text); }
    details ul { margin: 10px 0 0; padding-left: 18px; font-size: .88rem; color: var(--muted); }
    details li + li { margin-top: 6px; }
  </style>
</head>
<body>
  <div class="card hero">
    <div class="head-row">
      <div>
        <h1>⚡ share-my-colab</h1>
        <p class="subtitle">受控 Colab 算力共享网关 · 把主账号 GPU 安全分给家人、团队与 AI Agent</p>
      </div>
      <span id="healthBadge" class="badge info">检查网关状态中...</span>
    </div>
    <p class="sub">本网关将主账号的 Google Colab GPU 算力安全共享给家庭组或团队成员使用，绝不暴露 Google OAuth 凭证、个人 Google Drive、账号邮箱或宿主机文件。日常研发、多文件同步与持久化运行请交给 AI Agent（Claude Code、Codex、Cursor、Antigravity 等）或 <code>client.py</code> 命令行，本网页仅作为接入引导与快速连通性测试。</p>
    <div class="pills">
      <span class="pill">🔒 三重隔离保护主账号隐私</span>
      <span class="pill">🤖 专为 AI Agent 与 CLI 设计</span>
      <span class="pill">🤝 共享算力池（持久会话加个人前缀、用完即停）</span>
    </div>
  </div>

  <div class="card">
    <h2>🔑 第一步：填入访问密钥与查看算力状态</h2>
    <div class="row">
      <div style="flex:1; min-width:250px;">
        <label for="tokenInput">网关访问密钥</label>
        <div class="token-box">
          <input id="tokenInput" type="password" autocomplete="off" placeholder="粘贴管理员发给你的 cgw_... 密钥" />
          <button class="secondary sm" type="button" id="toggleTokenBtn">显示</button>
        </div>
      </div>
      <div style="align-self:flex-end; display:flex; gap:8px; flex-wrap:wrap;">
        <button class="secondary" type="button" id="usageBtn">查询剩余算力</button>
        <button class="secondary" type="button" id="sessionsBtn">查看运行中机器</button>
      </div>
    </div>
    <p id="usageText" class="hint">输入密钥后可查询剩余 CCU 余额与当前运行中的 Colab 机器（密钥仅保存在本机浏览器 localStorage）。</p>
    <pre id="sessionsBox" style="display:none;"></pre>
  </div>

  <div class="card">
    <h2>🤖 第二步：交给 AI Agent 或命令行使用 <span class="tag">推荐主力用法</span></h2>
    <p class="sub" style="margin-top:0;">把客户端脚本与使用规范交给本地 AI Agent，或在终端直接使用 <code>client.py</code>（支持本地文件自动上传、产物取回与 <code>--json</code> 结构化输出）。</p>
    <div class="row">
      <a class="btn secondary" href="client.py" download>⬇ 下载 client.py</a>
      <a class="btn secondary" href="SKILL.md" download>⬇ 下载 SKILL.md</a>
    </div>
    <div class="bar-between">
      <label class="check-inline">
        <input id="includeTokenCheck" type="checkbox" />
        <span>复制内容中带入上方已填密钥（仅发给受信任的本地 Agent）</span>
      </label>
      <button class="sm" type="button" id="copyPromptBtn">📋 复制给 AI Agent</button>
    </div>
    <pre id="agentPromptBox"></pre>
    <details class="sub-details">
      <summary>💻 命令行快速示例（client.py 常用命令）</summary>
      <div class="bar-between">
        <span class="hint" style="margin:0;">提示：<code>--json</code>、<code>--timeout</code>、<code>--url</code> 必须写在子命令前面。</span>
        <button class="secondary sm" type="button" id="copyCliBtn">📋 复制命令示例</button>
      </div>
      <pre id="cliSnippetBox"></pre>
    </details>
  </div>

  <div class="card">
    <h2>🧪 第三步：在线连通性测试（一次性运行并自动释放）</h2>
    <p class="sub" style="margin-top:0;">仅用于验证密钥有效性与云端 GPU 分配是否正常（自动开机 → 运行下方代码 → 立即释放机器）。多文件项目、依赖安装与持久交互请使用上方的 Agent / <code>client.py</code>。</p>
    <div class="row">
      <div style="flex:1; min-width:160px;">
        <label for="gpuSelect">硬件（跑完即释放）</label>
        <select id="gpuSelect">
          <option value="T4">T4 GPU（省额度）</option>
          <option value="L4">L4 GPU</option>
          <option value="G4">G4 GPU</option>
          <option value="A100">A100 GPU</option>
          <option value="H100">H100 GPU</option>
          <option value="CPU">仅 CPU</option>
        </select>
      </div>
      <div style="flex:1; min-width:140px;">
        <label for="timeoutInput">最长运行时间（秒）</label>
        <input id="timeoutInput" type="number" value="600" min="30" max="3600" />
      </div>
      <div style="padding-top:24px;">
        <label class="check-inline"><input id="highMemCheck" type="checkbox" /><span>高内存（High-RAM）</span></label>
      </div>
    </div>
    <div style="margin-top:12px;">
      <label for="codeArea">Python 脚本</label>
      <textarea id="codeArea">import torch
print("CUDA available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0))</textarea>
    </div>
    <div class="row">
      <button type="button" id="runBtn">🚀 开机运行并自动释放</button>
      <span id="statusBadge" class="badge info" style="display:none;"></span>
    </div>
    <pre id="outputBox">等待提交测试任务...</pre>
  </div>

  <details class="card">
    <summary>📖 安全边界、开放/禁止命令与共享须知</summary>
    <ul>
      <li><strong>开放命令白名单：</strong><code>usage</code>、<code>sessions</code>、<code>status</code>、<code>run</code>、<code>new</code>、<code>exec</code>、<code>repl</code>、<code>restart-kernel</code>、<code>ssh</code>、<code>ls</code>、<code>upload</code>、<code>download</code>、<code>rm</code>、<code>install</code>、<code>log</code>、<code>url</code>、<code>stop</code>、<code>help</code>、<code>readme</code>、<code>skill</code>、<code>version</code>（非交互 <code>ssh</code> 请使用 <code>ssh -s NAME -- CMD &lt;/dev/null</code>）。</li>
      <li><strong>禁止命令与选项：</strong>刻意屏蔽 <code>auth</code>（防止把宿主机 GCP 凭证注入云端 VM）、<code>drivemount</code>（防止挂载个人 Google Drive）、<code>whoami</code>（防止泄露账号邮箱与权限范围）、<code>update</code>（防止修改宿主机程序）、<code>edit</code>（依赖宿主机编辑器）、<code>pay</code>（打开浏览器），以及 <code>--auth</code>、<code>-c/--config</code> 等全局覆盖参数；底层由参数守卫、audit hook 与 bubblewrap 沙箱三重隔离。</li>
      <li><strong>共享算力池礼仪：</strong>所有密钥持有人共享同一账号的 CCU 余额与机器池。一次性脚本优先用 <code>run</code>；持久会话请加个人前缀（如 <code>-s alice-dev</code>），切勿干扰他人会话，用完务必执行 <code>stop</code>。</li>
      <li><strong>64 MiB 请求体上限：</strong>单次请求与回传文件上限均为 64 MiB。大数据集或模型权重请直接在云端下载，例如：<code>python3 client.py ssh -s &lt;名字&gt;-dev -- wget -q &lt;URL&gt; -O /content/data.tar.gz &lt;/dev/null</code>。</li>
    </ul>
  </details>

  <script>
    const $ = (id) => document.getElementById(id);
    const baseUrl = (location.origin + location.pathname).replace(/\/index\.html$/, '').replace(/\/+$/, '');
    const healthBadge = $('healthBadge'), tokenInput = $('tokenInput'), toggleTokenBtn = $('toggleTokenBtn');
    const usageBtn = $('usageBtn'), sessionsBtn = $('sessionsBtn'), usageText = $('usageText'), sessionsBox = $('sessionsBox');
    const includeTokenCheck = $('includeTokenCheck'), agentPromptBox = $('agentPromptBox'), cliSnippetBox = $('cliSnippetBox');
    const copyPromptBtn = $('copyPromptBtn'), copyCliBtn = $('copyCliBtn');
    const runBtn = $('runBtn'), statusBadge = $('statusBadge'), outputBox = $('outputBox');
    tokenInput.value = localStorage.getItem('colab_gw_token') || '';

    function renderSnippets() {
      const typed = tokenInput.value.trim();
      const tok = (includeTokenCheck.checked && typed) ? typed : '<cgw_你的密钥>';
      agentPromptBox.textContent = [
        '请按以下步骤接入并使用 share-my-colab（受控 Colab 算力网关）：',
        '1. 下载客户端与 Agent 使用规范到当前工作目录：',
        `   curl -fsSL -O ${baseUrl}/client.py -O ${baseUrl}/SKILL.md`,
        '2. 配置环境变量：',
        `   export COLAB_GW_URL="${baseUrl}"`,
        `   export COLAB_GW_TOKEN="${tok}"`,
        '3. 先阅读 SKILL.md，再运行自检命令确认算力余额与运行中机器：',
        '   python3 client.py --json usage',
        '   python3 client.py --json sessions',
        '4. 使用约定：一次性脚本优先用 `python3 client.py run --gpu T4 your_script.py`（自动开机并在结束时释放）；持久环境请使用 `-s <名字>-dev` 加个人前缀，用完务必执行 `python3 client.py stop -s <名字>-dev`。',
      ].join('\n');
      cliSnippetBox.textContent = [
        `export COLAB_GW_URL="${baseUrl}"`,
        `export COLAB_GW_TOKEN="${tok}"`,
        '',
        'python3 client.py usage                               # 查看剩余 CCU 算力',
        'python3 client.py --json sessions                     # 结构化查看运行中机器',
        'python3 client.py run --gpu T4 train.py               # 一次性开机→运行→自动释放',
        'python3 client.py new -s <名字>-dev --gpu T4          # 创建持久会话（加个人前缀）',
        'python3 client.py exec -s <名字>-dev -f train.py      # 自动上传本地脚本并执行',
        'python3 client.py ssh -s <名字>-dev -- nvidia-smi </dev/null  # 单次非交互远程命令',
        'python3 client.py download -s <名字>-dev /content/out.pt ./   # 拉取云端文件到本地',
        'python3 client.py stop -s <名字>-dev                  # 用完务必停机释放 CCU',
      ].join('\n');
    }

    async function copyText(text, btn) {
      let ok = false;
      if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {}
      }
      if (!ok) {
        const ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(ta); ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        document.body.removeChild(ta);
      }
      const orig = btn.dataset.origLabel || btn.textContent;
      btn.dataset.origLabel = orig;
      btn.textContent = ok ? '✅ 已复制' : '❌ 复制失败，请手动选中';
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(() => { btn.textContent = orig; }, 1800);
    }

    async function api(path, body) {
      const opts = { headers: { 'Authorization': 'Bearer ' + tokenInput.value.trim() } };
      if (body !== undefined) {
        opts.method = 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body);
      }
      const res = await fetch(path, opts);
      let data;
      try { data = await res.json(); } catch (_) { throw new Error('HTTP ' + res.status + '（网关没有返回 JSON）'); }
      if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    }

    const joinOutput = (d) => [d.stdout, d.stderr ? '[STDERR]\n' + d.stderr : ''].filter(Boolean).join('\n\n') || '(无输出)';
    function requireToken(target) {
      if (tokenInput.value.trim()) return true;
      target.textContent = '请先在上方填入网关访问密钥（cgw_...）。';
      tokenInput.focus();
      return false;
    }

    async function loadHealth() {
      try {
        const res = await fetch('api/health');
        const d = await res.json();
        if (!res.ok || !d.ok) throw new Error('HTTP ' + res.status);
        const ver = d.colab_cli_version || '?';
        const warn = d.sandbox === 'none';
        healthBadge.className = warn ? 'badge warn' : 'badge ok';
        healthBadge.textContent = `在线 · colab-cli ${ver} · ` + (warn ? '⚠️ bwrap 沙箱未启用' : 'bwrap 沙箱已启用');
      } catch (_) {
        healthBadge.className = 'badge err'; healthBadge.textContent = '网关状态获取失败';
      }
    }

    function syncToken() { localStorage.setItem('colab_gw_token', tokenInput.value.trim()); renderSnippets(); }
    tokenInput.addEventListener('input', syncToken);
    tokenInput.addEventListener('change', syncToken);
    includeTokenCheck.addEventListener('change', renderSnippets);
    toggleTokenBtn.addEventListener('click', () => {
      const show = tokenInput.type === 'password';
      tokenInput.type = show ? 'text' : 'password';
      toggleTokenBtn.textContent = show ? '隐藏' : '显示';
    });
    copyPromptBtn.addEventListener('click', () => copyText(agentPromptBox.textContent, copyPromptBtn));
    copyCliBtn.addEventListener('click', () => copyText(cliSnippetBox.textContent, copyCliBtn));

    usageBtn.addEventListener('click', async () => {
      if (!requireToken(usageText)) return;
      usageBtn.disabled = true; usageText.textContent = '查询中...';
      try {
        const d = await api('api/usage');
        usageText.textContent = `当前余额：${d.balance_ccu ?? '-'} CCU ｜ 消耗速率：${d.usage_rate_per_hr ?? '-'}/hr ｜ 运行中机器：${d.active_assignments ?? '-'} 台`;
      } catch (e) { usageText.textContent = '查询失败：' + e.message; }
      finally { usageBtn.disabled = false; }
    });

    sessionsBtn.addEventListener('click', async () => {
      sessionsBox.style.display = 'block';
      if (!requireToken(sessionsBox)) return;
      sessionsBtn.disabled = true; sessionsBox.textContent = '正在查询运行中机器...';
      try { sessionsBox.textContent = joinOutput(await api('api/cli', { args: ['sessions'], timeout: 60 })); }
      catch (e) { sessionsBox.textContent = '查询失败：' + e.message; }
      finally { sessionsBtn.disabled = false; }
    });

    runBtn.addEventListener('click', async () => {
      statusBadge.style.display = 'inline-block';
      if (!requireToken(outputBox)) {
        statusBadge.className = 'badge err'; statusBadge.textContent = '❌ 缺少密钥'; return;
      }
      runBtn.disabled = true; statusBadge.className = 'badge info';
      statusBadge.textContent = '正在申请 Colab 虚拟机并运行...';
      outputBox.textContent = '正在启动云端机器并执行脚本，请耐心等待...';
      try {
        const timeoutVal = parseInt($('timeoutInput').value || '600', 10);
        const d = await api('api/run', {
          code: $('codeArea').value, gpu: $('gpuSelect').value, high_mem: $('highMemCheck').checked,
          timeout: Number.isFinite(timeoutVal) ? timeoutVal : 600,
        });
        statusBadge.className = d.ok ? 'badge ok' : (d.timed_out ? 'badge warn' : 'badge err');
        statusBadge.textContent = d.ok ? `✅ 完成 (${d.duration_sec}s · 已释放)`
          : d.timed_out ? `⏱ 超时 (${d.duration_sec}s)` : `❌ 退出码 ${d.exit_code} (${d.duration_sec}s)`;
        outputBox.textContent = joinOutput(d);
      } catch (e) {
        statusBadge.className = 'badge err'; statusBadge.textContent = '❌ 请求失败'; outputBox.textContent = e.message;
      } finally { runBtn.disabled = false; }
    });

    renderSnippets();
    loadHealth();
  </script>
</body>
</html>
"""

HTML_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ShareMyColab/3"
    sys_version = ""
    timeout = 120  # 读写套接字的空闲超时，防止慢速客户端占住线程；不限制任务本身的运行时间

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self._client()} - {format % args}")

    def _client(self) -> str:
        fwd = self.headers.get("X-Forwarded-For") if hasattr(self, "headers") and self.headers else None
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    # -- 响应 --------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True  # 客户端已断开（例如 Agent 先超时了），任务结果只能丢弃

    def _json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self._json(status, {"ok": False, "error": message}, headers)

    # -- 请求 --------------------------------------------------------------
    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        scheme, _, provided = auth.partition(" ")
        if scheme.lower() != "bearer" or not provided.strip():
            self._error(HTTPStatus.UNAUTHORIZED, "缺少 Authorization: Bearer <网关密钥>", {"WWW-Authenticate": "Bearer"})
            return False
        # 头部按 latin-1 解码，可能含非 ASCII；compare_digest 对这种 str 会抛 TypeError，所以比较字节。
        if not hmac.compare_digest(provided.strip().encode("utf-8", "surrogateescape"), RT.token):
            self._error(HTTPStatus.UNAUTHORIZED, "网关密钥无效", {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self._error(HTTPStatus.LENGTH_REQUIRED, "不支持 chunked 请求体，请带 Content-Length")
            return None
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length")
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        f"请求体 {length // MiB} MiB 超过上限 {MAX_BODY_BYTES // MiB} MiB（COLAB_GW_MAX_BODY_BYTES）")
            return None
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "请求体为空")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            return None
        try:
            req = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON")
            return None
        if not isinstance(req, dict):
            self._error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")
            return None
        return req

    @staticmethod
    def _timeout(req: dict[str, Any]) -> int:
        try:
            value = int(req.get("timeout", DEFAULT_TIMEOUT_SEC))
        except (TypeError, ValueError):
            value = DEFAULT_TIMEOUT_SEC
        return max(10, min(value, MAX_TIMEOUT_SEC))

    def _run(self, args: list[str], **kw: Any) -> dict[str, Any] | None:
        try:
            return run_colab(args, client=self._client(), **kw)
        except JobError as e:
            headers = {"Retry-After": "30"} if e.status == HTTPStatus.TOO_MANY_REQUESTS else None
            self._error(e.status, str(e), headers)
            return None

    def _dispatch(self, method) -> None:
        with ACTIVE_LOCK:
            PENDING_RESPONSES[0] += 1
        try:
            method()
        except Exception:
            log(f"处理 {self.command} {self.path} 时出错：\n{traceback.format_exc()}")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "网关内部错误，请查看服务器日志")
        finally:
            with ACTIVE_LOCK:
                PENDING_RESPONSES[0] -= 1

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_HEAD(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def _get(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy": HTML_CSP, "X-Frame-Options": "DENY"})
        elif path == "/client.py":
            self._send(HTTPStatus.OK, CLIENT_FILE.read_bytes(), "text/x-python; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="client.py"'})
        elif path == "/SKILL.md" and SKILL_FILE.is_file():
            self._send(HTTPStatus.OK, SKILL_FILE.read_bytes(), "text/markdown; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="SKILL.md"'})
        elif path == "/api/health":
            self._json(HTTPStatus.OK, {
                "ok": True, "service": "share-my-colab", "version": 3, "colab_cli_version": RT.colab_version,
                "sandbox": "bwrap" if RT.bwrap else "none", "commands": sorted(ALLOWED_COMMANDS),
                "limits": {"max_body_bytes": MAX_BODY_BYTES, "max_return_bytes": MAX_RETURN_BYTES,
                           "max_output_bytes": MAX_OUTPUT_BYTES, "default_timeout": DEFAULT_TIMEOUT_SEC,
                           "max_timeout": MAX_TIMEOUT_SEC, "max_concurrent": MAX_CONCURRENT_JOBS},
            })
        elif path in ("/api/usage", "/api/skill"):
            if self.command == "HEAD":  # 不为 HEAD 真的跑一次 colab
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "application/json", {"Allow": "GET"})
                return
            if not self._authorized():
                return
            res = self._run(["usage" if path == "/api/usage" else "skill"], timeout_sec=60)
            if res is None:
                return
            if not res["ok"]:
                self._json(HTTPStatus.BAD_GATEWAY, {**res, "error": res["stderr"].strip() or "colab 执行失败"})
            elif path == "/api/usage":
                self._json(HTTPStatus.OK, {"ok": True, **parse_usage_output(res["stdout"])})
            else:
                self._json(HTTPStatus.OK, res)
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    def _post(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/api/cli", "/api/run"):
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self._authorized():
            return
        req = self._read_json()
        if req is None:
            return
        if path == "/api/cli":
            self._post_cli(req)
        else:
            self._post_run(req)

    def _post_cli(self, req: dict[str, Any]) -> None:
        args = req.get("args")
        if (
            not isinstance(args, list)
            or len(args) > 512
            or not all(isinstance(a, str) and "\x00" not in a and len(a) <= 65536 for a in args)
        ):
            self._error(HTTPStatus.BAD_REQUEST, "args 必须是字符串数组（不含 NUL，最多 512 项）")
            return
        files = req.get("files")
        if files is not None and (
            not isinstance(files, dict) or len(files) > 256
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
        ):
            self._error(HTTPStatus.BAD_REQUEST, "files 必须是 {相对路径: base64} 对象（最多 256 个）")
            return
        stdin: bytes | None = None
        if req.get("stdin_b64") is not None:
            try:
                stdin = base64.b64decode(req["stdin_b64"], validate=True)
            except (binascii.Error, ValueError, TypeError):
                self._error(HTTPStatus.BAD_REQUEST, "stdin_b64 不是合法的 base64")
                return
        elif req.get("stdin") is not None:
            if not isinstance(req["stdin"], str):
                self._error(HTTPStatus.BAD_REQUEST, "stdin 必须是字符串")
                return
            stdin = req["stdin"].encode("utf-8")
        res = self._run(args, files_b64=files, stdin=stdin, timeout_sec=self._timeout(req))
        if res is not None:
            self._json(HTTPStatus.OK, res)

    def _post_run(self, req: dict[str, Any]) -> None:
        code = req.get("code")
        if not isinstance(code, str) or not code.strip():
            self._error(HTTPStatus.BAD_REQUEST, "code 不能为空")
            return
        gpu = str(req.get("gpu") or "CPU").upper().strip()
        if gpu not in {"CPU", "T4", "L4", "G4", "A100", "H100"}:
            self._error(HTTPStatus.BAD_REQUEST, f"不支持的 GPU 类型：{gpu}")
            return
        high_mem = req.get("high_mem") is True
        timeout_sec = self._timeout(req)
        # colab run 的 --timeout 是“多久没有输出就失败”；网关的总时限另加 180 秒给开机与释放。
        args = ["run", "-s", "gw-" + secrets.token_hex(4), "--timeout", str(timeout_sec)]
        if gpu != "CPU":
            args += ["--gpu", gpu]
        if high_mem:
            args.append("--high-mem")
        args.append("main.py")
        files = {"main.py": base64.b64encode(code.encode("utf-8")).decode("ascii")}
        res = self._run(args, files_b64=files, timeout_sec=min(timeout_sec + 180, MAX_TIMEOUT_SEC + 180))
        if res is not None:
            self._json(HTTPStatus.OK, {**res, "gpu": gpu, "high_mem": high_mem})


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True  # 关闭时由 main() 有限期地等待在途响应，慢速客户端拖不住退出
    allow_reuse_address = True
    request_queue_size = 64


def main() -> None:
    prepare_runtime()
    server = GatewayServer((HOST, PORT), GatewayHandler)

    def _on_signal(signum: int, _frame: Any) -> None:
        if not SHUTTING_DOWN.is_set():
            SHUTTING_DOWN.set()
            log(f"收到 {signal.Signals(signum).name}，停止接收新请求")
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log(f"share-my-colab v3 已启动：http://{HOST}:{PORT}  colab-cli {RT.colab_version}  "
        f"沙箱：{'bwrap' if RT.bwrap else '无'}  并发上限：{MAX_CONCURRENT_JOBS}")
    log(f"网关密钥：{'环境变量 COLAB_GW_TOKEN' if os.environ.get('COLAB_GW_TOKEN') else TOKEN_FILE}")
    server.serve_forever()

    # 让正在运行的任务体面收尾（run 会释放 VM），再把结果发回客户端；整体有上限，不会无限等待。
    with ACTIVE_LOCK:
        procs = list(ACTIVE_JOBS)
    if procs:
        log(f"等待 {len(procs)} 个任务收尾（最多 {KILL_GRACE_SEC} 秒）")
        stoppers = [threading.Thread(target=_stop_job, args=(p,), daemon=True) for p in procs]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join(KILL_GRACE_SEC + 15)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with ACTIVE_LOCK:
            if PENDING_RESPONSES[0] == 0:
                break
        time.sleep(0.2)
    server.server_close()
    log("网关已关闭")


if __name__ == "__main__":
    main()
