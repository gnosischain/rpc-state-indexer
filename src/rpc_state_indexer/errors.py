"""Typed service errors used for fail-closed control flow."""


class IndexerError(RuntimeError):
    """Base class for expected indexer failures."""


class ConfigError(IndexerError):
    pass


class RpcError(IndexerError):
    pass


class RpcAttemptsExhausted(RpcError):
    pass


class ArchiveStateUnavailable(RpcError):
    pass


class AnchorError(IndexerError):
    pass


class AnchorNotFinalized(AnchorError):
    pass


class AnchorConflict(AnchorError):
    pass


class AnchorValidationError(AnchorError):
    pass


class BatchError(IndexerError):
    pass


class BatchVerificationError(BatchError):
    pass


class DiscoveryError(IndexerError):
    pass


class ObservationError(IndexerError):
    pass


class IntegrityError(IndexerError):
    pass


class PublicationBlocked(IndexerError):
    def __init__(self, checks: list[str]) -> None:
        self.checks = checks
        super().__init__("publication blocked: " + ", ".join(checks))

