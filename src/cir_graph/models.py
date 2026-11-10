"""In-memory model of the unified CIR knowledge graph.

The graph is organized in three layers plus the bridges between them:

* Macro (call graph): Program, TranslationUnit, FunctionSymbol, Function,
  GlobalVariable and CallEdge.
* Meso (control flow): BasicBlock and CFGEdge.
* Micro (instructions and data flow): Operation, Value and FlowEdge.

Every node carries a deterministic, program-scoped string ID so that the same
CIR snapshot always produces the same graph, and so that several programs can
share one Kùzu database without colliding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Value:
    """An SSA value: an operation result or a block argument."""

    id: str
    name: str                      # SSA spelling, e.g. "%5" or "%arg0"
    type: Optional[str]
    functionId: str
    kind: str                      # parameter | block_arg | result | constant
    isBlockArg: bool = False
    varName: Optional[str] = None  # source-level variable this value carries
    definingOpId: Optional[str] = None
    constValue: Optional[str] = None
    programId: str = ""


@dataclass
class Operation:
    """One CIR/MLIR operation, placed in exactly one basic block."""

    id: str
    name: str                      # e.g. "cir.call"
    sequenceIndex: int             # source order within the function
    blockId: str
    functionId: str
    text: str                      # printed operation without its regions
    location: Optional[str] = None
    line: Optional[int] = None
    col: Optional[int] = None
    isCall: bool = False
    isTerminator: bool = False
    calleeSymbol: Optional[str] = None
    calleeName: Optional[str] = None
    sideEffect: str = "none"
    parentOpId: Optional[str] = None   # enclosing structured op (cir.if, ...)
    regionIndex: Optional[int] = None
    results: List[str] = field(default_factory=list)   # Value IDs
    operands: List[str] = field(default_factory=list)  # Value IDs
    successorOperands: List[List[str]] = field(default_factory=list)  # arguments per CFG successor
    globalSymbol: Optional[str] = None  # cir.get_global target
    programId: str = ""

    @property
    def all_operands(self) -> List[str]:
        """All SSA uses, including values passed to successor block arguments."""
        return self.operands + [vid for args in self.successorOperands for vid in args]


@dataclass
class CFGEdge:
    src: str
    dst: str
    kind: str                      # unconditional | true | false | switch | exception
    successorIndex: int = 0
    conditionValueId: Optional[str] = None
    label: Optional[str] = None    # switch case label
    isBackEdge: bool = False
    terminatorOpId: Optional[str] = None


@dataclass
class BasicBlock:
    id: str
    name: str
    ordinal: int
    functionId: str
    isEntry: bool = False
    isExit: bool = False
    terminatorOp: Optional[str] = None
    operations: List[str] = field(default_factory=list)  # Operation IDs
    arguments: List[str] = field(default_factory=list)   # Value IDs
    programId: str = ""


@dataclass
class FlowEdge:
    src: str                       # Value ID
    dst: str                       # Value ID
    kind: str  # def_use | store_to_load | call_argument | call_return | block_arg | region_yield
    sourceOpId: Optional[str] = None
    targetOpId: Optional[str] = None
    argIndex: Optional[int] = None


@dataclass
class CallEdge:
    callerId: str
    calleeId: str
    callOpId: str
    isDirect: bool
    isCrossTU: bool
    isExternal: bool
    callSiteLine: Optional[int] = None
    callSiteCol: Optional[int] = None


@dataclass
class FunctionSymbol:
    id: str
    name: str                      # mangled linker symbol
    demangledName: str
    linkage: str
    isStatic: bool
    tuId: Optional[str] = None     # owning TU for static symbols only
    programId: str = ""


@dataclass
class Function:
    id: str
    name: str                      # mangled symbol
    demangledName: str
    symbolId: str
    tuId: Optional[str]
    sourceFile: Optional[str]
    linkage: str
    isDeclaration: bool
    isStatic: bool
    signature: str = ""
    returnType: Optional[str] = None
    paramTypes: List[str] = field(default_factory=list)
    location: Optional[str] = None
    startLine: Optional[int] = None
    endLine: Optional[int] = None
    isEntryPoint: bool = False
    isRecursive: bool = False
    isDeadCode: bool = False
    isAddressTaken: bool = False
    inDegree: int = 0
    outDegree: int = 0
    blocks: List[BasicBlock] = field(default_factory=list)
    operations: List[Operation] = field(default_factory=list)
    values: Dict[str, Value] = field(default_factory=dict)
    parameters: List[str] = field(default_factory=list)  # Value IDs
    cfgEdges: List[CFGEdge] = field(default_factory=list)
    flows: List[FlowEdge] = field(default_factory=list)  # intra-procedural
    programId: str = ""


@dataclass
class GlobalVariable:
    id: str
    name: str
    demangledName: str
    linkage: str
    isStatic: bool
    isDeclaration: bool
    isConstant: bool
    type: Optional[str]
    tuId: Optional[str]            # defining TU (or declaring TU if never defined)
    sourceFile: Optional[str]
    line: Optional[int] = None
    initializer: Optional[str] = None
    programId: str = ""


@dataclass
class TranslationUnit:
    id: str
    compilationId: str             # source basename, e.g. "main.cpp"
    sourceFile: str                # full source path from the CIR module
    cirFile: str
    programId: str
    contentHash: str = ""
    typeAliases: Dict[str, str] = field(default_factory=dict)
    attrAliases: Dict[str, str] = field(default_factory=dict)
    functions: List[Function] = field(default_factory=list)  # definitions only
    globalRefs: List[tuple] = field(default_factory=list)    # (GlobalVariable ID, isDefinition)


@dataclass
class Program:
    id: str
    name: str
    translationUnits: List[TranslationUnit] = field(default_factory=list)
    symbols: Dict[str, FunctionSymbol] = field(default_factory=dict)
    externFunctions: Dict[str, Function] = field(default_factory=dict)
    globals: Dict[str, GlobalVariable] = field(default_factory=dict)
    callEdges: List[CallEdge] = field(default_factory=list)
    interFlows: List[FlowEdge] = field(default_factory=list)  # cross-function flows
    globalAccesses: List[tuple] = field(default_factory=list)  # (Operation ID, GlobalVariable ID)
    recursionCycles: List[List[str]] = field(default_factory=list)

    def all_functions(self) -> List[Function]:
        """Definitions in TU order followed by unresolved external declarations."""
        defined = [fn for tu in self.translationUnits for fn in tu.functions]
        return defined + list(self.externFunctions.values())

    def function_index(self) -> Dict[str, Function]:
        return {fn.id: fn for fn in self.all_functions()}
