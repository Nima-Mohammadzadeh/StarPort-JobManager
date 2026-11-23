"""Incremental discovery and reviewed append with safe canonical PDF naming."""
from __future__ import annotations
import os, sys, re, csv, shutil, tempfile, uuid, threading, queue, stat, zipfile, collections, time
from .monitor_state import MonitorState, identity, fingerprint, INTERVAL_SECONDS
import xml.etree.ElementTree as ET
import posixpath
import warnings
from .proof_lookup import S,R
from dataclasses import dataclass, asdict, replace
from datetime import datetime
from pathlib import Path, PureWindowsPath
from copy import copy
from .proof_lookup import DEFAULT_BOOK, DEFAULT_ROOT, HERE, load_library, map_path, sha256, ID_RE, SHA_RE, normal, read_sheet, resolve_pdf
sys.path.append(str(HERE/'_vendor'))

@dataclass
class Candidate:
    path: str
    id: str=''
    customer: str=''
    item: str=''
    size: str=''
    classification: str='Review'
    description: str=''
    sha: str=''
    date: str=''
    status: str='New'
    issue: str=''


def index_path(stored,root):
    # Lexical mapping for discovery only. Do not resolve thousands of known paths
    # across SMB on each poll. Opening and publishing still resolve actual paths.
    w=PureWindowsPath(stored); parts=w.parts
    if not w.is_absolute() or len(parts)<3 or parts[1].casefold()!='proofs' or w.suffix.lower()!='.pdf': raise ValueError('Invalid proof path')
    if any(':' in a or a in ('.','..') for a in parts[2:]): raise ValueError('Unsafe proof path')
    return Path(root).joinpath(*parts[2:])


def indexes(lib,root):
    paths={}; ids=set(); hashes=set()
    def add(pid,path,h):
        ids.add(pid.upper())
        if SHA_RE.fullmatch(h): hashes.add(h.lower())
        try: paths.setdefault(str(index_path(path,root)).casefold(),set()).add(h.lower())
        except (OSError,ValueError): pass
    for p in lib.records: add(p.id,p.path,p.sha)
    for pid,rows in lib.inventory.items():
        for path,h in rows: add(pid,path,h)
    # Superseded files are deliberately excluded from active QA search but remain known to discovery.
    with zipfile.ZipFile(lib.source) as z:
        for _,r in revision_rows(z):
            add(r.get('A',''),r.get('X',''),r.get('M',''))
            add(r.get('A',''),r.get('J',''),r.get('M',''))
    return paths,ids,hashes


def validate_pdf(path,extract=True):
    from pypdf import PdfReader
    with open(path,'rb') as f:
        if f.read(5)!=b'%PDF-': raise ValueError('Missing PDF signature')
        f.seek(0)
        reader=PdfReader(f,strict=False)
        if reader.is_encrypted: raise ValueError('Encrypted PDF; review manually')
        if not len(reader.pages): raise ValueError('PDF has no pages')
        if not extract: return ''
        try: return '\n'.join(p.extract_text() or '' for p in reader.pages[:5])
        except Exception: return ''


def hints(c,txt):
    # Suggestions only: actual label dimensions are not PDF page dimensions.
    upcs=set(re.findall(r'(?im)^\s*(?:UPC(?:\s*(?:number|#))?|GTIN)\s*[:#]?\s*([0-9]{8,14})\s*$',txt))
    if len(upcs)==1: c.item=next(iter(upcs)); c.classification='UPC'
    else:
        items=set(re.findall(r'(?im)^\s*(?:Item (?:Number|No\.?|#)|Part Number)\s*[:#]?\s*([^\r\n]{1,100})$',txt))
        if len(items)==1: c.item=next(iter(items)).strip()
    desc=re.findall(r'(?im)^\s*Item Description\s*[:]?\s*([^\r\n]{1,180})$',txt)
    if len(set(desc))==1: c.description=desc[0].strip()
    lines=re.findall(r'(?im)^\s*(?:Label Size|Dimensions|Item Description)\s*[:]?\s*([^\r\n]{1,180})$',txt)
    pattern=r'(?<![\d.])(\d+(?:\.\d+)?)\s*["″]?\s*[x×]\s*(\d+(?:\.\d+)?)\s*(mm|cm|in(?:ches)?|["″])?(?![\d.])'
    sizes=set()
    for line in lines:
        for m in re.finditer(pattern,line,re.I):
            if re.match(r'\s*[x×]\s*\d',line[m.end():]): continue
            a,b,u=m.groups(); sizes.add(f'{a} x {b}'+(' '+('in' if u in ('"','″','inches') else u.lower()) if u else ''))
    if len(sizes)==1: c.size=next(iter(sizes))


