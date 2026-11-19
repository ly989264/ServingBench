#!/bin/sh
# Phase 1 (Python 3.11, standard library only): generate the cases and run the tool on each.
# /src: v030.tar (clean v0.30.0), v030port.tar (the port); /tool: tools/field_replay; /out
set -u
mkdir -p /w/clean /w/port /out/cases /out/merged /out/reports
tar -xf /src/v030.tar -C /w/clean vllm/v1/engine/async_llm.py
tar -xf /src/v030port.tar -C /w/port vllm/v1/engine/async_llm.py
python3 --version > /out/python.txt
python3 -I /tool/test/gen_variants.py /w/clean/vllm/v1/engine/async_llm.py /out/cases
for f in /out/cases/T*.py; do
  n=$(basename $f .py)
  python3 -I /tool/replay_field_insertions.py --field $f --field-func add_request \
    --original /w/clean/vllm/v1/engine/async_llm.py --port /w/port/vllm/v1/engine/async_llm.py \
    --qualname AsyncLLM.add_request --write /out/merged/$n.py --report /out/reports/$n.json > /dev/null
  echo "$n exit=$?"
done > /out/tool_exit_codes.txt
