#!/usr/bin/env bash
# ==============================================================================
# analyze.sh — one-shot runner: C/C++ sources and/or .cir files -> knowledge graph
#
#   ./analyze.sh examples/demo/*.cpp
#   ./analyze.sh testbench/cases/10_three_tu_chain        # committed .cir, no LLVM needed
#
# Artifacts (in --output-dir, default ./artifacts):
#   graph.kuzu  graph.cypher  graph.json  graph.dot  [graph.svg]  cir/<program>/*.cir
# ==============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options] <file.c|.cc|.cpp|.cxx|.cir|DIR> [more ...]

Each C/C++ source is compiled separately (one translation unit each) to raw CIR
with a CIR-enabled clang; .cir inputs are used as-is. All TUs are linked into one
program graph. A DIR argument expands to the .cir files directly inside it, or,
if it has none, to its C/C++ sources.

Options:
  --program-id ID     program identifier (default: name of the first input's directory)
  --output-dir DIR    artifact directory (default: ./artifacts)
  --database PATH     Kùzu database (default: <output-dir>/graph.kuzu)
  --no-database       skip the Kùzu import (Cypher/JSON/DOT only; kuzu not required)
  --no-svg            do not render graph.svg even if Graphviz is installed
  --absolute-paths    keep clang's absolute source paths in the graph (default: paths
                      relative to the current directory; others under \$HOME become ~/...)
  --reset             delete the database first (needed after schema changes)
  --entry NAME        extra entry point for dead-code analysis (repeatable)
  --std STD           language standard (default: c11 for .c, c++20 otherwise)
  -I DIR | -D MACRO   forwarded to clang
  -h, --help          show this help

Environment:
  CLANG       CIR-enabled clang (otherwise: \$LLVM_BUILD/bin/clang, <repo>/.toolchain/build/bin/clang
              from scripts/build-clang.sh, ~/llvm-project/build/bin/clang, then clang on
              PATH; each is probed for -emit-cir support)
  PYTHON      Python interpreter with the 'kuzu' package
EOF
}

die()  { echo "[-] $*" >&2; exit 1; }
info() { echo "[*] $*"; }

[[ $# -ge 1 ]] || { usage; exit 1; }

INPUTS=()
CLANG_FLAGS=()
GEN_FLAGS=()
PROGRAM_ID=""
OUT_DIR="artifacts"
DATABASE=""
USE_DB=true
USE_SVG=true
ABSOLUTE_PATHS=false
STD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --program-id) PROGRAM_ID="$2"; shift 2 ;;
        --output-dir) OUT_DIR="$2"; shift 2 ;;
        --database) DATABASE="$2"; shift 2 ;;
        --no-database) USE_DB=false; shift ;;
        --no-svg) USE_SVG=false; shift ;;
        --absolute-paths) ABSOLUTE_PATHS=true; shift ;;
        --reset) GEN_FLAGS+=("--reset"); shift ;;
        --entry) GEN_FLAGS+=("--entry" "$2"); shift 2 ;;
        --std) STD="$2"; shift 2 ;;
        -I|-D) CLANG_FLAGS+=("$1" "$2"); shift 2 ;;
        -I*|-D*) CLANG_FLAGS+=("$1"); shift ;;
        -*) usage >&2; die "unknown option: $1" ;;
        *) INPUTS+=("$1"); shift ;;
    esac
done

