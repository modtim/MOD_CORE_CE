package egressproxy

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	goruntime "runtime"

	"golang.org/x/net/dns/dnsmessage"
)

func TestDNSStubUDPQuery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "test-task")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Send A query for example.com
	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 1234, RecursionDesired: true},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	_, err = conn.Write(data)
	if err != nil {
		t.Fatalf("write dns query: %v", err)
	}

	respBuf := make([]byte, 512)
	n, err := conn.Read(respBuf)
	if err != nil {
		t.Fatalf("read dns response: %v", err)
	}

	var respMsg dnsmessage.Message
	if err := respMsg.Unpack(respBuf[:n]); err != nil {
		t.Fatalf("unpack response: %v", err)
	}

	if respMsg.Header.RCode != dnsmessage.RCodeNameError {
		t.Errorf("rcode = %d, want %d (NXDOMAIN)", respMsg.Header.RCode, dnsmessage.RCodeNameError)
	}

	// Check log line
	logStr := logBuf.String()
	if !strings.Contains(logStr, `"ev":"dns_deny"`) {
		t.Errorf("log missing dns_deny event: %q", logStr)
	}
	if !strings.Contains(logStr, `"name":"example.com."`) {
		t.Errorf("log missing name: %q", logStr)
	}
	if !strings.Contains(logStr, `"task_id":"test-task"`) {
		t.Errorf("log missing task_id: %q", logStr)
	}
}

func TestDNSStubAAAAQuery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Send AAAA query
	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 5678, RecursionDesired: true},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("ipv6.example.com."), Type: dnsmessage.TypeAAAA, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	_, err = conn.Write(data)
	if err != nil {
		t.Fatalf("write dns query: %v", err)
	}

	respBuf := make([]byte, 512)
	n, err := conn.Read(respBuf)
	if err != nil {
		t.Fatalf("read dns response: %v", err)
	}

	var respMsg dnsmessage.Message
	if err := respMsg.Unpack(respBuf[:n]); err != nil {
		t.Fatalf("unpack response: %v", err)
	}

	if respMsg.Header.RCode != dnsmessage.RCodeNameError {
		t.Errorf("rcode = %d, want NXDOMAIN", respMsg.Header.RCode)
	}
}

func TestDNSStubTXTQuery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Send TXT query
	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 9012, RecursionDesired: true},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("txt.example.com."), Type: dnsmessage.TypeTXT, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	_, err = conn.Write(data)
	if err != nil {
		t.Fatalf("write dns query: %v", err)
	}

	respBuf := make([]byte, 512)
	n, err := conn.Read(respBuf)
	if err != nil {
		t.Fatalf("read dns response: %v", err)
	}

	var respMsg dnsmessage.Message
	if err := respMsg.Unpack(respBuf[:n]); err != nil {
		t.Fatalf("unpack response: %v", err)
	}

	if respMsg.Header.RCode != dnsmessage.RCodeNameError {
		t.Errorf("rcode = %d, want NXDOMAIN", respMsg.Header.RCode)
	}
}

func TestDNSStubTCPQuery(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("tcp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Send A query with length prefix
	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 3456, RecursionDesired: true},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("tcp.example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	// Write length prefix + message
	lenBuf := make([]byte, 2)
	binary.BigEndian.PutUint16(lenBuf, uint16(len(data)))

	buf := bytes.NewBuffer(lenBuf)
	buf.Write(data)

	_, err = conn.Write(buf.Bytes())
	if err != nil {
		t.Fatalf("write dns query: %v", err)
	}

	// Read response length
	lenResp := make([]byte, 2)
	_, err = conn.Read(lenResp)
	if err != nil {
		t.Fatalf("read response length: %v", err)
	}
	respLen := int(binary.BigEndian.Uint16(lenResp))

	respBuf := make([]byte, respLen)
	_, err = conn.Read(respBuf)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}

	var respMsg dnsmessage.Message
	if err := respMsg.Unpack(respBuf); err != nil {
		t.Fatalf("unpack response: %v", err)
	}

	if respMsg.Header.RCode != dnsmessage.RCodeNameError {
		t.Errorf("rcode = %d, want NXDOMAIN", respMsg.Header.RCode)
	}
}

