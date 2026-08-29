"""Create a bounded, write-once Clash Lens deployment receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
CANONICAL_REPOSITORY_URL = "https://github.com/zzzubair/clashlens"
SCOPES = {"candidate-preparation", "deployed-stack"}
CANDIDATE_SCOPE_LABEL_KEY = "org.clashlens.scope"
CANDIDATE_SCOPE_LABEL_VALUE = "candidate"
CANDIDATE_SCOPE_LABEL = (
    f"{CANDIDATE_SCOPE_LABEL_KEY}={CANDIDATE_SCOPE_LABEL_VALUE}"
)
CANDIDATE_RECEIPT_OFFICIAL_API_PROOF = (
    "receipt-command-inspects-images-database-and-bounded-candidate-resources-only"
)
SAFE_CONFIGURATION_FIELDS = {
    "collector_database_pool_size",
    "spool_free_inode_floor",
    "spool_free_space_floor",
    "spool_max_body_bytes",
    "spool_max_bytes",
    "spool_max_objects",
    "worker_archive_pool_size",
    "worker_concurrency",
    "worker_database_pool_size",
    "worker_lease_seconds",
    "worker_replicas",
}
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_HEX_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"(?:[A-Za-z0-9._:/@+-]{1,256}@)?sha256:[0-9a-f]{64}\Z")
_SAFE_VALUE = re.compile(r"[0-9]{1,20}\Z")
_MIGRATION = re.compile(r"([0-9]{4})_[a-z0-9_]{1,240}\.sql\Z")
_VERSION_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+:/()=-]{0,255}\Z")
_PYTHON_VERSION = re.compile(
    r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?(?:[-+][A-Za-z0-9._-]{1,32})?\Z"
)
_PODMAN_VERSION = re.compile(r"podman version [0-9][A-Za-z0-9._+-]{0,63}\Z")
_POSTGRES_VERSION = re.compile(
    r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?(?:[ A-Za-z0-9._()+-]{0,240})\Z"
)
_POSTGRES_VERSION_NUM = re.compile(r"[0-9]{1,10}\Z")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{1,20}\Z")
_DEFAULT_RESOURCE_NAMES = {
    "network": "clashlens-private",
    "volume": "clashlens-postgres-data",
    "postgres_container": "clashlens-postgres",
    "collector": "clashlens-collector",
    "python": "clashlens-python-api",
    "worker": "clashlens-python-worker",
    "website": "clashlens-website",
}
_WORKER_REPLICA_MAX = 16

_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_scope",
    "environment_identity",
    "production_deployment_status",
    "created_at",
    "source",
    "migrations",
    "configuration",
    "application_images",
    "database",
    "candidate_resources",
    "runtime_versions",
    "official_api_requests",
}
_SOURCE_FIELDS = {
    "repository_url",
    "revision",
    "clean",
    "clean_check",
}
_MIGRATION_FIELDS = {"filename", "sha256", "applied"}
_CONFIGURATION_FIELDS = {"allowlist_version", "fields", "fingerprint"}
_IMAGE_FIELDS = {
    "requested_reference",
    "identity_type",
    "image_id",
    "registry_digest",
    "source_label",
    "revision_label",
}
_DEPLOYED_IMAGE_FIELDS = _IMAGE_FIELDS | {"container_name"}
_DATABASE_FIELDS = {
    "contract_version",
    "applied_migration_versions",
    "server_version",
    "server_version_num",
    "system_identifier",
    "container_name",
    "database_name",
    "identity_scope",
}
_RUNTIME_FIELDS = {"receipt_python", "podman", "postgresql"}
_API_PROOF_FIELDS = {"count", "proof"}
_CANDIDATE_RESOURCES_FIELDS = {
    "postgres_container",
    "network",
    "volume",
    "application_containers",
}
_CANDIDATE_RESOURCE_FIELDS = {"name", "scope_label"}
_CANDIDATE_APPLICATION_FIELDS = {"name", "present"}
_CANDIDATE_APPLICATIONS_FIELDS = {
    "collector",
    "python",
    "worker",
    "worker_replicas",
    "website",
}


class ReceiptError(RuntimeError):
    """The requested evidence could not be established safely."""


Runner = Callable[[Sequence[str]], str]


def _require_mapping(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ReceiptError(f"{label} schema is invalid")
    return value


def _require_text(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or pattern.fullmatch(value) is None
    ):
        raise ReceiptError(f"{label} is invalid")
    return value


def _run(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReceiptError("required receipt input is unavailable") from error
    if len(command) >= 3 and tuple(command[1:3]) == ("container", "exists"):
        # `container exists` uses status 1 for a normal, absent container. Do
        # not turn other Podman failures into a claim of absence.
        if result.returncode == 0:
            return "true"
        if result.returncode == 1:
            return "false"
    if result.returncode != 0:
        raise ReceiptError("required receipt input is unavailable")
    return result.stdout.strip()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(receipt: dict[str, Any]) -> str:
    canonical = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{_sha256(payload)}"


def _bounded(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str):
        raise ReceiptError(f"invalid {label}")
    value = value.strip()
    if pattern.fullmatch(value) is None:
        raise ReceiptError(f"invalid {label}")
    return value


def _optional_digest(value: str) -> str | None:
    value = value.strip()
    if value in {"", "<none>"}:
        return None
    return _bounded(value, _DIGEST, "registry digest")


def _image_id(value: str) -> str:
    """Normalize Podman's equivalent prefixed and bare local image IDs."""
    candidate = value.strip()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        candidate = "sha256:" + candidate
    return _bounded(candidate, _IMAGE_ID, "image ID")


