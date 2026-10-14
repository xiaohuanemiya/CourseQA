"""Process-local bounded caches for immutable, scope-keyed retrieval indexes.

Only published-document statistics and graph topology are cached, never queries,
dialogue, answers, authorization decisions, or database session objects.
"""
from collections import OrderedDict
from threading import RLock


class BoundedCache:
    def __init__(self, max_entries=24, max_weight=8_000_000):
        self.max_entries, self.max_weight = max_entries, max_weight
        self._values = OrderedDict()
        self._weight = 0
        self._lock = RLock()

    def get_or_create(self, key, factory):
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key][0]
        # Expensive tokenization does not hold a process-wide cache lock.
        value, weight = factory()
        if weight > self.max_weight:
            return value
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key][0]
            self._values[key] = (value, weight)
            self._weight += weight
            while len(self._values) > self.max_entries or self._weight > self.max_weight:
                _, (_, removed_weight) = self._values.popitem(last=False)
                self._weight -= removed_weight
        return value

    def clear(self):
        with self._lock:
            self._values.clear()
            self._weight = 0

    def info(self):
        with self._lock:
            return {"entries": len(self._values), "weight": self._weight}


indexes = BoundedCache()
