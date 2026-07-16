# pytunnel

Manage persistent SSH tunnels from a simple config file. pytunnel opens one
OpenSSH [ControlMaster](https://man.openbsd.org/ssh_config#ControlMaster)
connection per jump host carrying all of its port forwards, so tunnels are
idempotent to start, cheap to check, and clean to stop.

- **Local forwards** (`ssh -L`) — reach services behind a jump host
- **Remote forwards** (`ssh -R`) — expose local services on the jump host
- **Dynamic forwards** (`ssh -D`) — SOCKS proxy through the jump host
- Parallel start/stop/status across hosts, JSON output, dry-run mode

## Install

```bash
pipx install '.[yaml]'      # from a checkout; [yaml] enables YAML configs
# or
pip install -e '.[yaml]'
```

Requires Python ≥ 3.9 and OpenSSH. JSON configs work with no dependencies.

## Quick start

```bash
mkdir -p ~/.config/pytunnel
cp config.example.yaml ~/.config/pytunnel/config.yaml
$EDITOR ~/.config/pytunnel/config.yaml

pytunnel show       # print configured tunnels
pytunnel start      # start all tunnels (no-op if already running)
pytunnel status     # check masters and verify forwarded ports are listening
pytunnel stop       # tear everything down
```

## Usage

```
pytunnel [-c CONFIG] [-v] {show,start,status,stop,restart} [options]
```

| Command | Description |
|---|---|
| `show` | Print configured hosts and forwards |
| `start` | Start tunnels; already-running hosts are left alone (`--restart` to bounce them) |
| `status` | Report running/stopped per host; checks each local/dynamic port is actually listening |
| `stop` | Stop tunnels via the control socket |
| `restart` | Stop (if running) then start |

Options shared by all commands:

- `-j/--jumphost HOST [HOST ...]` — limit to specific hosts
- `--json` — machine-readable output
- `-c/--config PATH` — config file (default: `~/.config/pytunnel/config.{yaml,yml,json}`)
- `-v/--verbose` — pass `-v` to ssh and enable debug logging

`start`/`restart` also accept `--dry-run` (print the ssh commands instead of
running them) and `--skip-port-check` (don't verify local ports are free
first). `stop` accepts `--dry-run` too.

Examples:

```bash
pytunnel start -j gateway.example.com --dry-run
pytunnel status --json | jq '.[] | select(.ok | not)'
pytunnel restart -j 192.0.2.10
```

## Configuration

YAML (needs the `yaml` extra) or JSON. See [config.example.yaml](config.example.yaml).

```yaml
jump_hosts:                      # optional wrapper; hosts may also be top-level
  gateway.example.com:
    user: admin                  # optional (default: your ssh config / username)
    port: 2222                   # optional (default: 22)
    identity_file: ~/.ssh/id_ed25519   # optional
    forwards:                    # ssh -L  local_port -> destination via jump host
      10001: "10.0.0.50:8006"
      # or list form:
      # - local: 10001
      #   remote: "10.0.0.50:8006"
    remote_forwards:             # ssh -R  jump-host port -> destination via your machine
      19022: "127.0.0.1:22"
    dynamic:                     # ssh -D  local SOCKS proxy ports
      - 1080
```

Each host needs at least one of `forwards`, `remote_forwards`, or `dynamic`.
Everything else in your `~/.ssh/config` (ProxyJump, key agents, etc.) applies
as normal since pytunnel just drives `ssh`.

## How it works

Each host gets a ControlMaster started with
`ssh -fN -o ControlMaster=auto -o ControlPersist=600 -o ExitOnForwardFailure=yes ...`.
Control sockets live in `~/.ssh/cm/` using the `%C` hash token, so paths stay
under the Unix-socket length limit. `status` and `stop` talk to the socket with
`ssh -O check` / `ssh -O exit`. `BatchMode=yes` is set, so authentication must
be non-interactive (keys/agent); failures surface ssh's stderr in the output.

## Exit codes

- `0` — success (for `status`: all tunnels running)
- `1` — one or more hosts failed (or, for `status`, are down)
- `2` — configuration or usage error

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest --cov=pytunnel
```