func TestDNSStubRateLimiting(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	// Send multiple queries for the same name within 10 seconds
	for i := 0; i < 5; i++ {
		conn, err := net.Dial("udp", addr.String())
		if err != nil {
			t.Fatalf("dial dns: %v", err)
		}

		msg := dnsmessage.Message{
			Header: dnsmessage.Header{ID: uint16(1000 + i), RecursionDesired: true},
			Questions: []dnsmessage.Question{
				{Name: dnsmessage.MustNewName("rate.example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
			},
		}
		data, _ := msg.Pack()

		_, err = conn.Write(data)
		conn.Close()
		if err != nil {
			t.Fatalf("write dns query: %v", err)
		}
	}

	// Wait a bit for logging to complete
	time.Sleep(50 * time.Millisecond)

	logStr := logBuf.String()
	lines := strings.Split(strings.TrimSpace(logStr), "\n")
	if len(lines) != 1 {
		t.Errorf("log line count = %d, want 1 (rate limited)", len(lines))
		t.Logf("log: %q", logStr)
	}
}

func TestDNSStubNonQueryOpcode(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Create a message with IQUERY opcode (1)
	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 7890, OpCode: dnsmessage.OpCode(1)},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("opcode.example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	_, err = conn.Write(data)
	if err != nil {
		t.Fatalf("write dns query: %v", err)
	}

	respBuf := make([]byte, 512)
	n, err := conn.Read(respBuf)
	if err != nil {
		t.Fatalf("read dns response: %v", err)
	}

	var respMsg dnsmessage.Message
	if err := respMsg.Unpack(respBuf[:n]); err != nil {
		t.Fatalf("unpack response: %v", err)
	}

	if respMsg.Header.RCode != dnsmessage.RCodeRefused {
		t.Errorf("rcode = %d, want REFUSED", respMsg.Header.RCode)
	}
}

func TestDNSStubMalformedPacket(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Send garbage data (not a valid DNS packet)
	_, err = conn.Write([]byte{0x00, 0x01, 0x02, 0x03, 0x04})
	if err != nil {
		t.Fatalf("write garbage: %v", err)
	}

	// Should not receive a response for malformed packet
	respBuf := make([]byte, 512)
	_ = conn.SetDeadline(time.Now().Add(100 * time.Millisecond))
	n, err := conn.Read(respBuf)
	if err == nil && n > 0 {
		t.Errorf("got response to malformed packet, expected none")
	}
}

func TestDNSStubLogLineFormat(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "my-task-id")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	conn, err := net.Dial("udp", addr.String())
	if err != nil {
		t.Fatalf("dial dns: %v", err)
	}
	defer conn.Close()

	msg := dnsmessage.Message{
		Header: dnsmessage.Header{ID: 1111},
		Questions: []dnsmessage.Question{
			{Name: dnsmessage.MustNewName("myhost.test.local."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
		},
	}
	data, _ := msg.Pack()

	conn.Write(data)
	conn.Close()

	// Wait for log
	time.Sleep(50 * time.Millisecond)

	lines := strings.Split(strings.TrimSpace(logBuf.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("log line count = %d, want 1", len(lines))
	}

	var logLine DNSLogLine
	if err := json.Unmarshal([]byte(lines[0]), &logLine); err != nil {
		t.Fatalf("log line is not valid JSON: %v", err)
	}

	if logLine.Ev != "dns_deny" {
		t.Errorf("ev = %q, want dns_deny", logLine.Ev)
	}
	if logLine.Name != "myhost.test.local." {
		t.Errorf("name = %q, want myhost.test.local.", logLine.Name)
	}
	if logLine.TaskID != "my-task-id" {
		t.Errorf("task_id = %q, want my-task-id", logLine.TaskID)
	}
	if logLine.RCode != "NXDOMAIN" {
		t.Errorf("rcode = %q, want NXDOMAIN", logLine.RCode)
	}
	if logLine.Proto != "udp" {
		t.Errorf("proto = %q, want udp", logLine.Proto)
	}
}

// TestDNSStubMultipleNamesRateLimit verifies that different names are logged separately
func TestDNSStubMultipleNamesRateLimit(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	// Send queries for different names
	names := []string{"a.example.com.", "b.example.com.", "c.example.com."}
	for _, name := range names {
		conn, err := net.Dial("udp", addr.String())
		if err != nil {
			t.Fatalf("dial dns: %v", err)
		}

		msg := dnsmessage.Message{
			Header: dnsmessage.Header{ID: 2000},
			Questions: []dnsmessage.Question{
				{Name: dnsmessage.MustNewName(name), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
			},
		}
		data, _ := msg.Pack()

		conn.Write(data)
		conn.Close()
	}

	time.Sleep(50 * time.Millisecond)

	logStr := logBuf.String()
	lines := strings.Split(strings.TrimSpace(logStr), "\n")
	if len(lines) != 3 {
		t.Errorf("log line count = %d, want 3 (one per unique name)", len(lines))
		t.Logf("log: %q", logStr)
	}
}

// TestDNSStubRepeatedSameNameWithinRateLimit verifies that repeated queries
// for the same name within 10s only produce one log line
func TestDNSStubRepeatedSameNameWithinRateLimit(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	// Send 3 queries for the same name quickly
	for i := 0; i < 3; i++ {
		conn, _ := net.Dial("udp", addr.String())
		msg := dnsmessage.Message{
			Header: dnsmessage.Header{ID: uint16(3000 + i)},
			Questions: []dnsmessage.Question{
				{Name: dnsmessage.MustNewName("repeat.example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
			},
		}
		data, _ := msg.Pack()
		conn.Write(data)
		conn.Close()
	}

	time.Sleep(50 * time.Millisecond)

	logStr := logBuf.String()
	lines := strings.Split(strings.TrimSpace(logStr), "\n")
	if len(lines) != 1 {
		t.Errorf("log line count = %d, want 1 (rate limited)", len(lines))
		t.Logf("log: %q", logStr)
	}
}

// TestDNSStubConcurrentUDPQueries tests that concurrent UDP queries are handled correctly
func TestDNSStubConcurrentUDPQueries(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	dns := NewDNSStub("127.0.0.1:0", &logBuf, "")

	addr, err := dns.Start(ctx)
	if err != nil {
		t.Fatalf("start dns: %v", err)
	}

	done := make(chan bool, 10)
	for i := 0; i < 10; i++ {
		go func(id int) {
			conn, err := net.Dial("udp", addr.String())
			if err != nil {
				t.Errorf("dial dns: %v", err)
				done <- false
				return
			}
			defer conn.Close()

			msg := dnsmessage.Message{
				Header: dnsmessage.Header{ID: uint16(4000 + id)},
				Questions: []dnsmessage.Question{
					{Name: dnsmessage.MustNewName("concurrent.example.com."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
				},
			}
			data, _ := msg.Pack()
			conn.Write(data)

			respBuf := make([]byte, 512)
			n, err := conn.Read(respBuf)
			if err != nil {
				t.Errorf("read response: %v", err)
				done <- false
				return
			}

			var respMsg dnsmessage.Message
			if err := respMsg.Unpack(respBuf[:n]); err != nil {
				t.Errorf("unpack response: %v", err)
				done <- false
				return
			}

			if respMsg.Header.RCode != dnsmessage.RCodeNameError {
				t.Errorf("rcode = %d, want NXDOMAIN", respMsg.Header.RCode)
				done <- false
				return
			}
			done <- true
		}(i)
	}

	for i := 0; i < 10; i++ {
		<-done
	}

	// Should have 10 log lines (one per unique query)
	time.Sleep(100 * time.Millisecond)
	logStr := logBuf.String()
	lines := strings.Split(strings.TrimSpace(logStr), "\n")
	// Due to rate limiting on same name, we may have fewer
	if len(lines) == 0 {
		t.Errorf("no log lines produced")
	}
}

// testStub adapts ListenDNSStub/Serve to the tests' construct-then-Start shape.
type testStub struct {
	addr   string
	log    io.Writer
	taskID string
}

func NewDNSStub(addr string, log io.Writer, taskID string) *testStub {
	return &testStub{addr: addr, log: log, taskID: taskID}
}

func (s *testStub) Start(ctx context.Context) (net.Addr, error) {
	d, err := ListenDNSStub(s.addr, s.log)
	if err != nil {
		return nil, err
	}
	d.TaskID = s.taskID
	go func() { _ = d.Serve(ctx) }()
	return d.tcp.Addr(), nil
}

// ---- UDP read-error handling (audit D-1: dns.go serveUDP busy loop) ----

// udpReadCounter counts the fake UDP reads a test injected.
type udpReadCounter struct {
	mu sync.Mutex
	n  int
}

func (c *udpReadCounter) bump() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.n++
}

func (c *udpReadCounter) get() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.n
}

// fakeUDPTimeoutErr implements net.Error with Timeout()/Temporary()==true: the
// shape a transient read failure carries (same shape fence_test.go uses).
type fakeUDPTimeoutErr struct{}

func (fakeUDPTimeoutErr) Error() string   { return "read udp i/o timeout" }
func (fakeUDPTimeoutErr) Timeout() bool   { return true }
func (fakeUDPTimeoutErr) Temporary() bool { return true }

// TestDNSStubUDPLoopStopsOnClose: closing the stub must end the UDP serve
// loop goroutine promptly (Serve returns within 1 s) and the goroutine count
// must come back to baseline.
func TestDNSStubUDPLoopStopsOnClose(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	d, err := ListenDNSStub("127.0.0.1:0", &logBuf)
	if err != nil {
		t.Fatalf("listen dns: %v", err)
	}
	baseline := goruntime.NumGoroutine()

	done := make(chan bool, 1)
	go func() {
		_ = d.Serve(ctx)
		done <- true
	}()

	cancel()

	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Errorf("Serve did not return within 1 s of cancel: UDP loop spins")
		return
	}

	time.Sleep(50 * time.Millisecond)
	if now := goruntime.NumGoroutine(); now > baseline {
		t.Errorf("goroutines = %d, want <= %d after close", now, baseline)
	}
}

// TestDNSStubUDPReadGivesUp: a persistent (non-closed, non-transient) UDP read
// error must NOT spin. The injected read fails forever; the loop must back off
// and end after the configured consecutive-failure limit, so the read count
// stays bounded and Serve returns.
func TestDNSStubUDPReadGivesUp(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	d, err := ListenDNSStub("127.0.0.1:0", &logBuf)
	if err != nil {
		t.Fatalf("listen dns: %v", err)
	}

	var counter udpReadCounter
	limit := 5
	d.udpFailLimit = limit

	// Persistent failure: a plain error, so neither Timeout() nor Temporary().
	d.udpRead = func(buf []byte) (int, *net.UDPAddr, error) {
		counter.bump()
		return 0, nil, errors.New("persistent udp read failure")
	}

	done := make(chan bool, 1)
	go func() {
		_ = d.Serve(ctx)
		done <- true
	}()

	cancel()

	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Errorf("udp loop did not end within 1 s: busy loop, no give-up")
		return
	}

	if got := counter.get(); got != limit {
		t.Errorf("udp reads = %d, want %d (the consecutive-failure limit)", got, limit)
	}
	if lines := strings.Count(logBuf.String(), "udp_read_error"); lines < 1 {
		t.Errorf("persistent udp error was not logged")
	}
}

// TestDNSStubUDPTransientRecovers: a transient timeout read error backs off
// (does not end the loop) and the NEXT good packet is still served; the
// backoff resets after the successful read.
func TestDNSStubUDPTransientRecovers(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	d, err := ListenDNSStub("127.0.0.1:0", &logBuf)
	if err != nil {
		t.Fatalf("listen dns: %v", err)
	}

	var counter udpReadCounter
	fakeTimeout := &net.OpError{Op: "read", Net: "udp", Err: fakeUDPTimeoutErr{}}
	d.udpRead = func(buf []byte) (int, *net.UDPAddr, error) {
		counter.bump()
		if counter.get() == 1 {
			return 0, nil, fakeTimeout
		}
		if counter.get() == 2 {
			msg := dnsmessage.Message{
				Header: dnsmessage.Header{ID: 7777},
				Questions: []dnsmessage.Question{
					{Name: dnsmessage.MustNewName("backoff.test.local."), Type: dnsmessage.TypeA, Class: dnsmessage.ClassINET},
				},
			}
			data, _ := msg.Pack()
			copy(buf, data)
			return len(data), &net.UDPAddr{Port: 53}, nil
		}
		return 0, nil, net.ErrClosed
	}

	done := make(chan bool, 1)
	go func() {
		_ = d.Serve(ctx)
		done <- true
	}()

	cancel()

	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Errorf("udp loop did not end within 1 s")
		return
	}

	if got := counter.get(); got != 3 {
		t.Errorf("udp reads = %d, want 3 (timeout, good packet, closed)", got)
	}
	if lines := strings.Count(logBuf.String(), "dns_deny"); lines != 1 {
		t.Errorf("dns_deny lines = %d, want 1 (the packet after the transient error)", lines)
	}
	if strings.Count(logBuf.String(), "udp_read_error") != 0 {
		t.Errorf("a transient udp error must not log udp_read_error")
	}
}

// TestDNSStubUDPGiveUpClosesSocket: when the UDP serve loop gives up it must
// CLOSE the UDP socket exactly once (the port is released: clients get ICMP
// timeouts, not a silent black hole), set the udpDead() health flag, and log
// ONE udp_give_up line carrying the consecutive-failure count and the last
// error. The loop is never restarted silently.
func TestDNSStubUDPGiveUpClosesSocket(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	d, err := ListenDNSStub("127.0.0.1:0", &logBuf)
	if err != nil {
		t.Fatalf("listen dns: %v", err)
	}

	var counter udpReadCounter
	limit := 5
	d.udpFailLimit = limit
	lastErr := "persistent udp read failure"
	d.udpRead = func(buf []byte) (int, *net.UDPAddr, error) {
		counter.bump()
		return 0, nil, errors.New(lastErr)
	}

	done := make(chan bool, 1)
	go func() {
		_ = d.Serve(ctx)
		done <- true
	}()

	cancel()

	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Errorf("Serve did not return within 1 s: give-up did not end the loop")
		return
	}

	if got := counter.get(); got != limit {
		t.Errorf("udp reads = %d, want %d (the consecutive-failure limit)", got, limit)
	}
	if n := d.udpGiveUpCloses(); n != 1 {
		t.Errorf("give-up udp closes = %d, want exactly 1", n)
	}
	if !d.udpDead() {
		t.Errorf("udpDead() must be true after the serve loop gave up")
	}
	if strings.Count(logBuf.String(), "udp_give_up") != 1 {
		t.Errorf("udp_give_up lines = %d, want 1 (the give-up is surfaced once)", strings.Count(logBuf.String(), "udp_give_up"))
	}
	var parsed bool
	for _, l := range strings.Split(strings.TrimSpace(logBuf.String()), "\n") {
		var line DNSLogLine
		if json.Unmarshal([]byte(l), &line) != nil {
			continue
		}
		if line.Ev == "udp_give_up" {
			parsed = true
			if line.Fails != "5" {
				t.Errorf("udp_give_up fails = %q, want 5 (the consecutive-failure count)", line.Fails)
			}
			if line.Name != lastErr {
				t.Errorf("udp_give_up name = %q, want the last error %q", line.Name, lastErr)
			}
		}
	}
	if !parsed {
		t.Errorf("no udp_give_up line parsed")
	}
}

// TestDNSStubUDPGiveUpCloseIsIdempotent: after the serve loop closed the UDP
// socket on give-up, Close() must not panic and must not report the already-
// closed UDP socket as an error; repeated Close() calls stay no-ops.
func TestDNSStubUDPGiveUpCloseIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var logBuf syncBuf
	d, err := ListenDNSStub("127.0.0.1:0", &logBuf)
	if err != nil {
		t.Fatalf("listen dns: %v", err)
	}

	d.udpFailLimit = 3
	d.udpRead = func(buf []byte) (int, *net.UDPAddr, error) {
		return 0, nil, errors.New("persistent udp read failure")
	}

	done := make(chan bool, 1)
	go func() {
		_ = d.Serve(ctx)
		done <- true
	}()

	cancel()

	select {
	case <-done:
	case <-time.After(1 * time.Second):
		t.Errorf("Serve did not return within 1 s: give-up did not end the loop")
		return
	}

	if n := d.udpGiveUpCloses(); n != 1 {
		t.Errorf("give-up udp closes = %d, want 1", n)
	}

	first := d.Close()
	if first != nil {
		t.Errorf("Close() after give-up = %v, want no error (already-closed UDP is not an error)", first)
	}
	second := d.Close()
	if second != nil {
		t.Errorf("second Close() = %v, want no error (Close is idempotent)", second)
	}
	if n := d.udpCloses(); n != 1 {
		t.Errorf("Close-driven udp closes = %d, want 1 (the second Close is a no-op)", n)
	}
}
