import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from uuid import UUID

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TASK_NAME = "azp-keepalive"
CHECK_TIMEOUT_S = 90
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AzpError(Exception):
    pass


def azp_home():
    return Path(os.environ.get("AZP_HOME") or Path.home() / ".azp")


def shim_dir():
    return Path(os.environ.get("AZP_SHIM_DIR") or Path.home() / ".local" / "bin")


def registry_path():
    return azp_home() / "profiles.json"


def load_registry():
    path = registry_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        raise AzpError(f"invalid profile registry: {path}")
    return profiles


def save_registry(profiles):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps({"profiles": profiles}, indent=2) + "\n")


@contextmanager
def registry_lock():
    path = azp_home() / "profiles.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AzpError("timed out waiting for the profile registry lock")
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def registry_writer(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with registry_lock():
            return func(*args, **kwargs)
    return locked


def get_profile(profiles, name):
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none registered"
        raise AzpError(f"unknown profile '{name}' (known: {known})")
    prof = profiles[name]
    if (not NAME_RE.fullmatch(name) or not isinstance(prof, dict)
            or not isinstance(prof.get("dir"), str) or not prof["dir"]
            or ("tenant" in prof and not isinstance(prof["tenant"], str))):
        raise AzpError(f"invalid profile '{name}'; remove it with azp remove or replace it with azp add --force")
    return prof


def az_cmd():
    override = os.environ.get("AZP_AZ_CMD")
    if override:
        cmd = json.loads(override)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(p, str) and p for p in cmd):
            raise AzpError("AZP_AZ_CMD must be a nonempty JSON array of command arguments")
        return cmd
    found = shutil.which("az")
    if not found:
        raise AzpError("az not found on PATH")
    bundled = Path(found).parent.parent / "python.exe"
    if Path(found).name.lower() == "az.cmd" and bundled.exists():
        # az.cmd expands %* inside an IF block, so unquoted parentheses from PowerShell break cmd parsing.
        return [str(bundled), "-IBm", "azure.cli"]
    return [found]


def uses_bundled_python(cmd):
    return cmd[1:] == ["-IBm", "azure.cli"]


def profile_env(prof):
    env = os.environ.copy()
    env["AZURE_CONFIG_DIR"] = prof["dir"]
    try:
        if uses_bundled_python(az_cmd()):
            env.setdefault("AZ_INSTALLER", "MSI")
    except AzpError:
        pass
    return env


def read_subscriptions(config_dir):
    path = Path(config_dir) / "azureProfile.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    subs = data.get("subscriptions", []) if isinstance(data, dict) else None
    if not isinstance(subs, list):
        raise AzpError(f"invalid subscription data: {path}")
    for sub in subs:
        if (not isinstance(sub, dict)
                or not all(isinstance(sub.get(k), str) and sub[k] for k in ("id", "name", "tenantId"))
                or not isinstance(sub.get("user"), dict)
                or not all(isinstance(sub["user"].get(k), str) and sub["user"][k] for k in ("name", "type"))
                or ("environmentName" in sub and not isinstance(sub["environmentName"], str))):
            raise AzpError(f"invalid subscription entry: {path}")
    return subs


def users_of(subs):
    return sorted({s.get("user", {}).get("name", "?") for s in subs})


def failure_reason(stderr):
    match = re.search(r"(AADSTS\d+)[:\s]*([^\r\n.]*)", stderr)
    if match:
        return f"{match.group(1)} {match.group(2).strip()}".strip()
    if "az login" in stderr:
        return "login required"
    lines = [line.strip() for line in stderr.splitlines() if line.strip() and not line.startswith("WARNING")]
    return lines[-1][:160] if lines else "unknown failure"


