import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    return json.loads(path.read_text(encoding="utf-8")).get("profiles", {})


def save_registry(profiles):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"profiles": profiles}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def get_profile(profiles, name):
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none registered"
        raise AzpError(f"unknown profile '{name}' (known: {known})")
    return profiles[name]


def az_cmd():
    override = os.environ.get("AZP_AZ_CMD")
    if override:
        return json.loads(override)
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
    return data.get("subscriptions", [])


def users_of(subs):
    return sorted({s.get("user", {}).get("name", "?") for s in subs})


def tenants_of(prof, subs):
    tenants = []
    for tenant in [prof.get("tenant")] + [s.get("tenantId") for s in subs]:
        if tenant and tenant not in tenants:
            tenants.append(tenant)
    return tenants


def failure_reason(stderr):
    match = re.search(r"(AADSTS\d+)[:\s]*([^\r\n.]*)", stderr)
    if match:
        return f"{match.group(1)} {match.group(2).strip()}".strip()
    if "az login" in stderr:
        return "login required"
    lines = [line.strip() for line in stderr.splitlines() if line.strip() and not line.startswith("WARNING")]
    return lines[-1][:160] if lines else "unknown failure"


def check_tenant(prof, tenant, hidden):
    cmd = az_cmd() + ["account", "get-access-token", "--tenant", tenant, "--query", "expiresOn", "-o", "tsv"]
    try:
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
        return {"ok": False, "detail": f"timed out after {CHECK_TIMEOUT_S}s"}
    if proc.returncode == 0 and proc.stdout.strip():
        return {"ok": True, "detail": f"token valid until {proc.stdout.strip()[:16]}"}
    return {"ok": False, "detail": failure_reason(proc.stderr)}


def check_all(profiles, hidden=False):
    jobs = []
    for name, prof in sorted(profiles.items()):
        subs = read_subscriptions(prof["dir"])
        tenants = tenants_of(prof, subs)
        if not subs:
            jobs.append((name, prof, None, subs))
        for tenant in tenants if subs else []:
            jobs.append((name, prof, tenant, subs))

    def run(job):
        name, prof, tenant, subs = job
        row = {"profile": name, "dir": prof["dir"], "tenant": tenant, "users": users_of(subs)}
        if tenant is None:
            row.update(ok=False, detail="not logged in", subscriptions=[])
            return row
        row["subscriptions"] = [s.get("name") for s in subs if s.get("tenantId") == tenant]
        row.update(check_tenant(prof, tenant, hidden))
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(run, jobs))


def print_rows(rows):
    for row in rows:
        state = "ok" if row["ok"] else "LOGIN NEEDED"
        print(f"{row['profile']:<10} {state:<13} {', '.join(row['users']) or '-'}")
        print(f"{'':<10} tenant {row['tenant'] or '-'}  {row['detail']}")
        if row["subscriptions"]:
            print(f"{'':<10} subs: {', '.join(row['subscriptions'])}")
    failing = sorted({r["profile"] for r in rows if not r["ok"]})
    if failing:
        print()
        for name in failing:
            print(f"fix: azp login {name}")


def write_shims(profiles):
    target = shim_dir()
    target.mkdir(parents=True, exist_ok=True)
    cmd = az_cmd() if profiles else []
    invoke = " ".join(f'"{part}"' for part in cmd)
    installer = 'set "AZ_INSTALLER=MSI"\r\n' if uses_bundled_python(cmd) else ""
    for name, prof in profiles.items():
        win_dir = str(Path(prof["dir"]))
        posix_dir = win_dir.replace("\\", "/")
        write_text(
            target / f"az-{name}.cmd",
            f'@echo off\r\nsetlocal\r\nset "AZURE_CONFIG_DIR={win_dir}"\r\n{installer}{invoke} %*\r\n',
        )
        write_text(target / f"az-{name}", f"#!/usr/bin/env bash\nAZURE_CONFIG_DIR='{posix_dir}' exec az \"$@\"\n")


def remove_shims(name):
    for suffix in (".cmd", ""):
        path = shim_dir() / f"az-{name}{suffix}"
        if path.exists():
            path.unlink()


