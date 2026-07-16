#!/usr/bin/env python3

import argparse
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STOP_POLL_TIMEOUT = 2.0  # seconds to wait for a stopped master to release ports
STOP_POLL_INTERVAL = 0.1
PORT_PROBE_TIMEOUT = 0.5  # seconds when checking whether a forward is listening
STDERR_TAIL_LINES = 5


# ==========================
# Configuration
# ==========================

def find_default_config_path() -> Optional[Path]:
    candidates = [
        Path.home() / ".config" / "pytunnel" / "config.yaml",
        Path.home() / ".config" / "pytunnel" / "config.yml",
        Path.home() / ".config" / "pytunnel" / "config.json",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def load_config(config_path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    path: Optional[Path] = Path(config_path).expanduser() if config_path else find_default_config_path()
    if path is None:
        raise RuntimeError(
            "No config file found. Create ~/.config/pytunnel/config.yaml "
            "(see config.example.yaml in the repository) or pass --config."
        )

    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except Exception as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("YAML config provided but PyYAML is not installed. Install with 'pip install pyyaml'.") from exc
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        elif path.suffix.lower() == ".json":
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raise RuntimeError(f"Unsupported config extension: {path.suffix}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to load config from {path}: {exc}") from exc

    return parse_config_object(raw)


def _normalize_port_map(value: Any, host: str, key: str) -> Dict[int, str]:
    # Accepts {port: "host:port"} or [{"local"/"remote": port, "remote"/"local": "host:port"}]
    port_key, dest_key = ("local", "remote") if key == "forwards" else ("remote", "local")
    normalized: Dict[int, str] = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict) or port_key not in item or dest_key not in item:
                raise RuntimeError(f"'{key}' for host {host!r}: list items must be objects with '{port_key}' and '{dest_key}'")
            normalized[_to_port(item[port_key], host, key)] = _validate_hostport(str(item[dest_key]), host, key)
    elif isinstance(value, dict):
        for port, dest in value.items():
            normalized[_to_port(port, host, key)] = _validate_hostport(str(dest), host, key)
    else:
        raise RuntimeError(f"'{key}' for host {host!r} must be a list or object")
    return normalized


def _to_port(value: Any, host: str, key: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(f"'{key}' for host {host!r}: invalid port {value!r}") from None
    if not 1 <= port <= 65535:
        raise RuntimeError(f"'{key}' for host {host!r}: port {port} out of range")
    return port


def _validate_hostport(value: str, host: str, key: str) -> str:
    dest_host, sep, dest_port = value.rpartition(":")
    if not sep or not dest_host or not dest_port.isdigit():
        raise RuntimeError(f"'{key}' for host {host!r}: destination {value!r} must be 'host:port'")
    return value


def parse_config_object(raw: Any) -> Dict[str, Dict[str, Any]]:
    # Supported shapes:
    # {
    #   "jump_hosts": {
    #       "host": {
    #           "forwards": {10001: "1.2.3.4:8006"},          # or [{"local": 10001, "remote": "1.2.3.4:8006"}]
    #           "remote_forwards": {2222: "127.0.0.1:22"},    # or [{"remote": 2222, "local": "127.0.0.1:22"}]
    #           "dynamic": [1080],
    #           "user": "x", "port": 22, "identity_file": "~/.ssh/id_ed25519"
    #       }
    #   }
    # }
    # or directly: {"host": {...}}
    if not isinstance(raw, dict):
        raise RuntimeError("Config file must contain a JSON/YAML object at the top level")

    hosts_section: Any = raw.get("jump_hosts") if "jump_hosts" in raw else raw
    if not isinstance(hosts_section, dict):
        raise RuntimeError("Config 'jump_hosts' must be an object mapping hosts to configuration")

    parsed: Dict[str, Dict[str, Any]] = {}
    for host, cfg in hosts_section.items():
        if not isinstance(cfg, dict):
            raise RuntimeError(f"Config for host {host!r} must be an object")

        entry: Dict[str, Any] = {
            "forwards": _normalize_port_map(cfg.get("forwards", {}), host, "forwards"),
            "remote_forwards": _normalize_port_map(cfg.get("remote_forwards", {}), host, "remote_forwards"),
        }

        dynamic = cfg.get("dynamic", [])
        if isinstance(dynamic, (int, str)):
            dynamic = [dynamic]
        if not isinstance(dynamic, list):
            raise RuntimeError(f"'dynamic' for host {host!r} must be a port or list of ports")
        entry["dynamic"] = [_to_port(p, host, "dynamic") for p in dynamic]

        if not (entry["forwards"] or entry["remote_forwards"] or entry["dynamic"]):
            raise RuntimeError(f"Host {host!r} defines no forwards (need 'forwards', 'remote_forwards', or 'dynamic')")

        for opt_key in ("user", "port", "identity_file"):
            if opt_key in cfg and cfg[opt_key] is not None:
                entry[opt_key] = cfg[opt_key]
        if "port" in entry:
            entry["port"] = _to_port(entry["port"], host, "port")

        parsed[host] = entry

    return parsed


# ==========================
# SSH helpers
# ==========================

def control_path() -> str:
    cm_dir = Path.home() / ".ssh" / "cm"
    cm_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # %C expands to a hash of local host, remote host, port, and user, keeping the
    # socket path short enough for the Unix-socket limit regardless of hostname length.
    return str(cm_dir / "%C")


def _control_cmd_base(target: str, port: Optional[int], verbose: bool) -> List[str]:
    # -O check/exit must resolve the same %C hash as the start command, so the
    # target and -p port have to match exactly.
    cmd = ["ssh"]
    if verbose:
        cmd.append("-v")
    if port:
        cmd.extend(["-p", str(port)])
    cmd.extend(["-T", "-S", control_path()])
    return cmd


def _tail(text: Optional[str], lines: int = STDERR_TAIL_LINES) -> str:
    if not text:
        return ""
    stripped = text.strip().splitlines()
    return "; ".join(stripped[-lines:])


def _error_detail(exc: subprocess.CalledProcessError) -> str:
    stderr = _tail(exc.stderr)
    return f"exit status {exc.returncode}" + (f": {stderr}" if stderr else "")


def build_ssh_command(
    host: str,
    cfg: Dict[str, Any],
    *,
    verbose: bool = False,
) -> List[str]:
    user = cfg.get("user")
    port = cfg.get("port")
    identity_file = cfg.get("identity_file")
    target = f"{user}@{host}" if user else host

    cmd: List[str] = ["ssh"]
    if verbose:
        cmd.append("-v")

    cmd.extend([
        "-fN",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-S", control_path(),
    ])

    if identity_file:
        cmd.extend(["-i", os.path.expanduser(identity_file)])
    if port:
        cmd.extend(["-p", str(port)])

    for local_port, remote in cfg.get("forwards", {}).items():
        cmd.extend(["-L", f"{local_port}:{remote}"])
    for remote_port, local_dest in cfg.get("remote_forwards", {}).items():
        cmd.extend(["-R", f"{remote_port}:{local_dest}"])
    for dyn_port in cfg.get("dynamic", []):
        cmd.extend(["-D", str(dyn_port)])

    cmd.append(target)
    return cmd


def tunnel_status(target: str, port: Optional[int] = None, verbose: bool = False) -> bool:
    cmd = _control_cmd_base(target, port, verbose) + ["-O", "check", target]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True
    except subprocess.CalledProcessError:
        return False


def stop_tunnel(target: str, port: Optional[int] = None, verbose: bool = False, dry_run: bool = False) -> Tuple[bool, str]:
    cmd = _control_cmd_base(target, port, verbose) + ["-O", "exit", target]
    if dry_run:
        return True, shlex.join(cmd)
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True, "stopped"
    except subprocess.CalledProcessError as exc:
        return False, f"{shlex.join(cmd)} -> {_error_detail(exc)}"


def local_port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def local_port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(PORT_PROBE_TIMEOUT)
        return s.connect_ex((host, port)) == 0


def local_bind_ports(cfg: Dict[str, Any]) -> List[int]:
    return list(cfg.get("forwards", {})) + list(cfg.get("dynamic", []))


# ==========================
# Command implementations
# ==========================

def iter_target_hosts(all_hosts: Dict[str, Dict[str, Any]], selection: Optional[List[str]]) -> Dict[str, Dict[str, Any]]:
    if selection:
        missing = [h for h in selection if h not in all_hosts]
        if missing:
            raise ValueError(f"Unknown jump host(s): {', '.join(missing)}")
        return {h: all_hosts[h] for h in selection}
    return all_hosts


def describe_forwards(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    described: List[Dict[str, Any]] = []
    for lp, remote in cfg.get("forwards", {}).items():
        described.append({"type": "local", "port": lp, "destination": remote})
    for rp, local_dest in cfg.get("remote_forwards", {}).items():
        described.append({"type": "remote", "port": rp, "destination": local_dest})
    for dp in cfg.get("dynamic", []):
        described.append({"type": "dynamic", "port": dp, "destination": None})
    return described


def _format_forward(fwd: Dict[str, Any]) -> str:
    flag = {"local": "-L", "remote": "-R", "dynamic": "-D"}[fwd["type"]]
    if fwd["destination"]:
        return f"{flag} {fwd['port']} -> {fwd['destination']}"
    return f"{flag} {fwd['port']}"


def cmd_show(jump_hosts: Dict[str, Dict[str, Any]], as_json: bool = False) -> None:
    if as_json:
        payload = {host: {**{k: cfg[k] for k in ("user", "port", "identity_file") if k in cfg},
                          "forwards": describe_forwards(cfg)}
                   for host, cfg in jump_hosts.items()}
        print(json.dumps(payload, indent=2))
        return

    for host, cfg in jump_hosts.items():
        extras = ", ".join(f"{k}={cfg[k]}" for k in ("user", "port", "identity_file") if k in cfg)
        print(f"Jump Host: {host}" + (f" ({extras})" if extras else ""))
        forwards = describe_forwards(cfg)
        if not forwards:
            print("  No port forwardings defined.")
        for fwd in forwards:
            print(f"  {_format_forward(fwd)}")
        print()


def start_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool, dry_run: bool, restart: bool, skip_port_check: bool) -> Dict[str, Any]:
    user = cfg.get("user")
    port = cfg.get("port")
    target = f"{user}@{host}" if user else host
    cmd = build_ssh_command(host, cfg, verbose=verbose)

    # Idempotency: check if already running
    is_running = tunnel_status(target, port, verbose=verbose)
    if is_running and not restart:
        return {"host": host, "ok": True, "detail": "already running"}

    if is_running and restart:
        ok, stop_info = stop_tunnel(target, port, verbose=verbose, dry_run=dry_run)
        if not ok:
            return {"host": host, "ok": False, "detail": f"failed to stop existing: {stop_info}"}
        if not dry_run:
            # The old master may take a moment to release its listening ports.
            deadline = time.monotonic() + STOP_POLL_TIMEOUT
            while time.monotonic() < deadline:
                if not tunnel_status(target, port, verbose=verbose) and all(
                    local_port_is_free(p) for p in local_bind_ports(cfg)
                ):
                    break
                time.sleep(STOP_POLL_INTERVAL)

    if not skip_port_check:
        busy = [p for p in local_bind_ports(cfg) if not local_port_is_free(p)]
        if busy:
            return {"host": host, "ok": False, "detail": f"local port(s) busy: {', '.join(map(str, busy))}"}

    if dry_run:
        return {"host": host, "ok": True, "detail": shlex.join(cmd)}

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return {"host": host, "ok": True, "detail": "started"}
    except subprocess.CalledProcessError as exc:
        return {"host": host, "ok": False, "detail": _error_detail(exc)}


def stop_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool, dry_run: bool) -> Dict[str, Any]:
    user = cfg.get("user")
    port = cfg.get("port")
    target = f"{user}@{host}" if user else host
    if not tunnel_status(target, port, verbose=verbose):
        return {"host": host, "ok": True, "detail": "not running"}
    ok, info = stop_tunnel(target, port, verbose=verbose, dry_run=dry_run)
    return {"host": host, "ok": ok, "detail": info}


def status_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool) -> Dict[str, Any]:
    user = cfg.get("user")
    port = cfg.get("port")
    target = f"{user}@{host}" if user else host
    is_up = tunnel_status(target, port, verbose=verbose)
    forwards = describe_forwards(cfg)
    if is_up:
        for fwd in forwards:
            if fwd["type"] in {"local", "dynamic"}:
                fwd["listening"] = local_port_is_listening(fwd["port"])
    return {"host": host, "ok": is_up, "state": "running" if is_up else "stopped", "forwards": forwards}


