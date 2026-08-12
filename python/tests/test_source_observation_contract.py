from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from clashlens.source_observation_contract import (
    SOURCE_OBSERVATION_CONTRACTS,
    get_source_observation_contract,
    validate_source_observation_contract,
)


@pytest.mark.parametrize(
    ("endpoint", "endpoint_version", "schema_version", "parser_versions"),
    [
        (
            "profile",
            "profile-v1",
            "profile-schema-v1",
            frozenset({"supercell-source-parser-v1", "supercell-source-parser-v2"}),
        ),
        (
            "battle_log",
            "battle-log-v1",
            "battle-log-schema-v1",
            frozenset({"supercell-source-parser-v1", "supercell-source-parser-v2"}),
        ),
        (
            "global_player_rankings",
            "global-player-rankings-v1",
            "global-player-rankings-schema-v1",
            frozenset({"supercell-source-parser-v1", "supercell-source-parser-v2"}),
        ),
    ],
)
def test_source_observation_contract_accepts_each_installed_endpoint(
    endpoint: str,
    endpoint_version: str,
    schema_version: str,
    parser_versions: frozenset[str],
) -> None:
    contract = get_source_observation_contract(endpoint)

    assert contract is not None
    assert contract.endpoint == endpoint
    assert contract.endpoint_version == endpoint_version
    assert contract.schema_version == schema_version
    assert contract.default_parser_version == "supercell-source-parser-v1"
    assert contract.supported_parser_versions == parser_versions
    assert (
        validate_source_observation_contract(
            endpoint,
            endpoint_version,
            schema_version,
            "supercell-source-parser-v2",
        )
        is None
    )


@pytest.mark.parametrize(
    ("endpoint", "endpoint_version", "schema_version", "parser_version", "category"),
    [
        (
            "future_endpoint",
            "future-v1",
            "future-schema-v1",
            "supercell-source-parser-v1",
            "unsupported_endpoint",
        ),
        (
            "profile",
            "profile-v99",
            "profile-schema-v99",
            "supercell-source-parser-v99",
            "unsupported_endpoint_version",
        ),
        (
            "battle_log",
            "battle-log-v1",
            "battle-log-schema-v99",
            "supercell-source-parser-v99",
            "unsupported_schema_version",
        ),
        (
            "global_player_rankings",
            "global-player-rankings-v1",
            "global-player-rankings-schema-v1",
            "supercell-source-parser-v99",
            "unsupported_parser_version",
        ),
    ],
)
def test_source_observation_contract_rejects_in_validation_order(
    endpoint: str,
    endpoint_version: str,
    schema_version: str,
    parser_version: str,
    category: str,
) -> None:
    assert (
        validate_source_observation_contract(
            endpoint, endpoint_version, schema_version, parser_version
        )
        == category
    )


def test_source_observation_contract_registry_is_immutable() -> None:
    contract = SOURCE_OBSERVATION_CONTRACTS[0]

    with pytest.raises(FrozenInstanceError):
        contract.endpoint = "changed"  # type: ignore[misc]