def scan(book,root,progress=lambda *a:None,stop=lambda:False,verify_known=False):
    root=Path(root).resolve()
    if not root.is_dir(): raise FileNotFoundError(root)
    lib=load_library(book); paths,ids,hashes=indexes(lib,root)
    results=[]; files=[]; errors=[]
    def walk_error(e): errors.append(str(e))
    for folder,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
        if stop(): raise InterruptedError('Scan cancelled; no changes')
        keep=[]
        for d in dirs:
            try:
                st=(Path(folder)/d).lstat()
                if d.startswith(('_','.')) or stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400: continue
                keep.append(d)
            except OSError as e: errors.append(str(e))
        dirs[:]=keep
        for n in names:
            if n.startswith(('~$','.')): continue
            p=Path(folder)/n
            if p.suffix.lower() in ('.pdf','.docx','.dox','.doc','.docm'): files.append(p)
        progress(0,0,f'Discovering: {len(files):,} files found')
    known=0
    for i,p in enumerate(files,1):
        if stop(): raise InterruptedError('Scan cancelled; no changes')
        progress(i,len(files),p.name)
        rel=p.relative_to(root); c=Candidate(str(p),customer=rel.parts[0] if len(rel.parts)>1 else '')
        try:
            if p.suffix.lower()=='.pdf' and str(p).casefold() in paths and not verify_known: known+=1; continue
            st=p.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400: raise ValueError('Link/reparse point not processed')
            p.resolve().relative_to(root)
            if p.suffix.lower()!='.pdf':
                if not p.with_suffix('.pdf').is_file(): c.status='Word only'; c.issue='Convert to PDF before registration'; results.append(c)
                continue
            key=str(p.resolve()).casefold()
            if key in paths and not verify_known: known+=1; continue
            before=(st.st_size,st.st_mtime_ns); c.sha=sha256(p)
            if (p.stat().st_size,p.stat().st_mtime_ns)!=before: raise ValueError('File changed during scan')
            c.date=datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d')
            matches=ID_RE.findall(p.stem); c.id=matches[0].upper() if len(matches)==1 else 'PF-'+c.sha[:16].upper()
            if key in paths:
                if c.sha in paths[key]: known+=1; continue
                c.status='Conflict'; c.issue='Known PDF path has a different or unrecorded hash'
            elif len(matches)>1:
                c.status='Conflict'; c.issue='Multiple Proof IDs in filename'
            elif c.sha in hashes:
                c.status='Known copy'; c.issue='Content already in master or historical inventory'
            elif c.id in ids:
                c.status='Conflict'; c.issue='Existing Proof ID with different or unrecorded hash'
            elif not c.customer:
                c.status='Review location'; c.issue='File must be inside a customer folder'
            else:
                hints(c,validate_pdf(p))
                c.issue='Confirm customer, item, classification and actual label size before publishing'
        except Exception as e: c.status='Error'; c.issue=str(e)
        results.append(c)
    # Refuse ambiguous newly discovered IDs or duplicate content until reviewed separately.
    new=[c for c in results if c.status=='New']; counts=collections.Counter(c.id for c in new); hashes2=collections.Counter(c.sha for c in new)
    for c in new:
        if counts[c.id]>1 or hashes2[c.sha]>1: c.status='Conflict'; c.issue='Multiple new files share an ID or content hash; reconcile first'
    for e in errors: results.append(Candidate('',status='Error',issue=e))
    return results,known,len(files)


def report(rows):
    out=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Proof_Library'/'Discovery'; out.mkdir(parents=True,exist_ok=True)
    path=out/f'New_Proofs_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.csv'
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(Candidate.__dataclass_fields__)); w.writeheader()
        for c in rows:
            w.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v) for k,v in asdict(c).items()})
    return path