def write_text(path, content):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def launcher():
    py = shutil.which("py")
    return [py, "-3", "-E", "-s"] if py else [sys.executable, "-E", "-s"]


def cmd_add(args):
    if not NAME_RE.match(args.name):
        raise AzpError("profile names are lowercase letters, digits and dashes")
    profiles = load_registry()
    if args.name in profiles and not args.force:
        raise AzpError(f"profile '{args.name}' already exists (use --force to overwrite)")
    config_dir = Path(args.dir).expanduser() if args.dir else Path.home() / f".azure-{args.name}"
    config_dir = config_dir.resolve()
    owner = next((n for n, p in profiles.items() if Path(p["dir"]).resolve() == config_dir and n != args.name), None)
    if owner:
        raise AzpError(f"{config_dir} already belongs to profile '{owner}'")
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


def cmd_remove(args):
    profiles = load_registry()
    prof = get_profile(profiles, args.name)
    del profiles[args.name]
    save_registry(profiles)
    remove_shims(args.name)
    print(f"removed '{args.name}' (config dir left in place: {prof['dir']})")
    return 0


def cmd_list(args):
    profiles = load_registry()
    if args.json:
        print(json.dumps(profiles, indent=2))
        return 0
    for name, prof in sorted(profiles.items()):
        subs = read_subscriptions(prof["dir"])
        default = next((s.get("name") for s in subs if s.get("isDefault")), "-")
        print(f"{name:<10} {', '.join(users_of(subs)) or '(not logged in)'}  default: {default}  [{prof['dir']}]")
    return 0


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
        print(f"export AZURE_CONFIG_DIR='{path.replace(chr(92), '/')}'")
    elif args.shell == "cmd":
        print(f'set "AZURE_CONFIG_DIR={path}"')
    else:
        print(f"$env:AZURE_CONFIG_DIR = '{path}'")
    return 0


def cmd_which(args):
    query = args.query.lower()
    found = 0
    for name, prof in sorted(load_registry().items()):
        for sub in read_subscriptions(prof["dir"]):
            if query == sub.get("id", "").lower() or query in sub.get("name", "").lower():
                found += 1
                user = sub.get("user", {}).get("name", "?")
                print(f"{name:<10} {sub.get('name')}  {sub.get('id')}  tenant {sub.get('tenantId')}  {user}")
    if not found:
        print(f"no registered profile has a subscription matching '{args.query}'", file=sys.stderr)
        return 1
    return 0


def cmd_keepalive(args):
    if args.install:
        return install_task(args.every_hours)
    if args.uninstall:
        return subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]).returncode
    rows = check_all(load_registry(), hidden=args.notify)
    failing = sorted({r["profile"] for r in rows if not r["ok"]})
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    summary = "all ok" if not failing else "LOGIN NEEDED: " + ", ".join(failing)
    log = azp_home() / "keepalive.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {summary}\n")
        for row in rows:
            handle.write(f"  {row['profile']} {row['tenant']} {'ok' if row['ok'] else 'FAIL'} {row['detail']}\n")
    if not args.notify:
        print_rows(rows)
    elif failing:
        toast("Azure login needed", "Run: " + "; ".join(f"azp login {n}" for n in failing))
    return 0 if not failing else 1


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
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


def install_task(every_hours):
    pyw = shutil.which("pyw")
    if not pyw:
        raise AzpError("pyw (Python launcher, windowless) not found on PATH")
    action = f'"{pyw}" -3 -E -s "{Path(__file__).resolve()}" keepalive --notify'
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", str(every_hours), "/TR", action, "/F"]
    return subprocess.run(cmd).returncode


def cmd_install(args):
    target = shim_dir()
    target.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    exe, *flags = launcher()
    flags = " ".join(flags)
    write_text(target / "azp.cmd", f'@"{exe}" {flags} "{script}" %*\r\n')
    posix = lambda p: str(p).replace("\\", "/")
    write_text(target / "azp", f"#!/usr/bin/env bash\nexec '{posix(exe)}' {flags} '{posix(script)}' \"$@\"\n")
    write_shims(load_registry())
    print(f"shims written to {target}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="azp", description="Keep several Azure CLI identities logged in side by side.")
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
        return args.func(args)
    except AzpError as exc:
        print(f"azp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
