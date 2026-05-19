"""Plugin registration primitive.

Public API:

* :class:`PluginConfig` — dataclass holding optional lifecycle hooks
  plus an optional ``name``.
* :func:`register_plugin` — called from a plugin's module body to
  register a ``PluginConfig`` with the framework.
* :data:`registered_plugins` — the live list of registrations, in
  registration order. Public, mutable; plugins may inspect / reorder /
  replace entries.

The four hooks (``on_before_init`` / ``on_after_init`` /
``on_before_run`` / ``on_after_run``) are fired by ``Browser`` from
``__init__`` and ``run`` (see ``040-state.py``). Hook signatures are
typed as ``Any`` here because ``Browser`` / ``BrowserConfig`` are
defined later in the concatenated build; the docstring records the
intended shape.
"""

import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class PluginConfig:
    """Lifecycle-hook bundle for a plugin.

    Fields:
      name:           Display name (logs, ``registered_plugins``
                      inspection). If ``None`` at ``register_plugin``
                      time, defaults to the caller module's
                      ``__name__``.
      on_before_init: ``(browser, config) -> None`` — fires at the
                      top of ``Browser.__init__`` before any
                      construction. ``browser`` is essentially empty;
                      mutate ``config`` (a ``BrowserConfig``) to
                      influence what the Browser becomes.
      on_after_init:  ``(browser) -> None`` — fires at the end of
                      ``Browser.__init__``. Browser is fully built.
      on_before_run:  ``(browser) -> None`` — fires at the top of
                      ``Browser.run`` before the event loop starts.
      on_after_run:   ``(browser) -> None`` — fires in a ``finally``
                      at the end of ``Browser.run``, so cleanup runs
                      even when the loop exits via exception.
    """
    name: Optional[str] = None
    on_before_init: Optional[Callable[[Any, Any], None]] = None
    on_after_init:  Optional[Callable[[Any], None]] = None
    on_before_run:  Optional[Callable[[Any], None]] = None
    on_after_run:   Optional[Callable[[Any], None]] = None


registered_plugins: list = []


def register_plugin(cfg: PluginConfig) -> None:
    """Register ``cfg`` with the framework.

    Called from a plugin module's body. The plugin is appended to
    ``registered_plugins`` in call order. If ``cfg.name`` is ``None``,
    it is filled in from the caller frame's ``__name__`` so logs and
    introspection show the right module without each plugin having to
    spell its own name.

    Multiple calls from the same module are allowed and each appends
    a separate entry. Idempotency by module identity is not enforced
    — plugins that conditionally register different hook sets benefit
    from this.
    """
    if cfg.name is None:
        try:
            frame = sys._getframe(1)
            cfg.name = frame.f_globals.get('__name__', '<unknown>')
        except (ValueError, AttributeError):
            cfg.name = '<unknown>'
    registered_plugins.append(cfg)