def _validate_results_directory(root: Path, results_directory: Path) -> Path:
    root = root.resolve(strict=True)
    supplied = results_directory.absolute()
    try:
        resolved = results_directory.resolve(strict=True)
    except OSError as error:
        raise ReceiptError("results directory is missing") from error
    if supplied != resolved or results_directory.is_symlink():
        raise ReceiptError("results directory must not use symlinks")
    if not resolved.is_dir():
        raise ReceiptError("results destination is not a directory")
    if resolved == root or root in resolved.parents:
        raise ReceiptError("results directory must be outside the checkout")
    if not os.access(resolved, os.W_OK | os.X_OK):
        raise ReceiptError("results directory is not writable")
    return resolved


def _git_source(root: Path, git_bin: str, run: Runner) -> dict[str, Any]:
    status = run(
        [git_bin, "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"]
    )
    # The production runner returns an empty string for a clean checkout. A
    # fake runner can use the explicit sentinel without weakening that rule.
    if status not in {"", "__CLEAN__"}:
        raise ReceiptError("source checkout is dirty")
    revision = _bounded(
        run([git_bin, "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"]),
        _HEX_SHA,
        "source revision",
    )
    return {
        "repository_url": CANONICAL_REPOSITORY_URL,
        "revision": revision,
        "clean": True,
        "clean_check": "git-status-porcelain-v1-with-untracked-files",
    }


def _migration_state(root: Path, database: dict[str, Any]) -> list[dict[str, Any]]:
    applied = database.get("applied_migration_versions")
    if not isinstance(applied, list) or any(
        not isinstance(version, int) for version in applied
    ):
        raise ReceiptError("database migration state is unavailable")
    paths = sorted((root / "deploy" / "migrations").glob("*.sql"))
    migrations: list[dict[str, Any]] = []
    expected: list[int] = []
    for path in paths:
        match = _MIGRATION.fullmatch(path.name)
        if match is None:
            raise ReceiptError("migration filename is invalid")
        version = int(match.group(1))
        expected.append(version)
        migrations.append(
            {
                "filename": path.name,
                "sha256": _sha256(path.read_bytes()),
                "applied": version in applied,
            }
        )
    if not migrations or applied != expected:
        raise ReceiptError("database migration state does not match the source")
    return migrations