def check_account(prof, subscription, hidden):
    try:
        cmd = az_cmd() + ["account", "get-access-token", "--subscription", subscription, "--query", "expiresOn", "-o", "tsv"]
        proc = subprocess.run(
            cmd,
            env=profile_env(prof),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if hidden else 0,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": True, "detail": f"timed out after {CHECK_TIMEOUT_S}s"}
    except (AzpError, OSError, ValueError) as exc:
        return {"ok": False, "error": True, "detail": str(exc)}
    if proc.returncode == 0 and proc.stdout.strip():
        return {"ok": True, "detail": f"token valid until {proc.stdout.strip()[:16]}"}
    return {"ok": False, "detail": failure_reason(proc.stderr)}


def check_all(profiles, hidden=False):
    if not profiles:
        return [{"profile": "(registry)", "dir": str(azp_home()), "tenant": None, "users": [],
                 "subscriptions": [], "ok": False, "error": True, "detail": "no profiles registered"}]

    rows = []
    jobs = []
    for name in sorted(profiles):
        base = {"profile": name, "dir": None, "tenant": None, "users": [], "subscriptions": []}
        try:
            prof = get_profile(profiles, name)
            base["dir"] = prof["dir"]
            subs = read_subscriptions(prof["dir"])
        except (AzpError, OSError, ValueError) as exc:
            rows.append(dict(base, ok=False, error=True, detail=str(exc)))
            continue
        if not subs:
            rows.append(dict(base, ok=False, detail="not logged in"))
            continue
        groups = {}
        for sub in subs:
            key = (sub["tenantId"], sub["user"]["name"].lower(), sub["user"]["type"], sub.get("environmentName"))
            groups.setdefault(key, []).append(sub)
        for (tenant, _, _, _), accounts in groups.items():
            row = dict(base, tenant=tenant, users=users_of(accounts), subscriptions=[s["name"] for s in accounts])
            rows.append(row)
            jobs.append((prof, accounts[0]["id"], row))
        try:
            registered_tenant = str(UUID(prof.get("tenant", "")))
        except ValueError:
            registered_tenant = None
        if registered_tenant and not any(s["tenantId"].lower() == registered_tenant for s in subs):
            rows.append(dict(base, tenant=registered_tenant, ok=False, detail="registered tenant has no cached account; login required"))

    def run(job):
        prof, subscription, row = job
        row.update(check_account(prof, subscription, hidden))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(run, jobs):
            pass
    return rows


def print_rows(rows):
    for row in rows:
        state = "ok" if row["ok"] else "ERROR" if row.get("error") else "LOGIN NEEDED"
        print(f"{row['profile']:<10} {state:<13} {', '.join(row['users']) or '-'}")
        print(f"{'':<10} tenant {row['tenant'] or '-'}  {row['detail']}")
        if row["subscriptions"]:
            print(f"{'':<10} subs: {', '.join(row['subscriptions'])}")
    failing = sorted({r["profile"] for r in rows if not r["ok"] and not r.get("error")})
    if failing:
        print()
        for name in failing:
            print(f"fix: azp login {name}")


def write_shims(profiles):
    target = shim_dir()
    target.mkdir(parents=True, exist_ok=True)
    cmd = az_cmd() if profiles else []
    invoke = " ".join(batch_quote(part) for part in cmd)
    installer = 'set "AZ_INSTALLER=MSI" & ' if uses_bundled_python(cmd) else ""
    for name in profiles:
        try:
            prof = get_profile(profiles, name)
        except AzpError as exc:
            print(f"azp: {exc}", file=sys.stderr)
            continue
        win_dir = str(Path(prof["dir"]))
        posix_dir = win_dir.replace("\\", "/")
        write_batch_shim(
            target / f"az-{name}.cmd",
            f'set {batch_quote("AZURE_CONFIG_DIR=" + win_dir)} & {installer}{invoke} %*',
        )
        write_text(target / f"az-{name}", f"#!/usr/bin/env bash\nAZURE_CONFIG_DIR={bash_quote(posix_dir)} exec az \"$@\"\n")


def remove_shims(name):
    if not NAME_RE.fullmatch(name):
        return
    for suffix in (".cmd", ""):
        path = shim_dir() / f"az-{name}{suffix}"
        if path.exists():
            path.unlink()


def write_batch_shim(path, command):
    start = "@setlocal DisableDelayedExpansion & "
    content = start + command + " & call exit /b %%errorlevel%%\r\n"
    if path.exists():
        previous = path.read_bytes()
        jump = b"@goto azp_start\r\n"
        label = b":azp_start\r\n"
        if previous.startswith(jump) and label in previous:
            content = previous[:previous.index(label) + len(label)].decode("utf-8") + content
        elif not previous.startswith(start.encode()):
            # Older batch callers resume reading at their original EOF after the child exits.
            # Put an exit at that byte offset; new callers jump over it and parse exit with the child command.
            padding = max(0, len(previous) - len(jump))
            content = jump.decode() + " " * padding + "@exit /b %errorlevel%\r\n:azp_start\r\n" + content
        if previous == content.encode("utf-8"):
            return
    write_text(path, content)


def write_text(path, content):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(content)
        deadline = time.monotonic() + 5
        while True:
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def bash_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def powershell_quote(value):
    for quote in "'\u2018\u2019\u201a\u201b":
        value = value.replace(quote, quote * 2)
    return "'" + value + "'"


def batch_quote(value):
    if any(c in value for c in '\"\r\n\0'):
        raise AzpError("cannot safely represent this value in a Windows batch shim")
    return '"' + value.replace("%", "%%") + '"'


def launcher():
    py = shutil.which("py")
    return [py, "-3", "-E", "-s"] if py else [sys.executable, "-E", "-s"]


@registry_writer
def cmd_add(args):
    if not NAME_RE.fullmatch(args.name):
        raise AzpError("profile names are lowercase letters, digits and dashes")
    profiles = load_registry()
    if args.name in profiles and not args.force:
        raise AzpError(f"profile '{args.name}' already exists (use --force to overwrite)")
    config_dir = Path(args.dir).expanduser() if args.dir else Path.home() / f".azure-{args.name}"
    config_dir = config_dir.resolve()
    for name in profiles:
        if name == args.name:
            continue
        try:
            prof = get_profile(profiles, name)
            owned_dir = Path(prof["dir"]).resolve()
        except (AzpError, OSError, ValueError):
            continue
        if owned_dir == config_dir:
            raise AzpError(f"{config_dir} already belongs to profile '{name}'")
    config_dir.mkdir(parents=True, exist_ok=True)
    profiles[args.name] = {"dir": str(config_dir)}
    if args.tenant:
        profiles[args.name]["tenant"] = args.tenant
    save_registry(profiles)
    write_shims(profiles)
    print(f"added '{args.name}' -> {config_dir}")
    if not read_subscriptions(config_dir):
        print(f"next: azp login {args.name}")
    return 0


@registry_writer
def cmd_remove(args):
    profiles = load_registry()
    if args.name not in profiles:
        get_profile(profiles, args.name)
    prof = profiles.pop(args.name)
    save_registry(profiles)
    remove_shims(args.name)
    config_dir = prof.get("dir", "unknown") if isinstance(prof, dict) else "unknown"
    print(f"removed '{args.name}' (config dir left in place: {config_dir})")
    return 0


def cmd_list(args):
    profiles = load_registry()
    if args.json:
        print(json.dumps(profiles, indent=2))
        return 0
    failed = False
    for name in sorted(profiles):
        try:
            prof = get_profile(profiles, name)
            subs = read_subscriptions(prof["dir"])
        except (AzpError, OSError, ValueError) as exc:
            print(f"{name:<10} ERROR: {exc}", file=sys.stderr)
            failed = True
            continue
        default = next((s.get("name") for s in subs if s.get("isDefault")), "-")
        print(f"{name:<10} {', '.join(users_of(subs)) or '(not logged in)'}  default: {default}  [{prof['dir']}]")
    return 1 if failed else 0


def cmd_status(args):
    rows = check_all(load_registry())
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_rows(rows)
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_login(args):
    prof = get_profile(load_registry(), args.name)
    Path(prof["dir"]).mkdir(parents=True, exist_ok=True)
    cmd = az_cmd() + ["login"]
    if prof.get("tenant"):
        cmd += ["--tenant", prof["tenant"]]
    if args.device_code:
        cmd += ["--use-device-code"]
    cmd += args.extra
    return subprocess.run(cmd, env=profile_env(prof)).returncode


def cmd_run(args):
    if not args.command:
        raise AzpError("usage: azp run <profile> <command> [args...]")
    prof = get_profile(load_registry(), args.name)
    exe = shutil.which(args.command[0]) or args.command[0]
    return subprocess.run([exe] + args.command[1:], env=profile_env(prof)).returncode


def cmd_az(args):
    prof = get_profile(load_registry(), args.name)
    return subprocess.run(az_cmd() + args.args, env=profile_env(prof)).returncode


def cmd_env(args):
    prof = get_profile(load_registry(), args.name)
    path = prof["dir"]
    if args.shell == "bash":
        print(f"export AZURE_CONFIG_DIR={bash_quote(path.replace(chr(92), '/'))}")
    elif args.shell == "cmd":
        if any(c in path for c in '%!\"\r\n\0'):
            raise AzpError("this path cannot be safely emitted for cmd; use azp run or azp az instead")
        print(f'set "AZURE_CONFIG_DIR={path}"')
    else:
        print("$env:AZURE_CONFIG_DIR = " + powershell_quote(path))
    return 0


def cmd_which(args):
    query = args.query.lower()
    found = 0
    profiles = load_registry()
    failed = False
    for name in sorted(profiles):
        try:
            prof = get_profile(profiles, name)
            subs = read_subscriptions(prof["dir"])
        except (AzpError, OSError, ValueError) as exc:
            print(f"{name:<10} ERROR: {exc}", file=sys.stderr)
            failed = True
            continue
        for sub in subs:
            if query == sub.get("id", "").lower() or query in sub.get("name", "").lower():
                found += 1
                user = sub.get("user", {}).get("name", "?")
                print(f"{name:<10} {sub.get('name')}  {sub.get('id')}  tenant {sub.get('tenantId')}  {user}")
    if failed:
        return 2
    if not found:
        print(f"no registered profile has a subscription matching '{args.query}'", file=sys.stderr)
        return 1
    return 0


def cmd_keepalive(args):
    if args.install:
        return install_task(args.every_hours)
    if args.uninstall:
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]).returncode
    try:
        rows = check_all(load_registry(), hidden=args.notify)
    except (AzpError, OSError, ValueError) as exc:
        rows = [{"profile": "(registry)", "dir": str(azp_home()), "tenant": None, "users": [],
                 "subscriptions": [], "ok": False, "error": True, "detail": str(exc)}]
    failing = sorted({r["profile"] for r in rows if not r["ok"]})
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    label = "CHECK FAILED: " if any(r.get("error") for r in rows) else "LOGIN NEEDED: "
    summary = "all ok" if not failing else label + ", ".join(failing)
    log = azp_home() / "keepalive.log"
    log_error = None
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {summary}\n")
            for row in rows:
                detail = " ".join(row["detail"].splitlines())
                handle.write(f"  {row['profile']} {row['tenant']} {'ok' if row['ok'] else 'FAIL'} {detail}\n")
    except OSError as exc:
        log_error = str(exc)
        print(f"azp: could not write keepalive log: {exc}", file=sys.stderr)
    if not args.notify:
        print_rows(rows)
    elif failing or log_error:
        logins = sorted({r["profile"] for r in rows if not r["ok"] and not r.get("error")})
        login_help = "Run: " + "; ".join(f"azp login {n}" for n in logins) if logins else ""
        if log_error or any(r.get("error") for r in rows):
            title = "Azure keepalive failed"
            body = (login_help + ". " if login_help else "") + "Run azp status and inspect " + str(log)
        else:
            title, body = "Azure login needed", login_help
        try:
            toast(title, body)
        except (AzpError, OSError, subprocess.TimeoutExpired) as exc:
            detail = f"notification failed: {exc}"
            print(f"azp: {detail}", file=sys.stderr)
            try:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(f"{stamp} {detail}\n")
            except OSError:
                pass
            return 1
    return 0 if not failing and not log_error else 1


