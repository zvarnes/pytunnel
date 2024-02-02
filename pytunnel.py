#!/usr/bin/env python3

import subprocess
import argparse

parser = argparse.ArgumentParser(description='This script is to help you create and manage an ssh tunnel with Python')
parser.add_argument('-show', action='store_true', help='show the list of port forwardings')
parser.add_argument('-start', action='store_true', help='Starts the tunnel')
parser.add_argument('-status', action='store_true', help='Show tunnel status')
parser.add_argument('-stop', action='store_true', help='Stops the tunnel')
args = parser.parse_args()

# Define your port forwarding mappings here
port_forwardings = {
    # format: 'local port':'remote:port'
    10001: "192.168.1.16:9100",
    10002: "192.168.1.17:9100",
}
jump_host = '192.168.1.20'
base_command = ["ssh", "-fNT"]
control_path = '-S ~/.ssh/ssh_tunnel_%h_%p_%r'

def create_ssh_tunnel():
    # check that there are entries. better port/ip probably needs to be done
    if bool(port_forwardings) == False:
        print("No entries in 'port_forwardings'")
    elif args.show:
        print(f"Jump Host: {base_command[1]}")
        max_length_local_port = max(len(str(local_port)) for local_port, _ in port_forwardings.items())
        max_length_remote = max(len(str(remote)) for _, remote in port_forwardings.items())
        for local_port, remote in port_forwardings.items():
            print(f"{local_port:>{max_length_local_port}} {remote:<{max_length_remote}}")
    elif args.start:
        # Add port forwarding arguments
        for local_port, remote in port_forwardings.items():
            forward_arg = f"{local_port}:{remote}"
            base_command.extend(["-L", forward_arg])
        
        # Add control path
        base_command.append(control_path)
        base_command.append(jump_host)
        print(f"Running command: {' '.join(base_command)}")

        try:
            subprocess.run(base_command, check=True)
            print("SSH tunnel established successfully.")
        except subprocess.CalledProcessError as e:
            print(f"Error occurred: {e}")
    elif args.status:
        subprocess.run(f"ssh -TO check {control_path} {jump_host}")
    elif args.stop:
        subprocess.run(f"ssh -TO exit {control_path} {jump_host}")
    else:
        print('Please run with -h to see arguments')

create_ssh_tunnel()
