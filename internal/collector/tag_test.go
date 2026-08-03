package collector

import "testing"

func TestOfficialPlayerPathAcceptsDefensivelySafeNormalizedTag(t *testing.T) {
	t.Parallel()

	path, err := officialPlayerPath("#2PP")
	if err != nil {
		t.Fatalf("officialPlayerPath returned an error: %v", err)
	}
	if path != "/v1/players/#2PP" {
		t.Fatalf("officialPlayerPath = %q, want %q", path, "/v1/players/#2PP")
	}
}

func TestOfficialPlayerPathRejectsUnsafeOrNonNormalizedTag(t *testing.T) {
	t.Parallel()

	for _, tag := range []string{"", "2PP", "#2pp", "#2/PP", "#2 PP", "#2%23PP"} {
		t.Run(tag, func(t *testing.T) {
			t.Parallel()
			if _, err := officialPlayerPath(tag); err == nil {
				t.Fatalf("officialPlayerPath(%q) returned no error", tag)
			}
		})
	}
}
