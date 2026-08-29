from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from unittest import mock

from scripts import deployment_receipt as receipt

ROOT = Path(__file__).parents[1]
SOURCE_SHA = "01" * 20
IMAGE_ID = "sha256:" + "02" * 32


class FakeRunner:
    def __init__(
        self,
        *,
        dirty: bool = False,
        revision_label: str = SOURCE_SHA,
        applied: list[int] | None = None,
        image_id: str = IMAGE_ID,
        scope_label: str = receipt.CANDIDATE_SCOPE_LABEL_VALUE,
        present_containers: set[str] | None = None,
        inspected_names: dict[str, str] | None = None,
    ) -> None:
        self.dirty = dirty
        self.revision_label = revision_label
        self.applied = applied or list(range(1, 14))
        self.image_id = image_id
        self.scope_label = scope_label
        self.present_containers = present_containers or set()
        self.inspected_names = inspected_names or {}
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.commands.append(list(command))
        if command[0] == "git":
            if "status" in command:
                return " M deploy.sh" if self.dirty else ""
            return SOURCE_SHA
        if command[:2] == ["podman", "--version"]:
            return "podman version 5.8.4"
        if command[:3] == ["podman", "container", "exists"]:
            return "true" if command[3] in self.present_containers else "false"
        if command[:3] == ["podman", "image", "inspect"]:
            template = command[4]
            if template == "{{.Id}}":
                return self.image_id
            if template.startswith("{{if .RepoDigests}}"):
                return "<none>"
            if "image.source" in template:
                return receipt.CANONICAL_REPOSITORY_URL
            if "image.revision" in template:
                return self.revision_label
        if command[:3] == ["podman", "container", "inspect"]:
            template = command[4]
            name = command[5]
            if template == "{{.Name}}":
                return self.inspected_names.get(name, name)
            if "org.clashlens.scope" in template:
                return self.scope_label
            if template == "{{.State.Running}}":
                return "true"
            if template == "{{.ImageName}}":
                return "localhost/clashlens:deployment"
            if template == "{{.Image}}":
                return self.image_id
        if command[:3] in (["podman", "network", "inspect"], ["podman", "volume", "inspect"]):
            name = command[5]
            if command[4] == "{{.Name}}":
                return self.inspected_names.get(name, name)
            if "org.clashlens.scope" in command[4]:
                return self.scope_label
        if command[:2] == ["podman", "exec"]:
            return json.dumps(
                {
                    "contract_version": 5,
                    "applied_migration_versions": self.applied,
                    "server_version": "18.6",
                    "server_version_num": "180006",
                    "system_identifier": "1234567890",
                }
            )
        raise AssertionError(f"unexpected command: {command}")


def arguments(scope: str = "candidate-preparation") -> Namespace:
    value = Namespace(
        root=ROOT,
        scope=scope,
        environment="fedora-validation",
        results_dir=Path("/unused"),
        podman_bin="podman",
        git_bin="git",
        postgres_container="step8-postgres",
        podman_network="step8-private",
        podman_volume="step8-postgres-data",
        postgres_user="clashlens",
        postgres_database="clashlens",
        collector_image="localhost/clashlens-collector:deployment",
        python_image="localhost/clashlens-python:deployment",
        website_image="localhost/clashlens-website:deployment",
        collector_container="step8-collector",
        python_container="step8-python-api",
        worker_container="step8-python-worker",
        website_container="step8-website",
        safe_config=[
            f"{name}=1" for name in sorted(receipt.SAFE_CONFIGURATION_FIELDS)
        ],
        created_at="2026-08-28T20:00:00+00:00",
    )
    if scope == "deployed-stack":
        value.collector_container = "clashlens-collector"
        value.python_container = "clashlens-python-api"
        value.worker_container = "clashlens-python-worker"
        value.website_container = "clashlens-website"
    return value


