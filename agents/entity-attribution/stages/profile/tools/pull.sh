#!/usr/bin/env bash
# Pull the last N complete days for one tenant and environment, ONE DAY PER QUERY SET (a one-shot multi-day query does not finish).
# Writes <out_dir>/<YYYY-MM-DD>/{coverage,ipconc,tenant,series,burst}.csv. Skips a day whose files already exist, so a failed run resumes.
# Usage: pull.sh <customer_id> <environment> <end_date YYYY-MM-DD, last complete day> <days> <out_dir> [slice_hours]
# slice_hours (1,2,3,4,6,8,12; default 24): split each day into smaller queries when a full day does not finish. Files go to <out_dir>/<day>/sHH/.
set -euo pipefail
[ $# -ge 5 ] && [ $# -le 6 ] || { echo "usage: $0 <customer_id> <environment> <end_date> <days> <out_dir> [slice_hours]" >&2; exit 2; }
CID=$1 ENV=$2 END=$3 N=$4 OUT=$5 SL=${6:-24}
[[ $SL =~ ^(1|2|3|4|6|8|12|24)$ ]] || { echo "slice_hours must divide 24" >&2; exit 2; }
[[ $CID =~ ^[0-9a-f-]{36}$ ]] || { echo "customer_id must be a uuid" >&2; exit 2; }
[[ $ENV =~ ^[A-Za-z0-9._-]+$ ]] || { echo "bad environment" >&2; exit 2; }
[[ $N =~ ^[0-9]+$ ]] || { echo "days must be a number" >&2; exit 2; }
HERE=$(dirname "$0")
BASE="customer_id = '$CID' AND environment = '$ENV'"
for ((i = N - 1; i >= 0; i--)); do
  DAY=$(python3 -c "import datetime,sys;print(datetime.date.fromisoformat(sys.argv[1])-datetime.timedelta(days=int(sys.argv[2])))" "$END" "$i")
  NEXT=$(python3 -c "import datetime,sys;print(datetime.date.fromisoformat(sys.argv[1])+datetime.timedelta(days=1))" "$DAY")
  for ((h = 0; h < 24; h += SL)); do
    if [ "$SL" = 24 ]; then D="$OUT/$DAY"; WIN="start_time_millis_ts >= timestamp '$DAY 00:00:00' AND start_time_millis_ts < timestamp '$NEXT 00:00:00'"
    else
      printf -v HH '%02d' "$h"; e=$((h + SL)); D="$OUT/$DAY/s$HH"
      if [ $e -ge 24 ]; then END_TS="$NEXT 00:00:00"; else printf -v EH '%02d' "$e"; END_TS="$DAY $EH:00:00"; fi
      WIN="start_time_millis_ts >= timestamp '$DAY $HH:00:00' AND start_time_millis_ts < timestamp '$END_TS'"
    fi
    mkdir -p "$D"
    for f in "$HERE"/queries/*.sql; do
      n=$(basename "$f" .sql)
      [ -s "$D/$n.csv" ] && continue
      SQL=$(sed -e '/^--/d' -e "s/{{base}}/$BASE/" -e "s/{{day}}/$WIN/" "$f")
      kubectl -n traceable exec -i deployment/trino-coordinator-ro -c trino-coordinator-ro -- \
        trino --server localhost:8080 --catalog iceberg --schema iceberg_datalake \
        --execute "$SQL" --output-format CSV_HEADER 2>&1 | grep -v JDK_JAVA_OPTIONS > "$D/$n.csv.part"
      if grep -qE '^(Query [0-9_a-z]+ failed|line [0-9]+:)' "$D/$n.csv.part"; then echo "$D $n failed:" >&2; head -3 "$D/$n.csv.part" >&2; exit 1; fi
      mv "$D/$n.csv.part" "$D/$n.csv"
      echo "$D $n: $(($(wc -l < "$D/$n.csv") - 1)) rows"
    done
  done
done
