import threading

from tqdm import tqdm


class InterceptableTqdm(tqdm):
    """
    Sottoclasse di tqdm che si registra globalmente alla creazione,
    permettendo a thread esterni di campionare il progresso di tutti
    i nodi attivi del testbench.

    Il registro viene pulito automaticamente alla chiusura di ogni istanza
    (via context manager o chiusura esplicita). reset_all() è un safety net
    da chiamare dopo thread.join() a fine run.
    """

    _registry: dict[int, "InterceptableTqdm"] = {}
    _registry_lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with InterceptableTqdm._registry_lock:
            InterceptableTqdm._registry[id(self)] = self

    def close(self):
        with InterceptableTqdm._registry_lock:
            InterceptableTqdm._registry.pop(id(self), None)
        super().close()

    def get_progress(self) -> tuple[str, int, int | None]:
        """Restituisce (description, current, total) per questo nodo."""
        return (self.desc or "", self.n, self.total)

    @classmethod
    def get_all(cls) -> dict[int, "InterceptableTqdm"]:
        with cls._registry_lock:
            return dict(cls._registry)

    @classmethod
    def reset_all(cls):
        """
        Chiude tutte le istanze rimaste aperte e svuota il registro.
        Da chiamare nel blocco finally del thread dopo testbench.run().
        """
        with cls._registry_lock:
            instances = list(cls._registry.values())
            cls._registry.clear()
        for pbar in instances:
            pbar.close()