def _image_identity(
    podman_bin: str,
    requested_reference: str,
    source_revision: str,
    run: Runner,
) -> dict[str, Any]:
    reference = _bounded(requested_reference, _NAME, "image reference")
    prefix = [podman_bin, "image", "inspect", "--format"]
    image_id = _image_id(run([*prefix, "{{.Id}}", reference]))
    registry_digest = _optional_digest(
        run(
            [
                *prefix,
                "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}<none>{{end}}",
                reference,
            ]
        )
    )
    source_label = run(
        [
            *prefix,
            '{{index .Labels "org.opencontainers.image.source"}}',
            reference,
        ]
    ).strip()
    revision_label = run(
        [
            *prefix,
            '{{index .Labels "org.opencontainers.image.revision"}}',
            reference,
        ]
    ).strip()
    if source_label != CANONICAL_REPOSITORY_URL or revision_label != source_revision:
        raise ReceiptError("application image labels do not match the source")
    return {
        "requested_reference": reference,
        "identity_type": "image_id",
        "image_id": image_id,
        "registry_digest": registry_digest,
        "source_label": source_label,
        "revision_label": revision_label,
    }


def _container_identity(
    podman_bin: str,
    container_name: str,
    source_revision: str,
    run: Runner,
) -> dict[str, Any]:
    name = _bounded(container_name, _IDENTITY, "container name")
    prefix = [podman_bin, "container", "inspect", "--format"]
    running = run([*prefix, "{{.State.Running}}", name]).strip()
    if running != "true":
        raise ReceiptError("required deployed container is not running")
    reference = _bounded(
        run([*prefix, "{{.ImageName}}", name]), _NAME, "container image reference"
    )
    image_id = _image_id(run([*prefix, "{{.Image}}", name]))
    identity = _image_identity(podman_bin, image_id, source_revision, run)
    identity["requested_reference"] = reference
    identity["container_name"] = name
    return identity


def _candidate_name(value: Any, resource: str, *, strict: bool = False) -> str:
    name = (
        _require_text(value, _IDENTITY, f"candidate {resource} name")
        if strict
        else _bounded(value, _IDENTITY, f"candidate {resource} name")
    )
    default_name = _DEFAULT_RESOURCE_NAMES.get(resource)
    if default_name is not None and name.casefold() == default_name.casefold():
        raise ReceiptError("candidate resource identity is not dedicated")
    return name


def _worker_replica_names(worker_name: str) -> list[str]:
    return [
        _bounded(
            f"{worker_name}-{replica}",
            _IDENTITY,
            "candidate worker replica name",
        )
        for replica in range(1, _WORKER_REPLICA_MAX + 1)
    ]


def _candidate_resource_names(arguments: argparse.Namespace) -> dict[str, str]:
    fields = {
        "network": ("podman_network", "candidate_network", "network"),
        "volume": ("podman_volume", "candidate_volume", "volume"),
        "postgres_container": ("postgres_container",),
        "collector": ("collector_container",),
        "python": ("python_container",),
        "worker": ("worker_container", "python_worker_container"),
        "website": ("website_container",),
    }
    names: dict[str, str] = {}
    for resource, candidates in fields.items():
        value = next(
            (
                getattr(arguments, field)
                for field in candidates
                if hasattr(arguments, field)
            ),
            None,
        )
        name = _candidate_name(value, resource)
        names[resource] = name

    seen: set[str] = set()
    for name in (*names.values(), *_worker_replica_names(names["worker"])):
        normalized = name.casefold()
        if normalized in seen:
            raise ReceiptError("candidate resource scope is ambiguous")
        seen.add(normalized)
    return names


