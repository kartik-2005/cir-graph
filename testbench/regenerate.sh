#!/usr/bin/env bash
# ==============================================================================
# Recompile every testbench case to raw CIR (committed next to its sources) and
# check that each program builds natively and exits 0.
#
#   CLANG=/path/to/cir-enabled/clang testbench/regenerate.sh [case ...]
#
# Only needed after changing a case or moving to a new compiler. CIR's textual
# format follows the compiler version, so commit the .cir files together with
# the clang version recorded in testbench/CLANG_VERSION.
# ==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLANG_BIN="${CLANG:-${HOME}/llvm-project/build/bin/clang}"
[[ -x "${CLANG_BIN}" ]] || { echo "[-] set CLANG to a CIR-enabled clang" >&2; exit 1; }

if [[ $# -gt 0 ]]; then CASES=("$@"); else
    CASES=(); for dir in "${HERE}"/cases/*/; do CASES+=("$(basename "${dir}")"); done
fi

BIN_DIR="$(mktemp -d)"
trap 'rm -rf "${BIN_DIR}"' EXIT

for name in "${CASES[@]}"; do
    case_dir="${HERE}/cases/${name}"
    [[ -d "${case_dir}" ]] || { echo "[-] no such case: ${name}" >&2; exit 1; }
    (
        # Compile from inside the case so module names and locations stay relative.
        cd "${case_dir}"
        rm -f ./*.cir
        sources=()
        for src in *.c *.cpp; do
            [[ -f "${src}" ]] || continue
            sources+=("${src}")
            case "${src}" in *.c) std=c11 ;; *) std=c++20 ;; esac
            "${CLANG_BIN}" -c "-std=${std}" -Xclang -emit-cir -Xclang -clangir-disable-passes \
                -I . "${src}" -o "${src%.*}.cir"
            # clang records the absolute source path in the module name; keep fixtures portable.
            sed -i "s|${case_dir}/||g" "${src%.*}.cir"
        done
        # Native build + run: each program checks its own result and exits 0.
        driver="${CLANG_BIN}"
        for src in "${sources[@]}"; do [[ "${src}" == *.cpp ]] && driver="${CLANG_BIN}++"; done
        "${driver}" -I . "${sources[@]}" -o "${BIN_DIR}/${name}"
        "${BIN_DIR}/${name}" || { echo "[-] ${name}: program exited $?" >&2; exit 1; }
        echo "[+] ${name}: ${#sources[@]} TU(s), program exits 0"
    )
done

"${CLANG_BIN}" --version | head -1 > "${HERE}/CLANG_VERSION"
echo "[+] Recorded $(cat "${HERE}/CLANG_VERSION")"
