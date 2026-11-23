"""Session-scoped discovery notifications. No workbook writes or GUI dependencies."""
from dataclasses import astuple

INTERVAL_SECONDS = 60

def identity(c):
    return (c.path.casefold(), c.id.upper(), c.sha.lower(), c.status)

def fingerprint(rows):
    return tuple(sorted(tuple(str(v) for v in astuple(c)) for c in rows))

class MonitorState:
    def __init__(self):
        self.scope = None
        self.seen = set()
        self.last_report = None

    def switch(self, scope):
        if scope != self.scope:
            self.scope = scope
            self.seen.clear()
            self.last_report = None

    def new_alerts(self, rows):
        """Alert once for each distinct unambiguous PDF version per session/scope."""
        fresh = [c for c in rows if c.status == 'New' and identity(c) not in self.seen]
        self.seen.update(identity(c) for c in rows if c.status == 'New')
        return fresh
