package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"syscall"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type s3Archive struct {
	client            *minio.Client
	bucket            string
	region            string
	writeVerified     bool
	spool             *evidenceSpool
	maximumBodyBytes  int64
	catalogueVerified func(context.Context, string, int64) (bool, error)
	verifiedHashes    sync.Map
	markerMu          sync.Mutex
	markerCheckedAt   time.Time
	markerErr         error
	observeStage      func(string, time.Duration)
}

var (
	errArchiveChecksumMismatch = errors.New("archive checksum mismatch")
	errArchiveTerminal         = errors.New("archive terminal configuration or permission failure")
)

func newS3Archive(endpoint string, secure bool, bucket, accessKey, secretKey string) (*s3Archive, error) {
	return newS3ArchiveWithRegion(endpoint, "us-east-1", secure, bucket, accessKey, secretKey)
}

func newS3ArchiveWithRegion(endpoint, region string, secure bool, bucket, accessKey, secretKey string) (*s3Archive, error) {
	if endpoint == "" || region == "" || bucket == "" || accessKey == "" || secretKey == "" {
		return nil, errors.New("archive endpoint, region, bucket, access key, and secret key are required")
	}
	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: secure,
		Region: region,
	})
	if err != nil {
		return nil, fmt.Errorf("create S3 client: %w", err)
	}
	return &s3Archive{client: client, bucket: bucket, region: region, maximumBodyBytes: 64 << 20}, nil
}

func (a *s3Archive) spoolReady() error {
	if a.spool == nil {
		return nil
	}
	metrics, err := a.spool.metrics()
	if err != nil {
		return fmt.Errorf("spool metrics: %w", err)
	}
	if metrics.finalBytes+metrics.temporaryBytes+metrics.reservedBytes+a.maximumBodyBytes > a.spool.cfg.maxBytes || metrics.finalObjects+metrics.temporaryObjects+metrics.reservedObjects+1 > a.spool.cfg.maxObjects {
		return errSpoolCapacity
	}
	if metrics.freeInodes < a.spool.cfg.freeInodeFloor+1 {
		return errSpoolFreeInodeFloor
	}
	var stat struct {
		available uint64
	}
	// metrics already reports inode pressure; Statfs is kept in the spool
	// metrics seam for the byte floor so remote health remains independent.
	if a.spool.cfg.freeSpaceFloor > 0 {
		var fs syscall.Statfs_t
		if statErr := syscall.Statfs(a.spool.cfg.root, &fs); statErr != nil {
			return fmt.Errorf("spool free-space metrics: %w", statErr)
		}
		stat.available = fs.Bavail * uint64(fs.Bsize)
		if stat.available < a.spool.cfg.freeSpaceFloor+uint64(a.maximumBodyBytes) {
			return errSpoolFreeSpaceFloor
		}
	}
	return nil
}

func (a *s3Archive) ready(ctx context.Context) error {
	if a.spool != nil {
		return nil
	}
	// Contract-v3 production always has a spool. Keep the pre-v3 test seam's
	// bucket check for old fixtures; it is unreachable after migration 0009.
	if !a.writeVerified {
		return errors.New("archive write readiness was not verified")
	}
	exists, err := a.client.BucketExists(ctx, a.bucket)
	if err != nil {
		return fmt.Errorf("check archive bucket readiness: %w", err)
	}
	if !exists {
		return fmt.Errorf("archive bucket %q does not exist", a.bucket)
	}
	return nil
}

func (a *s3Archive) verifyWriteCapability(ctx context.Context, probeID string) error {
	if probeID == "" {
		return errors.New("archive write readiness probe ID is required")
	}
	emptyDigest := sha256.Sum256(nil)
	emptyHash := hex.EncodeToString(emptyDigest[:])
	objectKey := "readiness/" + probeID
	putOptions := minio.PutObjectOptions{
		ContentType:          "application/octet-stream",
		SendContentMd5:       true,
		DisableContentSha256: true,
		DisableMultipart:     true,
		UserMetadata: map[string]string{
			"sha256": emptyHash,
		},
	}
	putOptions.SetMatchETagExcept("*")
	_, err := a.client.PutObject(ctx, a.bucket, objectKey, bytes.NewReader(nil), 0, putOptions)
	if err != nil {
		return fmt.Errorf("archive write readiness: %w", err)
	}
	_, err = a.client.PutObject(ctx, a.bucket, objectKey, bytes.NewReader(nil), 0, putOptions)
	response := minio.ToErrorResponse(err)
	if err == nil || (response.Code != "PreconditionFailed" && response.StatusCode != http.StatusPreconditionFailed) {
		return errors.New("archive write readiness did not preserve conditional immutable creation")
	}
	info, err := a.client.StatObject(ctx, a.bucket, objectKey, minio.StatObjectOptions{})
	if err != nil {
		return fmt.Errorf("archive write readiness verification: %w", err)
	}
	if info.Size != 0 || info.Metadata.Get("X-Amz-Meta-Sha256") != emptyHash {
		return errors.New("archive write readiness verification returned unexpected object metadata")
	}
	a.writeVerified = true
	return nil
}

