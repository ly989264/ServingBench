#!/bin/bash
# /src: v030.tar v030port.tar (the tested tree) 01 patch; /kit: deliverable patches/vllm (ro)
set -u
P=/kit/t51-vllm-v0.30.0-pd-diag.patch; L=/kit/lab/t51-vllm-v0.30.0-pd-diag.lab-tests.patch
mkdir -p /w/a && cd /w/a && tar -xf /src/v030.tar
git init -q . && git add -A && git -c user.name=t -c user.email=t@l commit -qm base
echo "== SHA256SUMS.before against clean v0.30.0"; shasum -a 256 -c /kit/SHA256SUMS.before | grep -vc ": OK$"
echo "== 01 original: files that do not apply"; git apply --check /src/01-vllm-v0.32.0-diagnostics.patch 2>&1 | grep -c "patch does not apply"
echo "== site patch: files, and files outside vllm/"; grep -c "^diff --git" $P; grep "^diff --git" $P | grep -vc " a/vllm/"
echo "== git apply --check (site patch)"; git apply --check $P; echo "rc=$?"
git apply $P; echo "apply rc=$?"
echo "== SHA256SUMS.after against patched tree"; shasum -a 256 -c /kit/SHA256SUMS.after | grep -vc ": OK$"
echo "== lab patch (tests, tools, docs; lab only, not for the site): files, and files under vllm/"; grep -c "^diff --git" $L; grep "^diff --git" $L | grep -c " a/vllm/"
echo "== git apply --check (lab patch, on top)"; git apply --check $L; echo "rc=$?"
git apply $L; echo "apply rc=$?"
mkdir -p /w/ref && tar -xf /src/v030port.tar -C /w/ref
echo "== site + lab patch vs tested port tree"; diff -r -q -x .git /w/a /w/ref 2>&1 | grep -v "Directory loop detected"; echo "diff done"
echo "== replacement route: clean tree + files/"
mkdir -p /w/b && tar -xf /src/v030.tar -C /w/b && cp -a /kit/files/vllm/. /w/b/vllm/
diff -r -q /w/b/vllm /w/ref/vllm; echo "vllm/ diff rc=$?"
