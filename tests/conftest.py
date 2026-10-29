import logging

import pytest

from nomusyo.bot.app import QUIET_LOGGERS


def _tracked_loggers():
    names = set(QUIET_LOGGERS) | {"nomusyo"}
    names |= {n for n in logging.root.manager.loggerDict if n.startswith("nomusyo")}
    return sorted(names)


@pytest.fixture(autouse=True)
def restore_logging_state():
    """Snapshot and restore global logging state so tests calling configure_logging don't leak."""
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level, list(root.filters), root.disabled)
    saved_disable = logging.root.manager.disable
    saved = {}
    for name in _tracked_loggers():
        lg = logging.getLogger(name)
        saved[name] = (lg.level, lg.propagate, lg.disabled, list(lg.filters), list(lg.handlers))
    yield
    handlers, level, filters, disabled = saved_root
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    root.filters[:] = filters
    root.disabled = disabled
    logging.disable(saved_disable)
    for name in _tracked_loggers():
        lg = logging.getLogger(name)
        if name in saved:
            lg.setLevel(saved[name][0])
            lg.propagate, lg.disabled = saved[name][1], saved[name][2]
            lg.filters[:] = saved[name][3]
            lg.handlers[:] = saved[name][4]
        else:
            lg.setLevel(logging.NOTSET)
            lg.propagate, lg.disabled = True, False
