from rpc_state_indexer.errors import BatchError, BatchVerificationError


class HistoricalExecutionError(BatchError):
    pass


class UnsupportedExecutionRange(HistoricalExecutionError):
    pass


class BatchResultCountMismatch(BatchVerificationError):
    pass


class SentinelMismatch(BatchVerificationError):
    pass


class AnchorHashMismatch(BatchVerificationError):
    pass


class LegacyBatchResponseError(BatchVerificationError):
    pass


class MissingBatchResponses(LegacyBatchResponseError):
    pass


class DuplicateBatchResponseId(LegacyBatchResponseError):
    pass


class UnknownBatchResponseId(LegacyBatchResponseError):
    pass


class MalformedBatchResponse(LegacyBatchResponseError):
    pass


class ProviderQuorumMismatch(BatchVerificationError):
    pass

