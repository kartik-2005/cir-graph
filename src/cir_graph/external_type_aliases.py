"""Externalize oversized, metadata-only CIR type aliases without losing bytes.

The linker keeps typeAliases as metadata; it does not expand them during
CFG/DFG construction. Operation lines are never changed. External aliases are
explicit references, not substituted concrete types. Their original complete
physical lines remain available as gzip files, with SHA-256 receipts.
"""
import gzip
import hashlib
import re
from pathlib import Path

ALIAS = re.compile(rb'^(![A-Za-z0-9_$.\-]+)\s*=')


def prepare(source, output, aliases, threshold=1024*1024):
    source, output, aliases = map(Path, (source, output, aliases))
    aliases.mkdir(parents=True, exist_ok=True)
    original = hashlib.sha256()
    records = []
    opener=gzip.open if source.suffix=='.gz' else open
    with opener(source,'rb') as src, output.open('wb') as dst:
        line_number = 0
        while True:
            first = src.readline(threshold+1)
            if not first:
                break
            line_number += 1
            original.update(first)
            match = ALIAS.match(first)
            external = len(first) > threshold and match is not None
            if external:
                name = match.group(1).decode('ascii')
                archive = aliases / (str(line_number)+'.cir.gz')
                digest = hashlib.sha256()
                size = 0
                with gzip.open(archive, 'wb', compresslevel=3) as target:
                    chunk = first
                    while True:
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if chunk.endswith(b'\n'):
                            break
                        chunk = src.readline(65536)
                        if not chunk:
                            break
                        original.update(chunk)
                sha = digest.hexdigest()
                dst.write((name+' = !cs5187.external_type_alias<"sha256:'+sha+'">\n').encode())
                records.append(dict(name=name, line=line_number, bytes=size,
                    sha256=sha, gzip_path=str(archive.resolve()),
                    meaning='Complete original alias metadata is external; no inferred type layout.'))
            else:
                dst.write(first)
                while not first.endswith(b'\n'):
                    first = src.readline(65536)
                    if not first:
                        break
                    original.update(first)
                    dst.write(first)
    return dict(original_sha256=original.hexdigest(), external_aliases=records,
        operation_lines_changed=False, threshold_bytes=threshold)
