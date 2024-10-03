#!/bin/bash

# Description: This script helps create and manage an SSH tunnel.

# Define port forwarding mappings
declare port_forwardings=(
	[10001]="192.168.1.16:9100"
	[10002]="192.168.1.17:9100"
)

# Define jump host
jump_host="192.168.1.20"
control_path="~/.ssh/ssh_tunnel_${jump_host}_22_zvarnes" # Adjust as necessary

# Parse command line arguments
case "$1" in
-show)
	echo "Jump Host: $jump_host"
	for local_port in "${!port_forwardings[@]}"; do
		echo "$local_port ${port_forwardings[$local_port]}"
	done
	;;
-start)
	base_command="ssh -fN $jump_host"

	# Add port forwarding arguments
	for local_port in "${!port_forwardings[@]}"; do
		base_command+=" -L $local_port:${port_forwardings[$local_port]}"
	done

	# Add control path
	base_command+=" -S $control_path"

	echo "Running command: $base_command"
	eval $base_command
	;;
-status)
	ssh -S $control_path -O check
	;;
-stop)
	ssh -S $control_path -O exit
	;;
*)
	echo "Usage: $0 {-show|-start|-status|-stop}"
	exit 1
	;;
esac
