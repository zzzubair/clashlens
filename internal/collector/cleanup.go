package collector

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// cleanup is deliberately callback-driven: database eligibility remains in the
// store, while filesystem mutation and stripe/capacity ordering stay here.
func (s *evidenceSpool) cleanup(ctx context.Context, now time.Time, safetyAge time.Duration, batch int, eligible func(context.Context, string) (bool, error)) (int, error) {
	if batch < 1 || safetyAge <= 0 {
		return 0, os.ErrInvalid
	}
	count := 0
	prefixes, err := listSpoolRelative(s.cfg.root, "sha256")
	if err != nil {
		return 0, err
	}
	for _, prefix := range prefixes {
		if count >= batch || !prefix.IsDir() {
			break
		}
		entries, _ := listSpoolRelative(s.cfg.root, filepath.Join("sha256", prefix.Name()))
		for _, entry := range entries {
			if count >= batch || entry.IsDir() || len(entry.Name()) != sha256HexLength {
				continue
			}
			info, statErr := statSpoolRelative(s.cfg.root, filepath.Join("sha256", prefix.Name(), entry.Name()), false)
			if statErr != nil || now.Sub(info.ModTime()) < safetyAge {
				continue
			}
			hash := strings.ToLower(entry.Name())
			stripeIndex, stripeErr := s.stripeIndex(hash)
			if stripeErr != nil {
				return count, stripeErr
			}
			if lockErr := s.lockStripe(stripeIndex, true); lockErr != nil {
				return count, lockErr
			}
			if capErr := s.lockCapacity(); capErr != nil {
				s.unlockStripe(stripeIndex)
				return count, capErr
			}
			ok, checkErr := eligible(ctx, hash)
			if checkErr == nil && ok {
				// Substitution guard: a final replaced by a symlink must be
				// rejected outright rather than unlinked or followed.
				relative := filepath.Join("sha256", hash[:2], hash)
				if info, statErr := statSpoolRelative(s.cfg.root, relative, false); statErr != nil {
					s.unlockCapacity()
					s.unlockStripe(stripeIndex)
					return count, statErr
				} else if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
					s.unlockCapacity()
					s.unlockStripe(stripeIndex)
					return count, errors.New("spool final object was substituted")
				}
				if removeErr := unlinkSpoolRelative(s.cfg.root, relative); removeErr != nil && !os.IsNotExist(removeErr) {
					s.unlockCapacity()
					s.unlockStripe(stripeIndex)
					return count, removeErr
				}
				_ = s.syncDir(filepath.Dir(s.finalPath(hash)))
				count++
			}
			s.unlockCapacity()
			s.unlockStripe(stripeIndex)
			if checkErr != nil {
				return count, checkErr
			}
		}
	}
	if count > 0 {
		_ = s.reconcile()
	}
	return count, nil
}

func (s *evidenceSpool) orphanSweep(ctx context.Context, now time.Time, orphanAge time.Duration, batch int, catalogued func(context.Context, string) (bool, error), pending func(context.Context, string) (bool, error)) (int, error) {
	return s.cleanup(ctx, now, orphanAge, batch, func(ctx context.Context, hash string) (bool, error) {
		inCatalogue, err := catalogued(ctx, hash)
		if err != nil || inCatalogue {
			return false, err
		}
		livePending, err := pending(ctx, hash)
		if err != nil {
			return false, err
		}
		return !livePending, nil
	})
}
