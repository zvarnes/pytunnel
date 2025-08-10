#!/usr/bin/env python3

import argparse
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any


# ==========================
# Defaults and configuration
# ==========================

# Built-in default configuration (used if no config file provided/found)
DEFAULT_JUMP_HOSTS: Dict[str, Dict[str, Any]] = {
    "192.168.1.3": {
        # forwards: local_port -> "remote_host:remote_port"
        "forwards": {
            10001: "192.168.1.99:8006",
            10002: "192.168.1.17:9100",
        }
    },
    "192.168.1.30": {
        "forwards": {
            10003: "192.168.2.50:22",
        }
    },
}


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
        logging.debug("No config file found, using built-in defaults")
        return DEFAULT_JUMP_HOSTS.copy()

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
    except Exception as exc:
        raise RuntimeError(f"Failed to load config from {path}: {exc}") from exc

    return parse_config_object(raw)


def parse_config_object(raw: Any) -> Dict[str, Dict[str, Any]]:
    # Supported shapes:
    # {
    #   "jump_hosts": {
    #       "host": {"forwards": [{"local": 10001, "remote": "1.2.3.4:8006"}], "user": "x", "port": 22, "identity_file": "~/.ssh/id_ed25519"}
    #   }
    # }
    # or directly: {"host": {"forwards": {10001: "1.2.3.4:8006"}}}
    if not isinstance(raw, dict):
        raise RuntimeError("Config file must contain a JSON/YAML object at the top level")

    hosts_section: Any = raw.get("jump_hosts") if "jump_hosts" in raw else raw
    if not isinstance(hosts_section, dict):
        raise RuntimeError("Config 'jump_hosts' must be an object mapping hosts to configuration")

    parsed: Dict[str, Dict[str, Any]] = {}
    for host, cfg in hosts_section.items():
        if not isinstance(cfg, dict):
            raise RuntimeError(f"Config for host {host!r} must be an object")
        forwards = cfg.get("forwards", {})
        normalized_forwards: Dict[int, str] = {}
        if isinstance(forwards, list):
            for item in forwards:
                if not isinstance(item, dict) or "local" not in item or "remote" not in item:
                    raise RuntimeError(f"Forwards for host {host!r} list items must be objects with 'local' and 'remote'")
                normalized_forwards[int(item["local"])] = str(item["remote"])
        elif isinstance(forwards, dict):
            for lp, remote in forwards.items():
                normalized_forwards[int(lp)] = str(remote)
        else:
            raise RuntimeError(f"Forwards for host {host!r} must be a list or object")

        parsed[host] = {
            "forwards": normalized_forwards,
        }
        # Optional fields
        for opt_key in ("user", "port", "identity_file"):
            if opt_key in cfg and cfg[opt_key] is not None:
                parsed[host][opt_key] = cfg[opt_key]

    return parsed


# ==========================
# SSH helpers
# ==========================

def ensure_control_master_dir() -> Path:
    cm_dir = Path.home() / ".ssh" / "cm"
    cm_dir.mkdir(parents=True, exist_ok=True)
    return cm_dir


def control_path_for_host(host: str, user: Optional[str] = None, port: Optional[int] = None) -> str:
    cm_dir = ensure_control_master_dir()
    # Use a safe ControlPath to avoid length/collision issues
    user_part = f"{user}@" if user else ""
    port_part = f":{port}" if port else ":22"
    return str(cm_dir / f"{user_part}{host}{port_part}")


def build_ssh_command(
    host: str,
    forwards: Dict[int, str],
    *,
    user: Optional[str] = None,
    port: Optional[int] = None,
    identity_file: Optional[str] = None,
    verbose: bool = False,
) -> Tuple[List[str], str]:
    target = f"{user}@{host}" if user else host
    control_path = control_path_for_host(host, user, port)

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
        "-S", control_path,
    ])

    if identity_file:
        cmd.extend(["-i", os.path.expanduser(identity_file)])
    if port:
        cmd.extend(["-p", str(port)])

    for local_port, remote in forwards.items():
        cmd.extend(["-L", f"{local_port}:{remote}"])

    cmd.append(target)
    return cmd, control_path


def tunnel_status(control_path: str, target: str, verbose: bool = False) -> bool:
    cmd = ["ssh"]
    if verbose:
        cmd.append("-v")
    cmd.extend(["-T", "-O", "check", "-S", control_path, target])
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True
    except subprocess.CalledProcessError:
        return False


def stop_tunnel(control_path: str, target: str, verbose: bool = False, dry_run: bool = False) -> Tuple[bool, str]:
    cmd = ["ssh"]
    if verbose:
        cmd.append("-v")
    cmd.extend(["-T", "-O", "exit", "-S", control_path, target])
    if dry_run:
        return True, shlex.join(cmd)
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True, shlex.join(cmd)
    except subprocess.CalledProcessError as exc:
        return False, f"{shlex.join(cmd)} -> {exc}"


def local_port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# ==========================
# Command implementations
# ==========================

def iter_target_hosts(all_hosts: Dict[str, Dict[str, Any]], selection: Optional[List[str]]) -> Dict[str, Dict[str, Any]]:
    if selection:
        missing = [h for h in selection if h not in all_hosts]
        if missing:
            raise SystemExit(f"Unknown jump host(s): {', '.join(missing)}")
        return {h: all_hosts[h] for h in selection}
    return all_hosts


