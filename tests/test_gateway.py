from __future__ import annotations

import base64
import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client  # noqa: E402
import server  # noqa: E402


TEST_TOKEN = "cgw_test_token_123"


def _http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


class TestClientUnit(unittest.TestCase):
    def test_build_plan_run_and_stop_after_first_positional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "train.py"
            script.write_text("print('hi')\n", encoding="utf-8")

            plan = client.build_plan(
                ["run", "--gpu", "T4", str(script), "--json", "--timeout", "10", "-f", "/etc/passwd"]
            )

        self.assertTrue(plan.uses_file)
        self.assertEqual(len(plan.files), 1)
        staged = next(iter(plan.files))
        self.assertEqual(staged, "gw_in1/train.py")
        self.assertEqual(plan.files[staged], b"print('hi')\n")
        self.assertEqual(plan.display[staged], str(script))
        self.assertEqual(
            plan.args,
            ["run", "--gpu", "T4", staged, "--json", "--timeout", "10", "-f", "/etc/passwd"],
        )

    def test_build_plan_exec_py_and_ipynb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            py_file = Path(tmp) / "task.py"
            py_file.write_text("x = 1\n", encoding="utf-8")
            nb_file = Path(tmp) / "notebook.ipynb"
            nb_file.write_text('{"cells": []}\n', encoding="utf-8")

            py_plan = client.build_plan(["exec", "-s", "s1", "-f", str(py_file)])
            self.assertTrue(py_plan.uses_file)
            self.assertEqual(py_plan.args, ["exec", "-s", "s1", "-f", "gw_in1/task.py"])
            self.assertEqual(py_plan.outputs, {})

            nb_plan = client.build_plan(["exec", "-s", "s1", "--file=" + str(nb_file)])
            self.assertTrue(nb_plan.uses_file)
            self.assertEqual(nb_plan.args, ["exec", "-s", "s1", "--file", "gw_in1/notebook.ipynb"])
            expected_local_out = Path(tmp) / "notebook_output.ipynb"
            self.assertEqual(nb_plan.outputs.get("gw_in1/notebook_output.ipynb"), expected_local_out)
            self.assertEqual(nb_plan.display.get("gw_in1/notebook_output.ipynb"), str(expected_local_out))

    def test_build_plan_short_attached_and_opt_equals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("numpy\n", encoding="utf-8")
            script = Path(tmp) / "run_me.py"
            script.write_text("pass\n", encoding="utf-8")
            out_log = Path(tmp) / "out.log"

            p_exec = client.build_plan(["exec", "-s", "gpu1", f"-f{script}"])
            self.assertEqual(p_exec.args, ["exec", "-s", "gpu1", "-f", "gw_in1/run_me.py"])
            self.assertTrue(p_exec.uses_file)

            p_inst = client.build_plan(["install", "-s", "gpu1", f"--requirement={req}"])
            self.assertEqual(p_inst.args, ["install", "-s", "gpu1", "--requirement", "gw_in1/requirements.txt"])
            self.assertIn("gw_in1/requirements.txt", p_inst.files)

            p_log = client.build_plan(["log", "-s", "gpu1", f"--output={out_log}"])
            self.assertEqual(p_log.args, ["log", "-s", "gpu1", "--output", "gw_out1/out.log"])
            self.assertEqual(p_log.outputs["gw_out1/out.log"], out_log)

    def test_build_plan_upload_and_download_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_csv = Path(tmp) / "data.csv"
            local_csv.write_text("a,b\n1,2\n", encoding="utf-8")
            out_dir = Path(tmp) / "downloads"
            out_dir.mkdir()
            out_file = Path(tmp) / "model.pt"

            up_plan = client.build_plan(["upload", "-s", "s1", str(local_csv), "/content/data.csv"])
            self.assertEqual(up_plan.args, ["upload", "-s", "s1", "gw_in1/data.csv", "/content/data.csv"])
            self.assertIn("gw_in1/data.csv", up_plan.files)

            dl_file_plan = client.build_plan(["download", "-s", "s1", "/content/weights.pt", str(out_file)])
            self.assertEqual(
                dl_file_plan.args,
                ["download", "-s", "s1", "/content/weights.pt", "gw_out1/model.pt"],
            )
            self.assertEqual(dl_file_plan.outputs["gw_out1/model.pt"], out_file)

            dl_dir_plan = client.build_plan(["download", "-s", "s1", "/content/weights.pt", str(out_dir)])
            self.assertEqual(
                dl_dir_plan.args,
                ["download", "-s", "s1", "/content/weights.pt", "gw_out1/weights.pt"],
            )
            self.assertEqual(dl_dir_plan.outputs["gw_out1/weights.pt"], out_dir / "weights.pt")

            dl_slash_plan = client.build_plan(["download", "-s", "s1", "/content/ckpt.bin", str(out_dir) + "/"])
            self.assertEqual(dl_slash_plan.outputs["gw_out1/ckpt.bin"], out_dir / "ckpt.bin")

    def test_build_plan_install_and_log_and_ssh_stop_after_positional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "reqs.txt"
            req.write_text("torch\n", encoding="utf-8")
            log_path = Path(tmp) / "hist.ipynb"

            inst = client.build_plan(["install", "-s", "s1", "-r", str(req)])
            self.assertEqual(inst.args, ["install", "-s", "s1", "-r", "gw_in1/reqs.txt"])

            lg = client.build_plan(["log", "-s", "s1", "-o", str(log_path)])
            self.assertEqual(lg.args, ["log", "-s", "s1", "-o", "gw_out1/hist.ipynb"])
            self.assertEqual(lg.outputs["gw_out1/hist.ipynb"], log_path)

            ssh_plan = client.build_plan(["ssh", "-s", "x", "--", "cmd", "-f", "/etc/passwd"])
            self.assertEqual(ssh_plan.args, ["ssh", "-s", "x", "--", "cmd", "-f", "/etc/passwd"])
            self.assertEqual(ssh_plan.files, {})

    def test_build_plan_missing_input_file_raises_usage_error(self) -> None:
        with self.assertRaises(client.UsageError):
            client.build_plan(["run", "/nonexistent/path/to/script.py"])

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(client.UsageError):
                client.build_plan(["log", "-s", "s1", "-o", tmp])

    def test_save_files_writes_outputs_and_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "saved.txt"
            plan = client.Plan()
            plan.outputs["gw_out1/saved.txt"] = out_path
            res = {
                "files": {
                    "gw_out1/saved.txt": base64.b64encode(b"hello").decode("ascii"),
                    "../escape.txt": base64.b64encode(b"bad").decode("ascii"),
                },
                "skipped_files": [{"path": "gw_out1/big.bin", "size": 999, "reason": "too large"}],
            }
            saved, problems = client.save_files(res, plan)
            self.assertEqual(saved, [str(out_path)])
            self.assertEqual(out_path.read_bytes(), b"hello")
            self.assertEqual(len(problems), 2)
            self.assertIn("../escape.txt", problems[0])
            self.assertIn("too large", problems[1])

    def test_safe_local_path_rejects_traversal(self) -> None:
        for bad in ("", ".", "..", "./a", "../evil", "a/../b", "/etc/passwd", "a//b"):
            with self.subTest(bad=bad):
                self.assertIsNone(client.safe_local_path(bad))

        self.assertEqual(client.safe_local_path("gw_out1/model.pt"), Path("gw_out1/model.pt"))
        self.assertEqual(client.safe_local_path("colab_tmp/fig.png"), Path("colab_tmp/fig.png"))

    def test_restore_names(self) -> None:
        plan = client.Plan()
        plan.display = {
            "gw_in1/nb.ipynb": "local/nb.ipynb",
            "gw_in1/nb_output.ipynb": "local/nb_output.ipynb",
            "gw_in10/other.py": "src/other.py",
        }
        raw = "running gw_in1/nb.ipynb -> wrote gw_in1/nb_output.ipynb and gw_in10/other.py"
        self.assertEqual(
            client.restore_names(raw, plan),
            "running local/nb.ipynb -> wrote local/nb_output.ipynb and src/other.py",
        )

    def test_exit_code_of(self) -> None:
        self.assertEqual(client.exit_code_of({"timed_out": True, "exit_code": 0}), 124)
        self.assertEqual(client.exit_code_of({"ok": True, "exit_code": 0}), 0)
        self.assertEqual(client.exit_code_of({"ok": False, "exit_code": 7}), 7)
        self.assertEqual(client.exit_code_of({"ok": False, "exit_code": -15}), 143)
        self.assertEqual(client.exit_code_of({"ok": False, "exit_code": -200}), 255)
        self.assertEqual(client.exit_code_of({"ok": False, "exit_code": 300}), 1)
        self.assertEqual(client.exit_code_of({"ok": True}), 0)
        self.assertEqual(client.exit_code_of({"ok": False}), 1)

    def test_client_main_pre_subcommand_flag_validation(self) -> None:
        env_patch = {"COLAB_GW_TOKEN": TEST_TOKEN, "COLAB_GW_JSON": "0", "COLAB_PROXY_JSON": "0"}
        with mock.patch.dict(os.environ, env_patch, clear=False):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.assertEqual(client.main(["--timeout", "0", "usage"]), 2)
                self.assertEqual(client.main(["--url"]), 2)
                self.assertEqual(client.main(["--url", "ftp://example.com", "usage"]), 2)


