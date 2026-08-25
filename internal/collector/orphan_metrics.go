package collector

import (
	"context"
	"path/filepath"
)

func (s *evidenceSpool) orphanMetrics(ctx context.Context, catalogue func(context.Context, string) (bool, error), pending func(context.Context, string) (bool, error)) (int64, int64, error) {
	var count, bytes int64
	prefixes, err := listSpoolRelative(s.cfg.root, "sha256")
	if err != nil {
		return 0, 0, err
	}
	for _, prefix := range prefixes {
		if !prefix.IsDir() || len(prefix.Name()) != 2 {
			continue
		}
		entries, readErr := listSpoolRelative(s.cfg.root, filepath.Join("sha256", prefix.Name()))
		if readErr != nil {
			return count, bytes, readErr
		}
		for _, entry := range entries {
			if entry.IsDir() || len(entry.Name()) != sha256HexLength {
				continue
			}
			stripeIndex, stripeErr := s.stripeIndex(entry.Name())
			if stripeErr != nil {
				return count, bytes, stripeErr
			}
			if lockErr := s.lockStripe(stripeIndex, false); lockErr != nil {
				return count, bytes, lockErr
			}
			inCatalogue, catalogueErr := catalogue(ctx, entry.Name())
			inPending, pendingErr := pending(ctx, entry.Name())
			s.unlockStripe(stripeIndex)
			if catalogueErr != nil {
				return count, bytes, catalogueErr
			}
			if pendingErr != nil {
				return count, bytes, pendingErr
			}
			if !inCatalogue && !inPending {
				if info, statErr := entry.Info(); statErr == nil {
					count++
					bytes += info.Size()
				}
			}
		}
	}
	return count, bytes, nil
}
