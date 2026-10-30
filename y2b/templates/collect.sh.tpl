#!/bin/bash
RES_FILE=__RES_FILE__
# Diagnostic line outside the result block: ignored by the parser, logged on not_started / remote_incomplete
if [ -e "$RES_FILE" ]; then
  echo "y2-reader: res_file $(wc -c < "$RES_FILE") bytes, modified $(date -r "$RES_FILE" -Is), now $(date -Is), boot $(uptime -s 2>/dev/null)"
else
  echo "y2-reader: res_file missing, now $(date -Is), boot $(uptime -s 2>/dev/null)"
fi
echo "===BEGIN_RESULT_RES==="
cat "$RES_FILE" 2>/dev/null
echo "===END_RESULT_RES==="
exit 0