class TestServerUnitHelpers(unittest.TestCase):
    def test_safe_relpath(self) -> None:
        for bad in ("", ".", "..", "./a", "../a", "a/../b", "/etc/passwd", "a//b", "a\\b", "a\x00b", "x" * 1025):
            with self.subTest(bad=bad):
                with self.assertRaises(server.JobError) as ctx:
                    server.safe_relpath(bad)
                self.assertEqual(ctx.exception.status, 400)

        self.assertEqual(str(server.safe_relpath("main.py")), "main.py")
        self.assertEqual(str(server.safe_relpath("dir/sub/file.txt")), "dir/sub/file.txt")

    def test_clean_output(self) -> None:
        self.assertEqual(server.JOBS_ROOT.name, f"share-my-colab-{os.getuid()}")
        work = Path("/tmp/share-my-colab-1000/job_abc/work")
        ya29 = "ya29." + "A1b2C3d4E5f6G7h8I9j0K1L2"
        refresh = "1//" + "0eAbCdEfGhIjKlMnOpQrStUvWx"
        proxy_tok = "colab-runtime-proxy-token=super_secret_proxy_token_xyz"
        raw_stderr = (
            b"Warning: Could not read the OAuth token from the OS keyring\n"
            b"Warning: Could not save the OAuth token to the OS keyring: locked\n"
            + f"Error at {work}/gw_in1/main.py in {work}: {ya29} {refresh} {proxy_tok}&foo=1\n".encode("utf-8")
        )

        cleaned_err = server._clean_output(raw_stderr, work, is_stderr=True).decode("utf-8")
        self.assertNotIn("OS keyring", cleaned_err)
        self.assertNotIn(str(work), cleaned_err)
        self.assertIn("Error at gw_in1/main.py in .:", cleaned_err)
        self.assertIn("ya29.REDACTED", cleaned_err)
        self.assertIn("1//REDACTED", cleaned_err)
        self.assertIn("colab-runtime-proxy-token=REDACTED&foo=1", cleaned_err)
        self.assertNotIn(ya29, cleaned_err)
        self.assertNotIn(refresh, cleaned_err)
        self.assertNotIn("super_secret_proxy_token_xyz", cleaned_err)

        raw_stdout = f"{work}/out.txt tok={ya29}\n".encode("utf-8")
        cleaned_out = server._clean_output(raw_stdout, work, is_stderr=False).decode("utf-8")
        self.assertEqual(cleaned_out, "out.txt tok=ya29.REDACTED\n")

        # When is_stderr=False, keyring warning lines are NOT stripped
        keyring_stdout = b"Warning: Could not read the OAuth token from the OS keyring\nok\n"
        self.assertEqual(
            server._clean_output(keyring_stdout, work, is_stderr=False),
            keyring_stdout,
        )

    def test_mask_args(self) -> None:
        self.assertEqual(
            server._mask_args(["exec", "-s", "s1", "--env", "API_KEY=secret123", "--env=DB_PASS=p4ss"]),
            "exec -s s1 --env API_KEY=*** --env=DB_PASS=***",
        )
        long_cmd = ["run", "a" * 350]
        masked_long = server._mask_args(long_cmd)
        self.assertTrue(masked_long.endswith("…"))
        self.assertEqual(len(masked_long), 301)

    def test_parse_usage_output(self) -> None:
        sample = (
            "Current balance: 95.42 compute units\n"
            "Usage rate: 1.96/hr\n"
            "Active assignments: 2\n"
        )
        parsed = server.parse_usage_output(sample)
        self.assertEqual(parsed["balance_ccu"], 95.42)
        self.assertEqual(parsed["usage_rate_per_hr"], 1.96)
        self.assertEqual(parsed["active_assignments"], 2)
        self.assertEqual(parsed["raw"], sample.strip())

        empty = server.parse_usage_output("")
        self.assertIsNone(empty["balance_ccu"])
        self.assertIsNone(empty["usage_rate_per_hr"])
        self.assertIsNone(empty["active_assignments"])

    def test_real_run_colab_whitelist_rejection_spends_zero_ccu(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("must not spawn subprocess")):
            for bad_args in (["auth"], ["drivemount"], ["whoami"], ["--auth=adc", "sessions"]):
                with self.subTest(bad_args=bad_args):
                    with self.assertRaises(server.JobError) as ctx:
                        server.run_colab(bad_args)
                    self.assertEqual(ctx.exception.status, 403)


