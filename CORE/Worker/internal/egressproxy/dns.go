package egressproxy

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/dns/dnsmessage"
)

const (
	// DefaultDNSAddr is the default DNS stub listen address.
	DefaultDNSAddr = ":53"
	// DNSLogInterval is the minimum gap between two dns_deny lines for the
	// same name.
	DNSLogInterval = 10 * time.Second
	// dnsMaxRateEntries bounds the per-name rate-limit table.
	dnsMaxRateEntries = 4096
	// dnsTCPIdle bounds how long a TCP client may stay idle between messages.
	dnsTCPIdle = 10 * time.Second
	// dnsMaxTCPConns bounds concurrent TCP clients.
	dnsMaxTCPConns = 64
	// dnsBindAttempts bounds retries when binding UDP+TCP to one ephemeral port.
	dnsBindAttempts = 16
	// dnsUDPBackoffStart/dnsUDPBackoffMax bound the sleep the UDP serve loop
	// takes between read errors: 5 ms doubling up to 1 s. Without a sleep a
	// persistent read error is a 100 % CPU busy loop (audit D-1).
	dnsUDPBackoffStart = 5 * time.Millisecond
	dnsUDPBackoffMax   = 1 * time.Second
	// dnsUDPFailLimit is how many consecutive UDP read errors the serve loop
	// takes before it gives up and ends its goroutine (the audit's number).
	dnsUDPFailLimit = 32
	// dnsOpCodeQuery is the standard QUERY opcode (RFC 1035).
	dnsOpCodeQuery dnsmessage.OpCode = 0
)

// DNSLogLine is the JSON shape written for every denied DNS query.
type DNSLogLine struct {
	TS     string `json:"ts"`
	Ev     string `json:"ev"`
	TaskID string `json:"task_id,omitempty"`
	Name   string `json:"name"`
	QType  string `json:"qtype,omitempty"`
	RCode  string `json:"rcode"`
	Proto  string `json:"proto"`
	// Fails is the consecutive UDP read-failure count on udp_give_up lines.
	Fails string `json:"fails,omitempty"`
}

// DNSStub is a UDP+TCP DNS server that answers every QUERY with NXDOMAIN and
// every other opcode with REFUSED. It never forwards and never resolves, so a
// task on an egress-restricted network cannot use DNS as an exfil channel or
// learn real addresses outside the proxy. Malformed packets and packets with
// the response bit set are dropped without a reply.
type DNSStub struct {
	// Log receives JSON lines; nil → discarded.
	Log io.Writer
	// TaskID is stamped on every log line when set.
	TaskID string

	udp   *net.UDPConn
	tcp   *net.TCPListener
	nowFn func() time.Time

	logMu   sync.Mutex
	rateMu  sync.Mutex
	lastLog map[string]time.Time

	connMu sync.Mutex
	conns  map[net.Conn]struct{}
	closed bool

	// udpDone tracks the UDP serve loop so Close can wait for it to exit;
	// udpErrMu/udpErrLast rate-limit the "udp read error" log lines.
	udpDone    sync.WaitGroup
	udpErrMu   sync.Mutex
	udpErrLast time.Time

	// udpDeadMu/udpDeadFlag: set when the UDP serve loop gives up (see serveUDP);
	// udpDead() is the health flag a supervisor can read.
	udpDeadMu   sync.Mutex
	udpDeadFlag bool

	// udpFailLimit overrides dnsUDPFailLimit for tests (0 -> the default).
	udpFailLimit int

	// udpCloseCount counts Close calls on the UDP socket; udpGiveUpCloseCount
	// counts the closes the serve loop made when it gave up. Both are audit
	// counters a supervisor or a test can read.
	udpCloseCount       int
	udpGiveUpCloseCount int
	udpCountMu          sync.Mutex

	// udpRead replaces the UDP read for tests (nil -> the live socket).
	udpRead func([]byte) (int, *net.UDPAddr, error)
}

