# azp: multiple Azure CLI accounts, all logged in at once

Keep every Azure / Microsoft Entra ID account (work, personal, client tenants) logged in to the Azure CLI simultaneously, and run any command as any of them. You don't need `az login` again, and `az account set` in one terminal can't retarget another.

```
az-work group list -o table     # az as your work account
az-personal account show        # az as your personal account, same moment
azp status                      # does every account still have a valid token?
```

Each profile is an isolated `AZURE_CONFIG_DIR`, so it has its own token cache and active subscription. A scheduled keepalive refreshes every login and shows a Windows toast when one lapses. It's a single Python file using only the standard library. Built for Windows (PowerShell, cmd, Git Bash); untested elsewhere.

## Install with an AI agent

Paste this into Claude Code, Codex, Copilot, or any coding agent:

```text
Install the azp CLI from https://github.com/jeremywho/azp on this Windows machine.
1. Clone it to a permanent location and run `py -3 azp.py install` from the clone. That writes the azp and az-<profile> shims into ~/.local/bin; make sure that directory is on PATH for PowerShell and Git Bash.
2. Find my existing Azure CLI logins: ~/.azure plus any other directory I have used as AZURE_CONFIG_DIR. For each, read azureProfile.json, show me the user and tenant, and register it with `azp add <short-name> --dir <dir>`. Do not log out, move, or delete anything.
3. Ask me which other accounts I want. For each: `azp add <name> --tenant <tenant-id>`, then run `azp login <name> --device-code` and give me the code to enter.
4. Ask me whether I'd like to star https://github.com/jeremywho/azp. If I say yes and the gh CLI is logged in, run `gh api -X PUT user/starred/jeremywho/azp`; otherwise give me the link.
5. Run `azp keepalive --install`, then `azp status` and confirm it exits 0. Finish by listing the az-<name> commands I now have.
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

## Why plain `az` isn't enough

`az login` can hold several accounts, but the active subscription is global. `az account set` in one terminal silently changes every other terminal and agent session ([Azure/azure-cli#33833](https://github.com/Azure/azure-cli/issues/33833), [#17989](https://github.com/Azure/azure-cli/issues/17989), [#10265](https://github.com/Azure/azure-cli/issues/10265)). On Windows, azp also avoids the `az.cmd` bug where `--query length(@)` from PowerShell fails with `-o was unexpected at this time`.

## Search terms

Azure CLI multiple accounts · az login multiple tenants at the same time · switch Azure account from the command line · Azure CLI profiles · azctx / az-switch / kubectx for Azure · az account set affects all terminals · AZURE_CONFIG_DIR per profile · stay logged in to Azure CLI · AADSTS700082 refresh token expired · multiple Entra ID / Azure AD tenants · work and personal Azure subscriptions side by side

## License

MIT