class TestGatewayHTTPIntegration(unittest.TestCase):
    httpd: server.GatewayServer
    thread: threading.Thread
    port: int
    base_url: str
    _orig_token: bytes
    _orig_colab_version: str
    _log_patcher: mock._patch

    @classmethod
    def setUpClass(cls) -> None:
        cls._orig_token = server.RT.token
        cls._orig_colab_version = server.RT.colab_version
        server.RT.token = TEST_TOKEN.encode("utf-8")
        server.RT.colab_version = "0.8.0-test"
        cls._log_patcher = mock.patch.object(server, "log", lambda _msg: None)
        cls._log_patcher.start()

        cls.httpd = server.GatewayServer(("127.0.0.1", 0), server.GatewayHandler)
        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls._log_patcher.stop()
        server.RT.token = cls._orig_token
        server.RT.colab_version = cls._orig_colab_version

    def _auth_headers(self, token: str = TEST_TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def test_get_index_and_client_py_and_skill_md(self) -> None:
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, hdrs, body = _http_request("127.0.0.1", self.port, "GET", path)
                self.assertEqual(status, 200)
                self.assertEqual((hdrs.get("server") or "").strip(), "ShareMyColab/3")
                self.assertEqual(hdrs.get("content-security-policy"), server.HTML_CSP)
                self.assertEqual(hdrs.get("x-frame-options"), "DENY")
                self.assertIn(b"<html", body)
                self.assertIn(b"share-my-colab", body)
                self.assertGreater(len(body), 200)

        status, hdrs, body = _http_request("127.0.0.1", self.port, "GET", "/client.py")
        self.assertEqual(status, 200)
        self.assertIn(b"CLIENT_VERSION", body)
        self.assertIn("client.py", hdrs.get("content-disposition", ""))

        skill_file = getattr(server, "SKILL_FILE", None)
        if skill_file is not None and skill_file.is_file():
            status, hdrs, body = _http_request("127.0.0.1", self.port, "GET", "/SKILL.md")
            self.assertEqual(status, 200)
            self.assertIn("SKILL.md", hdrs.get("content-disposition", ""))
            self.assertGreater(len(body.strip()), 50)

    def test_get_health_no_auth(self) -> None:
        status, _, body = _http_request("127.0.0.1", self.port, "GET", "/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "share-my-colab")
        self.assertIn("run", data["commands"])
        self.assertNotIn("auth", data["commands"])
        self.assertIn("limits", data)

    def test_auth_required_for_usage_and_cli(self) -> None:
        for method, path, payload in (
            ("GET", "/api/usage", None),
            ("POST", "/api/cli", b'{"args":["usage"]}'),
        ):
            for headers in ({}, {"Authorization": "Bearer wrong_token"}):
                with self.subTest(method=method, path=path, headers=headers):
                    req_headers = dict(headers)
                    if payload is not None:
                        req_headers["Content-Type"] = "application/json"
                    status, hdrs, raw = _http_request("127.0.0.1", self.port, method, path, req_headers, payload)
                    self.assertEqual(status, 401)
                    self.assertEqual(hdrs.get("www-authenticate"), "Bearer")
                    self.assertFalse(json.loads(raw)["ok"])

    def test_post_cli_whitelist_rejection_via_http(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("must not spawn subprocess")):
            for bad_args in (["auth"], ["drivemount"], ["whoami"], ["--auth=adc", "sessions"]):
                with self.subTest(bad_args=bad_args):
                    payload = json.dumps({"args": bad_args}).encode("utf-8")
                    status, _, raw = _http_request(
                        "127.0.0.1", self.port, "POST", "/api/cli", self._auth_headers(), payload
                    )
                    self.assertEqual(status, 403)
                    self.assertFalse(json.loads(raw)["ok"])

    def test_get_usage_with_mocked_run_colab(self) -> None:
        fake_res = {
            "ok": True,
            "exit_code": 0,
            "command": "usage",
            "duration_sec": 0.12,
            "timed_out": False,
            "stdout": "Current balance: 88.5 compute units\nUsage rate: 1.5/hr\nActive assignments: 1\n",
            "stderr": "",
            "files": {},
        }
        with mock.patch.object(server, "run_colab", return_value=fake_res) as m_run:
            status, _, raw = _http_request(
                "127.0.0.1", self.port, "GET", "/api/usage", {"Authorization": f"Bearer {TEST_TOKEN}"}
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["balance_ccu"], 88.5)
        self.assertEqual(data["usage_rate_per_hr"], 1.5)
        self.assertEqual(data["active_assignments"], 1)
        m_run.assert_called_once()
        self.assertEqual(m_run.call_args.args[0], ["usage"])

    def test_post_run_validation_and_success(self) -> None:
        for bad_body in (
            {"code": "", "gpu": "T4"},
            {"code": "   \n", "gpu": "T4"},
            {"code": "print(1)", "gpu": "V100"},
        ):
            with self.subTest(bad_body=bad_body):
                status, _, raw = _http_request(
                    "127.0.0.1",
                    self.port,
                    "POST",
                    "/api/run",
                    self._auth_headers(),
                    json.dumps(bad_body).encode("utf-8"),
                )
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(raw)["ok"])

        fake_res = {
            "ok": True,
            "exit_code": 0,
            "command": "run",
            "duration_sec": 1.23,
            "timed_out": False,
            "stdout": "CUDA available: True\n",
            "stderr": "",
            "files": {},
        }
        with mock.patch.object(server, "run_colab", return_value=fake_res) as m_run:
            status, _, raw = _http_request(
                "127.0.0.1",
                self.port,
                "POST",
                "/api/run",
                self._auth_headers(),
                json.dumps({"code": "print('ok')", "gpu": "T4", "high_mem": True, "timeout": 120}).encode("utf-8"),
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["gpu"], "T4")
        self.assertTrue(data["high_mem"])
        called_args = m_run.call_args.args[0]
        self.assertEqual(called_args[0], "run")
        self.assertIn("--gpu", called_args)
        self.assertIn("T4", called_args)
        self.assertIn("--high-mem", called_args)
        self.assertEqual(called_args[-1], "main.py")
        files_b64 = m_run.call_args.kwargs["files_b64"]
        self.assertEqual(base64.b64decode(files_b64["main.py"]), b"print('ok')")

    def test_client_e2e_usage_and_pre_subcommand_flags(self) -> None:
        fake_res = {
            "ok": True,
            "exit_code": 0,
            "command": "usage",
            "duration_sec": 0.05,
            "timed_out": False,
            "stdout": "Current balance: 42.0 compute units\nUsage rate: 0.0/hr\nActive assignments: 0\n",
            "stderr": "",
            "files": {},
        }
        env_patch = {
            "COLAB_GW_TOKEN": TEST_TOKEN,
            "COLAB_GW_JSON": "0",
            "COLAB_PROXY_JSON": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        with (
            mock.patch.dict(os.environ, env_patch, clear=False),
            mock.patch.object(server, "run_colab", return_value=fake_res) as m_run,
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = client.main(["--url", self.base_url, "--timeout", "120", "--json", "usage"])
        self.assertEqual(rc, 0)
        body = json.loads(out.getvalue())
        self.assertTrue(body["ok"])
        self.assertEqual(body["command"], "usage")
        self.assertIn("42.0 compute units", body["stdout"])
        self.assertEqual(body["saved_files"], [])
        self.assertEqual(m_run.call_args.args[0], ["usage"])
        self.assertEqual(m_run.call_args.kwargs["timeout_sec"], 120)

    def test_client_post_subcommand_json_flag_passes_through(self) -> None:
        fake_res = {
            "ok": True,
            "exit_code": 0,
            "command": "run",
            "duration_sec": 0.05,
            "timed_out": False,
            "stdout": "args=['--json']\n",
            "stderr": "",
            "files": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "echo_args.py"
            script.write_text("import sys; print(sys.argv[1:])\n", encoding="utf-8")
            env_patch = {
                "COLAB_GW_TOKEN": TEST_TOKEN,
                "COLAB_GW_JSON": "0",
                "COLAB_PROXY_JSON": "0",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
            with (
                mock.patch.dict(os.environ, env_patch, clear=False),
                mock.patch.object(server, "run_colab", return_value=fake_res) as m_run,
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = client.main(["--url=" + self.base_url, "run", str(script), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "args=['--json']\n")
        self.assertEqual(m_run.call_args.args[0], ["run", "gw_in1/echo_args.py", "--json"])


class TestPrivacyAndSecretHygiene(unittest.TestCase):
    EXCLUDED_DIRS = {
        ".gateway_ssh",
        ".remember",
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "colab_tmp",
    }
    EXCLUDED_FILES = {
        ".gateway_token",
        "gateway.log",
        ".env",
        ".DS_Store",
    }
    FORBIDDEN_LITERALS = (
        "jaist" + ".ac.jp",
        "cld-" + "206",
        "temp/" + "gemini",
        "OPENSSH " + "PRIVATE KEY",
        "~/" + "websites",
    )

    def test_no_leaked_hostnames_or_private_keys_in_repo(self) -> None:
        scanned = 0
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in self.EXCLUDED_DIRS and not d.startswith(("gw_in", "gw_out"))
            ]
            for name in filenames:
                if name in self.EXCLUDED_FILES or name.endswith((".log", ".pyc", ".pyo")):
                    continue
                path = Path(dirpath) / name
                rel = path.relative_to(REPO_ROOT).as_posix()
                text = path.read_text(encoding="utf-8", errors="replace")
                scanned += 1
                for needle in self.FORBIDDEN_LITERALS:
                    if needle in text:
                        hits.append(f"{rel}: contains {needle!r}")
        self.assertGreater(scanned, 0)
        self.assertEqual(hits, [], "Leaked private strings found:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