HEADERS=['Proof ID','Customer','UPC / Non-UPC','UPC / Item Number','Label Size','Width','Height','Units','Date Last Modified','PDF Path','Source Path','Source Type','PDF SHA-256','Extraction Quality','Review Status','Notes','Conversion Status','Last Scanned','Duplicate Status','Item Description','Size Source']
SEARCH=['Customer Name','UPC / Item Number','Label Size','Item Description','Proof ID','UPC / Non-UPC','Date Last Modified','Review Status','PDF Path','Size Source','Duplicate Status']


HISTORY='Proof Revisions'
REV_HEADERS=HEADERS+['Superseded At','Replacement Proof ID','Previous Actual PDF Path','Archived PDF Path']

def revision_rows(z):
    wb=ET.fromstring(z.read('xl/workbook.xml'))
    if not any(x.get('name')==HISTORY for x in wb.find(S+'sheets')): return []
    rows=read_sheet(z,HISTORY)
    from openpyxl.utils import get_column_letter
    if not rows or rows[0][0]!=1 or [rows[0][1].get(get_column_letter(i),'') for i in range(1,len(REV_HEADERS)+1)]!=REV_HEADERS:
        raise ValueError('Unexpected Proof Revisions schema; no changes')
    return rows[1:]

def same_item(p,c):
    return (normal(p.customer)==normal(c.customer) and normal(p.item)==normal(c.item)
            and normal(p.classification)==normal(c.classification))

def replacement_matches(lib,c):
    return [p for p in lib.records if same_item(p,c) and normal(p.status)=='reviewed']

def restore_numeric_payload(source,stage,exclude_row=None):
    """Preserve original exact numeric serialization.
    Excel's Python writer otherwise rounds some of the original dimension values.
    """
    def sheetpath(z,name):
        wb=ET.fromstring(z.read('xl/workbook.xml')); sh=next(x for x in wb.find(S+'sheets') if x.get('name')==name)
        rel=next(x.get('Target') for x in ET.fromstring(z.read('xl/_rels/workbook.xml.rels')) if x.get('Id')==sh.get(R+'id'))
        return rel.lstrip('/') if rel.startswith('/') else posixpath.normpath(posixpath.join('xl',rel))
    fd,tmpname=tempfile.mkstemp(prefix='.proof_exact_',suffix='.xlsx',dir=stage.parent);os.close(fd);tmp=Path(tmpname)
    try:
        with zipfile.ZipFile(source) as a,zipfile.ZipFile(stage) as b,zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as out:
            oldxml=ET.fromstring(a.read(sheetpath(a,'Proof Registry')))
            values={c.get('r'):c.findtext(S+'v') for c in oldxml.iter(S+'c') if c.get('t','n')=='n' and c.findtext(S+'v') is not None}
            key=sheetpath(b,'Proof Registry')
            for info in b.infolist():
                payload=b.read(info.filename)
                if info.filename==key:
                    xml=payload.decode('utf-8')
                    def repl(m):
                        address=m.group(2)
                        if address not in values or (exclude_row is not None and int(re.search(r'\d+',address).group())==exclude_row):return m.group(0)
                        return m.group(1)+values[address]+m.group(4)
                    xml=re.sub(r'(<c\b[^>]*\br="([A-Z]+\d+)"[^>]*>\s*<v>)([^<]*)(</v>\s*</c>)',repl,xml)
                    payload=xml.encode('utf-8')
                out.writestr(info,payload)
        os.replace(tmp,stage)
    finally:tmp.unlink(missing_ok=True)


