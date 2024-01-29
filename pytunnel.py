#!/usr/bin/env python3

import subprocess
import sys

# Define your port forwarding mappings here
port_forwardings = {
    # format: 'local port':'remote:port'
    # Example: 10001: "192.168.1.16:9100",

}

def create_ssh_tunnel():
    if sys.argv[1] == '-h':
        print("This will output help")
    elif sys.argv[1] == 'show':
        # for local_port, remote in port_forwardings.items():
        #     print(f"{local_port:^4} {remote}")
        max_length_local_port = max(len(str(local_port)) for local_port, _ in port_forwardings.items())
        max_length_remote = max(len(str(remote)) for _, remote in port_forwardings.items())

        for local_port, remote in port_forwardings.items():
            print(f"{local_port:>{max_length_local_port}} {remote:<{max_length_remote}}")


    else:
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
