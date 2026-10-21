"""Bounded persistence. Atomic compare-and-set protects every read dependency."""
from copy import deepcopy
import threading


class CapacityError(RuntimeError):
    pass


class MemoryStore:
    """Test double only; production must use FirestoreStore."""
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()

    def get(self, path):
        with self.lock:
            return deepcopy(self.data.get(path))

    def scan(self, collection, limit=2500):
        with self.lock:
            result = {k: deepcopy(v) for k, v in self.data.items()
                      if k.startswith(collection + "/")}
        if len(result) > limit:
            raise CapacityError("Collection requires maintenance before continuing.")
        return result

    def atomic(self, paths, mutate):
        with self.lock:
            values = {p: deepcopy(self.data.get(p)) for p in paths}
            result = mutate(values)
            for path, value in values.items():
                if value is not None:
                    self.data[path] = deepcopy(value)
            return result


class FirestoreStore:
    def __init__(self, project, database="(default)"):
        from google.cloud import firestore
        self.db = firestore.Client(project=project, database=database)

    def get(self, path):
        doc = self.db.document(path).get(retry=None, timeout=8)
        return doc.to_dict() if doc.exists else None

    def scan(self, collection, limit=2500):
        docs = self.db.collection(collection).limit(limit + 1).stream(retry=None, timeout=8)
        result = {d.reference.path: d.to_dict() for d in docs}
        if len(result) > limit:
            raise CapacityError("Collection requires maintenance before continuing.")
        return result

    def atomic(self, paths, mutate):
        from google.api_core.exceptions import AlreadyExists, Conflict, FailedPrecondition
        # Rewrite each existing read dependency with an update-time precondition.
        # A single atomic commit either validates every dependency or writes none.
        # A timeout is NOT retried: a quota reservation might already have committed.
        for attempt in range(3):
            snapshots = {p: self.db.document(p).get(retry=None, timeout=8) for p in paths}
            values = {p: s.to_dict() if s.exists else None for p, s in snapshots.items()}
            result = mutate(values)
            batch = self.db.batch()
            for path, value in values.items():
                snapshot = snapshots[path]
                if value is None:
                    continue
                if snapshot.exists:
                    batch.update(snapshot.reference, value,
                                 option=self.db.write_option(last_update_time=snapshot.update_time))
                else:
                    batch.create(snapshot.reference, value)
            try:
                batch.commit(retry=None, timeout=8)
                return result
            except (AlreadyExists, Conflict, FailedPrecondition):
                if attempt == 2:
                    raise