// ListenDNSStub binds UDP and TCP on the same address ("" → DefaultDNSAddr).
// With port 0 both sockets share one ephemeral port. Log may be nil.
func ListenDNSStub(addr string, log io.Writer) (*DNSStub, error) {
	if addr == "" {
		addr = DefaultDNSAddr
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("dns stub: bad listen addr %q: %w", addr, err)
	}
	attempts := 1
	if port == "0" {
		attempts = dnsBindAttempts
	}
	var lastErr error
	for i := 0; i < attempts; i++ {
		u, err := net.ListenPacket("udp", addr)
		if err != nil {
			return nil, fmt.Errorf("dns stub: listen udp: %w", err)
		}
		uc := u.(*net.UDPConn)
		bound := net.JoinHostPort(host, strconv.Itoa(uc.LocalAddr().(*net.UDPAddr).Port))
		l, err := net.Listen("tcp", bound)
		if err != nil {
			_ = uc.Close()
			lastErr = err
			continue
		}
		return &DNSStub{
			Log:     log,
			udp:     uc,
			tcp:     l.(*net.TCPListener),
			nowFn:   time.Now,
			lastLog: make(map[string]time.Time),
			conns:   make(map[net.Conn]struct{}),
		}, nil
	}
	return nil, fmt.Errorf("dns stub: listen tcp: %w", lastErr)
}

// Addr returns the bound address (identical port for UDP and TCP).
func (d *DNSStub) Addr() string { return d.tcp.Addr().String() }

// udpDead is the health flag: true once the UDP serve loop gave up (its
// socket is closed and the listener is gone). A supervisor can poll it; the
// loop is never restarted silently.
func (d *DNSStub) udpDead() bool {
	d.udpDeadMu.Lock()
	defer d.udpDeadMu.Unlock()
	return d.udpDeadFlag
}

// udpCloses/udpGiveUpCloses expose the audit counters: how many times Close()
// closed the UDP socket, and how many times the serve loop closed it on give-up.
func (d *DNSStub) udpCloses() int {
	d.udpCountMu.Lock()
	defer d.udpCountMu.Unlock()
	return d.udpCloseCount
}

func (d *DNSStub) udpGiveUpCloses() int {
	d.udpCountMu.Lock()
	defer d.udpCountMu.Unlock()
	return d.udpGiveUpCloseCount
}

// Close stops both listeners and all open TCP connections.
func (d *DNSStub) Close() error {
	d.connMu.Lock()
	if d.closed {
		d.connMu.Unlock()
		return nil
	}
	d.closed = true
	for c := range d.conns {
		_ = c.Close()
	}
	d.connMu.Unlock()
	// The serve loop closes the UDP socket when it gives up, so Close() may
	// find it already closed: that is not an error, Close stays idempotent.
	udpErr := d.udp.Close()
	if errors.Is(udpErr, net.ErrClosed) {
		udpErr = nil
	}
	d.udpCountMu.Lock()
	d.udpCloseCount++
	d.udpCountMu.Unlock()
	err := errors.Join(udpErr, d.tcp.Close())
	// The UDP loop goroutine must be gone before Close returns: a closed UDP
	// socket makes its next read fail with net.ErrClosed, so this wait is
	// bounded (audit D-1: without it a dead loop keeps burning a core).
	d.udpDone.Wait()
	return err
}

// Serve answers queries until ctx is cancelled (or Close is called), then
// closes the listeners and waits for in-flight handlers to finish.
func (d *DNSStub) Serve(ctx context.Context) error {
	var wg sync.WaitGroup
	wg.Add(2)
	// Close() waits on udpDone: the UDP loop must exit, not spin.
	d.udpDone.Add(1)
	go func() { defer wg.Done(); d.serveUDP() }()
	go func() { defer wg.Done(); d.serveTCP(&wg) }()
	stop := context.AfterFunc(ctx, func() { _ = d.Close() })
	defer stop()
	wg.Wait()
	return nil
}

