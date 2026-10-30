"""Pipeline exceptions."""


class EtlError(Exception):
    """Base class for errors the pipeline can report cleanly."""


class ConfigError(EtlError):
    """The configuration file or environment is invalid."""


class IngestError(EtlError):
    """A source could not be read."""


class TransformError(EtlError):
    """The input table cannot be normalized."""


class LoadError(EtlError):
    """Rows could not be written to the database."""


class EmailError(EtlError):
    """The report could not be sent."""
