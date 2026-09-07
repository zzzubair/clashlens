package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
)

type evidenceCommit func(context.Context, string, string, int64) error

var errPendingRemoteVerification = errors.New("pending remote verification persisted")

type pendingEvidence func(context.Context, string, string, int64, officialResponse, string) error

type rawEvidenceStore interface {
	reserve(context.Context) (*spoolReservation, error)
	secureAndCommit(context.Context, *spoolReservation, []byte, evidenceCommit, pendingEvidence, ...officialResponse) error
	readLocal(string, int64) ([]byte, bool, error)
}

func (a *s3Archive) readLocal(hash string, size int64) ([]byte, bool, error) {
	if a.spool == nil {
		return nil, false, errors.New("raw-evidence spool is not configured")
	}
	return a.spool.read(hash, size)
}

func (a *s3Archive) reserve(ctx context.Context) (*spoolReservation, error) {
	if a.spool == nil {
		return nil, errors.New("raw-evidence spool is not configured")
	}
	return a.spool.reserve(a.maximumBodyBytes)
}

func (a *s3Archive) secureAndCommit(ctx context.Context, reservation *spoolReservation, body []byte, commit evidenceCommit, pending pendingEvidence, response ...officialResponse) error {
	if reservation == nil || a.spool == nil {
		return errors.New("raw-evidence reservation is required")
	}
	if int64(len(body)) > a.maximumBodyBytes {
		_ = reservation.release()
		return fmt.Errorf("%w: evidence body exceeds configured limit", errArchiveTerminal)
	}
	digest := sha256Hex(body)
	reference := "s3://" + a.bucket + "/sha256/" + digest[:2] + "/" + digest
	// The admission reservation covers only the in-flight body. Every exit
	// path releases it; promoted final bytes are accounted separately by the
	// spool write, and a leaked reservation would wedge future admission.
	defer reservation.release()
	stripeIndex, err := a.spool.stripeIndex(digest)
	if err != nil {
		_ = reservation.release()
		return err
	}
	if err := a.spool.lockStripe(stripeIndex, true); err != nil {
		_ = reservation.release()
		return err
	}
	defer a.spool.unlockStripe(stripeIndex)
	// Resolve under the shared spool stripe, also held by archive retirement.
	// A pre-lock cache hit could otherwise outlive deletion of its remote key.
	verified := false
	if a.catalogueLocation != nil {
		location, lookupErr := a.catalogueLocation(ctx, digest, int64(len(body)))
		if lookupErr != nil {
			return lookupErr
		}
		verified = location.verified
		if verified {
			reference = location.reference
		} else if len(response) > 0 && response[0].pendingArchiveReference != "" {
			reference = response[0].pendingArchiveReference
			if reference == location.reference {
				return fmt.Errorf("%w: pending archive location was retired", errArchiveTerminal)
			}
		} else if location.reference != "" {
			token, tokenErr := randomToken()
			if tokenErr != nil {
				return tokenErr
			}
			reference += "/generation/" + token
		}
	} else {
		verified, err = a.isCatalogueVerified(ctx, digest, int64(len(body)))
		if err != nil {
			return err
		}
	}
	localOK, err := a.spool.verify(digest, int64(len(body)), a.spool.locks[stripeIndex])
	if err != nil {
		_ = reservation.release()
		return err
	}
	if !localOK {
		localBody := body
		if verified {
			// A catalogue row proves remote bytes, not local bytes. Repair a
			// missing/corrupt local file with exactly one verified GET.
			localBody, err = a.readVerifiedObject(ctx, digest, reference, int64(len(body)))
			if err != nil {
				_ = reservation.release()
				return err
			}
		}
		if _, err := a.spool.write(reservation, bytesReader(localBody), a.spool.locks[stripeIndex]); err != nil {
			return err
		}
	} else {
		_ = reservation.release()
	}
	if !verified {
		if _, putErr := a.putVerifiedAt(ctx, digest, body, reference); putErr != nil {
			// reference stays the deterministic bucket path so the fenced
			// pending state can resume this exact evidence after recovery.
			if pending != nil && len(response) > 0 && isRetryableArchiveError(putErr) {
				if pendingErr := pending(ctx, digest, reference, int64(len(body)), response[0], ""); pendingErr == nil {
					return errors.Join(putErr, errPendingRemoteVerification)
				}
			}
			return putErr
		}
		a.verifiedHashes.Store(digest, struct{}{})
	}
	if commit == nil {
		return errors.New("raw-evidence commit callback is required")
	}
	if err := commit(ctx, reference, digest, int64(len(body))); err != nil {
		return fmt.Errorf("commit verified raw evidence: %w", err)
	}
	return nil
}

func (a *s3Archive) isCatalogueVerified(ctx context.Context, hash string, size int64) (bool, error) {
	if a.catalogueVerified != nil {
		return a.catalogueVerified(ctx, hash, size)
	}
	_, verified := a.verifiedHashes.Load(hash)
	return verified, nil
}

func isRetryableArchiveError(err error) bool {
	// Terminal provider failures (auth, permission, unsupported immutable
	// behavior, configuration) must never persist pending-remote-verification
	// state; only genuine transport/provider uncertainty may resume later.
	return err != nil && !errors.Is(err, errArchiveChecksumMismatch) && !errors.Is(err, errArchiveTerminal)
}

func sha256Hex(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}
func bytesReader(body []byte) io.Reader { return bytes.NewReader(body) }
