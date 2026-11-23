"""Server-side proof workflow. One service per Job Manager host; no automatic writes."""
from dataclasses import asdict, replace
from pathlib import Path
import hashlib, hmac, os, re, threading, time, tempfile, shutil, stat
from . import proof_lookup as library
from . import new_proof_scanner as scanner

class ProofService:
    def __init__(self, root='', book='', admin_digest='', interval=60):
        self.root=Path(root) if root else None
        self.book=Path(book) if book else (self.root/'_Proof_Registry'/'Proof_Registry.xlsx' if self.root else None)
        self.admin_digest=admin_digest.lower().strip()
        self.interval=max(30,int(interval)); self.lock=threading.RLock(); self.scan_lock=threading.Lock()
        self.cached=None; self.stamp=None; self.pending={}; self.last_scan=None; self.scan_error=''; self.thread=None
        self.revision=0

    def configured(self):
        if self.root is None or self.book is None: raise ValueError('Proof Library is not configured. Set [proofs] root in config.ini on the host.')
        if not self.root.is_dir(): raise OSError('Proof storage is unavailable. Check the host share connection.')
        if not self.book.is_file(): raise OSError('Live proof registry is unavailable. No bundled or empty workbook will be substituted.')

    def authorize(self,key):
        if not re.fullmatch('[a-f0-9]{64}',self.admin_digest): raise PermissionError('Proof management is disabled. Configure an administrator key on the host.')
        if not isinstance(key,str) or not hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(),self.admin_digest): raise PermissionError('Incorrect proof administrator key.')

    def load(self):
        self.configured()
        with self.lock:
            def stamp():
                s=self.book.stat(); return (s.st_mtime_ns,s.st_size)
            before=stamp()
            if self.cached is None or before!=self.stamp:
                new=library.load_library(self.book)
                if stamp()!=before: raise OSError('Registry changed during read; retry.')
                self.cached=new;self.stamp=before;self.revision+=1
            return self.cached

    def search(self,q='',customer='',size='',classification='',job=None):
        lib=self.load()
        counts={}
        for p in lib.records: counts[p.id]=counts.get(p.id,0)+1
        active=[p for p in lib.records if library.normal(p.status)=='reviewed' and counts[p.id]==1 and library.ID_RE.fullmatch(p.id)]
        if job is not None: active=[p for p in active if self.job_match(p,job)]
        return [self.public(p) for p in active if library.matches(p,q,customer,size,classification)],len(active),len(lib.records)

    @staticmethod
    def public(p):
        return {k:getattr(p,k) for k in ('id','customer','item','size','classification','description','date','status')}

    @staticmethod
    def job_match(p,job):
        if library.normal(p.customer)!=library.normal(job.get('customer')):return False
        upc=library.text(job.get('upc')); item=upc or library.text(job.get('part_number')) or library.text(job.get('title'))
        if not item:return False
        if upc:
            if library.normal(p.classification)!='upc' or not p.item.isdigit() or not upc.isdigit() or p.item!=upc:return False
        elif library.normal(p.classification)!='non-upc' or library.normal(p.item)!=library.normal(item):return False
        if job.get('label_size'):
            a,au=library.size_key(job['label_size']);b,bu=library.size_key(p.size)
            if a!=b or (au and (bu or p.units) and au!=(bu or library.normal(p.units))):return False
        return True

    def approved(self,pid):
        if not isinstance(pid,str) or not library.ID_RE.fullmatch(pid):raise LookupError('Invalid Proof ID.')
        lib=self.load(); matches=[p for p in lib.records if p.id==pid.upper()]
        if len(matches)!=1 or library.normal(matches[0].status)!='reviewed':raise LookupError('No unique reviewed proof with this ID.')
        return matches[0],lib

    def pdf(self,pid):
        p,lib=self.approved(pid);return library.resolve_pdf(p,lib,self.root)[0]

    def scan(self):
        self.configured()
        if not self.scan_lock.acquire(False):return False
        try:
            rows,_,_=scanner.scan(self.book,self.root)
            with self.lock:
                self.pending={self.key(c):c for c in rows};self.last_scan=time.time();self.scan_error=''
            return True
        except Exception as e:
            with self.lock:self.scan_error=str(e)
            raise
        finally:self.scan_lock.release()

    @staticmethod
    def key(c):
        return hashlib.sha256((c.path+'\0'+c.sha+'\0'+c.id+'\0'+c.status+'\0'+c.issue).encode()).hexdigest()

    def start(self):
        if self.root is None:return
        with self.lock:
            if self.thread and self.thread.is_alive():return
            def loop():
                while True:
                    try:self.scan()
                    except Exception as e:
                        with self.lock:self.scan_error=str(e)
                    time.sleep(self.interval)
            self.thread=threading.Thread(target=loop,name='proof-monitor',daemon=True); self.thread.start()

    def status(self):
        with self.lock:
            return dict(pending=sum(c.status=='New' for c in self.pending.values()),last_scan=self.last_scan,error=self.scan_error,scanning=self.scan_lock.locked(),interval=self.interval,revision=self.revision)

    def queue(self):
        with self.lock:
            # Do not expose arbitrary filesystem paths to browsers.
            return [dict(key=k,name=Path(c.path).name,id=c.id,customer=c.customer,item=c.item,size=c.size,classification=c.classification,description=c.description,status=c.status,issue=c.issue) for k,c in self.pending.items()]

    def candidate(self,key,fields):
        with self.lock:c=self.pending.get(key)
        if c is None or c.status!='New':raise ValueError('Candidate is no longer available. Rescan and review again.')
        values={}
        for k in ('customer','item','size','classification','description'):
            v=fields.get(k,'')
            if not isinstance(v,str) or len(v)>500:raise ValueError('Invalid review field: '+k)
            values[k]=v.strip()
        return replace(c,**values)

    def choices(self,key,fields):
        c=self.candidate(key,fields);return [self.public(p) for p in scanner.replacement_matches(self.load(),c)]

    def publish(self,key,fields,decision='',previous=''):
        self.configured()
        # Serialize against our scan so results cannot race the commit.
        if not self.scan_lock.acquire(False):raise ValueError('Discovery is running. Wait for the scan to finish and try again; your form stays filled.')
        try:
            c=self.candidate(key,fields);lib=self.load();matches=scanner.replacement_matches(lib,c)
            replacement=None
            if matches and decision not in ('keep','replace'):raise ValueError('An existing item matches. Choose Keep both or Replace explicitly.')
            if decision=='replace':
                eligible=[p for p in matches if p.id==previous]
                if len(eligible)!=1:raise ValueError('Choose one matching existing Proof ID to replace.')
                replacement=eligible[0]
            warnings=[]
            backup=scanner.publish(self.book,self.root,c,replacement=replacement,on_warning=warnings.append)
            with self.lock:
                self.cached=None;self.pending.pop(key,None)
            proof,_=self.approved(c.id)
            return dict(proof=self.public(proof),backup=backup.name,warnings=warnings)
        finally:self.scan_lock.release()

    def attach(self,pid,job,job_root):
        p,lib=self.approved(pid)
        if job.get('archived_at'):raise ValueError('Archived jobs cannot receive new proof snapshots.')
        if not self.job_match(p,job):raise ValueError('Proof does not exactly match the job customer, UPC/item and specified size.')
        if not library.SHA_RE.fullmatch(p.sha):raise ValueError('Proof has no recorded content hash; reconcile it before attaching.')
        source,_=library.resolve_pdf(p,lib,self.root)
        dest_root=Path(job['folder_path']).resolve();base=Path(job_root).resolve()
        if not dest_root.is_dir() or dest_root==base:raise ValueError('Job folder not reachable.')
        dest_root.relative_to(base)
        dest=dest_root/'Proof';dest.mkdir(exist_ok=True)
        st=dest.lstat()
        if dest.is_symlink() or getattr(st,'st_file_attributes',0)&0x400 or dest.resolve()!=dest_root/'Proof':raise ValueError('Job Proof folder escapes job root.')
        target=dest/(p.id+'.pdf')
        if target.exists():
            if target.is_symlink() or not target.is_file() or library.sha256(target)!=p.sha.lower():raise ValueError('Existing job snapshot conflicts; no overwrite.')
            return target,False
        fd,name=tempfile.mkstemp(prefix='.proof_snapshot_',suffix='.tmp',dir=dest);os.close(fd);stage=Path(name)
        try:
            shutil.copy2(source,stage)
            if library.sha256(stage)!=p.sha.lower():raise ValueError('Proof changed during copy.')
            # Re-check active record before exposing the job snapshot.
            current,_=self.approved(pid)
            if current!=p:raise ValueError('Proof changed during attachment; retry.')
            if os.name=='nt':os.rename(stage,target)
            else:os.link(stage,target)
            return target,True
        finally:stage.unlink(missing_ok=True)
