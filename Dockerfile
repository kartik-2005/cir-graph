# syntax=docker/dockerfile:1
# ==============================================================================
# cir-graph with a CIR-enabled clang, so it runs on any C/C++ code with no host setup.
#
# Build (pick one):
#   docker build -t cir-graph .                       # builds clang from source: once, ~1-3 h
#   docker build -t cir-graph --build-arg CLANG_SOURCE=prebuilt \
#       --build-context llvm-build=$HOME/llvm-project/build .   # reuse an existing build: minutes
#
# Run (from the directory holding your code):
#   /path/to/cir-graph/cir-graph-docker src/*.c -I include
# ==============================================================================

# LLVM commit the parser and the committed testbench CIR are validated against.
ARG LLVM_COMMIT=aa8d1afeed3714c2805bdf069ca273f00a789f31
ARG CLANG_MAJOR=24
ARG CLANG_SOURCE=source

# ---------------------------------------------------------------- clang: from source
FROM ubuntu:24.04 AS clang-source
ARG LLVM_COMMIT
ARG CLANG_MAJOR
# Parallel compile jobs. Budget ~2 GB RAM per job (linking clang alone needs a few
# GB); 3 is safe on an 8 GB machine. Raise it with --build-arg BUILD_JOBS=N if you
# have more memory. Running out of memory can kill the whole WSL/VM session.
ARG BUILD_JOBS=3
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build python3 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init -q llvm-project \
 && git -C llvm-project fetch -q --depth 1 https://github.com/llvm/llvm-project.git "${LLVM_COMMIT}" \
 && git -C llvm-project checkout -q FETCH_HEAD
# The build directory lives in a BuildKit cache mount, so an interrupted build
# (closed terminal, reboot, out of memory) resumes where it stopped on the next
# `docker build` instead of starting over.
RUN --mount=type=cache,id=cir-graph-llvm-build,target=/src/build \
    cmake -S llvm-project/llvm -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DLLVM_ENABLE_ASSERTIONS=OFF \
      -DLLVM_ENABLE_PROJECTS="clang;mlir" \
      -DCLANG_ENABLE_CIR=ON \
      -DLLVM_TARGETS_TO_BUILD=host \
      -DLLVM_ENABLE_ZLIB=OFF -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_LIBXML2=OFF \
      -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
      -DLLVM_PARALLEL_LINK_JOBS=1 \
 && ninja -C build -j "${BUILD_JOBS}" clang \
 && mkdir -p /opt/clang/bin /opt/clang/lib/clang \
 && cp "build/bin/clang-${CLANG_MAJOR}" /opt/clang/bin/ \
 && strip "/opt/clang/bin/clang-${CLANG_MAJOR}" \
 && cp -r "build/lib/clang/${CLANG_MAJOR}" /opt/clang/lib/clang/

# ------------------------------------------------- clang: reuse an existing build dir
FROM ubuntu:24.04 AS clang-prebuilt
ARG CLANG_MAJOR
COPY --from=llvm-build bin/clang-${CLANG_MAJOR} /opt/clang/bin/
COPY --from=llvm-build lib/clang/${CLANG_MAJOR} /opt/clang/lib/clang/${CLANG_MAJOR}

FROM clang-${CLANG_SOURCE} AS clang

# ---------------------------------------------------------------------------- runtime
FROM ubuntu:24.04
ARG CLANG_MAJOR
# libc/libstdc++ headers for compiling user code; c++filt for demangling;
# graphviz for graph.svg.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv libc6-dev libstdc++-13-dev libgcc-13-dev binutils graphviz \
 && rm -rf /var/lib/apt/lists/*
COPY --from=clang /opt/clang /opt/clang
RUN ln -s "clang-${CLANG_MAJOR}" /opt/clang/bin/clang \
 && ln -s "clang-${CLANG_MAJOR}" /opt/clang/bin/clang++

COPY . /opt/cir-graph
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir /opt/cir-graph

ENV PATH="/opt/venv/bin:/opt/cir-graph:/opt/clang/bin:${PATH}" \
    CLANG=/opt/clang/bin/clang \
    PYTHON=/opt/venv/bin/python \
    PYTHONDONTWRITEBYTECODE=1

# Fail the build unless clang emits CIR and the whole suite passes with it.
RUN echo 'int probe(void) { return 0; }' | clang -x c -c -Xclang -emit-cir -Xclang -clangir-disable-passes - -o /dev/null \
 && cd /opt/cir-graph && python -m unittest discover -s tests

WORKDIR /work
CMD ["analyze.sh", "--help"]