// serveUDP answers UDP queries until the socket is closed or the read loop
// gives up. A read error is NOT retried immediately: a closed socket ends the
// loop, a transient (timeout/temporary) net.Error backs off (5 ms doubling to
// 1 s, reset on the next good read), and any other persistent error is logged
// at most once per DNSLogInterval and ends the loop after dnsUDPFailLimit
// consecutive failures. Without the give-up a dead UDP socket spins at 100 %
// CPU forever (audit D-1).
func (d *DNSStub) serveUDP() {
	defer d.udpDone.Done()
	buf := make([]byte, 65535)
	fails := 0
	backoff := dnsUDPBackoffStart
	limit := d.udpFailLimit
	if limit <= 0 {
		limit = dnsUDPFailLimit
	}
	for {
		var n int
		var from *net.UDPAddr
		var err error
		if d.udpRead != nil {
			// Test seam: a fake read (see dns_test.go) stands in for the live
			// socket so persistent read errors can be injected.
			n, from, err = d.udpRead(buf)
		} else {
			n, from, err = d.udp.ReadFromUDP(buf)
		}
		if err != nil {
			// A closed socket is the normal shutdown path: end the loop.
			if errors.Is(err, net.ErrClosed) {
				return
			}
			fails++
			var transient bool
			var ne net.Error
			if errors.As(err, &ne) {
				transient = ne.Timeout() || ne.Temporary()
			}
			if !transient {
				// Persistent and not closed: log once per DNSLogInterval, then
				// give up after `limit` consecutive failures so the goroutine
				// ends instead of spinning at 100 % CPU.
				d.logUDPReadError(fails, err)
				if fails >= limit {
					// Give up: close the UDP socket so the port is released and
					// clients get ICMP/timeouts instead of a silent black hole,
					// surface the failure (one udp_give_up line with the
					// consecutive-failure count and the last error; udpDead()
					// is the health flag), then end the loop. The socket is
					// never restarted silently; Close() afterwards is a no-op on
					// an already-closed UDP socket (net.ErrClosed ignored).
					d.logUDPGiveUp(fails, err)
					deadErr := d.udp.Close()
					if errors.Is(deadErr, net.ErrClosed) {
						deadErr = nil
					}
					d.udpCountMu.Lock()
					d.udpGiveUpCloseCount++
					d.udpDeadMu.Lock()
					d.udpDeadFlag = true
					d.udpCountMu.Unlock()
					d.udpDeadMu.Unlock()
					if deadErr != nil {
						d.logUDPGiveUp(fails, deadErr)
					}
					return
				}
			}
			// Transient or not: always back off, never retry immediately.
			time.Sleep(backoff)
			backoff = nextUDPBackoff(backoff)
			continue
		}
		fails = 0
		backoff = dnsUDPBackoffStart
		if resp := d.answer(buf[:n], "udp"); resp != nil {
			_, _ = d.udp.WriteToUDP(resp, from)
		}
	}
}

// nextUDPBackoff doubles the sleep up to dnsUDPBackoffMax.
func nextUDPBackoff(cur time.Duration) time.Duration {
	if cur >= dnsUDPBackoffMax {
		return dnsUDPBackoffMax
	}
	next := cur * 2
	if next > dnsUDPBackoffMax {
		return dnsUDPBackoffMax
	}
	return next
}

// logUDPReadError writes one udp_read_error line per DNSLogInterval so a
// broken UDP socket cannot flood the log; the line carries the consecutive
// failure count (fails) and the last error (readErr) so the give-up is
// visible to an operator.
func (d *DNSStub) logUDPReadError(fails int, readErr error) {
	if d.Log == nil {
		return
	}
	now := d.nowFn()
	d.udpErrMu.Lock()
	defer d.udpErrMu.Unlock()
	if d.udpErrLast.Year() != 0 && now.Sub(d.udpErrLast) < DNSLogInterval {
		return
	}
	d.udpErrLast = now
	line := DNSLogLine{
		TS:     now.UTC().Format(time.RFC3339Nano),
		Ev:     "udp_read_error",
		TaskID: d.TaskID,
		Name:   readErr.Error(),
		Proto:  "udp",
		Fails:  strconv.Itoa(fails),
	}
	b, jerr := json.Marshal(line)
	if jerr != nil {
		return
	}
	b = append(b, '\n')
	_, _ = d.Log.Write(b)
}

// logUDPGiveUp writes ONE udp_give_up line (never rate-limited: it happens
// once per listener) carrying the consecutive-failure count and the last
// error, so an operator can see the UDP DNS listener died.
func (d *DNSStub) logUDPGiveUp(fails int, readErr error) {
	if d.Log == nil {
		return
	}
	line := DNSLogLine{
		TS:     d.nowFn().UTC().Format(time.RFC3339Nano),
		Ev:     "udp_give_up",
		TaskID: d.TaskID,
		Name:   readErr.Error(),
		Proto:  "udp",
		Fails:  strconv.Itoa(fails),
	}
	b, jerr := json.Marshal(line)
	if jerr != nil {
		return
	}
	b = append(b, '\n')
	_, _ = d.Log.Write(b)
}

