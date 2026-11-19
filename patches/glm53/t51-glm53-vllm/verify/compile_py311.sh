#!/bin/sh
# Syntax-compile the 25 runtime files and the new test file with this image's python3.
cd /kit/files
python3 --version
n=0; bad=0
for f in $(find vllm -name '*.py' | sort) /src/test_pd_finish_claim.py; do
  n=$((n+1))
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read(), sys.argv[1])" "$f" || bad=$((bad+1))
done
echo "files=$n syntax_errors=$bad"