def _inspect_name(
    podman_bin: str,
    resource_type: str,
    name: str,
    run: Runner,
) -> None:
    inspected = run(
        [podman_bin, resource_type, "inspect", "--format", "{{.Name}}", name]
    ).strip()
    inspected = inspected.removeprefix("/")
    inspected = _bounded(inspected, _IDENTITY, f"{resource_type} name")
    if inspected != name:
        raise ReceiptError("candidate resource name does not match configuration")


def _inspect_scope_label(
    podman_bin: str,
    resource_type: str,
    name: str,
    run: Runner,
) -> None:
    labels = ".Config.Labels" if resource_type == "container" else ".Labels"
    value = _bounded(
        run(
            [
                podman_bin,
                resource_type,
                "inspect",
                "--format",
                f'{{{{index {labels} "{CANDIDATE_SCOPE_LABEL_KEY}"}}}}',
                name,
            ]
        ),
        _IDENTITY,
        "candidate scope label",
    )
    if value != CANDIDATE_SCOPE_LABEL_VALUE:
        raise ReceiptError("candidate resource scope label is invalid")


def _container_exists(podman_bin: str, name: str, run: Runner) -> bool:
    state = run([podman_bin, "container", "exists", name]).strip().lower()
    if state in {"false", "absent", "0"}:
        return False
    if state in {"true", "present", "1"}:
        return True
    raise ReceiptError("candidate application container presence is unavailable")


def _verify_candidate_scope(
    arguments: argparse.Namespace,
    run: Runner,
) -> dict[str, Any]:
    names = _candidate_resource_names(arguments)
    for resource_type, resource in (
        ("network", "network"),
        ("volume", "volume"),
        ("container", "postgres_container"),
    ):
        name = names[resource]
        _inspect_name(arguments.podman_bin, resource_type, name, run)
        _inspect_scope_label(arguments.podman_bin, resource_type, name, run)

    worker_replicas = _worker_replica_names(names["worker"])
    application_containers = {
        "collector": names["collector"],
        "python": names["python"],
        "worker": names["worker"],
        "website": names["website"],
        "worker_replicas": worker_replicas,
    }
    for name in (
        application_containers["collector"],
        application_containers["python"],
        application_containers["worker"],
        application_containers["website"],
        *worker_replicas,
    ):
        if _container_exists(arguments.podman_bin, name, run):
            raise ReceiptError("candidate application container is present")

    return {
        "postgres_container": {
            "name": names["postgres_container"],
            "scope_label": CANDIDATE_SCOPE_LABEL,
        },
        "network": {
            "name": names["network"],
            "scope_label": CANDIDATE_SCOPE_LABEL,
        },
        "volume": {
            "name": names["volume"],
            "scope_label": CANDIDATE_SCOPE_LABEL,
        },
        "application_containers": {
            key: (
                [
                    {"name": replica, "present": False}
                    for replica in value
                ]
                if key == "worker_replicas"
                else {"name": value, "present": False}
            )
            for key, value in application_containers.items()
        },
    }


def _database_identity(
    podman_bin: str,
    container_name: str,
    database_user: str,
    database_name: str,
    scope: str,
    run: Runner,
) -> dict[str, Any]:
    container = _bounded(container_name, _IDENTITY, "PostgreSQL container")
    user = _bounded(database_user, _IDENTITY, "PostgreSQL user")
    name = _bounded(database_name, _IDENTITY, "PostgreSQL database")
    query = """
SELECT json_build_object(
  'contract_version', (SELECT version FROM clash_lens_contract WHERE singleton),
  'applied_migration_versions', (SELECT json_agg(version ORDER BY version) FROM clash_lens_schema_migrations),
  'server_version', current_setting('server_version'),
  'server_version_num', current_setting('server_version_num'),
  'system_identifier', (SELECT system_identifier::text FROM pg_control_system())
)::text
""".strip()
    raw = run(
        [
            podman_bin,
            "exec",
            container,
            "psql",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            user,
            "--dbname",
            name,
            "--command",
            query,
        ]
    )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ReceiptError("database migration state is unavailable") from error
    if not isinstance(payload, dict):
        raise ReceiptError("database migration state is unavailable")
    for field in ("server_version", "server_version_num", "system_identifier"):
        value = payload.get(field)
        if not isinstance(value, str):
            raise ReceiptError("PostgreSQL identity is unavailable")
        payload[field] = _bounded(value, _VERSION_TEXT, "PostgreSQL identity")
    if not isinstance(payload.get("contract_version"), int):
        raise ReceiptError("database contract version is unavailable")
    payload.update(
        {
            "container_name": container,
            "database_name": name,
            "identity_scope": (
                "disposable_validation_database"
                if scope == "candidate-preparation"
                else "deployed_stack_database"
            ),
        }
    )
    return payload


