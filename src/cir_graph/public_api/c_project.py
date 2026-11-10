"""C declaration metadata with a names-only function-macro catalog.

Existing base exporter (cir_graph.public_api.base) and its schema stay unchanged. Function-macro
parameters have no inferred C types; expansions and behavior are not exported.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from .base import extract

def function_macros(text,prefixes):
    if not prefixes or any(not p for p in prefixes):raise ValueError('Nonempty macro prefixes required')
    result={}
    for line in text.splitlines():
        match=re.match(r'^#define (\w+)\(([^)]*)\)(?:\s|$)',line)
        if not match or not match[1].startswith(tuple(prefixes)):continue
        params=[p.strip() for p in match[2].split(',') if p.strip()]
        if any(not re.fullmatch(r'(?:[A-Za-z_]\w*(?:\.\.\.)?|\.\.\.)',p) for p in params):continue
        result[match[1]]=dict(name=match[1],kind='function_macro',parameters=params,
            variadic=any('...' in p for p in params),parameter_types='not available',expansion='not exported')
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('header',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--clang',default='clang');p.add_argument('--include',action='append',default=[])
    p.add_argument('--function-prefix',action='append',required=True);p.add_argument('--constant-prefix',action='append',required=True)
    p.add_argument('--type-prefix',action='append');p.add_argument('--type-name',action='append',default=[])
    a=p.parse_args();flags=[a.clang,'-x','c','-std=gnu11',*['-I'+n for n in a.include]]
    commands=[flags+['-Xclang','-ast-dump=json','-fsyntax-only',str(a.header)],flags+['-E','-dM',str(a.header)]]
    outputs=[subprocess.check_output(c,text=True) for c in commands]
    data=extract(json.loads(outputs[0]),outputs[1],function_prefixes=a.function_prefix,constant_prefixes=a.constant_prefix,
        type_prefixes=a.type_prefix,type_names=a.type_name)
    data['schemaVersion']='v3-public-api-c-project-1'
    data['provenance']='Clang C declarations, object-like macros and function-macro name/parameter catalog'
    data['function_macros']=function_macros(outputs[1],a.function_prefix)
    data['limitations']=['No bodies, source excerpts, comments, tests or inferred behavioral contracts.',
        'Object-like macro expressions are preserved, not evaluated.',
        'Function-like macro names and parameter names only; no expansions or inferred argument/return types.']
    data['commands']=commands;data['compiler_version']=subprocess.check_output([a.clang,'--version'],text=True)
    data['public_header_sha256']={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for n in a.include for f in sorted(Path(n).rglob('*.h'))}
    data['header_sha256']=hashlib.sha256(a.header.read_bytes()).hexdigest()
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes((json.dumps(data,indent=2)+'\n').encode())
    print(json.dumps({k:len(data[k]) for k in ['functions','types','constants','function_macros']}))

if __name__=='__main__':main()
