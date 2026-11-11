"""
job_build.py — one place that knows how to undo a half-made job
===============================================================
Creating a job (or adding data to one) takes things one after another: a job number,
a folder, serial numbers, sheet numbers, files. If a later step fails, the earlier
ones have to be put back, and some must never be put back (serial numbers stay used).

Each step tells the Build what it took and how to undo it:

    build = Build()
    build.took("job number", undo=lambda why: discard_row(job_id))
    build.took("folder",     undo=lambda why: shutil.rmtree(job_dir, ignore_errors=True))
    build.took("serials")                       # no undo: a serial, once given out, stays used
    ...
    raise build.stop("The data sheet could not be written, so nothing was created.", 500)

stop() undoes everything still on the list, newest first, and returns a Stop carrying the
answer for the caller (error text and HTTP status). keep() says "whatever has been taken
so far now stays": use it once undoing would do more harm than good (for example, serials
are recorded under this job number, so the number must not be handed out again).

A step that refuses before anything was taken raises refuse(...), the same Stop with nothing to undo.
"""


class Stop(Exception):
    """Ends a build. answer is (body, http_status), ready to return."""

    def __init__(self, body, status):
        super().__init__(body.get("error") if isinstance(body, dict) else str(body))
        self.answer = (body, status)


def refuse(error, status, **extra):
    return Stop({"error": error, **extra}, status)


class Build:
    def __init__(self):
        self._taken = []            # every (what, undo) so far, in order
        self._undoable = []         # the ones stop() / rollback() would still undo
        self.warnings = []
        self.undo_errors = []       # (what, exception) for an undo that itself failed

    def took(self, what, undo=None):
        """Record something this build now holds. undo(why) puts it back; None = it is never put back."""
        self._taken.append(what)
        if undo is not None:
            self._undoable.append((what, undo))

    def has(self, what) -> bool:
        return what in self._taken

    def keep(self):
        """Everything taken so far stays, whatever happens next."""
        self._undoable = []

    def warn(self, message):
        self.warnings.append(message)

    def rollback(self, because="") -> list:
        """Undo what can still be undone, newest first. Returns what was undone."""
        done = []
        while self._undoable:
            what, undo = self._undoable.pop()
            try:
                undo(because)
                done.append(what)
            except Exception as e:          # one failed undo must not block the rest
                self.undo_errors.append((what, e))
        return done

    def stop(self, error, status, because="", **extra) -> Stop:
        """Undo, then hand back the Stop to raise:  raise build.stop("...", 500)"""
        self.rollback(because)
        return Stop({"error": error, **extra}, status)