def _configuration(values: Sequence[str]) -> dict[str, Any]:
    result: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or key not in SAFE_CONFIGURATION_FIELDS or key in result:
            raise ReceiptError("configuration field is not allowlisted")
        result[key] = _bounded(value, _SAFE_VALUE, "configuration value")
    if set(result) != SAFE_CONFIGURATION_FIELDS:
        raise ReceiptError("safe configuration allowlist is incomplete")
    fingerprint = _sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    )
    return {
        "allowlist_version": "step8-v1",
        "fields": result,
        "fingerprint": f"sha256:{fingerprint}",
    }


def _validate_candidate_resources(
    value: Any,
    database_container_name: str,
) -> None:
    resources = _require_mapping(
        value,
        _CANDIDATE_RESOURCES_FIELDS,
        "candidate resource proof",
    )
    resource_names: list[str] = []
    for resource in ("postgres_container", "network", "volume"):
        details = _require_mapping(
            resources[resource],
            _CANDIDATE_RESOURCE_FIELDS,
            f"candidate {resource} proof",
        )
        name = _candidate_name(details["name"], resource, strict=True)
        if details["scope_label"] != CANDIDATE_SCOPE_LABEL:
            raise ReceiptError("candidate resource scope label is invalid")
        resource_names.append(name)
    if resource_names[0] != database_container_name:
        raise ReceiptError("candidate database resource does not match configuration")

    applications = _require_mapping(
        resources["application_containers"],
        _CANDIDATE_APPLICATIONS_FIELDS,
        "candidate application proof",
    )
    application_names: list[str] = []
    for application in ("collector", "python", "worker", "website"):
        details = _require_mapping(
            applications[application],
            _CANDIDATE_APPLICATION_FIELDS,
            f"candidate {application} proof",
        )
        name = _candidate_name(details["name"], application, strict=True)
        if details["present"] is not False:
            raise ReceiptError("candidate application absence proof is invalid")
        application_names.append(name)

    replicas = applications["worker_replicas"]
    if not isinstance(replicas, list) or len(replicas) != _WORKER_REPLICA_MAX:
        raise ReceiptError("candidate worker replica proof is invalid")
    expected_replicas = _worker_replica_names(application_names[2])
    for index, details in enumerate(replicas):
        details = _require_mapping(
            details,
            _CANDIDATE_APPLICATION_FIELDS,
            "candidate worker replica proof",
        )
        name = _candidate_name(details["name"], "worker replica", strict=True)
        if (
            name != expected_replicas[index]
            or details["present"] is not False
        ):
            raise ReceiptError("candidate worker replica proof is invalid")
        application_names.append(name)

    all_names = resource_names + application_names
    if len({name.casefold() for name in all_names}) != len(all_names):
        raise ReceiptError("candidate resource scope is ambiguous")