def run_parallel(hosts: Dict[str, Dict[str, Any]], fn, max_workers: Optional[int] = None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    workers = max_workers or min(8, max(1, len(hosts)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        fut_to_host = {executor.submit(fn, host, cfg): host for host, cfg in hosts.items()}
        for fut in as_completed(fut_to_host):
            try:
                results.append(fut.result())
            except Exception as exc:  # pragma: no cover - defensive
                h = fut_to_host[fut]
                results.append({"host": h, "ok": False, "detail": f"exception: {exc}"})
    return sorted(results, key=lambda r: r["host"])


def print_results(results: List[Dict[str, Any]], as_json: bool) -> int:
    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for res in results:
            if "state" in res:
                print(f"{res['host']}: {res['state']}")
                for fwd in res.get("forwards", []):
                    suffix = ""
                    if "listening" in fwd:
                        suffix = " (listening)" if fwd["listening"] else " (not listening)"
                    print(f"  {_format_forward(fwd)}{suffix}")
            else:
                status = "ok" if res["ok"] else "error"
                print(f"{res['host']}: {status} - {res['detail']}")
    return 0 if all(res["ok"] for res in results) else 1


# ==========================
# CLI
# ==========================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and manage SSH tunnels")
    parser.add_argument("-c", "--config", help="Path to config file (JSON or YAML). Defaults to ~/.config/pytunnel/config.{yaml,json}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-j", "-H", "--jumphost", "--host", nargs="*", dest="jumphost", help="Limit to specific jump host(s)")
    common.add_argument("--json", action="store_true", help="Output results as JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", parents=[common], help="Show configured tunnels")

    p_start = sub.add_parser("start", parents=[common], help="Start tunnel(s)")
    p_start.add_argument("--restart", action="store_true", help="Restart if already running")
    p_start.add_argument("--skip-port-check", action="store_true", help="Do not check if local ports are free")
    p_start.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    sub.add_parser("status", parents=[common], help="Show tunnel status")

    p_stop = sub.add_parser("stop", parents=[common], help="Stop tunnel(s)")
    p_stop.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    p_restart = sub.add_parser("restart", parents=[common], help="Restart tunnel(s)")
    p_restart.add_argument("--skip-port-check", action="store_true", help="Do not check if local ports are free")
    p_restart.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    try:
        all_hosts = load_config(args.config)
        target_hosts = iter_target_hosts(all_hosts, args.jumphost)
    except (RuntimeError, ValueError) as exc:
        logging.error(str(exc))
        return 2

    if not target_hosts:
        logging.info("No jump hosts to manage.")
        return 0

    cmd = args.command
    if cmd == "show":
        cmd_show(target_hosts, as_json=args.json)
        return 0

    if cmd == "status":
        results = run_parallel(target_hosts, lambda h, c: status_single_host(h, c, verbose=args.verbose))
        return print_results(results, args.json)

    if cmd == "stop":
        results = run_parallel(target_hosts, lambda h, c: stop_single_host(h, c, verbose=args.verbose, dry_run=args.dry_run))
        return print_results(results, args.json)

    if cmd in {"start", "restart"}:
        restart_flag = args.restart if cmd == "start" else True
        results = run_parallel(
            target_hosts,
            lambda h, c: start_single_host(
                h,
                c,
                verbose=args.verbose,
                dry_run=args.dry_run,
                restart=restart_flag,
                skip_port_check=args.skip_port_check,
            ),
        )
        return print_results(results, args.json)

    parser.print_help()
    return 2


def cli() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli()
