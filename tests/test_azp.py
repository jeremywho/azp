import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
AZP = ROOT / "azp.py"
FAKE_AZ = Path(__file__).resolve().parent / "fake_az.py"
sys.path.insert(0, str(ROOT))
import azp

BASH = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if Path("C:/Program Files/Git/bin/bash.exe").exists() else None)

TENANT_A = "aaaaaaaa-0000-0000-0000-000000000000"
TENANT_B = "bbbbbbbb-0000-0000-0000-000000000000"
TENANT_C = "cccccccc-0000-0000-0000-000000000000"


def sub(name, tenant, user, default=False):
    return {"id": f"{name}-id", "name": name, "tenantId": tenant, "user": {"name": user, "type": "user"}, "isDefault": default}


class AzpTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="azp-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.home = self.tmp / "azp-home"
        self.shims = self.tmp / "bin"
        self.env = os.environ.copy()
        self.env.update(
            AZP_HOME=str(self.home),
            AZP_SHIM_DIR=str(self.shims),
            AZP_AZ_CMD=json.dumps([sys.executable, str(FAKE_AZ)]),
        )
        self.fake_bin = self.tmp / "fake-bin"
        self.fake_bin.mkdir()
        fake_command = " ".join(azp.bash_quote(str(p).replace("\\", "/")) for p in (sys.executable, FAKE_AZ))
        (self.fake_bin / "az").write_text(f'#!/usr/bin/env bash\nexec {fake_command} "$@"\n', encoding="utf-8")
        self.env["PATH"] = str(self.fake_bin) + os.pathsep + self.env["PATH"]

    def azp(self, *args):
        return subprocess.run(
            [sys.executable, str(AZP), *args], env=self.env, capture_output=True, text=True, encoding="utf-8"
        )

    def make_profile(self, name, subs, tokens=None, tenant=None):
        config_dir = self.tmp / f"cfg-{name}"
        config_dir.mkdir()
        if subs:
            (config_dir / "azureProfile.json").write_text(
                "﻿" + json.dumps({"subscriptions": subs}), encoding="utf-8"
            )
        (config_dir / "fake.json").write_text(json.dumps({"tokens": tokens or {}}))
        args = ["add", name, "--dir", str(config_dir)]
        if tenant:
            args += ["--tenant", tenant]
        result = self.azp(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return config_dir

    def test_add_writes_registry_and_shims(self):
        config_dir = self.make_profile("work", [sub("Prod", TENANT_A, "me@work.com", True)])
        registry = json.loads((self.home / "profiles.json").read_text())
        self.assertEqual(registry["profiles"]["work"]["dir"], str(config_dir.resolve()))
        cmd_shim = (self.shims / "az-work.cmd").read_bytes()
        self.assertIn(f'set "AZURE_CONFIG_DIR={config_dir.resolve()}"'.encode(), cmd_shim)
        self.assertIn(f'"{sys.executable}" "{FAKE_AZ}" %* & call exit /b %%errorlevel%%\r\n'.encode(), cmd_shim)
        bash_shim = (self.shims / "az-work").read_bytes()
        self.assertNotIn(b"\r", bash_shim)
        self.assertIn(str(config_dir.resolve()).replace("\\", "/").encode(), bash_shim)

    def test_add_rejects_bad_name_and_shared_dir(self):
        config_dir = self.make_profile("work", [])
        self.assertEqual(self.azp("add", "Bad_Name").returncode, 2)
        shared = self.azp("add", "other", "--dir", str(config_dir))
        self.assertEqual(shared.returncode, 2)
        self.assertIn("already belongs to profile 'work'", shared.stderr)
        self.assertEqual(self.azp("add", "work", "--dir", str(config_dir)).returncode, 2)

    def test_status_reports_each_tenant_and_fails_when_any_needs_login(self):
        self.make_profile(
            "home",
            [sub("Personal", TENANT_A, "me@home.com", True), sub("Lab", TENANT_B, "me@home.com")],
            tokens={TENANT_A: "2026-10-10 11:05:33.000000", TENANT_B: "AADSTS50078: Due to a configuration change"},
        )
        self.make_profile("empty", [])
        result = self.azp("status")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("token valid until 2026-10-10 11:05", result.stdout)
        self.assertIn("AADSTS50078 Due to a configuration change", result.stdout)
        self.assertIn("not logged in", result.stdout)
        self.assertIn("fix: azp login home", result.stdout)
        self.assertIn("fix: azp login empty", result.stdout)

        rows = json.loads(self.azp("status", "--json").stdout)
        by_tenant = {(r["profile"], r["tenant"]): r["ok"] for r in rows}
        self.assertEqual(by_tenant, {("home", TENANT_A): True, ("home", TENANT_B): False, ("empty", None): False})

    def test_status_passes_when_every_tenant_mints_a_token(self):
        self.make_profile("home", [sub("Personal", TENANT_A, "me@home.com", True)], tokens={TENANT_A: "2026-10-10 12:00:00"})
        self.make_profile("work", [sub("Prod", TENANT_C, "me@work.com", True)], tokens={TENANT_C: "2026-10-10 12:00:00"})
        result = self.azp("status")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("LOGIN NEEDED", result.stdout)

    def test_status_checks_registered_tenant_even_without_a_subscription_in_it(self):
        self.make_profile("work", [sub("Prod", TENANT_A, "me@work.com", True)], tokens={TENANT_A: "2026-10-10 12:00:00"}, tenant=TENANT_C)
        rows = json.loads(self.azp("status", "--json").stdout)
        self.assertEqual({r["tenant"]: r["ok"] for r in rows}, {TENANT_C: False, TENANT_A: True})

    def test_az_forwards_args_verbatim_under_profile_dir(self):
        config_dir = self.make_profile("work", [])
        query = "[?name=='Prod'].id"
        result = self.azp("az", "work", "--version", "account", "list", "--query", query)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(seen["config_dir"], str(config_dir.resolve()))
        self.assertEqual(seen["args"], ["--version", "account", "list", "--query", query])

    def test_az_propagates_exit_code(self):
        config_dir = self.make_profile("work", [])
        (config_dir / "fake.json").write_text(json.dumps({"exit": 7}))
        self.assertEqual(self.azp("az", "work", "group", "list").returncode, 7)

    def test_run_sets_config_dir_for_arbitrary_commands(self):
        config_dir = self.make_profile("work", [])
        result = self.azp("run", "work", "--", sys.executable, "-c", "import os; print(os.environ['AZURE_CONFIG_DIR'])")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(config_dir.resolve()))

    def test_unknown_profile_is_an_error(self):
        self.make_profile("work", [])
        result = self.azp("az", "nope", "account", "show")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown profile 'nope' (known: work)", result.stderr)

    def test_login_targets_registered_tenant(self):
        self.make_profile("work", [], tenant=TENANT_C)
        result = self.azp("login", "work", "--device-code", "--allow-no-subscriptions")
        seen = json.loads(result.stdout)
        self.assertEqual(seen["args"], ["login", "--tenant", TENANT_C, "--use-device-code", "--allow-no-subscriptions"])

    def test_cmd_shim_passes_unquoted_parentheses_through(self):
        config_dir = self.make_profile("work", [])
        args = ["account", "list", "--query", "length(@)", "-o", "tsv"]
        result = subprocess.run(
            [str(self.shims / "az-work.cmd"), *args], env=self.env, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(seen["args"], args)
        self.assertEqual(seen["config_dir"], str(config_dir.resolve()))

    def test_msi_install_runs_bundled_python_instead_of_az_cmd(self):
        cli = self.tmp / "CLI2"
        (cli / "wbin").mkdir(parents=True)
        (cli / "wbin" / "az.cmd").write_text("@echo off\r\n")
        (cli / "python.exe").write_bytes(b"")
        env = self.env.copy()
        del env["AZP_AZ_CMD"]
        env["PATH"] = str(cli / "wbin")
        probe = "import azp, json; print(json.dumps(azp.az_cmd()))"
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(cli / "python.exe"), "-IBm", "azure.cli"])

    def test_unknown_options_are_rejected_outside_login(self):
        self.make_profile("work", [])
        result = self.azp("status", "--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --bogus", result.stderr)

    def test_which_finds_the_owning_profile(self):
        self.make_profile("home", [sub("Personal", TENANT_A, "me@home.com", True)])
        self.make_profile("work", [sub("ContosoProd01", TENANT_C, "me@work.com", True)])
        result = self.azp("which", "prod01")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("work "), result.stdout)
        self.assertNotIn("home", result.stdout)
        self.assertEqual(self.azp("which", "ContosoProd01-id").returncode, 0)
        self.assertEqual(self.azp("which", "missing").returncode, 1)

    def test_env_prints_shell_specific_assignment(self):
        config_dir = str(self.make_profile("work", []).resolve())
        self.assertEqual(self.azp("env", "work").stdout.strip(), f"$env:AZURE_CONFIG_DIR = '{config_dir}'")
        self.assertEqual(self.azp("env", "work", "--shell", "cmd").stdout.strip(), f'set "AZURE_CONFIG_DIR={config_dir}"')
        self.assertEqual(
            self.azp("env", "work", "--shell", "bash").stdout.strip(),
            f"export AZURE_CONFIG_DIR='{config_dir.replace(chr(92), '/')}'",
        )

    def test_keepalive_logs_results(self):
        self.make_profile("home", [sub("Personal", TENANT_A, "me@home.com", True)], tokens={TENANT_A: "2026-10-10 12:00:00"})
        self.make_profile("work", [sub("Prod", TENANT_C, "me@work.com", True)], tokens={TENANT_C: "AADSTS700082: The refresh token has expired"})
        result = self.azp("keepalive")
        self.assertEqual(result.returncode, 1)
        log = (self.home / "keepalive.log").read_text()
        self.assertIn("LOGIN NEEDED: work", log)
        self.assertIn(f"work {TENANT_C} FAIL AADSTS700082", log)
        self.assertIn(f"home {TENANT_A} ok", log)

    def test_remove_drops_profile_and_shims_but_keeps_dir(self):
        config_dir = self.make_profile("work", [])
        result = self.azp("remove", "work")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.shims / "az-work.cmd").exists())
        self.assertFalse((self.shims / "az-work").exists())
        self.assertTrue(config_dir.exists())
        self.assertEqual(json.loads((self.home / "profiles.json").read_text())["profiles"], {})

    def test_install_writes_azp_shims(self):
        self.make_profile("work", [])
        result = self.azp("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(AZP).encode(), (self.shims / "azp.cmd").read_bytes())
        self.assertNotIn(b"\r", (self.shims / "azp").read_bytes())
        self.assertTrue((self.shims / "az-work.cmd").exists())

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_powershell_env_keeps_quote_payload_literal(self):
        self.env["PYTHONIOENCODING"] = "utf-8"
        for quote in "'\u2018\u2019\u201a\u201b":
            with self.subTest(quote=ascii(quote)):
                config_dir = self.tmp / ("cfg" + quote + ";Write-Output AZP_INJECTED;#")
                self.assertEqual(self.azp("add", "work", "--force", "--dir", str(config_dir)).returncode, 0)
                assignment = self.azp("env", "work").stdout
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     assignment + "\n[Console]::Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:AZURE_CONFIG_DIR)))"],
                    env=self.env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, base64.b64encode(str(config_dir).encode()).decode())

    @unittest.skipUnless(BASH, "Bash required")
    def test_bash_env_and_shim_keep_quote_payload_literal(self):
        config_dir = self.tmp / "cfg'; echo AZP_INJECTED; #"
        self.assertEqual(self.azp("add", "work", "--dir", str(config_dir)).returncode, 0)
        assignment = self.azp("env", "work", "--shell", "bash").stdout
        result = subprocess.run(
            [BASH, "-c", assignment + '\nprintf "%s" "$AZURE_CONFIG_DIR"'],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(config_dir).replace("\\", "/"))
        result = subprocess.run([BASH, str(self.shims / "az-work"), "account", "show"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["config_dir"], str(config_dir))

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_cmd_shim_keeps_percent_and_bang_paths_literal(self):
        config_dir = self.tmp / "cfg-%AZP_TEST_MARKER%-!AZP_TEST_MARKER! & (x)"
        self.env["AZP_TEST_MARKER"] = "EXPANDED"
        self.assertEqual(self.azp("add", "work", "--dir", str(config_dir)).returncode, 0)
        result = subprocess.run([str(self.shims / "az-work.cmd"), "account", "show"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["config_dir"], str(config_dir))
        result = self.azp("env", "work", "--shell", "cmd")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("use azp run", result.stderr)

    @unittest.skipUnless(os.name == "nt" and BASH, "Windows and Git Bash")
    def test_installation_path_is_escaped_in_both_launchers(self):
        directory = self.tmp / "O'Brien-%AZP_TEST_MARKER%-!test!"
        directory.mkdir()
        script = directory / "azp.py"
        shutil.copyfile(AZP, script)
        result = subprocess.run([sys.executable, str(script), "install"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["AZP_TEST_MARKER"] = "EXPANDED"
        for command in ([str(self.shims / "azp.cmd"), "list", "--json"],
                        [BASH, str(self.shims / "azp"), "list", "--json"]):
            result = subprocess.run(command, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})

    def test_status_checks_each_identity_in_same_and_different_tenants(self):
        config_dir = self.make_profile("mixed", [
            sub("Alice", TENANT_A, "alice@example.test", True),
            sub("Bob", TENANT_A, "bob@example.test"),
            sub("BobOther", TENANT_B, "bob@example.test"),
        ])
        (config_dir / "fake.json").write_text(json.dumps({"identity_tokens": {
            "alice@example.test|" + TENANT_A: "2026-10-10 12:00:00",
            "bob@example.test|" + TENANT_A: "AADSTS700082: expired",
            "bob@example.test|" + TENANT_B: "2026-10-10 12:00:00",
        }}))
        result = self.azp("status", "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual({(r["users"][0], r["tenant"]): r["ok"] for r in rows}, {
            ("alice@example.test", TENANT_A): True,
            ("bob@example.test", TENANT_A): False,
            ("bob@example.test", TENANT_B): True,
        })

    def test_concurrent_writers_preserve_registry_and_shims(self):
        def add(i):
            return self.azp("add", f"p{i}", "--dir", str(self.tmp / f"cfg-{i}"))
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(add, range(12)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(self.azp("list", "--json").stdout)), {f"p{i}" for i in range(12)})
        commands = [("remove", f"p{i}") for i in range(0, 12, 2)]
        commands += [("add", f"p{i}", "--dir", str(self.tmp / f"cfg-{i}")) for i in range(12, 18)]
        commands += [("install",)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda args: self.azp(*args), commands))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        expected = {f"p{i}" for i in [*range(1, 12, 2), *range(12, 18)]}
        self.assertEqual(set(json.loads(self.azp("list", "--json").stdout)), expected)
        self.assertEqual({p.stem[3:] for p in self.shims.glob("az-*.cmd")}, expected)
        self.assertEqual({p.name[3:] for p in self.shims.glob("az-*") if p.suffix != ".cmd"}, expected)

    def test_keepalive_continues_after_bad_profile_and_notifies(self):
        bad = self.make_profile("bad", [])
        self.make_profile("good", [sub("Good", TENANT_A, "good@example.test", True)],
                          tokens={TENANT_A: "2026-10-10 12:00:00"})
        for content in ("{", "[]", '{"subscriptions": [{}]}'):
            with self.subTest(content=content):
                (bad / "azureProfile.json").write_text(content)
                with patch.dict(os.environ, self.env), patch("azp.toast") as notify:
                    self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
                    notify.assert_called_once()
                    self.assertEqual(notify.call_args.args[0], "Azure keepalive failed")
                log = (self.home / "keepalive.log").read_text()
                self.assertIn("bad None FAIL", log)
                self.assertIn(f"good {TENANT_A} ok", log)

    def test_missing_executable_is_logged_and_notified(self):
        self.make_profile("work", [sub("Prod", TENANT_A, "me@example.test", True)])
        self.env["AZP_AZ_CMD"] = json.dumps([str(self.tmp / "missing.exe")])
        with patch.dict(os.environ, self.env), patch("azp.toast") as notify:
            self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
            notify.assert_called_once()
        self.assertIn("FAIL", (self.home / "keepalive.log").read_text())

    def test_invalid_registry_is_logged_and_notified(self):
        self.home.mkdir()
        for content in ("{", "[]"):
            with self.subTest(content=content):
                (self.home / "profiles.json").write_text(content)
                with patch.dict(os.environ, self.env), patch("azp.toast") as notify:
                    self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
                    notify.assert_called_once()
        self.assertIn("(registry) None FAIL", (self.home / "keepalive.log").read_text())

    def test_log_failure_still_notifies(self):
        self.make_profile("work", [sub("Prod", TENANT_A, "me@example.test", True)],
                          tokens={TENANT_A: "2026-10-10 12:00:00"})
        (self.home / "keepalive.log").mkdir()
        with patch.dict(os.environ, self.env), patch("azp.toast") as notify:
            self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
            notify.assert_called_once()

    def test_notification_failure_is_logged(self):
        self.make_profile("work", [])
        with patch.dict(os.environ, self.env), patch("azp.toast", side_effect=OSError("notification unavailable")):
            self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
        self.assertIn("notification failed: notification unavailable", (self.home / "keepalive.log").read_text())

    def test_scheduled_action_pins_home_and_runs_without_installing_environment(self):
        self.make_profile("work", [sub("Prod", TENANT_A, "me@example.test", True)],
                          tokens={TENANT_A: "2026-10-10 12:00:00"})
        with patch.dict(os.environ, self.env), patch("azp.shutil.which", return_value="C:/Python/pyw.exe"), patch("azp.subprocess.run") as run:
            azp.install_task(6)
        command = run.call_args.args[0]
        action = command[command.index("/TR") + 1]
        expected = subprocess.list2cmdline(["C:/Python/pyw.exe", "-3", "-E", "-s", str(AZP),
                                           "--home", str(self.home), "keepalive", "--notify"])
        self.assertEqual(action, expected)
        self.env["AZP_HOME"] = str(self.tmp / "wrong-home")
        result = self.azp("--home", str(self.home), "keepalive", "--notify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all ok", (self.home / "keepalive.log").read_text())
        self.assertFalse((self.tmp / "wrong-home").exists())

    def test_empty_registry_is_not_reported_as_healthy(self):
        result = self.azp("status", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no profiles registered", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_install_can_upgrade_the_executing_launcher(self):
        self.shims.mkdir()
        shim = self.shims / "azp.cmd"
        invocation = f'"{sys.executable}" -E -s "{AZP}" %*'
        for previous in (f'@{invocation}\r\n',
                         f'@echo off\r\nsetlocal DisableDelayedExpansion\r\n{invocation}\r\n'):
            with self.subTest(previous=previous):
                shim.write_bytes(previous.encode())
                for _ in range(3):
                    result = subprocess.run([str(shim), "install"], env=self.env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout.count("shims written"), 1)
                result = subprocess.run([str(shim), "status"], env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("no profiles registered", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_profile_shim_can_be_replaced_while_running_and_keeps_exit_code(self):
        config_dir = self.make_profile("work", [])
        shim = self.shims / "az-work.cmd"
        invocation = f'"{sys.executable}" "{FAKE_AZ}" %*'
        layouts = (f'@echo off\r\nsetlocal\r\nset "AZURE_CONFIG_DIR={config_dir}"\r\n{invocation}\r\n',
                   f'@echo off\r\nsetlocal DisableDelayedExpansion\r\nset "AZURE_CONFIG_DIR={config_dir}"\r\n{invocation}\r\n')
        for previous in layouts:
            with self.subTest(previous=previous):
                shim.write_bytes(previous.encode())
                for code in (7, 0):
                    (config_dir / "fake.json").write_text(json.dumps({"reinstall": 2, "exit": code}))
                    result = subprocess.run([str(shim), "account", "show"], env=self.env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout)["config_dir"], str(config_dir))

    def test_tenant_aliases_and_uppercase_guids_do_not_create_false_failures(self):
        for i, tenant in enumerate((TENANT_A.upper(), "Contoso.onmicrosoft.com", "login.contoso.example")):
            self.make_profile(f"p{i}", [sub(f"Prod{i}", TENANT_A, "me@example.test", True)],
                              tokens={TENANT_A: "2026-10-10 12:00:00"}, tenant=tenant)
            login = self.azp("login", f"p{i}")
            self.assertIn(tenant, json.loads(login.stdout)["args"])
        for command in ("status", "keepalive"):
            result = self.azp(command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("LOGIN NEEDED", result.stdout)

    def test_installation_only_azure_profile_is_logged_out_not_invalid(self):
        config_dir = self.tmp / "fresh"
        config_dir.mkdir()
        (config_dir / "azureProfile.json").write_text(json.dumps({"installationId": "test-installation"}))
        result = self.azp("add", "fresh", "--dir", str(config_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("next: azp login fresh", result.stdout)
        result = self.azp("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not logged in", result.stdout)
        self.assertEqual(self.azp("which", "missing").returncode, 1)
        for command in ("status", "keepalive"):
            result = self.azp(command)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("not logged in", result.stdout)
            self.assertNotIn("ERROR", result.stdout)

    def test_damaged_registry_entry_can_be_removed_or_replaced(self):
        self.make_profile("good", [sub("Prod", TENANT_A, "me@example.test", True)],
                          tokens={TENANT_A: "2026-10-10 12:00:00"})
        registry_path = self.home / "profiles.json"
        data = json.loads(registry_path.read_text())
        data["profiles"].update(bad={}, broken=None)
        registry_path.write_text(json.dumps(data))
        result = self.azp("status", "--json")
        rows = {r["profile"]: r for r in json.loads(result.stdout)}
        self.assertTrue(rows["good"]["ok"])
        self.assertTrue(rows["bad"]["error"])
        self.assertTrue(rows["broken"]["error"])
        self.assertEqual(self.azp("az", "good", "account", "show").returncode, 0)
        self.assertEqual(self.azp("remove", "bad").returncode, 0)
        result = self.azp("add", "broken", "--force", "--dir", str(self.tmp / "repaired"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(self.azp("list", "--json").stdout)), {"good", "broken"})

    def test_removing_invalid_registry_name_does_not_delete_outside_shim_dir(self):
        self.home.mkdir()
        (self.shims / "az-x").mkdir(parents=True)
        protected = self.tmp / "protected.cmd"
        protected.write_text("keep")
        name = "x/../../protected"
        (self.home / "profiles.json").write_text(json.dumps({"profiles": {name: {}}}))
        result = self.azp("remove", name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(protected.read_text(), "keep")

    @unittest.skipUnless(os.name == "nt", "Windows shell")
    def test_running_new_shim_can_be_retargeted_without_changing_its_result(self):
        old_dir = self.make_profile("work", [])
        new_dir = self.tmp / "new-dir"
        (old_dir / "fake.json").write_text(json.dumps({
            "azp_commands": [["add", "work", "--force", "--dir", str(new_dir)]], "exit": 7,
        }))
        shim = self.shims / "az-work.cmd"
        result = subprocess.run([str(shim), "account", "show"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["config_dir"], str(old_dir))
        result = subprocess.run([str(shim), "account", "show"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["config_dir"], str(new_dir))

    @unittest.skipUnless(BASH, "Bash required")
    def test_bash_shim_resolves_az_from_current_path(self):
        self.make_profile("work", [])
        moved = self.tmp / "moved-cli"
        self.fake_bin.rename(moved)
        self.env["PATH"] = str(moved) + os.pathsep + self.env["PATH"]
        result = subprocess.run([BASH, str(self.shims / "az-work"), "account", "list", "--query", "length(@)"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["args"], ["account", "list", "--query", "length(@)"])

    def test_identity_checks_within_a_profile_run_in_parallel(self):
        config_dir = self.make_profile("mixed", [
            sub("Alice", TENANT_A, "alice@example.test", True),
            sub("Bob", TENANT_A, "bob@example.test"),
            sub("BobOther", TENANT_B, "bob@example.test"),
        ])
        barrier = threading.Barrier(3, timeout=10)
        def check(prof, subscription, hidden):
            barrier.wait()
            return {"ok": True, "detail": "checked"}
        with patch("azp.check_account", side_effect=check):
            rows = azp.check_all({"mixed": {"dir": str(config_dir)}})
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["ok"] for r in rows))

    def test_mixed_failure_notification_keeps_login_instructions(self):
        bad = self.make_profile("bad", [])
        (bad / "azureProfile.json").write_text("{")
        self.make_profile("expired", [sub("Prod", TENANT_A, "me@example.test", True)],
                          tokens={TENANT_A: "AADSTS700082: expired"})
        with patch.dict(os.environ, self.env), patch("azp.toast") as notify:
            self.assertEqual(azp.main(["keepalive", "--notify"]), 1)
            title, body = notify.call_args.args
            self.assertEqual(title, "Azure keepalive failed")
            self.assertIn("azp login expired", body)
            self.assertIn("azp status", body)
            self.assertNotIn("azp login bad", body)


if __name__ == "__main__":
    unittest.main()
