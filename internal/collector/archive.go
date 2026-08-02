package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type s3Archive struct {
	client *minio.Client
	bucket string
}

var errArchiveChecksumMismatch = errors.New("archive checksum mismatch")

func newS3Archive(endpoint string, secure bool, bucket, accessKey, secretKey string) (*s3Archive, error) {
	if endpoint == "" || bucket == "" || accessKey == "" || secretKey == "" {
		return nil, errors.New("archive endpoint, bucket, access key, and secret key are required")
	}
	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: secure,
		Region: "us-east-1",
	})
	if err != nil {
		return nil, fmt.Errorf("create S3 client: %w", err)
	}
	return &s3Archive{client: client, bucket: bucket}, nil
}

func (a *s3Archive) ready(ctx context.Context) error {
	exists, err := a.client.BucketExists(ctx, a.bucket)
	if err != nil {
		return fmt.Errorf("check archive bucket readiness: %w", err)
	}
	if !exists {
		return fmt.Errorf("archive bucket %q does not exist", a.bucket)
	}
	return nil
}

func (a *s3Archive) store(ctx context.Context, hash string, body []byte) (string, error) {
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
	info, err := a.client.StatObject(ctx, a.bucket, objectKey, minio.StatObjectOptions{})
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

	_, err = a.client.PutObject(ctx, a.bucket, objectKey, bytes.NewReader(body), int64(len(body)), minio.PutObjectOptions{
		ContentType:          "application/octet-stream",
		DisableContentSha256: true,
		DisableMultipart:     true,
		UserMetadata: map[string]string{
			"sha256": hash,
		},
	})
	if err != nil {
		return "", fmt.Errorf("write archive object %s: %w", reference, err)
	}

	info, err = a.client.StatObject(ctx, a.bucket, objectKey, minio.StatObjectOptions{})
	if err != nil {
		return "", fmt.Errorf("verify archive object %s: %w", reference, err)
	}
	if info.Size != int64(len(body)) || info.Metadata.Get("X-Amz-Meta-Sha256") != hash {
		return "", fmt.Errorf(
			"%w: archive object %s failed post-write hash verification: size=%d hash=%q",
			errArchiveChecksumMismatch,
			reference,
			info.Size,
			info.Metadata.Get("X-Amz-Meta-Sha256"),
		)
	}
	if err := a.verifyObjectBytes(ctx, objectKey, reference, hash, int64(len(body))); err != nil {
		return "", err
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
	object, err := a.client.GetObject(ctx, a.bucket, objectKey, minio.GetObjectOptions{})
	if err != nil {
		return fmt.Errorf("read archive object %s for verification: %w", reference, err)
	}
	defer object.Close()
	body, err := io.ReadAll(io.LimitReader(object, expectedSize+1))
	if err != nil {
		return fmt.Errorf("read archive object %s for verification: %w", reference, err)
	}
	if int64(len(body)) != expectedSize {
		return fmt.Errorf("%w: archive object %s failed byte-size verification", errArchiveChecksumMismatch, reference)
	}
	digest := sha256.Sum256(body)
	if hex.EncodeToString(digest[:]) != expectedHash {
		return fmt.Errorf("%w: archive object %s failed byte-hash verification", errArchiveChecksumMismatch, reference)
	}
	return nil
}

const sha256HexLength = 64