class DeploymentReceiptTest(unittest.TestCase):
    def _unsigned_candidate(self) -> dict:
        value = deepcopy(receipt.collect_receipt(arguments(), FakeRunner()))
        value.pop("receipt_digest")
        return value

    def test_candidate_receipt_is_bounded_truthful_and_makes_no_api_call(self) -> None:
        runner = FakeRunner()

        result = receipt.collect_receipt(arguments(), runner)

        self.assertEqual(result["receipt_scope"], "candidate-preparation")
        self.assertEqual(result["schema_version"], receipt.SCHEMA_VERSION)
        self.assertEqual(result["production_deployment_status"], "not_asserted")
        self.assertEqual(result["official_api_requests"]["count"], 0)
        self.assertEqual(
            result["official_api_requests"]["proof"],
            receipt.CANDIDATE_RECEIPT_OFFICIAL_API_PROOF,
        )
        self.assertEqual(result["database"]["identity_scope"], "disposable_validation_database")
        resources = result["candidate_resources"]
        self.assertEqual(resources["postgres_container"]["name"], "step8-postgres")
        self.assertEqual(resources["network"]["name"], "step8-private")
        self.assertEqual(resources["volume"]["name"], "step8-postgres-data")
        for resource in ("postgres_container", "network", "volume"):
            self.assertEqual(
                resources[resource]["scope_label"], receipt.CANDIDATE_SCOPE_LABEL
            )
        self.assertTrue(
            all(
                not item["present"]
                for key, item in resources["application_containers"].items()
                if key != "worker_replicas"
            )
        )
        self.assertTrue(
            all(not item["present"] for item in resources["application_containers"]["worker_replicas"])
        )
        self.assertEqual(len(result["migrations"]), 13)
        self.assertTrue(all(item["applied"] for item in result["migrations"]))
        self.assertRegex(result["receipt_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertFalse(
            any(
                any(token in {"run", "start", "curl", "wget"} for token in command)
                for command in runner.commands
            )
        )

    def test_deployed_receipt_uses_actual_running_containers(self) -> None:
        result = receipt.collect_receipt(arguments("deployed-stack"), FakeRunner())

        self.assertIsNone(result["official_api_requests"])
        self.assertIsNone(result["candidate_resources"])
        self.assertEqual(result["database"]["identity_scope"], "deployed_stack_database")
        self.assertEqual(
            result["application_images"]["python"]["container_name"],
            "clashlens-python-api",
        )

    def test_bare_podman_image_id_is_normalized_without_changing_identity(self) -> None:
        bare = "02" * 32
        result = receipt.collect_receipt(arguments(), FakeRunner(image_id=bare))

        self.assertEqual(
            result["application_images"]["collector"]["image_id"],
            "sha256:" + bare,
        )

    def test_dirty_source_label_mismatch_and_migration_mismatch_fail(self) -> None:
        with self.assertRaisesRegex(receipt.ReceiptError, "dirty"):
            receipt.collect_receipt(arguments(), FakeRunner(dirty=True))
        with self.assertRaisesRegex(receipt.ReceiptError, "labels"):
            receipt.collect_receipt(
                arguments(), FakeRunner(revision_label="03" * 20)
            )
        with self.assertRaisesRegex(receipt.ReceiptError, "migration state"):
            receipt.collect_receipt(
                arguments(), FakeRunner(applied=list(range(1, 13)))
            )

    def test_candidate_scope_requires_matching_resource_labels_and_names(self) -> None:
        with self.assertRaisesRegex(receipt.ReceiptError, "scope label"):
            receipt.collect_receipt(arguments(), FakeRunner(scope_label="private"))

        wrong_name = arguments()
        wrong_name.podman_volume = "clashlens-candidate-postgres-data"
        with self.assertRaisesRegex(receipt.ReceiptError, "name does not match"):
            receipt.collect_receipt(
                wrong_name,
                FakeRunner(
                    inspected_names={
                        "clashlens-candidate-postgres-data": "other-volume"
                    }
                ),
            )

    def test_candidate_scope_rejects_default_or_ambiguous_names(self) -> None:
        for field, value in (("podman_network", "clashlens-private"),):
            candidate = arguments()
            setattr(candidate, field, value)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(receipt.ReceiptError, "dedicated"),
            ):
                receipt.collect_receipt(candidate, FakeRunner())

        production_named = arguments()
        production_named.postgres_container = "clashlens-production-postgres"
        result = receipt.collect_receipt(production_named, FakeRunner())
        self.assertEqual(
            result["candidate_resources"]["postgres_container"]["name"],
            "clashlens-production-postgres",
        )

        candidate = arguments()
        candidate.website_container = candidate.collector_container
        with self.assertRaisesRegex(receipt.ReceiptError, "ambiguous"):
            receipt.collect_receipt(candidate, FakeRunner())

    def test_candidate_scope_rejects_present_application_containers(self) -> None:
        with self.assertRaisesRegex(receipt.ReceiptError, "application container"):
            receipt.collect_receipt(
                arguments(), FakeRunner(present_containers={"step8-python-worker-1"})
            )

    def test_candidate_scope_rejects_worker_replica_name_collisions(self) -> None:
        candidate = arguments()
        candidate.collector_container = "step8-python-worker-1"
        with self.assertRaisesRegex(receipt.ReceiptError, "ambiguous"):
            receipt.collect_receipt(candidate, FakeRunner())

    def test_candidate_resource_proof_is_required_and_bounded(self) -> None:
        candidate = self._unsigned_candidate()
        candidate["candidate_resources"]["network"]["scope_label"] = "candidate"
        with self.assertRaisesRegex(receipt.ReceiptError, "scope label"):
            receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["candidate_resources"]["volume"]["name"] = "step8-private"
        with self.assertRaisesRegex(receipt.ReceiptError, "ambiguous"):
            receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["candidate_resources"]["application_containers"]["worker"]["present"] = True
        with self.assertRaisesRegex(receipt.ReceiptError, "absence"):
            receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["candidate_resources"]["application_containers"]["worker_replicas"][0]["name"] = "step8-python-worker-2"
        with self.assertRaisesRegex(receipt.ReceiptError, "replica"):
            receipt.validate_receipt(candidate)

    def test_only_complete_numeric_safe_configuration_is_accepted(self) -> None:
        incomplete = arguments()
        incomplete.safe_config.pop()
        with self.assertRaisesRegex(receipt.ReceiptError, "incomplete"):
            receipt.collect_receipt(incomplete, FakeRunner())

        sentinel = arguments()
        sentinel.safe_config[0] = "collector_database_pool_size=sentinel-secret"
        with self.assertRaisesRegex(receipt.ReceiptError, "configuration value"):
            receipt.collect_receipt(sentinel, FakeRunner())

    def test_write_is_digest_verified_exclusive_and_mode_0600(self) -> None:
        value = receipt.collect_receipt(arguments(), FakeRunner())
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            path = receipt.write_receipt(value, ROOT, destination)

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            written = json.loads(path.read_text(encoding="utf-8"))
            receipt.validate_receipt(written, require_digest=True)
            with self.assertRaisesRegex(receipt.ReceiptError, "occupied"):
                receipt.write_receipt(value, ROOT, destination)
            self.assertEqual(list(destination.iterdir()), [path])

    def test_in_checkout_and_symlink_destinations_are_rejected(self) -> None:
        value = receipt.collect_receipt(arguments(), FakeRunner())
        with self.assertRaisesRegex(receipt.ReceiptError, "outside"):
            receipt.write_receipt(value, ROOT, ROOT / "scripts")

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real = parent / "real"
            link = parent / "link"
            real.mkdir()
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(receipt.ReceiptError, "symlinks"):
                receipt.write_receipt(value, ROOT, link)

    def test_failed_publication_leaves_no_partial_file(self) -> None:
        value = receipt.collect_receipt(arguments(), FakeRunner())
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with (
                mock.patch.object(os, "link", side_effect=OSError("injected")),
                self.assertRaisesRegex(receipt.ReceiptError, "atomically"),
            ):
                receipt.write_receipt(value, ROOT, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_digest_tampering_is_rejected(self) -> None:
        value = receipt.collect_receipt(arguments(), FakeRunner())
        value["environment_identity"] = "other-environment"
        with self.assertRaisesRegex(receipt.ReceiptError, "digest"):
            receipt.validate_receipt(value, require_digest=True)

    def test_nested_schemas_reject_unknown_and_sensitive_fields(self) -> None:
        mutations = (
            ("source", "untrusted_detail", "secret-account"),
            ("runtime_versions", "environment", "database-password"),
            ("database", "database_url", "postgresql://user:password@host/db"),
        )
        for section, field, value in mutations:
            with self.subTest(section=section):
                candidate = self._unsigned_candidate()
                candidate[section][field] = value
                with self.assertRaises(receipt.ReceiptError):
                    receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["application_images"]["collector"]["player_tag"] = "#secret"
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(candidate)

    def test_nested_types_and_bounds_are_validated_for_both_scopes(self) -> None:
        candidate = self._unsigned_candidate()
        candidate["environment_identity"] = "fedora validation with secret"
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["database"]["system_identifier"] = "not-a-postgres-identity"
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(candidate)

        candidate = self._unsigned_candidate()
        candidate["runtime_versions"]["podman"] = "podman version secret-token"
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(candidate)

        deployed = deepcopy(receipt.collect_receipt(arguments("deployed-stack"), FakeRunner()))
        deployed.pop("receipt_digest")
        del deployed["application_images"]["python"]["container_name"]
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(deployed)

        deployed = deepcopy(receipt.collect_receipt(arguments("deployed-stack"), FakeRunner()))
        deployed.pop("receipt_digest")
        deployed["official_api_requests"] = {"count": 0, "proof": "not allowed"}
        with self.assertRaises(receipt.ReceiptError):
            receipt.validate_receipt(deployed)


if __name__ == "__main__":
    unittest.main()
