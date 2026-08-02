package collector

import (
	"errors"
)

func officialPlayerPath(tag string) (string, error) {
	if len(tag) < 2 || tag[0] != '#' {
		return "", errors.New("player tag must start with #")
	}
	for _, r := range tag[1:] {
		if (r < 'A' || r > 'Z') && (r < '0' || r > '9') {
			return "", errors.New("player tag must contain only uppercase ASCII letters and digits")
		}
	}
	return "/v1/players/" + tag, nil
}
