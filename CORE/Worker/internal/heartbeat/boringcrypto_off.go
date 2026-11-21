//go:build !boringcrypto

// GY.C2b — default build: stock Go crypto, no BoringCrypto. The
// boringcrypto-tagged twin (boringcrypto_on.go) reports true when the
// binary was built against the BoringCrypto fork (the -tags=boringcrypto
// build used by x/crypto's BoringCrypto replace).
package heartbeat

func boringcryptoBuild() bool { return false }
