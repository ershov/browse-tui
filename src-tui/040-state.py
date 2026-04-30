"""browse-tui: state layer (visible tree, cursor/scope, async workers, post queue, Pending)."""


class Pending:
    """A handle for an async operation that may chain follow-up callbacks.

    Lifecycle:
      - Constructed: ``done`` is False, ``_chain`` is empty.
      - ``.then(cb)`` before resolve: appends cb to _chain; returns self.
      - ``.then(cb)`` after resolve: invokes cb synchronously; returns self.
      - ``._resolve()``: sets done=True (idempotent), snapshots and clears
        _chain, then runs each snapshotted callback in registration order.
        A callback may register further callbacks via ``.then()`` during
        its execution; because _done is already True, those callbacks fire
        synchronously inside the inner ``.then()`` call rather than being
        queued -- see the snapshot-and-clear pattern below.
    """

    __slots__ = ('_done', '_chain')

    def __init__(self):
        self._done = False
        self._chain = []

    @property
    def done(self) -> bool:
        return self._done

    def then(self, callback) -> 'Pending':
        if self._done:
            callback()
        else:
            self._chain.append(callback)
        return self

    def _resolve(self) -> None:
        if self._done:
            return  # idempotent -- second resolve is a no-op
        self._done = True
        # Snapshot-and-clear before iterating: a callback that registers
        # further callbacks via .then() during iteration will see _done=True
        # and have its callback fire synchronously inside .then(), bypassing
        # this loop. Without the clear, those would also accumulate in the
        # iterated list and fire here, breaking the documented order.
        chain, self._chain = self._chain, []
        for cb in chain:
            cb()
