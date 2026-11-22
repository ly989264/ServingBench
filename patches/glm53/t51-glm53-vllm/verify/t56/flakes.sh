#!/bin/bash
export PATH=/opt/venv/bin:$PATH
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple pip install -q pyflakes >/dev/null 2>&1; python -m pyflakes --version
python --version
cd /cg
F=$(cd t56 && find vllm -name '*.py' | sort | grep -Fx -f /src/t56_runtime_files.txt)
echo "files: $(echo $F | wc -w)"
for V in t51 t56; do (cd $V && python -m pyflakes $F 2>&1 | sed 's/:[0-9]*:[0-9]*:/:/' | sort > /tmp/flakes.$V.txt); done
echo "== new warnings t51 -> t56"; comm -13 /tmp/flakes.t51.txt /tmp/flakes.t56.txt; echo "== gone"; comm -23 /tmp/flakes.t51.txt /tmp/flakes.t56.txt
echo "== t56 warnings in total: $(wc -l < /tmp/flakes.t56.txt)"
n=0; bad=0; for f in $F; do n=$((n+1)); python -c "import ast,sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" t56/$f || bad=$((bad+1)); done
for f in tests/v1/metrics/test_pd_kv_ready_wait.py; do :; done
echo "py311 compile: files=$n errors=$bad"
