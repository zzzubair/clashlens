from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceObservationContract:
    endpoint: str
    endpoint_version: str
    schema_version: str
    default_parser_version: str
    supported_parser_versions: frozenset[str]


_DEFAULT_PARSER_VERSION = "supercell-source-parser-v1"
_SUPPORTED_PARSER_VERSIONS = frozenset(
    {_DEFAULT_PARSER_VERSION, "supercell-source-parser-v2"}
)

PROFILE_SOURCE_OBSERVATION_CONTRACT = SourceObservationContract(
    endpoint="profile",
    endpoint_version="profile-v1",
    schema_version="profile-schema-v1",
    default_parser_version=_DEFAULT_PARSER_VERSION,
    supported_parser_versions=_SUPPORTED_PARSER_VERSIONS,
)
BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT = SourceObservationContract(
    endpoint="battle_log",
    endpoint_version="battle-log-v1",
    schema_version="battle-log-schema-v1",
    default_parser_version=_DEFAULT_PARSER_VERSION,
    supported_parser_versions=_SUPPORTED_PARSER_VERSIONS,
)
GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT = SourceObservationContract(
    endpoint="global_player_rankings",
    endpoint_version="global-player-rankings-v1",
    schema_version="global-player-rankings-schema-v1",
    default_parser_version=_DEFAULT_PARSER_VERSION,
    supported_parser_versions=_SUPPORTED_PARSER_VERSIONS,
)

SOURCE_OBSERVATION_CONTRACTS = (
    PROFILE_SOURCE_OBSERVATION_CONTRACT,
    BATTLE_LOG_SOURCE_OBSERVATION_CONTRACT,
    GLOBAL_PLAYER_RANKINGS_SOURCE_OBSERVATION_CONTRACT,
)


def get_source_observation_contract(
    endpoint: str | None,
) -> SourceObservationContract | None:
    return next(
        (
            contract
            for contract in SOURCE_OBSERVATION_CONTRACTS
            if contract.endpoint == endpoint
        ),
        None,
    )


def validate_source_observation_contract(
    endpoint: str | None,
    endpoint_version: str | None,
    schema_version: str | None,
    parser_version: str,
) -> str | None:
    contract = get_source_observation_contract(endpoint)
    if contract is None:
        return "unsupported_endpoint"
    if endpoint_version != contract.endpoint_version:
        return "unsupported_endpoint_version"
    if schema_version != contract.schema_version:
        return "unsupported_schema_version"
    if parser_version not in contract.supported_parser_versions:
        return "unsupported_parser_version"
    return None
