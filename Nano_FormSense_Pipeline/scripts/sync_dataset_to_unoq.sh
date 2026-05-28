#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 <session_dir> <session_id> <user@unoq-host>" >&2
  exit 2
fi

session_dir="$1"
session_id="$2"
unoq_host="$3"
remote_dir="~/formsense_data/inbox"

for suffix in raw filtered features; do
  file="${session_dir}/${session_id}_${suffix}.csv"
  if [[ ! -f "${file}" ]]; then
    echo "Missing dataset file: ${file}" >&2
    exit 1
  fi
done

ssh "${unoq_host}" "mkdir -p ${remote_dir}"
scp "${session_dir}/${session_id}_raw.csv" \
    "${session_dir}/${session_id}_filtered.csv" \
    "${session_dir}/${session_id}_features.csv" \
    "${unoq_host}:${remote_dir}/"

echo "Synced ${session_id} CSV dataset to ${unoq_host}:${remote_dir}/"
