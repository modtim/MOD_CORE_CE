package main

import (
	"errors"
	"net/http"
	"time"
)

// newSelfUpdateHTTPClient builds the client used for the self-update binary
// download and its .sig fetch (both carry X-Bootstrap-Token).
func newSelfUpdateHTTPClient() *http.Client {
	return &http.Client{Timeout: 5 * time.Minute, CheckRedirect: refuseCrossHostRedirect}
}

// refuseCrossHostRedirect is the CheckRedirect policy for the self-update
// download client. Go strips only Authorization/Cookie on a cross-host
// redirect, so the custom X-Bootstrap-Token header would otherwise be
// forwarded to whatever host the redirect names. Refuse any redirect that
// leaves the original host (and stop after 10 redirects like the default).
func refuseCrossHostRedirect(req *http.Request, via []*http.Request) error {
	if len(via) >= 10 {
		return errors.New("stopped after 10 redirects")
	}
	if len(via) > 0 && req.URL.Host != via[0].URL.Host {
		return errors.New("self-update: refusing cross-host redirect")
	}
	return nil
}
