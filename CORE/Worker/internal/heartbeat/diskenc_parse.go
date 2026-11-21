// FX.123.s2 — the OS-independent half of full-disk-encryption detection:
// pure parsers over captured probe output. Every parser answers "unknown"
// when its input does not actually prove a verdict, so a truncated,
// localised, or garbage probe can never be read as "unencrypted" (which
// the API would act on) — only as "I could not tell".
package heartbeat

import (
	"encoding/json"
	"strings"
)

// lsblkHit is the walk accumulator: the deepest mount that contains the
// probe target, and whether a crypto node sat in that mount's ancestry.
type lsblkHit struct {
 match  int  // score of the best mount found (-1 = nothing matched)
 crypto bool // a crypt/LUKS node was seen on the path to that mount
}

// parseLSBLKJSON reads `lsblk -J -o NAME,TYPE,FSTYPE,MOUNTPOINTS` output and
// reports whether the filesystem holding target (the worker's data dir, or
// "/" as the fallback) sits on a dm-crypt/LUKS device.
//
// lsblk nests nodes under "children". A LUKS container is the PARENT of the
// filesystem's own node (sda3 type=crypt fstype=LUKS1 -> dm0 type=crypto
// fstype=ext4 mountpoints=["/"]), so the crypto marker is looked for in the
// mount node's ANCESTRY, not at the mount itself.
//
// Verdicts: crypto in the ancestry -> encrypted/luks; a mount that IS listed
// with no crypto ancestor -> unencrypted (method "": Linux names no foreign
// method); a mount that is not in the listing at all -> unknown, because
// "not listed" is a probe artefact (a container without /dev, a kernel that
// hides its own dm devices), not proof of a plain volume.
func parseLSBLKJSON(out string, target string) DiskEncryption {
 top := lsblkRoot(out)
 if len(top) == 0 {
  return diskUnknown()
 }
 hit := lsblkHit{match: -1}
 want := lsblkNorm(target)
 for _, n := range top {
  lsblkWalk(n, false, want, &hit)
 }
 if hit.match < 0 {
  return diskUnknown()
 }
 if hit.crypto {
  return diskEncVerdict("encrypted", "luks")
 }
 return diskEncVerdict("unencrypted", "")
}

// lsblkRoot decodes the probe payload. lsblk wraps the array in
// {"blockdevices": [...]}; a bare array is accepted too. Garbage, empty or
// truncated output yields no nodes so the verdict is unknown.
func lsblkRoot(out string) []interface{} {
 if strings.TrimSpace(out) == "" {
  return []interface{}{}
 }
 var v interface{}
 if err := json.Unmarshal([]byte(out), &v); err != nil {
  return []interface{}{}
 }
 if m, isMap := v.(map[string]interface{}); isMap {
  for _, key := range []string{"blockdevices", "lsblk", "devices"} {
   child, present := m[key]
   if present {
    if list, isList := child.([]interface{}); isList {
     return list
    }
   }
  }
  return []interface{}{}
 }
 if l, isList := v.([]interface{}); isList {
  return l
 }
 return []interface{}{}
}

// lsblkWalk visits one node. cryptoAbove is whether a crypto node has already
// been seen on the path above this node, which is what makes the LUKS
// container visible at the filesystem node it was unlocked for.
func lsblkWalk(node interface{}, cryptoAbove bool, want string, hit *lsblkHit) {
 m, isMap := node.(map[string]interface{})
 if !isMap {
  return
 }
 typ := strings.ToLower(lsblkText(m, "type"))
 fst := strings.ToLower(lsblkText(m, "fstype"))
 cryptoHere := typ == "crypt" || typ == "crypto" || strings.Contains(fst, "luks")
 here := cryptoAbove || cryptoHere
 for _, mp := range lsblkMounts(m) {
  score := lsblkScore(mp, want)
  if score > hit.match {
   hit.match, hit.crypto = score, here
  }
 }
 for _, child := range lsblkList(m["children"]) {
  lsblkWalk(child, here, want, hit)
 }
}