def collect_receipt(arguments: argparse.Namespace, run: Runner = _run) -> dict[str, Any]:
    root = Path(arguments.root).resolve(strict=True)
    if arguments.scope not in SCOPES:
        raise ReceiptError("invalid receipt scope")
    environment = _bounded(arguments.environment, _IDENTITY, "environment identity")
    source = _git_source(root, arguments.git_bin, run)
    candidate_resources = (
        _verify_candidate_scope(arguments, run)
        if arguments.scope == "candidate-preparation"
        else None
    )
    database = _database_identity(
        arguments.podman_bin,
        arguments.postgres_container,
        arguments.postgres_user,
        arguments.postgres_database,
        arguments.scope,
        run,
    )
    migrations = _migration_state(root, database)
    image_specs = (
        ("collector", arguments.collector_image, arguments.collector_container),
        ("python", arguments.python_image, arguments.python_container),
        ("website", arguments.website_image, arguments.website_container),
    )
    images = {}
    for application, reference, container in image_specs:
        if arguments.scope == "candidate-preparation":
            images[application] = _image_identity(
                arguments.podman_bin, reference, source["revision"], run
            )
        else:
            images[application] = _container_identity(
                arguments.podman_bin, container, source["revision"], run
            )
    created_at = arguments.created_at or datetime.now(tz=UTC).isoformat()
    try:
        normalized_created_at = datetime.fromisoformat(created_at).astimezone(UTC)
    except ValueError as error:
        raise ReceiptError("creation time is invalid") from error
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "receipt_scope": arguments.scope,
        "environment_identity": environment,
        "production_deployment_status": "not_asserted",
        "created_at": normalized_created_at.isoformat(),
        "source": source,
        "migrations": migrations,
        "configuration": _configuration(arguments.safe_config),
        "application_images": images,
        "database": database,
        "candidate_resources": candidate_resources,
        "runtime_versions": {
            "receipt_python": platform.python_version(),
            "podman": _bounded(
                run([arguments.podman_bin, "--version"]),
                _VERSION_TEXT,
                "Podman version",
            ),
            "postgresql": database["server_version"],
        },
        "official_api_requests": (
            {
                "count": 0,
                "proof": CANDIDATE_RECEIPT_OFFICIAL_API_PROOF,
            }
            if arguments.scope == "candidate-preparation"
            else None
        ),
    }
    validate_receipt(receipt)
    receipt["receipt_digest"] = _canonical_digest(receipt)
    validate_receipt(receipt, require_digest=True)
    return receipt


