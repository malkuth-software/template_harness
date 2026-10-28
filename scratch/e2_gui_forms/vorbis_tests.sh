#!/bin/sh
# Builds and runs stx_vorbis's own tests standalone: plain, then ASan+UBSan.
. /d/gui_forms_w3/env.sh
SRC=/d/gui_forms_w3/gui_forms/stx_vorbis
TC="$(cygpath -m /d/gui_forms_w3/gui_forms/cmake/llvm22.cmake)"
OUT=/c/Users/Shadow/AppData/Local/Temp/claude/C--Users-Shadow-gui-forms/005dc99f-1086-463f-978e-5723b295c49c/scratchpad/e2
for MODE in plain sanitize; do
  B=/d/gui_forms_w3/.build/native-windows-x64/stx-vorbis-$MODE
  SAN=OFF
  [ "$MODE" = sanitize ] && SAN=ON
  cmake -S $SRC -B $B -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE="$TC" \
    -DSTX_VORBIS_TESTS=ON -DSTX_VORBIS_SANITIZERS=$SAN > $OUT/vorbis_$MODE.log 2>&1 || { echo "$MODE CONFIGURE FAILED"; continue; }
  cmake --build $B --parallel 6 >> $OUT/vorbis_$MODE.log 2>&1 || { echo "$MODE BUILD FAILED"; continue; }
  ctest --test-dir $B --output-on-failure --timeout 600 >> $OUT/vorbis_$MODE.log 2>&1
  echo "$MODE ctest rc=$?"
  grep -E "tests passed|Failed|\*\*\*|runtime error|AddressSanitizer" $OUT/vorbis_$MODE.log | head -12
done
