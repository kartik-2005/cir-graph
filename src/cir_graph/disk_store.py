"""Bounded-memory store for per-TU graphs and project-wide symbol links.

The original monolithic linker/exporter is unchanged. CFG and memory DFG facts
are those of each fully analyzed translation unit. The SQL symbol pass links
calls across units; pointer-write memory summaries are not closed across units.
This boundary is explicit in metadata, rather than presented as full aliasing.
"""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import threading
from functools import wraps
import zlib

def encode(value):
    return zlib.compress(json.dumps(value,separators=(',',':'),ensure_ascii=False).encode(),3)

def decode(value):
    return json.loads(zlib.decompress(value))

def locked(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return call

class DiskGraph:
    def __init__(self,path,program_id=None,readonly=False):
        self.lock=threading.RLock()
        self.path=Path(path)
        if readonly:
            self.db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(self.path,timeout=180)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA cache_size=-32768')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY,source TEXT NOT NULL,record BLOB NOT NULL);
              CREATE TABLE IF NOT EXISTS functions(id TEXT PRIMARY KEY,tu TEXT,symbol TEXT NOT NULL,
                name TEXT NOT NULL,is_static INTEGER NOT NULL,linkage TEXT,record BLOB NOT NULL,
                cfg BLOB NOT NULL,micro BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS fn_symbol ON functions(symbol,is_static,linkage);
              CREATE INDEX IF NOT EXISTS fn_name ON functions(name COLLATE NOCASE);
              CREATE INDEX IF NOT EXISTS fn_tu ON functions(tu);
              CREATE INDEX IF NOT EXISTS fn_search_cover ON functions(name COLLATE NOCASE,symbol,is_static,id);
              CREATE TABLE IF NOT EXISTS canonical_symbols(symbol TEXT PRIMARY KEY,id TEXT NOT NULL UNIQUE);
              CREATE TABLE IF NOT EXISTS ambiguous_symbols(symbol TEXT PRIMARY KEY,definitions INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS declarations(symbol TEXT PRIMARY KEY,id TEXT NOT NULL,record BLOB NOT NULL);
              CREATE TABLE IF NOT EXISTS calls(caller TEXT NOT NULL,op TEXT NOT NULL,target_symbol TEXT NOT NULL,
                local_target TEXT,resolved_target TEXT,direct INTEGER NOT NULL,line INTEGER,col INTEGER,
                PRIMARY KEY(op,target_symbol));
              CREATE INDEX IF NOT EXISTS calls_caller ON calls(caller);
              CREATE INDEX IF NOT EXISTS calls_target ON calls(resolved_target);
              CREATE TABLE IF NOT EXISTS globals(id TEXT PRIMARY KEY,tu TEXT,record BLOB NOT NULL);
              CREATE TABLE IF NOT EXISTS bridges(tu TEXT PRIMARY KEY,record BLOB NOT NULL);
            ''')
            if program_id is not None:
                prior=self.meta('program_id')
                if prior is not None and prior!=program_id:
                    self.db.close()
                    raise ValueError('Program identity mismatch')
                self.set_meta('program_id',program_id)
                self.db.commit()
        self.has_search_index=bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='fn_search_cover'").fetchone())

    @locked
    def close(self):self.db.close()
    @locked
    def meta(self,key):
        row=self.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    @locked
    def set_meta(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,json.dumps(value,separators=(',',':'))))

    @locked
    def add_graph(self,data):
        units=data['layers']['macro']['translationUnits']
        if len(units)!=1:raise ValueError('Add exactly one completely analyzed TU at a time')
        unit=units[0];tu=unit['id'];functions=data['layers']['macro']['functions'];by_id={f['id']:f for f in functions}
        if self.db.execute('SELECT 1 FROM units WHERE id=?',(tu,)).fetchone():raise ValueError('Duplicate TU; no implicit replacement')
        with self.db:
            self.set_meta('linked',False)
            self.db.execute('INSERT INTO units VALUES(?,?,?)',(tu,unit.get('sourceFile',''),encode(unit)))
            for f in functions:
                if f.get('isDeclaration'):
                    decl=dict(f,id=self.meta('program_id')+':extern:'+f['name'])
                    self.db.execute('INSERT OR IGNORE INTO declarations VALUES(?,?,?)',(f['name'],decl['id'],encode(decl)))
                    continue
                self.db.execute('INSERT INTO functions VALUES(?,?,?,?,?,?,?,?,?)',
                    (f['id'],tu,f['name'],f.get('demangledName',f['name']),int(f.get('isStatic',False)),f.get('linkage','external'),
                     encode(f),encode(data['layers']['meso'][f['id']]),encode(data['layers']['micro'][f['id']])))
            for c in data['layers']['macro']['calls']:
                callee=by_id[c['callee']]
                self.db.execute('INSERT INTO calls(caller,op,target_symbol,local_target,direct,line,col) VALUES(?,?,?,?,?,?,?)',
                    (c['caller'],c['callOp'],callee['name'],None if callee.get('isDeclaration') else callee['id'],
                     int(c['isDirect']),c.get('line'),c.get('col')))
            for g in data['layers']['macro'].get('globals',[]):
                self.db.execute('INSERT INTO globals VALUES(?,?,?)',(g['id'],tu,encode(g)))
            self.db.execute('INSERT INTO bridges VALUES(?,?)',(tu,encode(data['layers'].get('bridges',{}))))
            stats=self.meta('body_counts') or dict(basicBlocks=0,cfgEdges=0,operations=0,values=0)
            for key in stats:stats[key]+=data['summary'].get(key,0)
            self.set_meta('body_counts',stats)

    @locked
    def link(self):
        # Same-TU definitions win. Otherwise prefer a strong external definition
        # to weak/ODR definitions, then choose a stable ID. Internal functions
        # from another TU can never satisfy an external reference.
        with self.db:
            self.db.execute('DELETE FROM canonical_symbols')
            self.db.execute('DELETE FROM ambiguous_symbols')
            self.db.execute('''INSERT INTO ambiguous_symbols
                SELECT symbol,COUNT(*) FROM functions WHERE is_static=0 AND linkage='external'
                GROUP BY symbol HAVING COUNT(*)>1''')
            self.db.execute('''INSERT INTO canonical_symbols(symbol,id)
                SELECT symbol,id FROM (SELECT symbol,id,ROW_NUMBER() OVER(PARTITION BY symbol
                    ORDER BY CASE WHEN linkage='external' THEN 0 ELSE 1 END,id) AS rank
                    FROM functions WHERE is_static=0) WHERE rank=1''')
            self.db.execute('''UPDATE calls SET resolved_target=COALESCE(local_target,
                (SELECT id FROM canonical_symbols f WHERE f.symbol=calls.target_symbol
                 AND NOT EXISTS (SELECT 1 FROM ambiguous_symbols a WHERE a.symbol=f.symbol)),
                (SELECT id FROM declarations d WHERE d.symbol=calls.target_symbol))''')
            unknown=self.db.execute('SELECT COUNT(*) FROM calls WHERE resolved_target IS NULL').fetchone()[0]
            if unknown:raise ValueError(f'{unknown} symbolic calls lost their declaration')
            summary=dict(translationUnits=self.db.execute('SELECT COUNT(*) FROM units').fetchone()[0],
                definedFunctions=self.db.execute('SELECT COUNT(*) FROM functions').fetchone()[0],
                canonicalNonstaticFunctions=self.db.execute('SELECT COUNT(*) FROM canonical_symbols').fetchone()[0],
                ambiguousNonstaticSymbols=self.db.execute('SELECT COUNT(*) FROM ambiguous_symbols').fetchone()[0],
                externalFunctions=self.db.execute('SELECT COUNT(DISTINCT resolved_target) FROM calls WHERE resolved_target NOT IN (SELECT id FROM functions)').fetchone()[0],
                globals=self.db.execute('SELECT COUNT(*) FROM globals').fetchone()[0],
                callEdges=self.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0],
                crossTUCalls=self.db.execute('SELECT COUNT(*) FROM calls c JOIN functions a ON a.id=c.caller JOIN functions b ON b.id=c.resolved_target WHERE a.tu<>b.tu').fetchone()[0],
                **(self.meta('body_counts') or {}))
            self.set_meta('summary',summary)
            self.set_meta('analysisLimits',dict(storage='Per-TU compressed function bodies and global SQLite symbol/call index.',
                memory='Existing memory analysis within each TU; pointer-write summaries are not propagated between TUs.',
                callBindings='Cross-TU argument/return bindings are generated from stored values on demand.',
                globals='Per-TU global facts are retained; global-variable DFG is not propagated between TUs.',
                recursion='Per-TU recursion facts retained; project-wide recursion and reachability are not recomputed.',
                unresolved='Unknown indirect/virtual targets remain unknown; failed compiler inputs are reported separately.'))
            self.set_meta('linked',True)
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return summary

    @locked
    def find(self,name,offset=0,limit=80,exact_method=False):
        if offset<0 or not 1<=limit<=200:raise ValueError('Invalid pagination')
        if self.db.execute('SELECT 1 FROM functions WHERE id=?',(name,)).fetchone():
            return [self.function(name)] if offset==0 else [],1
        literal=name.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        pattern=literal+'(%' if exact_method else '%'+literal+'%'
        where="(name LIKE ? ESCAPE '\\' OR name=? OR symbol=?) AND (is_static=1 OR id IN (SELECT id FROM canonical_symbols) OR symbol IN (SELECT symbol FROM ambiguous_symbols))"
        args=(pattern,name,name)
        # Keep substring searches on the small metadata index. A table scan
        # would otherwise read every compressed CFG/DFG body in the project.
        source='functions INDEXED BY fn_search_cover' if self.has_search_index else 'functions'
        total=self.db.execute('SELECT COUNT(*) FROM '+source+' WHERE '+where,args).fetchone()[0]
        ids=self.db.execute('SELECT id FROM '+source+' WHERE '+where+' ORDER BY id LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        return [self.function(row[0]) for row in ids],total

    @locked
    def function(self,identifier):
        row=self.db.execute('SELECT record FROM functions WHERE id=?',(identifier,)).fetchone()
        if row is None:
            row=self.db.execute('SELECT record FROM declarations WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
        record=decode(row[0])
        for key,column,condition in [('callees','resolved_target','caller'),('callers','caller','resolved_target')]:
            values=[r[0] for r in self.db.execute(f'SELECT DISTINCT {column} FROM calls WHERE {condition}=? ORDER BY {column} LIMIT 81',(identifier,))]
            record[key]=values[:80]
            record[key+'_truncated']=len(values)>80
        return record

    @locked
    def body(self,identifier):
        row=self.db.execute('SELECT cfg,micro FROM functions WHERE id=?',(identifier,)).fetchone()
        if row is None:raise KeyError('No analyzed function body: '+identifier)
        return decode(row[0]),decode(row[1])

    @locked
    def relations(self,identifier,direction='callees',offset=0,limit=80):
        if direction not in ('callers','callees') or offset<0 or not 1<=limit<=200:raise ValueError('Invalid relation query')
        field,condition=('resolved_target','caller') if direction=='callees' else ('caller','resolved_target')
        total=self.db.execute(f'SELECT COUNT(DISTINCT {field}) FROM calls WHERE {condition}=?',(identifier,)).fetchone()[0]
        ids=self.db.execute(f'SELECT DISTINCT {field} FROM calls WHERE {condition}=? ORDER BY {field} LIMIT ? OFFSET ?',(identifier,limit,offset)).fetchall()
        return [self.function(r[0]) for r in ids],total

    @locked
    def bindings(self,caller,offset=0,limit=80):
        """Return bounded direct/previously resolved call-site argument/return facts."""
        if offset<0 or not 1<=limit<=200:raise ValueError('Invalid pagination')
        _,micro=self.body(caller);ops={o['id']:o for o in micro['operations']}
        total=self.db.execute('SELECT COUNT(*) FROM calls WHERE caller=?',(caller,)).fetchone()[0]
        rows=self.db.execute('SELECT op,resolved_target,direct FROM calls WHERE caller=? ORDER BY op,target_symbol LIMIT ? OFFSET ?',(caller,limit,offset)).fetchall()
        result=[]
        for op_id,callee,direct in rows:
            op=ops[op_id];arguments=op.get('operands',[])[0 if direct else 1:]
            try:_,target=self.body(callee)
            except KeyError:target=None
            params=target.get('parameters',[]) if target else []
            returns=[dict(value=o['operands'][0],operation=o['id']) for o in target['operations'] if o['name']=='cir.return' and o.get('operands')] if target else []
            result.append(dict(callOp=op_id,caller=caller,callee=callee,isDirect=bool(direct),
                argumentBindings=[dict(argument=a,parameter=p,index=i) for i,(a,p) in enumerate(zip(arguments,params))],
                returnBindings=[dict(returnValue=r['value'],returnOp=r['operation'],result=op['results'][0]) for r in returns] if op.get('results') else []))
        return result,total
