package collector

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
)

var errArchiveCatalogueContradiction = errors.New("archive catalogue contradiction")

func (s *store) verifiedCatalogue(ctx context.Context, hash string, size int64) (bool, error) {
	if s.archiveInstanceID == "" {
		return false, nil
	}
	var found bool
	err := s.pool.QueryRow(ctx, `
        SELECT EXISTS (SELECT 1 FROM archive_catalogue
                       WHERE response_hash = $1 AND byte_size = $2
                         AND archive_instance_id = $3)
    `, hash, size, s.archiveInstanceID).Scan(&found)
	if err != nil {
		return false, fmt.Errorf("read verified archive catalogue: %w", err)
	}
	return found, nil
}

func (s *store) insertCatalogue(ctx context.Context, tx pgx.Tx, hash, reference string, size int64) error {
	if s.archiveInstanceID == "" {
		return errors.New("archive instance ID is required for contract v3")
	}
	_, err := tx.Exec(ctx, `
        INSERT INTO archive_catalogue(response_hash, archive_reference, byte_size, archive_instance_id)
        VALUES ($1,$2,$3,$4) ON CONFLICT (response_hash) DO NOTHING
    `, hash, reference, size, s.archiveInstanceID)
	return err
}

func (s *store) validateArchiveInstance(ctx context.Context, endpoint, region, bucket, markerKey, markerHash, markerPayloadVersion string) error {
	if s.contractVersion < 3 {
		return nil
	}
	if s.archiveInstanceID == "" || endpoint == "" || region == "" || bucket == "" || markerKey == "" || markerHash == "" || markerPayloadVersion == "" {
		return errors.New("complete archive instance configuration is required")
	}
	var matches bool
	if err := s.pool.QueryRow(ctx, `
        SELECT EXISTS (SELECT 1 FROM archive_instances
          WHERE instance_id=$1 AND endpoint=$2 AND region=$3 AND bucket=$4
            AND marker_key=$5 AND marker_hash=$6 AND marker_payload_version=$7)
    `, s.archiveInstanceID, endpoint, region, bucket, markerKey, markerHash, markerPayloadVersion).Scan(&matches); err != nil {
		return fmt.Errorf("validate archive instance contract: %w", err)
	}
	if !matches {
		return errors.New("archive instance configuration contradicts durable contract")
	}
	return nil
}