def cmd_show(jump_hosts: Dict[str, Dict[str, Any]]) -> None:
    for host, cfg in jump_hosts.items():
        forwards: Dict[int, str] = cfg.get("forwards", {})
        logging.info("Jump Host: %s", host)
        if not forwards:
            print("  No port forwardings defined.\n")
            continue
        max_lp = max((len(str(lp)) for lp in forwards), default=1)
        max_remote = max((len(str(r)) for r in forwards.values()), default=1)
        for lp, remote in forwards.items():
            print(f"  {str(lp):>{max_lp}} -> {remote:<{max_remote}}")
        print()


def start_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool, dry_run: bool, restart: bool, skip_port_check: bool) -> Tuple[str, bool, str]:
    forwards: Dict[int, str] = cfg.get("forwards", {})
    user = cfg.get("user")
    port = cfg.get("port")
    identity = cfg.get("identity_file")
    target = f"{user}@{host}" if user else host
    cmd, control_path = build_ssh_command(host, forwards, user=user, port=port, identity_file=identity, verbose=verbose)

    # Idempotency: check if already running
    is_running = tunnel_status(control_path, target, verbose=verbose)
    if is_running and not restart:
        return host, True, "already running"

    # Optionally stop first (restart)
    if is_running and restart:
        ok, stop_info = stop_tunnel(control_path, target, verbose=verbose, dry_run=dry_run)
        if not ok:
            return host, False, f"failed to stop existing: {stop_info}"

    # Local port checks
    if not skip_port_check:
        busy = [lp for lp in forwards if not local_port_is_free(lp)]
        if busy:
            return host, False, f"local port(s) busy: {', '.join(map(str, busy))}"

    if dry_run:
        return host, True, shlex.join(cmd)

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return host, True, "started"
    except subprocess.CalledProcessError as exc:
        return host, False, f"{exc}"


def stop_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool, dry_run: bool) -> Tuple[str, bool, str]:
    user = cfg.get("user")
    port = cfg.get("port")
    target = f"{user}@{host}" if user else host
    control_path = control_path_for_host(host, user, port)
    if not tunnel_status(control_path, target, verbose=verbose):
        return host, True, "not running"
    ok, info = stop_tunnel(control_path, target, verbose=verbose, dry_run=dry_run)
    return host, ok, info


def status_single_host(host: str, cfg: Dict[str, Any], *, verbose: bool) -> Tuple[str, bool]:
    user = cfg.get("user")
    port = cfg.get("port")
    target = f"{user}@{host}" if user else host
    control_path = control_path_for_host(host, user, port)
    return host, tunnel_status(control_path, target, verbose=verbose)


def run_parallel(hosts: Dict[str, Dict[str, Any]], fn, max_workers: Optional[int] = None):
    results = []
    workers = max_workers or min(8, max(1, len(hosts)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        fut_to_host = {executor.submit(fn, host, cfg): host for host, cfg in hosts.items()}
        for fut in as_completed(fut_to_host):
            try:
                results.append(fut.result())
            except Exception as exc:  # pragma: no cover - defensive
                h = fut_to_host[fut]
                results.append((h, False, f"exception: {exc}"))
    return results


# ==========================
# CLI
# ==========================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and manage SSH tunnels")
    parser.add_argument("-c", "--config", help="Path to config file (JSON or YAML). Defaults to ~/.config/pytunnel/config.{yaml,json}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-j", "-H", "--jumphost", "--host", nargs="*", dest="jumphost", help="Limit to specific jump host(s)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", parents=[common], help="Show configured tunnels")

    p_start = sub.add_parser("start", parents=[common], help="Start tunnel(s)")
    p_start.add_argument("--restart", action="store_true", help="Restart if already running")
    p_start.add_argument("--skip-port-check", action="store_true", help="Do not check if local ports are free")
    p_start.add_argument("--dry-run", action="store_true", help="Print commands without executing")

    p_status = sub.add_parser("status", parents=[common], help="Show tunnel status")

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
    except Exception as exc:
        logging.error(str(exc))
        return 2

    try:
        target_hosts = iter_target_hosts(all_hosts, args.jumphost)
    except SystemExit as exc:
        logging.error(str(exc))
        return 2

    if not target_hosts:
        logging.info("No jump hosts to manage.")
        return 0

    cmd = args.command
    if cmd == "show":
        cmd_show(target_hosts)
        return 0

    if cmd == "status":
        results = run_parallel(target_hosts, lambda h, c: status_single_host(h, c, verbose=args.verbose))
        for host, is_up in sorted(results):
            print(f"{host}: {'running' if is_up else 'stopped'}")
        # Exit code non-zero if any are down
        return 0 if all(is_up for _, is_up in results) else 1

    if cmd == "stop":
        results = run_parallel(target_hosts, lambda h, c: stop_single_host(h, c, verbose=args.verbose, dry_run=args.dry_run))
        rc = 0
        for host, ok, info in sorted(results):
            status = "ok" if ok else "error"
            print(f"{host}: {status} - {info}")
            if not ok:
                rc = 1
        return rc

    if cmd in {"start", "restart"}:
        restart_flag = args.restart if cmd == "start" else True
        skip_port_check = args.skip_port_check
        dry_run = args.dry_run
        results = run_parallel(
            target_hosts,
            lambda h, c: start_single_host(
                h,
                c,
                verbose=args.verbose,
                dry_run=dry_run,
                restart=restart_flag,
                skip_port_check=skip_port_check,
            ),
        )
        rc = 0
        for host, ok, info in sorted(results):
            status = "ok" if ok else "error"
            print(f"{host}: {status} - {info}")
            if not ok:
                rc = 1
        return rc

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
