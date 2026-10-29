#!/bin/bash
RES_FILE=__RES_FILE__
echo "===BEGIN_RESULT_RES==="
cat "$RES_FILE" 2>/dev/null
echo "===END_RESULT_RES==="
exit 0

