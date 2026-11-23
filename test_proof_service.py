"""Disposable proof storage; never touches the live workbook or job database."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib, shutil, unittest, tempfile, zipfile
from proofing.service import ProofService
from proofing import proof_lookup as lib
from proofing import new_proof_scanner as scanner
from pypdf import PdfWriter
import openpyxl

class ProofWorkflow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name); self.root=self.base/'Proofs'; self.root.mkdir();self.customer=self.root/'Test Customer';self.customer.mkdir()
        self.book=self.root/'_Proof_Registry'/'Proof_Registry.xlsx';self.book.parent.mkdir()
        # Actual completed registry, bundled for offline checks ONLY.
        shutil.copy2(Path(__file__).resolve().parents[1]/'proofing'/'reference'/'Proof_Registry_Optimized.xlsx',self.book)
        self.svc=ProofService(str(self.root),str(self.book),hashlib.sha256(b'test-admin-key-only').hexdigest())
        self.before=tuple(self.svc.load().records)
    def pdf(self,name,width=500):
        path=self.customer/name;w=PdfWriter();w.add_blank_page(width=width,height=600);w.write(path);return path
    def record(self,p):
        self.svc.scan();return next(c for c in self.svc.queue() if c['name']==p.name)
    def fields(self):return dict(customer='Test Customer',item='001234567890',size='4 x 6 in',classification='UPC',description='Reviewed label')
    def test_actual_master_and_search(self):
        rows,total,master=self.svc.search();self.assertEqual((len(rows),total,master),(808,808,808));self.assertEqual(len({r['id'] for r in rows}),808)
        p=next(r for r in self.before if r.item.startswith('0') and r.item.isdigit());found=self.svc.search(q=p.item)[0];self.assertIn(p.id,[x['id'] for x in found]);self.assertEqual(next(x for x in found if x['id']==p.id)['item'],p.item)
    def test_missing_never_falls_back(self):
        self.book.unlink();self.assertRaises(OSError,self.svc.load)
    def test_authorization(self):
        self.svc.authorize('test-admin-key-only');self.assertRaises(PermissionError,self.svc.authorize,'wrong');self.assertRaises(PermissionError,ProofService().authorize,'')
    def test_scan_publish_revision_job_capture(self):
        p=self.pdf('new.pdf');c=self.record(p);self.assertEqual(c['status'],'New');self.assertEqual(self.svc.status()['pending'],1)
        r=self.svc.publish(c['key'],self.fields());pid=r['proof']['id'];self.assertEqual(len(self.svc.load().records),809);self.assertEqual(tuple(self.svc.load().records[:808]),self.before)
        self.assertFalse(p.exists());actual=self.svc.pdf(pid);self.assertIn(pid,actual.name)
        self.svc.scan();self.assertEqual(self.svc.status()['pending'],0)
        jobs=self.base/'jobs';jobfolder=jobs/'test';jobfolder.mkdir(parents=True);job=dict(customer='Test Customer',upc='001234567890',label_size='4 x 6',folder_path=str(jobfolder),archived_at=None)
        self.assertEqual([r['id'] for r in self.svc.search(job=job)[0]],[pid]);dest,created=self.svc.attach(pid,job,jobs);self.assertTrue(created);self.assertEqual(lib.sha256(dest),lib.sha256(actual));self.assertFalse(self.svc.attach(pid,job,jobs)[1]);self.assertRaises(ValueError,self.svc.attach,pid,{**job,'upc':'999'},jobs)
        p2=self.pdf('revision.pdf',510);c2=self.record(p2);self.assertEqual([r['id'] for r in self.svc.choices(c2['key'],self.fields())],[pid]);self.assertRaises(ValueError,self.svc.publish,c2['key'],self.fields());new=self.svc.publish(c2['key'],self.fields(),'replace',pid)['proof']['id'];self.assertNotEqual(pid,new);self.assertEqual(len(self.svc.load().records),809);self.assertEqual(tuple(self.svc.load().records[:808]),self.before);self.assertRaises(LookupError,self.svc.pdf,pid);self.assertTrue(self.svc.pdf(new).exists());self.assertTrue(dest.exists());self.assertTrue(any((self.root/'_Superseded_Proofs').rglob('*.pdf')))
    def test_pending_and_changed_pdf(self):
        p=self.pdf('change.pdf');c=self.record(p);self.pdf('change.pdf',501);old=lib.sha256(self.book);self.assertRaises(ValueError,self.svc.publish,c['key'],self.fields());self.assertEqual(lib.sha256(self.book),old);self.assertTrue(p.exists())
    def test_scan_lock_and_invalid_candidate(self):
        self.svc.scan_lock.acquire()
        try:self.assertRaises(ValueError,self.svc.publish,'invalid',self.fields())
        finally:self.svc.scan_lock.release()
        self.assertRaises(ValueError,self.svc.publish,'invalid',self.fields())
    def test_locked_workbook(self):
        p=self.pdf('locked.pdf');c=self.record(p);old=lib.sha256(self.book);lock=self.book.with_name('~$'+self.book.name);lock.touch()
        self.assertRaises(PermissionError,self.svc.publish,c['key'],self.fields());self.assertEqual(lib.sha256(self.book),old);self.assertTrue(p.exists())
    def test_second_reader_refreshes(self):
        other=ProofService(str(self.root),str(self.book));self.assertEqual(len(other.search()[0]),808)
        p=self.pdf('fresh.pdf');c=self.record(p);pid=self.svc.publish(c['key'],self.fields())['proof']['id'];self.assertEqual(len(other.search()[0]),809);self.assertIn(pid,[x['id'] for x in other.search()[0]])
    def test_pending_not_visible(self):
        p=self.pdf('unapproved.pdf');c=self.record(p);self.assertEqual(len(self.svc.search()[0]),808);self.assertRaises(LookupError,self.svc.pdf,c['id'])
    def test_no_snapshot_for_wrong_job_or_outside_root(self):
        p=self.pdf('snapshot.pdf');c=self.record(p);pid=self.svc.publish(c['key'],self.fields())['proof']['id'];jobs=self.base/'jobs';jobs.mkdir();outside=self.base/'outside';outside.mkdir();job=dict(customer='Test Customer',upc='001234567890',label_size='4 x 6',folder_path=str(outside),archived_at=None)
        self.assertRaises(ValueError,self.svc.attach,pid,job,jobs);self.assertFalse((outside/'Proof').exists())
        self.assertRaises(ValueError,self.svc.attach,pid,{**job,'label_size':'1 x 2'},jobs)
    def test_flask_blueprint_when_available(self):
        try:from flask import Flask
        except ImportError:self.skipTest('Flask not installed in this environment')
        from proofing.routes import install
        app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'));install(app,self.svc,lambda _:None,lambda *_:{'ok':True});client=app.test_client();self.assertEqual(client.get('/proofs').status_code,200);self.assertEqual(len(client.get('/api/proofs').json['records']),808);self.assertEqual(client.get('/api/proofs/queue').status_code,403);self.assertEqual(client.post('/api/proofs/publish',json={}).status_code,403);self.assertEqual(client.get('/api/proofs?job_id=999').status_code,404);self.assertEqual(client.get('/api/proofs/UNKNOWN/pdf').status_code,404)

if __name__=='__main__':unittest.main(verbosity=2)
