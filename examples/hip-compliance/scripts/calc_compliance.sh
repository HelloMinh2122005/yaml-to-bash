#!/usr/bin/env bash
# Computed column script evaluated on host
# Has access to $VM_NAME, $HIP_VERSION, $KERNEL_COUNT, $COL_RECOMMENDATION
if [ "$COL_RECOMMENDATION" = "Compliant" ]; then
  echo "OK"
else
  echo "FAILED"
fi

