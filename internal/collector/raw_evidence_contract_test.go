package collector

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestRawEvidenceMigrationContainsDurableSafetyContract(t *testing.T) {
	body, err := os.ReadFile("../../deploy/migrations/0009_raw_evidence.sql")
	if err != nil {
		t.Fatal(err)
	}
	sql := string(body)
	for _, required := range []string{
		"archive_instances", "archive_catalogue", "archive_catalogue_hash",
		"pending_remote_verification", "NOT VALID", "FOREIGN KEY (archive_catalogue_hash, archive_reference)",
		"UPDATE clash_lens_contract SET version = 3", "waiting_dependency",
	} {
		if !strings.Contains(sql, required) {
			t.Errorf("migration omitted %q", required)
		}
	}
}

func TestPendingRemoteVerificationPayloadNeverContainsBodyOrCredentials(t *testing.T) {
	payload, err := json.Marshal(pendingRemoteVerification{
		ResponseHash:     "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		ArchiveReference: "s3://bucket/object", ByteSize: 12, ArchiveInstanceID: "instance",
		Endpoint: "profile", AttemptID: 1, RequestCount: 1, StatusCode: 200,
	})
	if err != nil {
		t.Fatal(err)
	}
	text := string(payload)
	for _, forbidden := range []string{"body", "authorization", "credentials", "secret"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("pending payload contains forbidden field %q: %s", forbidden, text)
		}
	}
}
