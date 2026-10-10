import json
import os
import sys
from pathlib import Path

config_dir = Path(os.environ["AZURE_CONFIG_DIR"])
args = sys.argv[1:]
behavior_path = config_dir / "fake.json"
behavior = json.loads(behavior_path.read_text()) if behavior_path.exists() else {}

if args[:2] == ["account", "get-access-token"]:
    tenant = args[args.index("--tenant") + 1]
    result = behavior.get("tokens", {}).get(tenant)
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
