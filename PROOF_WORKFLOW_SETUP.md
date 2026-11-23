# StarPort Job Manager 3.4 — Proof Library integration (pilot)

This is the uploaded Job Manager 3.3 source with a native proof workspace and the proof discovery, registration, naming and revision engine from Proof Library V2.11. No database migration, serial allocation changes, data-sheet changes or production gate changes. The live Excel workbook stays the master; database migration and a separate formal approval/role system are not included in this pilot.

## Install safely on the existing host

1. Back up the existing Job Manager installation, database/serial registry, shared proof workbook and PDFs. Shut down Job Manager and the standalone Proof Library host/monitor. Do not run `new_install.bat` for an upgrade.
2. Extract this package and copy its application files into your existing Job Manager installation. Preserve your actual `config.ini`, `local.db`, `serial_registry`, backup folders, template files and job folders. The uploaded source has no live database or config. Do not overwrite anything with the offline test workbook in `proofing/reference/`.
3. In your existing `config.ini`, append this section (or edit it if present):

```ini
[proofs]
root = Z:\Proofs
workbook =
interval = 60
admin_key_sha256 =
```

Use the corresponding UNC path instead of Z: if the chosen host runs in a different Windows user/service context. A blank workbook setting uses `<root>/_Proof_Registry/Proof_Registry.xlsx`. A blank root disables proof access. A missing or disconnected workbook is reported and never substituted with the bundled copy. This package does not replace your master workbook.

4. Run `python setup_proof_admin.py` from the app folder to generate a SHA-256 digest for a strong administrator key (at least 16 characters). Paste only the resulting digest into `admin_key_sha256`. With no valid digest, management and job capture are disabled. Restrict config and network write access to authorized administrators. This is an extra shared-key gate for the new proof mutations, not application-wide user authentication or SSO. Use an IT-approved internal HTTPS reverse proxy/secure network before using the key remotely. The existing Job Manager's other routes retain their original access controls. Never expose this server publicly.
5. Close the Excel workbook; open Excel only for manual maintenance, not as a background process. Use the optimized live V2.11 registry format, with `Proof Registry`, `Proof Search`, `Dashboard`, `PDF Inventory` and `Scan Issues`. The publisher refuses unexpected layouts/locks instead of rebuilding your master.
6. Run `Test_Proof_Workflow.bat` (uses disposable local test copies only). Confirm all tests pass, with none skipped on the Windows host. If pytest is installed for your normal development environment, also run `python -m pytest tests` before deployment. Start normal `run.bat` and use the existing Job Manager URL, e.g. `http://localhost:5050/proofs` for local-only verification.

## Daily workflow

- Left navigation **Proof Library**: search customer, UPC/item, label size and Proof ID, with barcode scanner keyboard input (Enter submits). Only unique, reviewed registry records appear. Preview in the browser or open PDF normally; no automatic print dialog. A browser without embedded PDF support can still open the PDF separately. Search refreshes periodically when the live registry changes.
- Job detail **Find job proof**: opens the same library scoped to exact customer, UPC/item and label size if the job has one. For non-UPC, uses the job part number or title. It deliberately does not guess customer aliases or match partial UPCs. If there is no exact match, use the general library to investigate; do not attach the wrong item. **Capture for this job** (administrator key) copies the hash-verified approved PDF into that job's `Proof/PF-....pdf`. It is a historical snapshot, never overwritten by later revisions. Existing job uploads remain unchanged.
- **New proofs / management**: the host checks the root every 60 seconds after completing a scan, even with no browser open. New PDFs are detected and surfaced in the queue/status while the page is open. Enter the administrator key, select a new PDF, preview it, confirm customer, UPC/item, label size and class, then publish. Save new revisions as separate PDF files rather than overwriting an active file. Item matches require explicit Keep Both or a selected old Proof ID for replacement. The V2.11 engine backs up the workbook, validates the staged workbook and PDF, uses the standard name `Customer - Proof ID - YYYY-MM-DD.pdf`, and archives superseded PDFs under `_Superseded_Proofs`. QA only sees current reviewed rows. Locked workbooks or invalid proofs are refused. If scanning is in progress, wait for it to finish and retry; pending form fields remain intact. Word-only files and conflicting copies are reported rather than converted or auto-registered.
- Publishing uses the existing workbook lock convention, but one designated publishing host is recommended. Do not keep the old standalone monitor or host running as a second writer. Network permissions still matter. Monitoring ends when the Job Manager host stops. Keep independent backup coverage for the proof workbook, PDFs and revision folder; existing Job Manager backups do not automatically cover them.

## Acceptance checklist on the real host

- Existing jobs, serial registry, roll tracking, floor and rewind pages load as before. Confirm the configured job and proof paths are correct.
- Master count matches the live registry (the bundled reference has 808 reviewed rows, not necessarily the current live count); test a known leading-zero UPC and open its real PDF.
- From a test job, locate the exact proof and (with permission) capture a verified snapshot; reopen via its existing job Files panel. Verify no existing files were overwritten.
- Use a disposable staging proof/customer in an approved test root and test discovery, review, publish, replacement and workbook-lock behavior. Verify older proof in archive and its job snapshot still exists. Do not test replacement on a production proof without approval.
- From a second workstation, reach the same Job Manager host and confirm read-only lookup refreshes. Verify unauthorized publish and attachment requests are refused. Validate real HTTPS/access controls with IT.

## Rollback

Stop this version and restore the previous application source/config (not the database or live registry backups unless a separately reviewed recovery is required). The proof module makes no Job Manager database schema changes. New proof publications are persistent in the shared registry; do not overwrite it with the bundled offline reference. Existing job snapshots are ordinary PDFs and can remain. Reopen the previous Job Manager after verification.
