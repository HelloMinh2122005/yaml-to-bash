#!/usr/bin/env bash
# sample-linux.sh - Sample Linux payload script for ADO patch templates
# Pure ASCII, LF line endings, BOM-less UTF-8.

set -euo pipefail

hostname=$(uname -n)
timestamp=$(date -u "+%Y-%m-%d %H:%M:%S UTC")
uptime_info=$(uptime)

echo "=========================================="
echo "Sample Linux Payload Executing"
echo "Host:      ${hostname}"
echo "Timestamp: ${timestamp}"
echo "Uptime:    ${uptime_info}"
echo "Status:    OK"
echo "=========================================="

exit 0
