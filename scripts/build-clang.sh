#!/usr/bin/env bash
# ==============================================================================
# Build the CIR-enabled clang that cir-graph is validated against, without Docker.
#
#   scripts/build-clang.sh [--jobs N] [--dir DIR]
#
# Default location: <repo>/.toolchain (git-ignored). analyze.sh finds
# <repo>/.toolchain/build/bin/clang automatically, so after this one-time build
#   ./analyze.sh my_code/*.c
# just works. Needs: git, cmake, ninja, a C++17 compiler, ~8 GB RAM, ~30 GB disk.
# Takes roughly 1-3 hours. Re-running resumes an interrupted build.
# ==============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LLVM_COMMIT="aa8d1afeed3714c2805bdf069ca273f00a789f31"   # keep in sync with Dockerfile
DIR="${REPO_DIR}/.toolchain"
MEM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 8388608) / 1048576 ))
CPUS="$(nproc 2>/dev/null || echo 4)"
JOBS=$(( MEM_GB < CPUS ? MEM_GB : CPUS ))     # ~1 GB RAM per compile job
(( JOBS >= 1 )) || JOBS=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --jobs) JOBS="$2"; shift 2 ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "[-] unknown option: $1" >&2; exit 1 ;;
    esac
done

for tool in git cmake ninja c++; do
    command -v "${tool}" >/dev/null 2>&1 || {
        echo "[-] '${tool}' not found. On Debian/Ubuntu: sudo apt install git cmake ninja-build build-essential" >&2
        exit 1; }
done

mkdir -p "${DIR}"
SRC="${DIR}/llvm-project"
BUILD="${DIR}/build"

if [[ ! -d "${SRC}/.git" ]]; then
    echo "[*] Fetching llvm-project @ ${LLVM_COMMIT:0:12} (shallow) ..."
    git init -q "${SRC}"
    git -C "${SRC}" fetch -q --depth 1 https://github.com/llvm/llvm-project.git "${LLVM_COMMIT}"
    git -C "${SRC}" checkout -q FETCH_HEAD
fi

if [[ ! -f "${BUILD}/build.ninja" ]]; then
    echo "[*] Configuring (Release, clang + mlir, CIR enabled) ..."
    cmake -S "${SRC}/llvm" -B "${BUILD}" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLVM_ENABLE_ASSERTIONS=OFF \
        -DLLVM_ENABLE_PROJECTS="clang;mlir" \
        -DCLANG_ENABLE_CIR=ON \
        -DLLVM_TARGETS_TO_BUILD=host \
        -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
        -DLLVM_PARALLEL_LINK_JOBS=1
fi

echo "[*] Building clang with ${JOBS} job(s) (override with --jobs N) ..."
ninja -C "${BUILD}" -j "${JOBS}" clang

CLANG_BIN="${BUILD}/bin/clang"
echo 'int probe(void) { return 0; }' |
    "${CLANG_BIN}" -x c -c -Xclang -emit-cir -Xclang -clangir-disable-passes - -o /dev/null
echo "[+] CIR-enabled clang ready: ${CLANG_BIN}"
echo "    analyze.sh picks it up automatically, or: export CLANG=${CLANG_BIN}"
