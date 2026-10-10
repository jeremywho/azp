import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AZP = ROOT / "azp.py"
FAKE_AZ = Path(__file__).resolve().parent / "fake_az.py"

TENANT_A = "aaaaaaaa-0000-0000-0000-000000000000"
TENANT_B = "bbbbbbbb-0000-0000-0000-000000000000"
TENANT_C = "cccccccc-0000-0000-0000-000000000000"


def sub(name, tenant, user, default=False):
    return {"id": f"{name}-id", "name": name, "tenantId": tenant, "user": {"name": user, "type": "user"}, "isDefault": default}


class AzpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "azp-home"
        self.shims = self.tmp / "bin"
        self.env = os.environ.copy()
        self.env.update(
            AZP_HOME=str(self.home),
            AZP_SHIM_DIR=str(self.shims),
            AZP_AZ_CMD=json.dumps([sys.executable, str(FAKE_AZ)]),
        )

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
        self.assertIn(f'"{sys.executable}" "{FAKE_AZ}" %*\r\n'.encode(), cmd_shim)
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


if __name__ == "__main__":
    unittest.main()
