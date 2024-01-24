#!/usr/bin/env python3

import subprocess

# Define your port forwarding mappings here
port_forwardings = {
    10001: "192.168.1.16:22",
    # Add more mappings as needed
}

def create_ssh_tunnel():
    base_command = ["ssh", "192.168.1.20"]

    # Add port forwarding arguments
    for local_port, remote in port_forwardings.items():
        forward_arg = f"{local_port}:{remote}"
        base_command.extend(["-L", forward_arg])

    print(f"Running command: {' '.join(base_command)}")

    try:
        subprocess.run(base_command, check=True)
        print("SSH tunnel established successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error occurred: {e}")

create_ssh_tunnel()
