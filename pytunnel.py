#!/usr/bin/env python3

import subprocess
import argparse
import os
import sys

parser = argparse.ArgumentParser(description='This script helps you create and manage SSH tunnels with Python')

# Use mutually exclusive group to prevent conflicting arguments
group = parser.add_mutually_exclusive_group()
group.add_argument('-show', action='store_true', help='Show the list of port forwardings for all jump hosts')
group.add_argument('-start', action='store_true', help='Start the tunnel(s)')
group.add_argument('-status', action='store_true', help='Show tunnel status')
group.add_argument('-stop', action='store_true', help='Stop the tunnel(s)')

# Add a verbose option
parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')

# Add an argument to specify the jump host(s)
parser.add_argument('-j', '--jumphost', nargs='*', help='Specify the jump host(s) to manage. If omitted, applies to all.')

args = parser.parse_args()

# Define your port forwarding mappings here
# Now using a dictionary to hold port forwardings per jump host
jump_hosts = {
    '192.168.1.3': {
        # format: local_port: "remote:port"
        10001: "192.168.1.99:8006",
        10002: "192.168.1.17:9100",
    },
    '192.168.1.30': {
        10003: "192.168.2.50:22",
    },
    # Add more jump hosts and their port forwardings as needed
}

# Expand the control path template
control_path_template = os.path.expanduser('~/.ssh/ssh_tunnel_%h')

def create_ssh_tunnel():
    # Determine which jump hosts to manage
    if args.jumphost:
        # Use specified jump hosts
        target_jump_hosts = {jh: jump_hosts[jh] for jh in args.jumphost if jh in jump_hosts}
        missing_hosts = [jh for jh in args.jumphost if jh not in jump_hosts]
        if missing_hosts:
            print(f"Error: Specified jump host(s) not found in configuration: {', '.join(missing_hosts)}")
            sys.exit(1)
    else:
        # Use all jump hosts
        target_jump_hosts = jump_hosts

    if not target_jump_hosts:
        print("No jump hosts to manage.")
        sys.exit(1)

    if args.show:
        for jump_host, port_forwardings in target_jump_hosts.items():
            print(f"Jump Host: {jump_host}")
            if port_forwardings:
                max_length_local_port = max(len(str(local_port)) for local_port in port_forwardings)
                max_length_remote = max(len(str(remote)) for remote in port_forwardings.values())
                for local_port, remote in port_forwardings.items():
                    print(f"  {str(local_port):>{max_length_local_port}} -> {remote:<{max_length_remote}}")
            else:
                print("  No port forwardings defined.")
            print()
    elif args.start:
        for jump_host, port_forwardings in target_jump_hosts.items():
            control_path = control_path_template.replace('%h', jump_host)
            base_command = ["ssh"]

            if args.verbose:
                base_command.append("-v")

            base_command.extend([
                "-fN",
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=yes",
                "-S", control_path
            ])

            # Add port forwarding arguments
            for local_port, remote in port_forwardings.items():
                forward_arg = f"{local_port}:{remote}"
                base_command.extend(["-L", forward_arg])

            # Add the jump host at the end
            base_command.append(jump_host)

            print(f"Starting tunnel to {jump_host}...")
            print(f"Running command: {' '.join(base_command)}")

            try:
                result = subprocess.run(base_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
                print(f"SSH tunnel to {jump_host} established successfully.\n")
            except subprocess.CalledProcessError as e:
                print(f"Error occurred while starting tunnel to {jump_host}: {e}")
                print(f"Standard Output:\n{e.stdout}")
                print(f"Standard Error:\n{e.stderr}\n")
    elif args.status:
        for jump_host in target_jump_hosts:
            control_path = control_path_template.replace('%h', jump_host)
            cmd = ["ssh"]

            if args.verbose:
                cmd.append("-v")

            cmd.extend(["-T", "-O", "check", "-S", control_path, jump_host])
            print(f"Checking tunnel status for {jump_host}...")
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                print(f"SSH tunnel to {jump_host} is running.\n")
            except subprocess.CalledProcessError:
                print(f"SSH tunnel to {jump_host} is not running.\n")
    elif args.stop:
        for jump_host in target_jump_hosts:
            control_path = control_path_template.replace('%h', jump_host)
            cmd = ["ssh"]

            if args.verbose:
                cmd.append("-v")

            cmd.extend(["-T", "-O", "exit", "-S", control_path, jump_host])
            print(f"Stopping tunnel to {jump_host}...")
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                print(f"SSH tunnel to {jump_host} stopped successfully.\n")
            except subprocess.CalledProcessError as e:
                print(f"Error occurred while stopping tunnel to {jump_host}: {e}")
                print(f"Standard Output:\n{e.stdout}")
                print(f"Standard Error:\n{e.stderr}\n")
    else:
        print('Please run with -h to see arguments')

create_ssh_tunnel()
