import threading

from tqdm import tqdm


class InterceptableTqdm(tqdm):
    """
    Subclass of tqdm that registers itself globally upon creation,
    allowing external threads to sample the progress of all
    active nodes in the testbench.

    In addition to maintaining a registry of *active* instances, it also keeps
    a registry of *closed* instances, so that an external poller can retrieve
    the final state of a progress bar even after it has removed itself
    from the active registry (otherwise a node that finishes between two polls
    would silently disappear from progress messages).
    """

    _registry: dict[int, "InterceptableTqdm"] = {}
    _closed_registry: dict[str, tuple[str, int, int | None]] = {}
    _registry_lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with InterceptableTqdm._registry_lock:
            InterceptableTqdm._registry[id(self)] = self

    def close(self):
        with InterceptableTqdm._registry_lock:
            InterceptableTqdm._registry.pop(id(self), None)
            node_id = self._extract_node_id()
            if node_id is not None:
                InterceptableTqdm._closed_registry[node_id] = (
                    self.desc or "",
                    self.n,
                    self.total,
                )
        super().close()

    def _extract_node_id(self) -> str | None:
        """
        Parses the node_id out of the 'desc|node_id' convention used
        throughout plctestbench. Returns None if the bar wasn't tagged
        with a node_id (shouldn't normally happen for real nodes, but
        guards against malformed descriptions).
        """
        desc = self.desc or ""
        if "|" not in desc:
            return None
        return desc.split("|", 1)[1] or None

    def get_progress(self) -> tuple[str, int, int | None]:
        """Restituisce (description, current, total) per questo nodo."""
        return (self.desc or "", self.n, self.total)

    @classmethod
    def get_all(cls) -> dict[int, "InterceptableTqdm"]:
        with cls._registry_lock:
            return dict(cls._registry)

    @classmethod
    def get_all_closed(cls) -> dict[str, tuple[str, int, int | None]]:
        """Returns node_id -> (desc, current, total) for bars that have
        already closed. Entries persist for the lifetime of the run
        (cleared by reset_all()), since a node only closes once."""
        with cls._registry_lock:
            return dict(cls._closed_registry)

    @classmethod
    def reset_all(cls):
        """
        Closes all remaining open instances and clears both registries.
        Should be called in the finally block of the thread after testbench.run(),
        so that the registries (which are class-level, thus shared between
        different runs in the same process) don't retain state between runs.
        """
        with cls._registry_lock:
            instances = list(cls._registry.values())
            cls._registry.clear()
            cls._closed_registry.clear()
        for pbar in instances:
            pbar.close()
