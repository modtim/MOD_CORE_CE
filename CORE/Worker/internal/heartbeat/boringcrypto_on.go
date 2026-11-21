//go:build boringcrypto

// GY.C2b — this file is only compiled when the binary was built with
// -tags=boringcrypto (BoringCrypto fork of the crypto libraries), so the
// heartbeat's crypto report can say so; the stock build compiles
// boringcrypto_off.go instead.
package heartbeat

func boringcryptoBuild() bool { return true }
