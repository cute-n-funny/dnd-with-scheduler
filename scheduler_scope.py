"""Isolate test scheduler data by Discord interaction/task context.

ContextVar values are local to each asyncio task, so guilds and callbacks
cannot accidentally switch each other's database selection.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_TEST_SCOPE = ContextVar('dnd_scheduler_test_scope', default=False)


def in_test_scope():
    return _TEST_SCOPE.get()


@contextmanager
def scheduler_scope(test_mode):
    token = _TEST_SCOPE.set(bool(test_mode))
    try:
        yield
    finally:
        _TEST_SCOPE.reset(token)


def scoped_callback(func):
    """Route persistent and ephemeral Discord interactions to their poll's DB."""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        with scheduler_scope(getattr(self, 'test_mode', False)):
            return await func(self, *args, **kwargs)
    return wrapper