def toast(title, body):
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $t.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($t.CreateTextNode($env:AZP_TOAST_TITLE)) | Out-Null;"
        "$n.Item(1).AppendChild($t.CreateTextNode($env:AZP_TOAST_BODY)) | Out-Null;"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    env = os.environ.copy()
    env.update(AZP_TOAST_TITLE=title, AZP_TOAST_BODY=body)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )
    if proc.returncode:
        raise AzpError(f"toast process exited with code {proc.returncode}")


def install_task(every_hours):
    if not 1 <= every_hours <= 23:
        raise AzpError("--every-hours must be between 1 and 23")
    pyw = shutil.which("pyw")
    if not pyw:
        raise AzpError("pyw (Python launcher, windowless) not found on PATH")
    action = subprocess.list2cmdline([pyw, "-3", "-E", "-s", str(Path(__file__).resolve()),
                                    "--home", str(azp_home().expanduser().resolve()), "keepalive", "--notify"])
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", str(every_hours), "/TR", action, "/F"]
    return subprocess.run(cmd).returncode


@registry_writer
def cmd_install(args):
    target = shim_dir()
    target.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    exe, *flags = launcher()
    flags = " ".join(flags)
    write_batch_shim(target / "azp.cmd", f'{batch_quote(exe)} {flags} {batch_quote(str(script))} %*')
    posix = lambda p: str(p).replace("\\", "/")
    write_text(target / "azp", f"#!/usr/bin/env bash\nexec {bash_quote(posix(exe))} {flags} {bash_quote(posix(script))} \"$@\"\n")
    write_shims(load_registry())
    print(f"shims written to {target}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="azp", description="Keep several Azure CLI identities logged in side by side.")
    parser.add_argument("--home", help="profile registry and log directory (overrides AZP_HOME)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="register a profile (one isolated az config dir)")
    p.add_argument("name")
    p.add_argument("--dir", help="config dir (default ~/.azure-<name>)")
    p.add_argument("--tenant", help="tenant id used for login and keepalive")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="unregister a profile and its shims; the config dir is kept")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="list profiles from local files (no network)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="check every profile/tenant can mint a token; exit 1 if any cannot")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("login", help="az login into a profile's config dir; other args go to az login")
    p.add_argument("name")
    p.add_argument("--device-code", action="store_true")
    p.set_defaults(func=cmd_login, passthrough=True)

    p = sub.add_parser("az", help="run az as a profile: azp az <profile> <az args...>")
    p.add_argument("name")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_az)

    p = sub.add_parser("run", help="run any command with AZURE_CONFIG_DIR set to a profile")
    p.add_argument("name")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("env", help="print the line that points the current shell at a profile")
    p.add_argument("name")
    p.add_argument("--shell", choices=["pwsh", "bash", "cmd"], default="pwsh")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("which", help="find which profile holds a subscription (name substring or id)")
    p.add_argument("query")
    p.set_defaults(func=cmd_which)

    p = sub.add_parser("keepalive", help="refresh every profile's tokens; --install schedules it")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    p.add_argument("--every-hours", type=int, default=6)
    p.add_argument("--notify", action="store_true", help="quiet mode: log only, toast on failure")
    p.set_defaults(func=cmd_keepalive)

    p = sub.add_parser("install", help="write azp and az-<profile> shims into ~/.local/bin")
    p.set_defaults(func=cmd_install)
    return parser


def main(argv=None):
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if getattr(args, "passthrough", False):
        args.extra = unknown
    elif unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    for attr in ("args", "command"):
        value = getattr(args, attr, None)
        if value and value[0] == "--":
            setattr(args, attr, value[1:])
    try:
        if args.home:
            os.environ["AZP_HOME"] = str(Path(args.home).expanduser().resolve())
        return args.func(args)
    except (AzpError, OSError, ValueError) as exc:
        print(f"azp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