def validate_receipt(receipt: dict[str, Any], *, require_digest: bool = False) -> None:
    allowed = _RECEIPT_FIELDS | ({"receipt_digest"} if require_digest else set())
    if not isinstance(receipt, dict) or set(receipt) != allowed:
        raise ReceiptError("receipt schema is incomplete")

    schema_version = receipt["schema_version"]
    scope = receipt["receipt_scope"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
        or not isinstance(scope, str)
        or scope not in SCOPES
    ):
        raise ReceiptError("receipt schema is invalid")
    _require_text(receipt["environment_identity"], _IDENTITY, "environment identity")
    if receipt["production_deployment_status"] != "not_asserted":
        raise ReceiptError("receipt must not assert production deployment")

    created_at = _require_text(
        receipt["created_at"],
        re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00\Z"),
        "receipt creation time",
    )
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ReceiptError("receipt creation time is invalid") from error
    if parsed_created_at.tzinfo is None or parsed_created_at.astimezone(UTC).isoformat() != created_at:
        raise ReceiptError("receipt creation time is invalid")

    source = _require_mapping(receipt["source"], _SOURCE_FIELDS, "receipt source")
    if source["repository_url"] != CANONICAL_REPOSITORY_URL:
        raise ReceiptError("receipt source is invalid")
    revision = _require_text(source["revision"], _HEX_SHA, "source revision")
    if source["clean"] is not True:
        raise ReceiptError("receipt source is invalid")
    if source["clean_check"] != "git-status-porcelain-v1-with-untracked-files":
        raise ReceiptError("receipt source is invalid")

    migrations = receipt["migrations"]
    if not isinstance(migrations, list) or not migrations or len(migrations) > 256:
        raise ReceiptError("receipt migrations are invalid")
    migration_versions: list[int] = []
    for migration in migrations:
        migration = _require_mapping(migration, _MIGRATION_FIELDS, "receipt migration")
        filename = _require_text(migration["filename"], _MIGRATION, "migration filename")
        match = _MIGRATION.fullmatch(filename)
        assert match is not None
        migration_versions.append(int(match.group(1)))
        _require_text(migration["sha256"], re.compile(r"[0-9a-f]{64}\Z"), "migration hash")
        if migration["applied"] is not True:
            raise ReceiptError("receipt migrations are invalid")
    if migration_versions != sorted(set(migration_versions)):
        raise ReceiptError("receipt migrations are invalid")

    configuration = _require_mapping(
        receipt["configuration"], _CONFIGURATION_FIELDS, "receipt configuration"
    )
    if configuration["allowlist_version"] != "step8-v1":
        raise ReceiptError("receipt configuration is invalid")
    fields = configuration["fields"]
    if not isinstance(fields, dict) or set(fields) != SAFE_CONFIGURATION_FIELDS:
        raise ReceiptError("receipt configuration is invalid")
    for value in fields.values():
        _require_text(value, _SAFE_VALUE, "configuration value")
    fingerprint = _require_text(configuration["fingerprint"], _DIGEST, "configuration fingerprint")
    expected_fingerprint = "sha256:" + _sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    )
    if fingerprint != expected_fingerprint:
        raise ReceiptError("receipt configuration fingerprint is invalid")

    images = receipt["application_images"]
    if not isinstance(images, dict) or set(images) != {"collector", "python", "website"}:
        raise ReceiptError("receipt image identities are incomplete")
    image_fields = _DEPLOYED_IMAGE_FIELDS if scope == "deployed-stack" else _IMAGE_FIELDS
    for identity in images.values():
        identity = _require_mapping(identity, image_fields, "receipt image identity")
        if identity["identity_type"] != "image_id":
            raise ReceiptError("receipt image identity type is invalid")
        _require_text(identity["requested_reference"], _NAME, "receipt image reference")
        _require_text(identity["image_id"], _IMAGE_ID, "receipt image ID")
        digest = identity["registry_digest"]
        if digest is not None:
            _require_text(digest, _DIGEST, "receipt registry digest")
        if identity["source_label"] != CANONICAL_REPOSITORY_URL or identity["revision_label"] != revision:
            raise ReceiptError("receipt image labels do not match the source")
        if scope == "deployed-stack":
            _require_text(identity["container_name"], _IDENTITY, "receipt container name")

    database = _require_mapping(receipt["database"], _DATABASE_FIELDS, "receipt database")
    contract_version = database["contract_version"]
    if (
        isinstance(contract_version, bool)
        or not isinstance(contract_version, int)
        or contract_version != 5
    ):
        raise ReceiptError("receipt database contract is invalid")
    applied_versions = database["applied_migration_versions"]
    if (
        not isinstance(applied_versions, list)
        or len(applied_versions) > 256
        or any(
            isinstance(version, bool) or not isinstance(version, int)
            for version in applied_versions
        )
        or applied_versions != migration_versions
    ):
        raise ReceiptError("receipt database migration state is invalid")
    _require_text(database["server_version"], _POSTGRES_VERSION, "PostgreSQL version")
    _require_text(database["server_version_num"], _POSTGRES_VERSION_NUM, "PostgreSQL version number")
    _require_text(database["system_identifier"], _SYSTEM_IDENTIFIER, "PostgreSQL system identifier")
    _require_text(database["container_name"], _IDENTITY, "PostgreSQL container name")
    _require_text(database["database_name"], _IDENTITY, "PostgreSQL database name")
    expected_database_scope = (
        "disposable_validation_database"
        if scope == "candidate-preparation"
        else "deployed_stack_database"
    )
    if database["identity_scope"] != expected_database_scope:
        raise ReceiptError("receipt database scope is invalid")

    candidate_resources = receipt["candidate_resources"]
    if scope == "candidate-preparation":
        _validate_candidate_resources(candidate_resources, database["container_name"])
    elif candidate_resources is not None:
        raise ReceiptError("deployed receipt candidate resource proof is invalid")

    runtime = _require_mapping(receipt["runtime_versions"], _RUNTIME_FIELDS, "receipt runtime")
    _require_text(runtime["receipt_python"], _PYTHON_VERSION, "receipt Python version")
    _require_text(runtime["podman"], _PODMAN_VERSION, "receipt Podman version")
    _require_text(runtime["postgresql"], _POSTGRES_VERSION, "receipt PostgreSQL version")

    official_api_requests = receipt["official_api_requests"]
    if scope == "candidate-preparation":
        proof = _require_mapping(official_api_requests, _API_PROOF_FIELDS, "official API proof")
        count = proof["count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count != 0
            or proof["proof"] != CANDIDATE_RECEIPT_OFFICIAL_API_PROOF
        ):
            raise ReceiptError("candidate receipt official API proof is invalid")
    elif official_api_requests is not None:
        raise ReceiptError("deployed receipt official API proof is invalid")

    if require_digest:
        _require_text(receipt["receipt_digest"], _DIGEST, "receipt digest")
        if receipt["receipt_digest"] != _canonical_digest(receipt):
            raise ReceiptError("receipt digest is invalid")