def synchronize_search(m,s,last):
    """Repair only the derived Search table, preserving the master and backup."""
    from openpyxl.utils.cell import range_boundaries
    st=s.tables['ProofSearchTable']
    a,b,c,d=range_boundaries(st.ref)
    if (a,b,c)!=(1,4,11): raise ValueError('Unexpected Proof Search table shape; no changes')
    # Refuse unexpected user content outside the known derived table.
    if any(cell.value is not None for rr in s.iter_rows(min_row=5,max_row=s.max_row) for cell in rr if cell.column>11 or cell.row>d):
        raise ValueError('Unexpected content outside Proof Search table; no changes')
    cols='BDETA CIOJUS'.replace(' ','')
    # Rewrite all linked values even if the row count already matches: an old
    # search table can be in a different row order or contain stale snapshots.
    for r in range(5,last+1):
        for i,col in enumerate(cols,1):
            cell=s.cell(r,i); cell.value=f'=IF(\'Proof Registry\'!${col}${r}="","",\'Proof Registry\'!${col}${r})'
    if d>last:
        for rr in s.iter_rows(min_row=last+1,max_row=d,max_col=11):
            for cell in rr: cell.value=None
    st.ref=f'A4:K{last}'
    if st.autoFilter: st.autoFilter.ref=st.ref; st.autoFilter.sortState=None
    st.sortState=None


def canonical_pdf(p,c,date):
    """Use reviewed customer text, preserving the folder and the assigned ID."""
    customer=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', c.customer.strip()).rstrip(' .')
    if not customer or ID_RE.search(customer):
        raise ValueError('Customer name is empty or contains a Proof ID; correct it before publishing')
    name=f'{customer} - {c.id.upper()} - {date}.pdf'
    if len(name.encode('utf-16-le'))//2>255:
        raise ValueError('Canonical PDF name is too long; shorten the reviewed customer name')
    target=p.with_name(name)
    if target==p: return p
    # Windows treats differently cased spellings as the same existing path.
    if os.name=='nt' and target.name.casefold()==p.name.casefold(): return p
    if any(x.name.casefold()==name.casefold() for x in p.parent.iterdir()):
        raise FileExistsError(f'Canonical PDF already exists: {target}. No files overwritten; reconcile first.')
    return target


def install_pdf_copy(source,target,digest,mtime_ns):
    """Copy locally on the share, validate, then expose the new name without overwrite.
    Source remains intact until the workbook commit is verified.
    """
    fd,name=tempfile.mkstemp(prefix='.proof_filename_',suffix='.tmp',dir=source.parent)
    os.close(fd); stage=Path(name)
    try:
        shutil.copy2(source,stage)
        if sha256(stage)!=digest or stage.stat().st_mtime_ns!=mtime_ns:
            raise ValueError('Staged PDF content or last-modified timestamp did not match original')
        # Windows rename fails when the destination exists. POSIX rename overwrites,
        # so use an exclusive hard link for non-Windows development/test hosts.
        if os.name=='nt': os.rename(stage,target)
        else: os.link(stage,target)
    finally:
        stage.unlink(missing_ok=True)


def unchanged_pdf(path,digest,mtime_ns):
    st=path.lstat()
    return (stat.S_ISREG(st.st_mode) and not getattr(st,'st_file_attributes',0)&0x400
            and st.st_mtime_ns==mtime_ns and sha256(path)==digest)


def publish_warning(message):
    warnings.warn(message,RuntimeWarning)


def publish(book,root,c,*,replacement=None,on_warning=publish_warning):
    """Serialise administrator publishing across workstations using an exclusive lock."""
    path=Path(book); lock=path.with_name(path.name+".publish.lock")
    try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError: raise PermissionError("Another publish is in progress (or a previous lock remains). Confirm no admin is publishing before removing the .publish.lock file.")
    try:
        os.write(fd,str(os.getpid()).encode()); os.close(fd); fd=None
        return _publish(book,root,c,replacement=replacement,on_warning=on_warning)
    finally:
        if fd is not None: os.close(fd)
        lock.unlink(missing_ok=True)