func (d *DNSStub) serveTCP(wg *sync.WaitGroup) {
	sem := make(chan struct{}, dnsMaxTCPConns)
	for {
		c, err := d.tcp.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		select {
		case sem <- struct{}{}:
		default:
			_ = c.Close()
			continue
		}
		d.connMu.Lock()
		if d.closed {
			d.connMu.Unlock()
			_ = c.Close()
			<-sem
			return
		}
		d.conns[c] = struct{}{}
		d.connMu.Unlock()
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			d.handleTCP(c)
		}()
	}
}

// handleTCP serves length-prefixed messages (RFC 7766) until the client
// closes, idles out, or sends something malformed.
func (d *DNSStub) handleTCP(c net.Conn) {
	defer func() {
		d.connMu.Lock()
		delete(d.conns, c)
		d.connMu.Unlock()
		_ = c.Close()
	}()
	var hdr [2]byte
	for {
		_ = c.SetDeadline(time.Now().Add(dnsTCPIdle))
		if _, err := io.ReadFull(c, hdr[:]); err != nil {
			return
		}
		n := int(binary.BigEndian.Uint16(hdr[:]))
		if n == 0 {
			return
		}
		msg := make([]byte, n)
		if _, err := io.ReadFull(c, msg); err != nil {
			return
		}
		resp := d.answer(msg, "tcp")
		if resp == nil {
			return
		}
		out := make([]byte, 2+len(resp))
		binary.BigEndian.PutUint16(out, uint16(len(resp)))
		copy(out[2:], resp)
		if _, err := c.Write(out); err != nil {
			return
		}
	}
}

// answer builds the reply for one DNS message, or nil to drop it.
func (d *DNSStub) answer(pkt []byte, proto string) []byte {
	var p dnsmessage.Parser
	h, err := p.Start(pkt)
	if err != nil || h.Response {
		return nil
	}
	var qs []dnsmessage.Question
	for {
		q, err := p.Question()
		if errors.Is(err, dnsmessage.ErrSectionDone) {
			break
		}
		if err != nil {
			return nil
		}
		qs = append(qs, q)
	}
	rcode := dnsmessage.RCodeNameError
	if h.OpCode != dnsOpCodeQuery {
		rcode = dnsmessage.RCodeRefused
	}
	rh := dnsmessage.Header{
		ID:               h.ID,
		Response:         true,
		OpCode:           h.OpCode,
		RecursionDesired: h.RecursionDesired,
		RCode:            rcode,
	}
	b := dnsmessage.NewBuilder(make([]byte, 0, 512), rh)
	b.EnableCompression()
	if err := b.StartQuestions(); err != nil {
		return nil
	}
	for _, q := range qs {
		if err := b.Question(q); err != nil {
			return nil
		}
	}
	resp, err := b.Finish()
	if err != nil {
		return nil
	}
	name, qtype := "", ""
	if len(qs) > 0 {
		name = qs[0].Name.String()
		qtype = strings.TrimPrefix(qs[0].Type.String(), "Type")
	}
	d.logDeny(name, qtype, rcode, proto)
	return resp
}

// allowLog applies the per-name rate limit.
func (d *DNSStub) allowLog(key string, now time.Time) bool {
	d.rateMu.Lock()
	defer d.rateMu.Unlock()
	if last, ok := d.lastLog[key]; ok && now.Sub(last) < DNSLogInterval {
		return false
	}
	if len(d.lastLog) >= dnsMaxRateEntries {
		for k, t := range d.lastLog {
			if now.Sub(t) >= DNSLogInterval {
				delete(d.lastLog, k)
			}
		}
		if len(d.lastLog) >= dnsMaxRateEntries {
			d.lastLog = make(map[string]time.Time)
		}
	}
	d.lastLog[key] = now
	return true
}

func (d *DNSStub) logDeny(name, qtype string, rcode dnsmessage.RCode, proto string) {
	if d.Log == nil {
		return
	}
	now := d.nowFn()
	if !d.allowLog(strings.ToLower(name), now) {
		return
	}
	rc := "NXDOMAIN"
	if rcode == dnsmessage.RCodeRefused {
		rc = "REFUSED"
	}
	line := DNSLogLine{
		TS:     now.UTC().Format(time.RFC3339Nano),
		Ev:     "dns_deny",
		TaskID: d.TaskID,
		Name:   name,
		QType:  qtype,
		RCode:  rc,
		Proto:  proto,
	}
	b, err := json.Marshal(line)
	if err != nil {
		return
	}
	b = append(b, '\n')
	d.logMu.Lock()
	defer d.logMu.Unlock()
	_, _ = d.Log.Write(b)
}
