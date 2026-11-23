"""Proof Library V2.11: streamlined integrated lookup, passive monitoring and administrator review."""
from __future__ import annotations
import argparse
import collections
import hashlib
import ntpath
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
import subprocess
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = r'Z:\Proofs'
DEFAULT_BOOK = r'Z:\Proofs\_Proof_Registry\Proof_Registry.xlsx'
DEMO_BOOK = HERE/'Proof_Registry_Optimized.xlsx'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
ID_RE = re.compile(r'PF-[0-9A-F]{16}', re.I)
SHA_RE = re.compile(r'[a-f0-9]{64}', re.I)


def text(v):
    return '' if v is None else str(v).strip()


def normal(v):
    return ' '.join(text(v).casefold().split())


def size_key(v):
    """Normalize decimal sizes and x/×, but do not guess unit conversions."""
    val=normal(v).replace('×','x').replace('"','').replace('″','')
    m=re.fullmatch(r'\s*(\d+(?:\.\d+)?|\.\d+)\s*x\s*(\d+(?:\.\d+)?|\.\d+)\s*(in(?:ch(?:es)?)?|mm|cm)?\s*',val)
    if m:
        try:
            a,b=(format(Decimal(x).normalize(),'f') for x in m.group(1,2))
            unit=m.group(3) or ''
            return a+'x'+b, 'in' if unit.startswith('in') else unit
        except InvalidOperation:
            pass
    return re.sub(r'\s+','',val),''


def read_sheet(z, name):
    wb=ET.fromstring(z.read('xl/workbook.xml'))
    sheets=wb.find(S+'sheets')
    sheet=next((s for s in sheets if s.get('name')==name),None)
    if sheet is None:
        raise ValueError(f'Worksheet {name!r} not found.')
    rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    target=next((r.get('Target') for r in rels if r.get('Id')==sheet.get(R+'id')),None)
    if not target: raise ValueError('Missing worksheet relationship.')
    path=target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl',target))
    shared=[]
    if 'xl/sharedStrings.xml' in z.namelist():
        shared=[''.join(t.text or '' for t in node.iter(S+'t')) for node in ET.fromstring(z.read('xl/sharedStrings.xml'))]
    result=[]
    for row in ET.fromstring(z.read(path)).iter(S+'row'):
        values={}
        for c in row.findall(S+'c'):
            col=re.match(r'[A-Z]+',c.get('r',''))
            if not col: continue
            t=c.get('t'); v=c.findtext(S+'v',default='')
            if t=='s' and v: v=shared[int(v)]
            elif t=='inlineStr': v=''.join(n.text or '' for n in c.iter(S+'t'))
            if v!='': values[col.group()]=v
        if values: result.append((int(row.get('r','0')),values))
    return result


ALIASES={
 'id':('Proof ID',), 'customer':('Customer','Customer Name'),
 'item':('UPC / Item Number','UPC / Item','UPC','Item Number'),
 'size':('Label Size',), 'classification':('UPC / Non-UPC','Classification'),
 'date':('Date Last Modified','Last Modified'), 'path':('PDF Path','PDF File'),
 'sha':('PDF SHA-256','Content SHA-256'), 'status':('Review Status',),
 'description':('Item Description',), 'notes':('Notes',), 'units':('Units',),
 'duplicate':('Duplicate Status',), 'width':('Width',), 'height':('Height',),
}


def get_mapping(rows, required):
    for n,row in rows[:30]:
        hdr={normal(v):c for c,v in row.items()}
        mapping={k:next((hdr[normal(a)] for a in aliases if normal(a) in hdr),'') for k,aliases in ALIASES.items()}
        if all(mapping[k] for k in required): return n,mapping
    raise ValueError('Required columns missing: '+', '.join(ALIASES[k][0] for k in required))


@dataclass(frozen=True)
class Proof:
    row: int
    id: str
    customer: str
    item: str
    size: str
    classification: str
    date: str
    path: str
    sha: str
    status: str
    description: str
    notes: str
    units: str
    duplicate: str
    width: str
    height: str


@dataclass
class Library:
    records: list[Proof]
    inventory: dict
    warnings: list[str]
    source: Path


