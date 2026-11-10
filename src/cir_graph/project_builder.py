"""Project-scale entry point with conservative canonical CIR normalization.

The historical linker/parser files remain unchanged for frozen experiments.
Only a pure, argument-free switch-region entry forwarding block is removed.
All cases, bodies, dispatch conditions and runtime operations are preserved.
"""
import os
from .cir_parser import parse_cir_file
from .linker import ProgramLinker,_collect_symbols,demangle_all
from .models import TranslationUnit

def normalize_switch_entries(module):
    changes=[]
    def walk(region,function):
        for block in list(region.blocks):
            for op in block.ops:
                if op.name=='cir.switch' and len(op.regions)==1:
                    body=op.regions[0]
                    for index in range(len(body.blocks)-1,0,-1):
                        entry,cases=body.blocks[index-1:index+1]
                        branch=entry.ops[0] if len(entry.ops)==1 else None
                        valid=(branch is not None and branch.name=='cir.br' and len(branch.successors)==1
                            and not branch.successors[0].operands and not entry.args and not cases.args
                            and cases.label and branch.successors[0].label==cases.label
                            and any(c.name=='cir.case' for c in cases.ops)
                            and all(c.name in ('cir.case','cir.yield') for c in cases.ops))
                        def references(sub,label):
                            for b in sub.blocks:
                                for child in b.ops:
                                    if any(s.label==label for s in child.successors):return True
                                    if any(references(r,label) for r in child.regions):return True
                            return False
                        # An explicit jump back to the container is not a pure
                        # dispatch wrapper, so leave it intact and fail visibly.
                        if valid and not any(references(r,cases.label) for c in cases.ops for r in c.regions):
                            # Also accept a dead forwarding block following a
                            # terminating case. Keep preceding case operations.
                            if entry.label and references(body,entry.label):continue
                            other=[b for b in body.blocks if b is not entry]
                            if any(s.label==cases.label for b in other for child in b.ops for s in child.successors):continue
                            changes.append(dict(function=function,kind='pure_switch_entry_forwarder',label=cases.label))
                            cases.label=None;body.blocks.pop(index-1)
                for sub in op.regions:walk(sub,function)
    for function in module.functions:
        if function.body:walk(function.body,function.symbol)
    return changes

class ProjectLinker(ProgramLinker):
    def load(self,cir_paths):
        modules=[parse_cir_file(path) for path in cir_paths]
        changes=[change for module in modules for change in normalize_switch_entries(module)]
        symbols=set()
        for module in modules:symbols|=_collect_symbols(module)
        self.names=demangle_all(symbols);used_ids={}
        for module in modules:
            base=os.path.basename(module.sourcePath) or os.path.basename(module.path)
            count=used_ids.get(base,0);used_ids[base]=count+1
            compilation_id=base if count==0 else f'{base}~{count+1}'
            tu=TranslationUnit(id=f'{self.program.id}:{compilation_id}',compilationId=compilation_id,
                sourceFile=module.sourcePath,cirFile=os.path.basename(module.path),programId=self.program.id,
                contentHash=module.contentHash,typeAliases=dict(module.typeAliases),attrAliases=dict(module.attrAliases))
            self.program.translationUnits.append(tu);self.modules[tu.id]=module
        for tu in self.program.translationUnits:self._load_tu(tu,self.modules[tu.id])
        self._load_globals();self._link_calls();self._memory_flows();self._analyze_call_graph()
        self.program.projectNormalizations=changes
        return self.program

def build_program(cir_paths,program_id,name=None,entry_points=None):
    return ProjectLinker(program_id,name,entry_points).load(cir_paths)
