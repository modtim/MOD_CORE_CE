package nativeapps

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"os"
)

// ParseBundleShortVersion extracts CFBundleShortVersionString from raw
// Info.plist XML bytes. Pure parsing — no OS calls — so it's fully
// unit-testable on any platform, including where mdfind/Spotlight don't
// exist (Linux CI).
func ParseBundleShortVersion(data []byte) (string, error) {
	dec := xml.NewDecoder(bytes.NewReader(data))
	wantValue := false
	for {
		tok, err := dec.Token()
		if err != nil {
			break
		}
		start, ok := tok.(xml.StartElement)
		if !ok {
			continue
		}
		switch start.Name.Local {
		case "key":
			var key string
			if err := dec.DecodeElement(&key, &start); err == nil {
				wantValue = key == "CFBundleShortVersionString"
			} else {
				wantValue = false
			}
		case "string":
			if wantValue {
				var val string
				if err := dec.DecodeElement(&val, &start); err == nil {
					return val, nil
				}
			}
			wantValue = false
		default:
			wantValue = false
		}
	}
	return "", fmt.Errorf("nativeapps: CFBundleShortVersionString not found in plist")
}

// ReadBundleShortVersion reads <bundlePath>/Contents/Info.plist and returns
// its CFBundleShortVersionString.
func ReadBundleShortVersion(bundlePath string) (string, error) {
	data, err := os.ReadFile(bundlePath + "/Contents/Info.plist")
	if err != nil {
		return "", err
	}
	return ParseBundleShortVersion(data)
}