def _publish(book,root,c,*,replacement=None,on_warning=publish_warning):
    """Append a single explicitly reviewed proof; stage, validate, backup, replace."""
    import openpyxl
    book=Path(book); root=Path(root).resolve(); source=Path(c.path)
    try: source_stat=source.lstat()
    except FileNotFoundError as e: raise ValueError('Source PDF no longer exists; scan again') from e
    if not stat.S_ISREG(source_stat.st_mode) or getattr(source_stat,'st_file_attributes',0)&0x400: raise ValueError('Source must be a regular PDF, not a link')
    p=source.resolve()
    rel=p.relative_to(root)
    if c.status!='New' or len(rel.parts)<2 or p.suffix.lower()!='.pdf': raise ValueError('Only unambiguous new PDFs can be published')
    if not all(str(x).strip() for x in (c.customer,c.item,c.size)) or c.classification not in ('UPC','Non-UPC'): raise ValueError('Confirm customer, item, label size and classification')
    if c.classification=='UPC' and not re.fullmatch(r'\d{8,14}',c.item.strip()): raise ValueError('UPC/GTIN must contain 8–14 digits; confirm classification')
    if not ID_RE.fullmatch(c.id): raise ValueError('Invalid proof ID')
    if book.with_name('~$'+book.name).exists(): raise PermissionError('Close Excel before publishing')
    original=sha256(book); lib=load_library(book); paths,ids,hashes=indexes(lib,root)
    if c.id in ids or c.sha in hashes or str(p).casefold() in paths: raise ValueError('Already indexed or now conflicting; scan again')
    if not unchanged_pdf(p,c.sha,source_stat.st_mtime_ns): raise ValueError('PDF changed since scan; scan again')
    date=datetime.fromtimestamp(source_stat.st_mtime).strftime('%Y-%m-%d')
    if c.date!=date: raise ValueError('PDF modification date changed since scan; scan again')
    target=canonical_pdf(p,c,date)
    if target!=p and str(target).casefold() in paths: raise ValueError('Canonical PDF path already registered or historical; reconcile first')
    validate_pdf(p,False)
    old_pdf=None; archive=None; old_stamp=None; archived=False
    if replacement is not None:
        # Require the exact record shown to the administrator, not merely an ID.
        current=[x for x in lib.records if x.id==replacement.id]
        if current!=[replacement] or replacement not in replacement_matches(lib,c):
            raise ValueError('Selected previous proof changed or does not match customer, item and type. Review again.')
        if not SHA_RE.fullmatch(replacement.sha): raise ValueError('Previous proof has no verified hash; cannot safely replace it.')
        old_pdf,_=resolve_pdf(replacement,lib,root)
        old_stamp=old_pdf.stat().st_mtime_ns
        if not unchanged_pdf(old_pdf,replacement.sha.lower(),old_stamp): raise ValueError('Previous PDF changed; no replacement.')
        if old_pdf==p or old_pdf==target: raise ValueError('New and previous paths must be different. Save the revision as a separate PDF first.')
        # Never remove a PDF shared by another active record, including fallback paths.
        for x in lib.records:
            if x==replacement: continue
            if x.sha.lower()==replacement.sha.lower(): raise ValueError('Previous PDF content is shared by another active proof; reconcile before replacing.')
            try: xp=map_path(x.path,root)
            except (ValueError,OSError): continue
            if xp==old_pdf: raise ValueError('Previous PDF is shared by another active proof; reconcile first.')
        archive=root/'_Superseded_Proofs'/f'{replacement.id}_{uuid.uuid4().hex}'/old_pdf.name
        archive.resolve().relative_to(root)
    def stored_path(path): return 'Z:\\Proofs\\'+'\\'.join(path.relative_to(root).parts)
    w=openpyxl.load_workbook(book)
    temp=None; installed=False
    try:
        m=w['Proof Registry']; s=w['Proof Search']
        if [m.cell(4,i).value for i in range(1,22)]!=HEADERS or [s.cell(4,i).value for i in range(1,12)]!=SEARCH: raise ValueError('Install/use the optimized V2 workbook first; no changes')
        last=max(p.row for p in lib.records); row=replacement.row if replacement else last+1; end=last if replacement else row
        mt=m.tables['ProofRegistryTable']; st=s.tables['ProofSearchTable']
        if mt.ref!=f'A4:U{last}': raise ValueError('Master table range does not match registry records; no changes')
        if any(cell.value is not None for rr in m.iter_rows(min_row=last+1,max_row=max(last+1,m.max_row),max_col=21) for cell in rr): raise ValueError('Rows exist outside registry table; stop to preserve them')
        synchronize_search(m,s,last)
        # Store canonical relative mapping; root can be Z: or corresponding UNC share.
        stored='Z:\\Proofs\\'+'\\'.join(target.relative_to(root).parts)
        if replacement:
            from openpyxl.utils import get_column_letter
            from openpyxl.styles import Font, PatternFill
            with zipfile.ZipFile(book) as z:
                old_history=revision_rows(z)
                raw_old=dict(read_sheet(z,'Proof Registry'))[row]
            h=w[HISTORY] if HISTORY in w.sheetnames else w.create_sheet(HISTORY)
            if not old_history and h.cell(1,1).value is None:
                for i,header in enumerate(REV_HEADERS,1): h.cell(1,i,header)
                h.freeze_panes='C2'
                for cell in h[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='123B50')
            hist=[raw_old.get(get_column_letter(i),'') for i in range(1,22)]+[datetime.now().strftime('%Y-%m-%d %H:%M:%S'),c.id,stored_path(old_pdf),stored_path(archive)]
            hr=h.max_row+1
            for i,v in enumerate(hist,1):
                cell=h.cell(hr,i);cell.value=str(v) if v!='' else None
                if v!='':cell.data_type='s'
                h.column_dimensions[get_column_letter(i)].width=42 if i in (10,11,13,24,25) else 23
            h.auto_filter.ref=f'A1:Y{hr}'
        size=c.size.strip(); dims=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(in|mm|cm)?\s*',size,re.I)
        width,height,unit=dims.groups() if dims else ('','','')
        vals=[c.id,c.customer.strip(),c.classification,c.item.strip(),size,width,height,unit or '',c.date,stored,stored,'PDF',c.sha,'Manual','Reviewed','','PDF Ready',datetime.now().strftime('%Y-%m-%d %H:%M:%S'),'Unique',c.description.strip(),'QA reviewed']
        for i,v in enumerate(vals,1):
            dest=m.cell(row,i); dest._style=copy(m.cell(last,i)._style) if not replacement else dest._style; dest.value=v or None
            if v: dest.data_type='s'
        m.cell(row,4).number_format='@'
        for i,col in enumerate('BDETA CIOJUS'.replace(' ',''),1):
            dest=s.cell(row,i); dest._style=copy(s.cell(last,i)._style); dest.value=f'=IF(\'Proof Registry\'!${col}${row}="","",\'Proof Registry\'!${col}${row})'
        for table,col in ((mt,'U'),(st,'K')):
            table.ref=f'A4:{col}{end}'
            if table.autoFilter: table.autoFilter.ref=table.ref; table.autoFilter.sortState=None
            table.sortState=None
        for rr in w['Dashboard'].iter_rows():
            for cell in rr:
                if cell.data_type=='f': cell.value=re.sub(r'(\$[A-Z]+\$)'+str(last)+r'\b',lambda x:x.group(1)+str(end),cell.value)
        from openpyxl.workbook.properties import CalcProperties
        if w.calculation is None: w.calculation=CalcProperties()
        w.calculation.fullCalcOnLoad=True; w.calculation.forceFullCalc=True; w.calculation.calcMode='auto'
        fd,name=tempfile.mkstemp(prefix='.proof_publish_',suffix='.xlsx',dir=book.parent); os.close(fd); temp=Path(name); w.save(temp); restore_numeric_payload(book,temp,exclude_row=row if replacement else None)
        new=load_library(temp)
        actual=[x for x in new.records if x.row==row]
        if ([x for x in new.records if x.row!=row]!=[x for x in lib.records if x.row!=row]
                or new.inventory!=lib.inventory or len(new.records)!=len(lib.records)+(0 if replacement else 1)
                or len(actual)!=1 or actual[0].id!=c.id or actual[0].item!=c.item.strip() or actual[0].sha!=c.sha or actual[0].path!=stored):
            raise ValueError('Staged workbook validation failed; no changes')
        with zipfile.ZipFile(book) as a,zipfile.ZipFile(temp) as b:
            if b.testzip(): raise ValueError('Staged ZIP invalid')
            if [r for r in read_sheet(a,'Proof Registry') if r[0]>=5 and r[0]!=row] != [r for r in read_sheet(b,'Proof Registry') if 5<=r[0]<=last and r[0]!=row]: raise ValueError('Existing master rows changed; no changes')
            a_hist=revision_rows(a); b_hist=revision_rows(b)
            if replacement:
                if b_hist[:-1]!=a_hist or len(b_hist)!=len(a_hist)+1 or b_hist[-1][1].get('A')!=replacement.id or b_hist[-1][1].get('W')!=c.id or b_hist[-1][1].get('Y')!=stored_path(archive): raise ValueError('Revision history validation failed')
            elif a_hist!=b_hist: raise ValueError('Revision history changed unexpectedly')
            for sheet in ('PDF Inventory','Scan Issues'):
                if read_sheet(a,sheet)!=read_sheet(b,sheet): raise ValueError(f'{sheet} changed; no changes')
        if sha256(book)!=original or book.with_name('~$'+book.name).exists() or not unchanged_pdf(p,c.sha,source_stat.st_mtime_ns): raise ValueError('Workbook or PDF changed during publish; retry')
        backup=book.with_name(f'{book.stem}_Backup_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx'); shutil.copy2(book,backup)
        if sha256(backup)!=original or sha256(book)!=original or book.with_name('~$'+book.name).exists(): raise ValueError('Backup or concurrent edit check failed')
        if replacement:
            if not unchanged_pdf(old_pdf,replacement.sha.lower(),old_stamp): raise ValueError('Previous PDF changed before archive; retry')
            archive.parent.mkdir(parents=True,exist_ok=False)
            install_pdf_copy(old_pdf,archive,replacement.sha.lower(),old_stamp); archived=True
        if target!=p:
            install_pdf_copy(p,target,c.sha,source_stat.st_mtime_ns); installed=True
        if not unchanged_pdf(target,c.sha,source_stat.st_mtime_ns) or not unchanged_pdf(p,c.sha,source_stat.st_mtime_ns) or sha256(book)!=original or book.with_name('~$'+book.name).exists():
            raise ValueError('Workbook or PDF changed before commit; retry')
        if replacement and (not unchanged_pdf(archive,replacement.sha.lower(),old_stamp) or not unchanged_pdf(old_pdf,replacement.sha.lower(),old_stamp)):
            raise ValueError('Previous PDF or archive changed before commit; retry')
        expected=sha256(temp)
        os.replace(temp,book); temp=None
        # Do not remove the original unless we know the new workbook and PDF
        # are both present and verified. A crash can leave two copies, never a
        # registry pointing exclusively to a name that has not been created.
        try:
            if sha256(book)!=expected or not unchanged_pdf(target,c.sha,source_stat.st_mtime_ns):
                raise ValueError('Post-commit verification failed')
            if replacement:
                try:
                    if not unchanged_pdf(archive,replacement.sha.lower(),old_stamp) or not unchanged_pdf(old_pdf,replacement.sha.lower(),old_stamp): raise ValueError('Previous PDF or archive changed; retained')
                    old_pdf.unlink()
                except Exception as e: on_warning(f'Revision saved; previous PDF may remain at {old_pdf}. Verified archive: {archive}. Cleanup warning: {e}')
            if target!=p:
                if not unchanged_pdf(p,c.sha,source_stat.st_mtime_ns): raise ValueError('Original changed; retained')
                p.unlink()
        except Exception as e:
            on_warning(f'Workbook replacement completed, but final verification/old-name cleanup was not confirmed: {e}. Original may remain at {p}. Verify the registry and both paths before removing anything.')
        return backup
    except Exception:
        # Roll back only our own verified copy, and only when the old workbook
        # and source are still intact. If an SMB commit is uncertain, keep both.
        if installed:
            try:
                if sha256(book)==original and unchanged_pdf(p,c.sha,source_stat.st_mtime_ns) and unchanged_pdf(target,c.sha,source_stat.st_mtime_ns):
                    target.unlink()
                else: on_warning(f'Publish interrupted; retained both PDF paths for recovery: {p} | {target}')
            except Exception as e: on_warning(f'Publish interrupted; normalized PDF may remain at {target}: {e}')
        if archived:
            try:
                if sha256(book)==original and unchanged_pdf(old_pdf,replacement.sha.lower(),old_stamp) and unchanged_pdf(archive,replacement.sha.lower(),old_stamp):
                    archive.unlink(); archive.parent.rmdir()
                else: on_warning(f'Revision archive retained for recovery: {archive}')
            except Exception as e: on_warning(f'Revision archive retained at {archive}: {e}')
        raise
    finally:
        w.close()
        if temp: temp.unlink(missing_ok=True)