# --- expand inputs ---------------------------------------------------------------
SOURCES=()
for input in "${INPUTS[@]}"; do
    if [[ -d "${input}" ]]; then
        # A directory with pre-compiled .cir uses only those (they are the compiled
        # TUs); otherwise its C/C++ sources. Mixing both would duplicate every TU.
        found=()
        while IFS= read -r -d '' file; do found+=("${file}"); done < <(
            find "${input}" -maxdepth 1 -type f -name '*.cir' -print0 | sort -z)
        if [[ ${#found[@]} -eq 0 ]]; then
            while IFS= read -r -d '' file; do found+=("${file}"); done < <(
                find "${input}" -maxdepth 1 -type f \
                    \( -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.cxx' \) -print0 | sort -z)
        else
            info "${input}: using ${#found[@]} pre-compiled .cir file(s); pass sources explicitly to recompile"
        fi
        SOURCES+=(${found[@]+"${found[@]}"})
    elif [[ -f "${input}" ]]; then
        SOURCES+=("${input}")
    else
        die "input not found: ${input}"
    fi
done
[[ ${#SOURCES[@]} -gt 0 ]] || die "no C/C++/CIR inputs found"

NEED_CLANG=false
for src in "${SOURCES[@]}"; do
    case "${src}" in
        *.cir) ;;
        *.c|*.cc|*.cpp|*.cxx) NEED_CLANG=true ;;
        *) die "unsupported input (expected .c/.cc/.cpp/.cxx/.cir): ${src}" ;;
    esac
done

# --- toolchain discovery ------------------------------------------------------
supports_cir() {
    [[ -x "$1" ]] || return 1
    echo 'int probe(void) { return 0; }' |
        "$1" -x c -c -Xclang -emit-cir -Xclang -clangir-disable-passes - -o /dev/null >/dev/null 2>&1
}

CLANG_BIN=""
if [[ "${NEED_CLANG}" == true ]]; then
    if [[ -n "${CLANG:-}" ]]; then
        supports_cir "${CLANG}" || die "CLANG=${CLANG} is not a CIR-enabled clang (needs -emit-cir)"
        CLANG_BIN="${CLANG}"
    else
        for candidate in "${LLVM_BUILD:-/nonexistent}/bin/clang" \
                         "${REPO_DIR}/.toolchain/build/bin/clang" \
                         "${HOME}/llvm-project/build/bin/clang" \
                         "$(command -v clang 2>/dev/null || true)"; do
            if [[ -n "${candidate}" ]] && supports_cir "${candidate}"; then
                CLANG_BIN="${candidate}"; break
            fi
        done
    fi
    if [[ -z "${CLANG_BIN}" ]]; then
        cat >&2 <<EOF
[-] No CIR-enabled clang found, but C/C++ sources were given. Either:
      * build the compiler once (a few hours):  ${REPO_DIR}/scripts/build-clang.sh
      * point CLANG at a clang built with -DCLANG_ENABLE_CIR=ON, or
      * pass pre-compiled .cir files instead (no LLVM needed), e.g.
        $(basename "$0") testbench/cases/10_three_tu_chain
EOF
        exit 1
    fi
    info "clang: ${CLANG_BIN}"
fi

has_kuzu() { "$1" -c 'import kuzu' >/dev/null 2>&1; }

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    for candidate in "${VIRTUAL_ENV:+${VIRTUAL_ENV}/bin/python}" \
                     "${REPO_DIR}/.venv/bin/python" "${REPO_DIR}/.conda-env/bin/python" python3; do
        if [[ -n "${candidate}" ]] && command -v "${candidate}" >/dev/null 2>&1 && has_kuzu "${candidate}"; then
            PYTHON_BIN="${candidate}"; break
        fi
    done
    PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || die "python not found: ${PYTHON_BIN}"
if [[ "${USE_DB}" == true ]] && ! has_kuzu "${PYTHON_BIN}"; then
    echo "[!] '${PYTHON_BIN}' has no 'kuzu' package (pip install -r requirements.txt);" \
         "skipping the database (Cypher/JSON/DOT are still written)." >&2
    USE_DB=false
fi

if [[ -z "${PROGRAM_ID}" ]]; then
    PROGRAM_ID="$(basename "$(cd "$(dirname "${SOURCES[0]}")" && pwd)")"
fi

mkdir -p "${OUT_DIR}"
CIR_DIR="${OUT_DIR}/cir/${PROGRAM_ID}"

# clang records each source's absolute path (module name and loc(...) entries),
# and the graph copies it into sourceFile / locations. Rewrite those two spots only
# (never string literals) so graphs carry project-relative paths: no user names or
# machine layout, and identical graphs wherever the project is checked out.
WORK_ROOTS=("$(pwd -P)" "${PWD}")
HOST_HOME="${CIR_GRAPH_HOME:-${HOME:-}}"
relativize_paths() {
    [[ "${ABSOLUTE_PATHS}" == true ]] && return 0
    "${PYTHON_BIN}" - "$1" "${HOST_HOME}" "${WORK_ROOTS[@]}" <<'PY'
import sys
path, home, *roots = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    text = handle.read()
replacements = [(root.rstrip("/") + "/", "") for root in dict.fromkeys(roots) if root.strip("/")]
if home.strip("/"):
    replacements.append((home.rstrip("/") + "/", "~/"))
for prefix in ('module @"', 'loc("'):
    for old, new in replacements:          # project root first, then home
        text = text.replace(prefix + old, prefix + new)
with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
}

# --- compile each translation unit to raw (structured) CIR ------------------
CIR_FILES=()
SEEN_STEMS=$'\n'          # newline-separated (bash 3.2 has no associative arrays)
for src in "${SOURCES[@]}"; do
    if [[ "${src}" == *.cir ]]; then
        CIR_FILES+=("${src}")
        continue
    fi
    mkdir -p "${CIR_DIR}"
    stem="$(basename "${src%.*}")"
    base_stem="${stem}"
    suffix=1
    while [[ "${SEEN_STEMS}" == *$'\n'"${stem}"$'\n'* ]]; do
        suffix=$(( suffix + 1 ))
        stem="${base_stem}_${suffix}"
    done
    SEEN_STEMS+="${stem}"$'\n'
    std="${STD}"
    if [[ -z "${std}" ]]; then
        case "${src}" in *.c) std="c11" ;; *) std="c++20" ;; esac
    fi
    out="${CIR_DIR}/${stem}.cir"
    info "${src} -> ${out}"
    "${CLANG_BIN}" -c "-std=${std}" -Xclang -emit-cir -Xclang -clangir-disable-passes \
        ${CLANG_FLAGS[@]+"${CLANG_FLAGS[@]}"} "${src}" -o "${out}"
    relativize_paths "${out}"
    CIR_FILES+=("${out}")
done

# --- build the unified graph -----------------------------------------------------
ARGS=("${CIR_FILES[@]}" --program-id "${PROGRAM_ID}"
      --output "${OUT_DIR}/graph.cypher" --json "${OUT_DIR}/graph.json" --dot "${OUT_DIR}/graph.dot")
if [[ "${USE_DB}" == true ]]; then
    ARGS+=(--database "${DATABASE:-${OUT_DIR}/graph.kuzu}")
fi
PYTHONPATH="${REPO_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYTHON_BIN}" -m cir_graph.cli "${ARGS[@]}" ${GEN_FLAGS[@]+"${GEN_FLAGS[@]}"}

if [[ "${USE_SVG}" == true ]]; then
    if command -v dot >/dev/null 2>&1; then
        dot -Tsvg "${OUT_DIR}/graph.dot" -o "${OUT_DIR}/graph.svg" && echo "[+] SVG: ${OUT_DIR}/graph.svg"
    else
        echo "[i] Graphviz 'dot' not found; skipping SVG (install graphviz to render ${OUT_DIR}/graph.dot)"
    fi
fi
