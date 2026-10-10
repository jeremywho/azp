# azp: multiple Azure CLI accounts, all logged in at once

Keep every Azure / Microsoft Entra ID account (work, personal, client tenants) logged in to the Azure CLI simultaneously, and run any command as any of them. Switching between profiles doesn't require another `az login`, and `az account set` in one profile can't retarget a different profile.

```
az-work group list -o table     # az as your work account
az-personal account show        # az as your personal account, same moment
azp status                      # does every account still have a valid token?
```

Each profile is an isolated `AZURE_CONFIG_DIR`, so it has its own token cache and active subscription. A scheduled keepalive refreshes every login and shows a Windows toast when one lapses. It's a single Python file using only the standard library. Built for Windows (PowerShell, cmd, Git Bash); untested elsewhere.

Isolation is per config directory, not per terminal. Terminals using the same profile share its active subscription. Registering `~/.azure` also keeps that profile shared with plain `az`; use separate directories when you need independent defaults.

## Install with an AI agent

Paste this into Claude Code, Codex, Copilot, or any coding agent:

```text
Install the azp CLI from https://github.com/jeremywho/azp on this Windows machine.
1. Clone it to a permanent location and run `py -3 azp.py install` from the clone. That writes the azp and az-<profile> shims into ~/.local/bin; make sure that directory is on PATH for PowerShell and Git Bash.
2. Find my existing Azure CLI logins: ~/.azure plus any other directory I have used as AZURE_CONFIG_DIR. For each, read azureProfile.json, show me the user and tenant, and register it with `azp add <short-name> --dir <dir>`. Do not log out, move, or delete anything.
3. Ask me which other accounts I want. For each: `azp add <name> --tenant <tenant-id>`, then run `azp login <name> --device-code` and give me the code to enter.
4. Run `azp keepalive --install`, then `azp status` and confirm it exits 0. Finish by listing the az-<name> commands I now have.
```

Manual install: `git clone https://github.com/jeremywho/azp && py -3 azp/azp.py install`

## Commands

| Command | Does |
|---|---|
| `azp add <name> [--tenant ID] [--dir D]` | Register a profile (dir defaults to `~/.azure-<name>`) and create its `az-<name>` command |
| `azp login <name> [--device-code]` | `az login` into that profile; extra args pass through |
| `az-<name> ...` / `azp az <name> ...` | Run `az` as that account |
| `azp run <name> -- <cmd>` | Run any command (e.g. an SDK script using `AzureCliCredential`) as that account |
| `azp env <name> [--shell bash\|cmd]` | Print the line that points the current shell at a profile (`azp env work \| iex`) |
| `azp which <subscription>` | Find which account owns a subscription |
| `azp status` / `azp list` | Token health per account and tenant (exit 1 if any need login) / offline listing |
| `azp keepalive --install` | Refresh every account every 6 h; toast on failure |

Limit: tenants that enforce a Conditional Access sign-in frequency or fresh MFA (`AADSTS50078`) still need `azp login <name>` on their schedule. The keepalive turns that into a toast instead of a mid-task failure.

Imported directories may contain multiple identities. Status and keepalive select a subscription for each cached identity/tenant pair instead of using the default identity for every tenant, with up to eight checks running concurrently. Registered tenant GUIDs are compared without regard to case; a GUID with no cached account is reported as needing login. Domain aliases are passed to `az login` unchanged, and health checks use the resulting cached tenant GUIDs rather than comparing a domain name to a GUID.

A fresh Azure config containing only installation metadata is reported as not logged in. A damaged profile or registry entry is reported as an error while the other profiles are still checked. Recover an invalid entry with `azp remove <name>` or `azp add <name> --force`; other profiles remain usable. An empty registry is not considered healthy. When a check error and an expired login occur together, notifications include both diagnostic guidance and the affected login commands.

`AZP_HOME` selects the registry and log directory (default `~/.azp`). You can also pass `azp --home <directory> <command>`. Installing keepalive records the resolved home in the task, so a session-specific environment variable is not required when it runs. The installation manages one task named `azp-keepalive`; installing again replaces that task. Failures are written to `keepalive.log`, including notification failures when the log is writable.

Generated shims and PowerShell/Bash environment output escape literal paths. `azp env --shell cmd` rejects paths containing percent signs, exclamation marks, or other characters that cmd cannot safely preserve in an assignment; use `azp run` or `azp az` for those paths. The generated `.cmd` shims support percent signs and exclamation marks in paths. Bash profile shims find `az` on the current PATH, so moving the Azure CLI does not require regenerating them.

After upgrading, run `azp install` to regenerate existing shims, then `azp keepalive --install` to update the scheduled action. Profile registrations and Azure credential caches are kept in place.

## Tests

Run `py -3 -m unittest discover -s tests -v` on Windows. Tests use temporary profiles and a fake Azure CLI; they do not access real credentials or install scheduled tasks. Windows CI exercises Python 3.10 and 3.14, including PowerShell, cmd, Git Bash, concurrent registry changes, and keepalive failures.

## Why plain `az` isn't enough

`az login` can hold several accounts, but the active subscription is global. `az account set` in one terminal silently changes every other terminal and agent session ([Azure/azure-cli#33833](https://github.com/Azure/azure-cli/issues/33833), [#17989](https://github.com/Azure/azure-cli/issues/17989), [#10265](https://github.com/Azure/azure-cli/issues/10265)). On Windows, azp also avoids the `az.cmd` bug where `--query length(@)` from PowerShell fails with `-o was unexpected at this time`.

## Search terms

Azure CLI multiple accounts · az login multiple tenants at the same time · switch Azure account from the command line · Azure CLI profiles · azctx / az-switch / kubectx for Azure · az account set affects all terminals · AZURE_CONFIG_DIR per profile · stay logged in to Azure CLI · AADSTS700082 refresh token expired · multiple Entra ID / Azure AD tenants · work and personal Azure subscriptions side by side

## License

MIT
