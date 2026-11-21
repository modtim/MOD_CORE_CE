package auth

import (
	"encoding/base64"
	"encoding/json"

	"github.com/docker/docker/api/types/registry"
)

// EncodeRegistryAuth creates the base64-encoded auth header for Docker image pulls.
// Docker SDK requires this format for ImagePull operations.
// The result carries the plaintext password (base64url is encoding, not
// encryption): it is the Docker SDK RegistryAuth format, so never log it.
// It returns the marshal error instead of discarding it (same shape as
// EncodeRegistryToken); callers must not send an empty header on error.
func EncodeRegistryAuth(username, password string) (string, error) {
	authConfig := registry.AuthConfig{
		Username: username,
		Password: password,
	}
	jsonAuth, err := json.Marshal(authConfig)
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(jsonAuth), nil
}

// EncodeRegistryToken encodes a registry bearer token as a base64url-encoded
// JSON auth config (the same encoding EncodeRegistryAuth uses, but populated
// with the RegistryToken field so a token string can be pulled verbatim).
func EncodeRegistryToken(token string) (string, error) {
	authConfig := registry.AuthConfig{
		RegistryToken: token,
	}
	jsonAuth, err := json.Marshal(authConfig)
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(jsonAuth), nil
}