def load_library(file):
    file=Path(file)
    with zipfile.ZipFile(file) as z:
        raw=read_sheet(z,'Proof Registry')
        head,mapping=get_mapping(raw,('id','customer','item','size','path','status'))
        records=[]; warnings=[]
        for n,r in raw:
            if n<=head: continue
            vals={k:text(r.get(c,'')) for k,c in mapping.items()}
            if not vals['id']:
                if any(vals[k] for k in ('customer','item','size','path')): warnings.append(f'Master row {n}: missing Proof ID; not indexed.')
                continue
            if not ID_RE.fullmatch(vals['id']): warnings.append(f'Master row {n}: unusual Proof ID; retained.')
            vals['id']=vals['id'].upper()
            if not all(vals[k] for k in ('customer','item','size','path')): warnings.append(f'Master row {n}: customer, item, size or path is missing.')
            records.append(Proof(row=n,**vals))
        if not records: raise ValueError('No proof rows found in Proof Registry.')
        counts=collections.Counter(p.id for p in records)
        for pid,num in counts.items():
            if num>1: warnings.append(f'{pid}: {num} master rows share this ID; retained separately, automatic opening disabled.')
        inventory=collections.defaultdict(list)
        try:
            raw_inv=read_sheet(z,'PDF Inventory')
            ih,im=get_mapping(raw_inv,('id','path','sha'))
            for n,r in raw_inv:
                if n>ih:
                    pid=text(r.get(im['id'])).upper(); path=text(r.get(im['path'])); sha=text(r.get(im['sha']))
                    if pid and path: inventory[pid].append((path,sha))
        except (ValueError,KeyError) as exc:
            warnings.append(f'Historical path fallback unavailable: {exc}')
    return Library(records,dict(inventory),warnings,file)


def matches(p, query='', customer='', size='', classification='', status=''):
    if customer and normal(customer) not in normal(p.customer): return False
    if classification and normal(classification)!=normal(p.classification): return False
    if status and normal(status)!=normal(p.status): return False
    if size:
        ask,au=size_key(size); have,hu=size_key(p.size)
        if not hu: hu=normal(p.units)
        if ask!=have and normal(size) not in normal(p.size): return False
        if au and hu and au!=hu: return False
    hay=normal(' '.join((p.customer,p.item,p.size,p.id,p.description,p.classification,p.notes,p.units)))
    # A whole numeric UPC/item search tolerates spaces and leading-zero omission,
    # but only for all-digit identifiers; the original is always displayed.
    q=normal(query)
    if not q: return True
    compact=re.sub(r'[\s-]','',q)
    if compact.isdigit() and p.item.isdigit() and (compact.lstrip('0') or '0')==(p.item.lstrip('0') or '0'): return True
    return all(token in hay for token in q.split())


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def map_path(stored, root):
    """Map the original Z:\\Proofs or UNC share Proofs prefix to configured root.
    Reject non-PDFs, traversal, paths outside that root, and unknown prefixes.
    """
    w=PureWindowsPath(stored)
    parts=w.parts
    if not w.is_absolute() or not parts or '..' in parts or w.suffix.lower()!='.pdf':
        raise ValueError('Not a safe absolute PDF path in the proof library.')
    if len(parts)<3 or parts[1].casefold()!='proofs':
        raise ValueError('Stored path is not under the expected Proofs root.')
    # All paths are mapped relative to the user's selected library, not arbitrary drives.
    relative=parts[2:]
    if not relative or any(':' in a or a in ('.','..') for a in relative): raise ValueError('Invalid relative PDF path.')
    base=Path(root).resolve(); candidate=base.joinpath(*relative).resolve()
    try: candidate.relative_to(base)
    except ValueError: raise ValueError('PDF path escapes the library root.')
    return candidate


def resolve_pdf(p, library, root):
    if sum(r.id==p.id for r in library.records)!=1:
        raise ValueError('Duplicate master Proof ID. Ask the registry owner to reconcile the rows before opening.')
    if not ID_RE.fullmatch(p.id): raise ValueError('Invalid Proof ID; ask the registry owner to correct it.')
    if not p.path: raise FileNotFoundError('This record has no PDF path.')
    candidates=[(p.path,False)]
    for old,sha in library.inventory.get(p.id,[]):
        if normal(old)!=normal(p.path) and SHA_RE.fullmatch(p.sha) and p.sha.casefold()==sha.casefold():
            if all(normal(old)!=normal(a) for a,_ in candidates): candidates.append((old,True))
    errors=[]
    for stored,fallback in candidates:
        try:
            candidate=map_path(stored,root)
            if p.id not in candidate.name.upper() and not SHA_RE.fullmatch(p.sha): raise ValueError('PDF name lacks the Proof ID and no stored hash is available.')
            if not candidate.is_file(): raise FileNotFoundError('File not found.')
            with open(candidate,'rb') as f:
                if f.read(5)!=b'%PDF-': raise ValueError('File does not have a PDF signature.')
            if SHA_RE.fullmatch(p.sha) and sha256(candidate).casefold()!=p.sha.casefold():
                raise ValueError('PDF content hash no longer matches the registry.')
            return candidate,fallback
        except (OSError,ValueError) as exc: errors.append(f'{stored}: {exc}')
    raise FileNotFoundError('No verified PDF found.\n'+'\n'.join(errors))


