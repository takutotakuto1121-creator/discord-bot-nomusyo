"""Storage-layer exceptions. Messages never contain row data or secrets."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for storage failures."""


class MigrationError(StorageError):
    """A migration could not be discovered, verified or applied."""


class SchemaTooNewError(MigrationError):
    """The database was migrated by a newer version of the code (downgrade is unsupported)."""


class ChecksumMismatchError(MigrationError):
    """An already applied migration file was edited."""


class InstanceLockError(StorageError):
    """Another live instance holds the instance lock."""
