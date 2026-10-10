import json
import os
import subprocess
import sys
from pathlib import Path

config_dir = Path(os.environ["AZURE_CONFIG_DIR"])
args = sys.argv[1:]
behavior_path = config_dir / "fake.json"
behavior = json.loads(behavior_path.read_text()) if behavior_path.exists() else {}

commands = [["install"]] * behavior.get("reinstall", 0) + behavior.get("azp_commands", [])
for command in commands:
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "azp.py"), *command],
                          capture_output=True, text=True)
    if proc.returncode:
        print(proc.stderr, file=sys.stderr)
        sys.exit(proc.returncode)

if args[:2] == ["account", "get-access-token"]:
    subs = json.loads((config_dir / "azureProfile.json").read_text(encoding="utf-8-sig"))["subscriptions"]
    if "--subscription" in args:
        subscription = args[args.index("--subscription") + 1]
        account = next(s for s in subs if s["id"] == subscription)
        tenant = account["tenantId"]
    else:
        account = next(s for s in subs if s.get("isDefault"))
        tenant = args[args.index("--tenant") + 1]
    identity = account["user"]["name"] + "|" + tenant
    result = behavior.get("identity_tokens", {}).get(identity, behavior.get("tokens", {}).get(tenant))
    if result is None:
        print("ERROR: Please run 'az login' to setup account.", file=sys.stderr)
        sys.exit(1)
    if result.startswith("AADSTS"):
        print(f"ERROR: {result}. Trace ID: x", file=sys.stderr)
        sys.exit(1)
    print(result)
    sys.exit(0)

print(json.dumps({"config_dir": str(config_dir), "args": args}))
sys.exit(behavior.get("exit", 0))
