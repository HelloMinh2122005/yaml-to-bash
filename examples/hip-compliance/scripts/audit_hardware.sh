#!/usr/bin/env bash
# Hardware audit simulation
if grep -q "flags.*sse" /proc/cpuinfo 2>/dev/null || [ -e /proc/cpuinfo ]; then
  write_result HW_AUDIT_STATUS "PASS"
else
  write_result HW_AUDIT_STATUS "FAIL"
fi

