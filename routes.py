"""Native Flask routes; existing job API and DB schema remain unchanged."""
from flask import Blueprint, request, jsonify, render_template, send_file, make_response
from pathlib import Path
from .service import ProofService

def install(app,service,get_job,attach_job):
    bp=Blueprint('proofs',__name__)
    def fail(e):
        return jsonify(error=str(e)),403 if isinstance(e,PermissionError) else 404 if isinstance(e,LookupError) else 409 if isinstance(e,(ValueError,FileExistsError)) else 503
    def admin():service.authorize(request.headers.get('X-Proof-Admin',''))
    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';response.headers['Referrer-Policy']='no-referrer';return response
    @bp.get('/proofs')
    def page():return render_template('proofs.html')
    @bp.get('/proofs/logo.png')
    def logo():return send_file(Path(__file__).parent/'assets'/'icon.png',mimetype='image/png')
    @bp.get('/api/proofs')
    def search():
        try:
            job_id=request.args.get('job_id',type=int)
            if 'job_id' in request.args and job_id is None:raise ValueError('Invalid job number.')
            job=get_job(job_id) if job_id is not None else None
            if job_id is not None and job is None:raise LookupError('Job not found.')
            rows,total,master=service.search(*(request.args.get(k,'')[:500] for k in ('q','customer','size','classification')),job=job)
            return jsonify(records=rows,total=total,master=master,job={k:job.get(k) for k in ('id','customer','upc','part_number','title','label_size')} if job else None)
        except Exception as e:return fail(e)
    @bp.get('/api/proofs/status')
    def status():
        try:service.load();service.start();return jsonify(service.status())
        except Exception as e:return fail(e)
    @bp.get('/api/proofs/<pid>/pdf')
    def pdf(pid):
        try:return send_file(service.pdf(pid),mimetype='application/pdf',conditional=True)
        except Exception as e:return fail(e)
    @bp.get('/api/proofs/queue')
    def queue():
        try:admin();service.configured();service.start();return jsonify(records=service.queue(),**service.status())
        except Exception as e:return fail(e)
    @bp.get('/api/proofs/queue/<key>/pdf')
    def candidate_pdf(key):
        try:
            admin()
            from . import proof_lookup as lib
            from . import new_proof_scanner as scan
            import stat
            with service.lock: c=service.pending.get(key)
            if not c or c.status!='New':raise LookupError('Pending proof not available.')
            path=Path(c.path); st=path.lstat()
            if not stat.S_ISREG(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400:raise ValueError('Not a regular PDF.')
            path.resolve().relative_to(service.root.resolve())
            if not scan.unchanged_pdf(path,c.sha,st.st_mtime_ns):raise ValueError('File changed; rescan.')
            return send_file(path,mimetype='application/pdf',conditional=True)
        except Exception as e:return fail(e)
    @bp.post('/api/proofs/scan')
    def scan():
        try:
            admin();service.configured()
            import threading
            def worker():
                try:service.scan()
                except Exception:pass
            threading.Thread(target=worker,daemon=True).start();return jsonify(ok=True),202
        except Exception as e:return fail(e)
    @bp.post('/api/proofs/choices')
    def choices():
        try:admin();d=request.get_json();return jsonify(matches=service.choices(d['key'],d['fields']))
        except Exception as e:return fail(e)
    @bp.post('/api/proofs/publish')
    def publish():
        try:admin();d=request.get_json();return jsonify(service.publish(d['key'],d['fields'],d.get('decision',''),d.get('previous','')))
        except Exception as e:return fail(e)
    @bp.post('/api/jobs/<int:job_id>/proof-snapshot')
    def attach(job_id):
        try:
            admin();d=request.get_json();return jsonify(attach_job(job_id,d['id']))
        except Exception as e:return fail(e)
    app.register_blueprint(bp)
