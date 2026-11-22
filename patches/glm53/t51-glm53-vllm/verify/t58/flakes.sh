#!/bin/bash
# pyflakes on the 25 runtime files, T56 vs T58, plus the new test; py3.11 compile. /p = patches/vllm (ro), /t = new test
export PATH=/opt/venv/bin:$PATH
pip install -q --timeout 30 pyflakes >/dev/null 2>&1 || PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple pip install -q --timeout 30 pyflakes >/dev/null 2>&1; python -m pyflakes --version; python --version
F=$(cd /p/t58/files && find vllm -name '*.py' | sort)
echo "files: $(echo $F | wc -w)"
for V in t56 t58; do (cd /p/$V/files && python -m pyflakes $F 2>&1 | sed 's/:[0-9]*:[0-9]*:/:/' | sort > /tmp/flakes.$V.txt); done
echo "== new warnings t56 -> t58"; comm -13 /tmp/flakes.t56.txt /tmp/flakes.t58.txt; echo "== gone"; comm -23 /tmp/flakes.t56.txt /tmp/flakes.t58.txt
echo "== t58 warnings in total: $(wc -l < /tmp/flakes.t58.txt)"
echo "== new test file:"; python -m pyflakes /t/test_pd_diag_level.py; echo "rc=$?"
n=0; bad=0; for f in $F; do n=$((n+1)); python -c "import sys; compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" /p/t58/files/$f || bad=$((bad+1)); done
echo "py311 compile: files=$n errors=$bad"