// putVerified is the immutable write path. It deliberately never uses HEAD as
// evidence: a successful conditional PUT is followed by a bounded GET and
// SHA-256 verification.
func (a *s3Archive) putVerified(ctx context.Context, hash string, body []byte) (string, error) {
	if int64(len(body)) > a.maximumBodyBytes {
		return "", fmt.Errorf("%w: archive body exceeds single-part limit", errArchiveTerminal)
	}
	if len(hash) != sha256HexLength {
		return "", errors.New("archive hash must be a SHA-256 hex digest")
	}
	if _, err := hex.DecodeString(hash); err != nil {
		return "", errors.New("archive hash must be a SHA-256 hex digest")
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != hash {
		return "", fmt.Errorf("%w: supplied body does not match its hash", errArchiveChecksumMismatch)
	}
	objectKey := "sha256/" + hash[:2] + "/" + hash
	reference := "s3://" + a.bucket + "/" + objectKey
	options := minio.PutObjectOptions{ContentType: "application/octet-stream", SendContentMd5: true, DisableContentSha256: true, DisableMultipart: true, UserMetadata: map[string]string{"sha256": hash}}
	options.SetMatchETagExcept("*")
	putStartedAt := time.Now()
	_, err := a.client.PutObject(ctx, a.bucket, objectKey, bytes.NewReader(body), int64(len(body)), options)
	a.recordStage("archive_put", putStartedAt)
	if err != nil {
		if archiveErrorIsTerminal(err) {
			return "", fmt.Errorf("%w: immutable archive PUT rejected: %v", errArchiveTerminal, err)
		}
		// A timeout/5xx leaves the PUT outcome unknown. GET is the only
		// acceptable reconciliation proof; a conflict follows the same path.
		if verifyErr := a.verifyObjectBytes(ctx, objectKey, reference, hash, int64(len(body))); verifyErr != nil {
			response := minio.ToErrorResponse(err)
			if response.Code != "PreconditionFailed" && response.Code != "Conflict" && response.StatusCode != http.StatusPreconditionFailed && response.StatusCode != http.StatusConflict {
				return "", fmt.Errorf("write archive object %s: %w", reference, err)
			}
			return "", verifyErr
		}
		return reference, nil
	}
	if err := a.verifyObjectBytes(ctx, objectKey, reference, hash, int64(len(body))); err != nil {
		return "", err
	}
	return reference, nil
}

func (a *s3Archive) store(ctx context.Context, hash string, body []byte) (string, error) {
	if a.spool != nil {
		return a.putVerified(ctx, hash, body)
	}
	if len(hash) != sha256HexLength {
		return "", errors.New("archive hash must be a SHA-256 hex digest")
	}
	if _, err := hex.DecodeString(hash); err != nil {
		return "", errors.New("archive hash must be a SHA-256 hex digest")
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != hash {
		return "", fmt.Errorf("%w: supplied body does not match its hash", errArchiveChecksumMismatch)
	}

	objectKey := "sha256/" + hash[:2] + "/" + hash
	reference := "s3://" + a.bucket + "/" + objectKey
	startedAt := time.Now()
	info, err := a.client.StatObject(ctx, a.bucket, objectKey, minio.StatObjectOptions{})
	a.recordStage("archive_head", startedAt)
	if err == nil {
		if info.Size != int64(len(body)) || info.Metadata.Get("X-Amz-Meta-Sha256") != hash {
			return "", fmt.Errorf("%w: archive object %s failed immutable hash verification", errArchiveChecksumMismatch, reference)
		}
		if err := a.verifyObjectBytes(ctx, objectKey, reference, hash, int64(len(body))); err != nil {
			return "", err
		}
		return reference, nil
	}
	response := minio.ToErrorResponse(err)
	if response.Code != "NoSuchKey" && response.Code != "NoSuchObject" && response.StatusCode != 404 {
		return "", fmt.Errorf("inspect archive object %s: %w", reference, err)
	}

	putOptions := minio.PutObjectOptions{
		ContentType:          "application/octet-stream",
		SendContentMd5:       true,
		DisableContentSha256: true,
		DisableMultipart:     true,
		UserMetadata: map[string]string{
			"sha256": hash,
		},
	}
	putOptions.SetMatchETagExcept("*")
	startedAt = time.Now()
	_, err = a.client.PutObject(ctx, a.bucket, objectKey, bytes.NewReader(body), int64(len(body)), putOptions)
	a.recordStage("archive_put", startedAt)
	if err != nil {
		response := minio.ToErrorResponse(err)
		if response.Code == "PreconditionFailed" || response.StatusCode == http.StatusPreconditionFailed {
			startedAt = time.Now()
			info, statErr := a.client.StatObject(ctx, a.bucket, objectKey, minio.StatObjectOptions{})
			a.recordStage("archive_head", startedAt)
			if statErr != nil {
				return "", fmt.Errorf("inspect concurrently-created archive object %s: %w", reference, statErr)
			}
			if info.Size != int64(len(body)) || info.Metadata.Get("X-Amz-Meta-Sha256") != hash {
				return "", fmt.Errorf("%w: concurrently-created archive object %s failed immutable hash verification", errArchiveChecksumMismatch, reference)
			}
			if verifyErr := a.verifyObjectBytes(ctx, objectKey, reference, hash, int64(len(body))); verifyErr != nil {
				return "", verifyErr
			}
			return reference, nil
		}
		return "", fmt.Errorf("write archive object %s: %w", reference, err)
	}
	return reference, nil
}

func (a *s3Archive) verifyObjectBytes(
	ctx context.Context,
	objectKey string,
	reference string,
	expectedHash string,
	expectedSize int64,
) error {
	_, err := a.readObjectBytes(ctx, objectKey, reference, expectedHash, expectedSize)
	return err
}

func (a *s3Archive) readVerifiedObject(ctx context.Context, hash, reference string, size int64) ([]byte, error) {
	return a.readObjectBytes(ctx, "sha256/"+hash[:2]+"/"+hash, reference, hash, size)
}

func (a *s3Archive) readObjectBytes(ctx context.Context, objectKey, reference, expectedHash string, expectedSize int64) ([]byte, error) {
	startedAt := time.Now()
	object, err := a.client.GetObject(ctx, a.bucket, objectKey, minio.GetObjectOptions{})
	if err != nil {
		if archiveErrorIsTerminal(err) {
			return nil, fmt.Errorf("%w: read archive object %s: %v", errArchiveTerminal, reference, err)
		}
		return nil, fmt.Errorf("read archive object %s for verification: %w", reference, err)
	}
	defer object.Close()
	body, err := io.ReadAll(io.LimitReader(object, expectedSize+1))
	a.recordStage("archive_get_verify", startedAt)
	if err != nil {
		return nil, fmt.Errorf("read archive object %s for verification: %w", reference, err)
	}
	if int64(len(body)) != expectedSize {
		return nil, fmt.Errorf("%w: archive object %s failed byte-size verification", errArchiveChecksumMismatch, reference)
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != expectedHash {
		return nil, fmt.Errorf("%w: archive object %s failed byte-hash verification", errArchiveChecksumMismatch, reference)
	}
	return body, nil
}

func (a *s3Archive) recordStage(stage string, startedAt time.Time) {
	if a.observeStage != nil {
		a.observeStage(stage, time.Since(startedAt))
	}
}

func (a *s3Archive) markerHealth(ctx context.Context, markerKey, expectedHash string) error {
	a.markerMu.Lock()
	defer a.markerMu.Unlock()
	if !a.markerCheckedAt.IsZero() && time.Since(a.markerCheckedAt) < 5*time.Minute {
		return a.markerErr
	}
	a.markerCheckedAt = time.Now()
	object, err := a.client.GetObject(ctx, a.bucket, markerKey, minio.GetObjectOptions{})
	if err == nil {
		defer object.Close()
		body, readErr := io.ReadAll(io.LimitReader(object, 1<<20))
		if readErr != nil {
			err = readErr
		} else {
			digest := sha256.Sum256(body)
			if hex.EncodeToString(digest[:]) != expectedHash {
				err = errors.New("archive marker hash mismatch")
			}
		}
	}
	if err != nil {
		a.markerErr = fmt.Errorf("archive marker health: %w", err)
	} else {
		a.markerErr = nil
	}
	return a.markerErr
}

func archiveErrorIsTerminal(err error) bool {
	response := minio.ToErrorResponse(err)
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return true
	}
	if response.StatusCode == http.StatusBadRequest || response.StatusCode == http.StatusMethodNotAllowed || response.StatusCode == http.StatusNotImplemented {
		return true
	}
	switch response.Code {
	case "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidRequest", "NotImplemented", "MethodNotAllowed", "NoSuchBucket", "InvalidBucketName", "AuthorizationHeaderMalformed", "PermanentRedirect", "InvalidRegion":
		return true
	default:
		return false
	}
}

const sha256HexLength = 64
