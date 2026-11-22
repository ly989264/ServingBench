#!/bin/bash
# After drive2.sh: post_build.sh (parsing table, class layout, small test library, sym_check, exports v1/v2,
# NEEDED/RUNPATH/SONAME, old-signature test, full-ring test, integration test compile) in a container from the build
# image, with ~6 GB headroom checked first; then lab_test_v2.sh in a plain cann container with /r = v2 + small library.
set -u
SP=${SP:?set SP to the build scratch folder}; T=$SP/t58mc; K=${K:?set K to patches/glm53/t51-glm53-kit/mooncake/replace}
docker rm -fv t58-mcpost >/dev/null 2>&1
timeout 3600 docker run --rm --name t58-mcpost --memory 12g -v $T/src:/build/src -v $SP/mcbuild_oe/deps:/build/deps:ro -v $SP/mcbuild_oe/deps_dl:/build/deps_dl:ro -v $K/build:/build/scripts:ro -v $K/build_v2:/build/v2:ro -v $T/v1:/v1:ro -v $T/out:/out -v $T/orig_client_service.h:/orig/client_service.h:ro -v $SP/mcbuild_oe/out/lay:/lay:ro -e SKIP_ITEST=${SKIP_ITEST:-0} t58-mcbuild-img:1 bash /build/v2/post_build.sh > $T/post_build.txt 2>&1
echo "post_build exit $?"
mkdir -p $T/r && cp $T/out/libmooncake_store.so $T/r/ && cp $T/out/post/small_relinked.so $T/r/ 2>/dev/null
timeout 3600 docker run --rm --name t58-mclab -v $T/r:/r:ro -v $SP/mcbuild_oe/out/lay:/lay:ro -v $K/build_v2:/build/v2:ro quay.io/ascend/cann:9.2.0-a3-openeuler24.03-py3.11 bash /build/v2/lab_test_v2.sh > $T/lab_test_v2.txt 2>&1
echo "lab exit $?"