// lsblkText returns a string attribute, "" when absent or non-string.
func lsblkText(m map[string]interface{}, key string) string {
 v, ok := m[key]
 if !ok {
  return ""
 }
 if s, isStr := v.(string); isStr {
  return strings.TrimSpace(s)
 }
 return ""
}

// lsblkList returns a list value, empty for anything else.
func lsblkList(v interface{}) []interface{} {
 if l, isList := v.([]interface{}); isList {
  return l
 }
 return []interface{}{}
}

// lsblkMounts returns a node's mountpoints, which lsblk emits as a list
// (older builds emit a bare string).
func lsblkMounts(m map[string]interface{}) []string {
 v, ok := m["mountpoints"]
 if !ok {
  return []string{}
 }
 if s, isStr := v.(string); isStr {
  return []string{s}
 }
 out := []string{}
 for _, e := range lsblkList(v) {
  if s, isStr := e.(string); isStr {
   out = append(out, s)
  }
 }
 return out
}

// lsblkNorm normalises a path for prefix matching: trailing-slash-stripped,
// with "" (and a bare "/") both meaning the root mount.
func lsblkNorm(p string) string {
 p = strings.TrimSpace(p)
 if p == "" {
  return "/"
 }
 for len(p) > 1 && strings.HasSuffix(p, "/") {
  p = p[:len(p)-1]
 }
 return p
}

// lsblkScore scores one mount against the target: 0 when the mount IS the
// target, a positive depth when the target lives under it (deeper = a better,
// more specific filesystem), -1 when it does not contain it at all. Matching
// on mount-point containment rather than device names is what keeps the answer
// right for /dev/sda3-style targets and for overlay/tmpfs mounts.
func lsblkScore(mount string, want string) int {
 m := lsblkNorm(mount)
 if m == want {
  return 0
 }
 if m == "/" {
  return 1
 }
 if strings.HasPrefix(want, m+"/") {
  return 1 + len(m)
 }
 return -1
}

// parseBitlocker reads `manage-bde -status C:` (or the PowerShell
// Get-BitLockerVolume rendering). Only an explicit "Protection On" for the
// probed volume is encryption; "Protection Off" is unencrypted; anything
// else — a permission error, a localised string, a volume that is not the
// one probed, or the in-between states (Suspended / For Recovery) — is
// unknown, because none of those prove the volume is plain.
//
// Both renderings are accepted: manage-bde writes "Protection On:" /
// "Protection Off:", Get-BitLockerVolume writes "ProtectionStatus : On".
func parseBitlocker(out string, volume string) DiskEncryption {
 want := strings.ToUpper(strings.TrimSpace(volume))
 if want == "" {
  return diskUnknown()
 }
 sawVolume := false
 on, off := false, false
 for _, line := range strings.Split(strings.ToUpper(out), "\n") {
  if strings.Contains(line, want) {
   sawVolume = true
  }
  if strings.Contains(line, "PROTECTION ON") ||
   (strings.Contains(line, "PROTECTIONSTATUS") && strings.Contains(line, ": ON")) {
   on = true
  }
  if strings.Contains(line, "PROTECTION OFF") ||
   (strings.Contains(line, "PROTECTIONSTATUS") && strings.Contains(line, ": OFF")) {
   off = true
  }
 }
 if !sawVolume {
  return diskUnknown()
 }
 if on && !off {
  return diskEncVerdict("encrypted", "bitlocker")
 }
 if off && !on {
  return diskEncVerdict("unencrypted", "")
 }
 return diskUnknown()
}

// parseFileVault reads `fdesetup status`. Only the two documented answers
// count — matched WITH their terminating period, so fdesetup's hedged
// "FileVault is On, but locked to another user." (the volume is on but this
// user cannot act on it, and the answer came from a probe that could not
// actually read the state) is not a verdict. A permission error, an
// unlock-in-progress message, or garbage is unknown.
func parseFileVault(out string) DiskEncryption {
 low := strings.ToLower(out)
 if strings.Contains(low, "filevault is on.") {
  return diskEncVerdict("encrypted", "filevault")
 }
 if strings.Contains(low, "filevault is off.") {
  return diskEncVerdict("unencrypted", "")
 }
 return diskUnknown()
}
