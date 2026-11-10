// Example queries for the demo program, one block per layer.
// Build it first:   ./analyze.sh examples/demo/main.cpp examples/demo/math.cpp --program-id demo
// Then paste a query into Kùzu Explorer, or run:
//   cir-graph-query cypher "<query>" --database artifacts/graph.kuzu

// ============================ MACRO: architecture & call graph ============================

// Translation units and the functions they define
MATCH (:Program {id: 'demo'})-[:CONTAINS_TU]->(tu:TranslationUnit)-[:CONTAINS_FUNCTION]->(f:Function)
RETURN tu.compilationId, f.demangledName, f.linkage, f.isEntryPoint, f.isDeadCode
ORDER BY tu.compilationId, f.startLine;

// Cross-TU calls
MATCH (a:Function)-[c:CALLS]->(b:Function)
WHERE c.isCrossTU AND a.programId = 'demo'
RETURN a.demangledName AS caller, a.tuId, b.demangledName AS callee, b.tuId, c.callSiteLine;

// Same-named static functions stay distinct and bind within their own TU
MATCH (caller:Function)-[:CALLS]->(f:Function)-[:REPRESENTS_SYMBOL]->(s:FunctionSymbol)
WHERE f.isStatic AND f.programId = 'demo'
RETURN f.demangledName, s.id, f.tuId, caller.demangledName;

// Recursion and dead code
MATCH (f:Function) WHERE f.programId = 'demo' AND (f.isRecursive OR f.isDeadCode)
RETURN f.demangledName, f.isRecursive, f.isDeadCode;

// Everything transitively reachable from main
MATCH (m:Function {name: 'main', programId: 'demo'})-[:CALLS*1..10]->(f:Function)
RETURN DISTINCT f.demangledName;

// ============================ MESO: control flow ============================

// CFG of one function
MATCH (f:Function {programId: 'demo'})-[:HAS_BLOCK]->(a:BasicBlock)-[e:CFG_NEXT]->(b:BasicBlock)
WHERE f.demangledName = 'checked_sum(int)'
RETURN a.ordinal, a.name, e.kind, e.isBackEdge, b.ordinal, b.name
ORDER BY a.ordinal, e.successorIndex;

// Loops: back edges (latch -> header)
MATCH (f:Function)-[:HAS_BLOCK]->(latch:BasicBlock)-[e:CFG_NEXT {isBackEdge: true}]->(header:BasicBlock)
WHERE f.programId = 'demo'
RETURN f.demangledName, header.name, latch.name;

// The instruction that computes each branch condition
MATCH (a:BasicBlock)-[e:CFG_NEXT {kind: 'true'}]->(b:BasicBlock), (o:Operation)-[:DEFINES_VALUE]->(v:Value)
WHERE v.id = e.conditionValueId AND a.programId = 'demo'
RETURN a.functionId, a.name, b.name, o.text;

// ============================ MICRO: instructions & data flow ============================

// Instructions of the entry block, in order
MATCH (f:Function {programId: 'demo'})-[:HAS_BLOCK]->(b:BasicBlock {isEntry: true})-[c:CONTAINS_OPERATION]->(o:Operation)
WHERE f.demangledName = 'fact(int)'
RETURN c.index, o.line, o.text ORDER BY c.index;

// Def-use chains around a source variable
MATCH (def:Operation)-[:DEFINES_VALUE]->(v:Value)<-[:USES_VALUE]-(use:Operation)
WHERE v.programId = 'demo' AND v.varName = 'sum'
RETURN def.text, use.text LIMIT 10;

// Which stores can each load of `total` observe? (includes the write made by scale())
MATCH (src:Value)-[r:FLOWS_TO {kind: 'store_to_load'}]->(ld:Value)
WHERE ld.programId = 'demo' AND ld.varName = 'total'
RETURN ld.id, src.id, r.sourceOpId, r.isInterprocedural;

// Backward slice: values that can flow into main's return value
MATCH (ret:Operation {name: 'cir.return'})-[:USES_VALUE]->(v:Value), p = (src:Value)-[:FLOWS_TO*1..8]->(v)
WHERE ret.functionId = 'demo:main.cpp:main'
RETURN DISTINCT src.functionId, coalesce(src.varName, src.constValue, src.name) LIMIT 25;

// ============================ BRIDGES: crossing layers ============================

// Call site -> callee, with argument -> parameter bindings
MATCH (o:Operation)-[i:INVOKES]->(g:Function)-[:HAS_PARAMETER]->(p:Value),
      (arg:Value)-[f:FLOWS_TO {kind: 'call_argument'}]->(p)
WHERE f.targetOpId = o.id AND o.programId = 'demo'
RETURN o.functionId, o.line, g.demangledName, i.isDirect, arg.id, p.varName;

// Callee return value -> caller call result
MATCH (r:Value)-[f:FLOWS_TO {kind: 'call_return'}]->(res:Value)<-[:DEFINES_VALUE]-(o:Operation)
WHERE o.programId = 'demo'
RETURN o.functionId, o.calleeName, r.id, res.id;

// Zoom from a call-graph edge down to its block and instruction
MATCH (a:Function)-[c:CALLS]->(b:Function), (blk:BasicBlock)-[:CONTAINS_OPERATION]->(o:Operation {id: c.callOpId})
WHERE a.programId = 'demo'
RETURN a.demangledName, blk.name, o.text, b.demangledName;