def write_receipt(
    receipt: dict[str, Any],
    root: Path,
    results_directory: Path,
) -> Path:
    destination = _validate_results_directory(root, results_directory)
    validate_receipt(receipt, require_digest=True)
    created = datetime.fromisoformat(receipt["created_at"]).astimezone(UTC)
    timestamp = created.strftime("%Y%m%dT%H%M%S.%fZ")
    filename = (
        f"clashlens-{receipt['receipt_scope']}-{timestamp}-"
        f"{receipt['source']['revision'][:12]}.json"
    )
    final_path = destination / filename
    temporary_path = destination / f".{filename}.{os.getpid()}.tmp"
    payload = json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n"
    descriptor: int | None = None
    linked = False
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, final_path)
        linked = True
        temporary_path.unlink()
        directory_descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        written = json.loads(final_path.read_text(encoding="utf-8"))
        validate_receipt(written, require_digest=True)
        return final_path
    except FileExistsError as error:
        raise ReceiptError("receipt destination is already occupied") from error
    except (OSError, json.JSONDecodeError, ReceiptError) as error:
        if linked:
            final_path.unlink(missing_ok=True)
        if isinstance(error, ReceiptError):
            raise
        raise ReceiptError("receipt could not be written atomically") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--scope", choices=sorted(SCOPES), required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--podman-bin", default=os.environ.get("PODMAN_BIN", "podman"))
    parser.add_argument("--git-bin", default=os.environ.get("GIT_BIN", "git"))
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument(
        "--podman-network",
        "--candidate-network",
        "--network",
        dest="podman_network",
        required=True,
    )
    parser.add_argument(
        "--podman-volume",
        "--candidate-volume",
        "--volume",
        dest="podman_volume",
        required=True,
    )
    parser.add_argument("--postgres-user", required=True)
    parser.add_argument("--postgres-database", required=True)
    parser.add_argument("--collector-image", required=True)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--website-image", required=True)
    parser.add_argument("--collector-container", required=True)
    parser.add_argument("--python-container", required=True)
    parser.add_argument(
        "--worker-container", "--python-worker-container", dest="worker_container", required=True
    )
    parser.add_argument("--website-container", required=True)
    parser.add_argument("--safe-config", action="append", default=[])
    parser.add_argument("--created-at", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        receipt = collect_receipt(arguments)
        path = write_receipt(receipt, Path(arguments.root), arguments.results_dir)
    except ReceiptError as error:
        print(f"deployment receipt: {error}", file=sys.stderr)
        return 2
    print(path)
    print(receipt["receipt_digest"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
