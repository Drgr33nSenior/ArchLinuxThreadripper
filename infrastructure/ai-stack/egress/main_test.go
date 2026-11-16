package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/stripe/smokescreen/pkg/smokescreen"
	"github.com/stripe/smokescreen/pkg/smokescreen/conntrack"
	noop "github.com/stripe/smokescreen/pkg/smokescreen/metrics"
)

func TestParseListFormatsAndSuffixMatch(t *testing.T) {
	set, err := parseList(strings.NewReader(`# Title: fixture
adult.example
*.wild.example
0.0.0.0 hosts.example
0.0.0.0 0.0.0.0
127.0.0.1 localhost
||adblock.example^
UPPER.Example. # trailing comment
not a domain line at all
com
192.0.2.7
bad/../path.example
`))
	if err != nil {
		t.Fatal(err)
	}
	if len(set) != 5 {
		t.Fatalf("want 5 entries, got %d: %v", len(set), set)
	}
	for host, want := range map[string]bool{
		"adult.example": true, "www.adult.example": true, "a.b.adult.example": true,
		"notadult.example": false, "adult.example.evil.test": false, "example": false,
		"wild.example": true, "x.wild.example": true, "hosts.example": true, "adblock.example": true,
		"upper.example": true, "localhost": false, "com": false,
	} {
		if got := set.blocked(host); got != want {
			t.Errorf("blocked(%q) = %v, want %v", host, got, want)
		}
	}
}

func TestPolicyReasons(t *testing.T) {
	maintained := domains{"adult.example": {}}
	list := &source{Name: "fixture"}
	list.current.Store(&maintained)
	family := &policy{owner: domains{"owner.example": {}}, sources: []*source{list}, denyLiterals: true}
	parents := &policy{owner: domains{"owner.example": {}}}
	for host, want := range map[string]string{
		"WWW.Adult.Example:443": reasonFamily, "owner.example": reasonBlocklist, "sub.owner.example.": reasonBlocklist,
		"192.0.2.7:443": reasonLiteral, "[2001:db8::1]:443": reasonLiteral, "docs.python.org:443": "",
		"Sandbox-Egress.invalid:80": reasonStatus,
	} {
		if got := family.reason(host); got != want {
			t.Errorf("family reason(%q) = %q, want %q", host, got, want)
		}
	}
	for host, want := range map[string]string{"adult.example": "", "192.0.2.7": "", "owner.example": reasonBlocklist,
		statusHost: reasonStatus} {
		if got := parents.reason(host); got != want {
			t.Errorf("parents reason(%q) = %q, want %q", host, got, want)
		}
	}
}

func TestRefusalTextNeverCarriesAddresses(t *testing.T) {
	for _, c := range []struct {
		reason   string
		filtered bool
		want     string
	}{
		{reasonFamily, false, reasonFamily},
		{reasonBlocklist, false, reasonBlocklist},
		{"no valid IP found among resolved addresses - 0.0.0.0 denied by rule 'Deny: Not Global Unicast'", true, reasonFamily},
		{"no valid IP found among resolved addresses - 0.0.0.0 denied by rule 'Deny: Not Global Unicast'", false, reasonPrivate},
		{"no valid IP found among resolved addresses - 192.168.1.40 denied by rule 'Deny: User Configured'", true, reasonPrivate},
		{"no valid IP found among resolved addresses - 10.0.0.0 denied by rule 'Deny: Private Range'", true, reasonPrivate},
		{"something unexpected", true, reasonOther},
	} {
		if got := refusal(c.reason, c.filtered); got != c.want {
			t.Errorf("refusal(%q, %v) = %q, want %q", c.reason, c.filtered, got, c.want)
		}
	}
}

func TestPublicAddressesOnly(t *testing.T) {
	for address, want := range map[string]bool{"140.82.112.3": true, "192.168.1.40": false, "10.43.0.1": false,
		"127.0.0.1": false, "169.254.169.254": false, "0.0.0.0": false} {
		if got := public(net.ParseIP(address)); got != want {
			t.Errorf("public(%s) = %v, want %v", address, got, want)
		}
	}
}

func TestLogDomainIsNeverClientText(t *testing.T) {
	for value, want := range map[string]string{"Docs.Python.org:443": "docs.python.org", "a/b?c=d": "invalid",
		"x y": "invalid", "192.168.1.40:22": "192.168.1.40"} {
		if got := domain(value); got != want {
			t.Errorf("domain(%q) = %q, want %q", value, got, want)
		}
	}
}

func TestDenialsArePerClientBoundedAndExpire(t *testing.T) {
	clock := time.Unix(1_800_000_000, 0)
	d := newDenials()
	d.now = func() time.Time { return clock }
	d.add("10.42.0.7", "adult.example", reasonFamily)
	d.add("10.42.0.8", "other-child.example", reasonFamily)
	d.add("", "nobody.example", reasonFamily)
	clock = clock.Add(2 * time.Second)
	d.add("10.42.0.7", "owner.example", reasonBlocklist)
	got := d.recent("10.42.0.7", "")
	want := []denialEntry{{"owner.example", reasonBlocklist, 0}, {"adult.example", reasonFamily, 2}}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("own refusals = %v, want %v", got, want)
	}
	for _, e := range got {
		if e.Host == "other-child.example" {
			t.Fatal("a workspace was shown another workspace's refusal")
		}
	}
	if got := d.recent("10.42.0.9", ""); len(got) != 0 {
		t.Fatalf("a workspace with no refusals sees %v", got)
	}
	if got := d.recent("", ""); len(got) != 0 {
		t.Fatalf("an unknown client sees %v", got)
	}
	if got := d.recent("10.42.0.7", "adult.example"); len(got) != 1 || got[0].Host != "adult.example" {
		t.Fatalf("host filter = %v", got)
	}
	// A repeated host moves to the front instead of filling the list.
	d.add("10.42.0.7", "adult.example", reasonFamily)
	if got := d.recent("10.42.0.7", ""); len(got) != 2 || got[0].Host != "adult.example" {
		t.Fatalf("repeat = %v", got)
	}
	for i := 0; i < 2*denialsPerClient; i++ {
		d.add("10.42.0.7", strings.Repeat("a", i+1)+".example", reasonFamily)
	}
	if got := d.recent("10.42.0.7", ""); len(got) != denialsPerClient {
		t.Fatalf("kept %d entries, bound is %d", len(got), denialsPerClient)
	}
	clock = clock.Add(denialWindow)
	if got := d.recent("10.42.0.7", ""); len(got) != 0 {
		t.Fatalf("expired entries still shown: %v", got)
	}
	// The table of clients is bounded; expired clients go first, then the stalest.
	for i := 0; i < denialClients+10; i++ {
		clock = clock.Add(time.Millisecond)
		d.add(net.IPv4(10, 42, byte(i/250), byte(i%250+1)).String(), "x.example", reasonFamily)
	}
	if len(d.clients) != denialClients {
		t.Fatalf("%d clients held, bound is %d", len(d.clients), denialClients)
	}
	if got := d.recent("10.42.0.1", ""); len(got) != 0 {
		t.Fatalf("the stalest client was not evicted: %v", got)
	}
}

func TestStatusPageShowsOnlyTheCallersRefusals(t *testing.T) {
	recent := newDenials()
	handle := reject(true, recent)
	refuse := func(remote, host, reason string) *http.Response {
		request := httptest.NewRequest(http.MethodConnect, "http://"+host, nil)
		request.RemoteAddr = remote
		response := &http.Response{StatusCode: http.StatusProxyAuthRequired, Header: http.Header{}, Request: request}
		handle(&smokescreen.SmokescreenContext{Decision: &smokescreen.ACLDecision{Reason: reason},
			RequestedHost: host, ProxyType: "connect"}, response)
		return response
	}
	status := func(remote, target string) (int, map[string]interface{}) {
		request := httptest.NewRequest(http.MethodGet, target, nil)
		request.RemoteAddr = remote
		response := &http.Response{StatusCode: http.StatusProxyAuthRequired, Header: http.Header{}, Request: request}
		handle(&smokescreen.SmokescreenContext{Decision: &smokescreen.ACLDecision{Reason: reasonStatus},
			RequestedHost: statusHost, ProxyType: "http"}, response)
		body, _ := io.ReadAll(response.Body)
		var value map[string]interface{}
		_ = json.Unmarshal(body, &value)
		return response.StatusCode, value
	}
	// The refusal a client sees is unchanged: a 403, the reason header and the short body.
	response := refuse("10.42.0.7:40000", "adult.example:443", reasonFamily)
	body, _ := io.ReadAll(response.Body)
	if response.StatusCode != 403 || response.Header.Get("X-Sandbox-Blocked") != reasonFamily ||
		string(body) != "Blocked by the sandbox proxy: "+reasonFamily+".\n" {
		t.Fatalf("refusal changed: %d %v %q", response.StatusCode, response.Header, body)
	}
	refuse("10.42.0.8:40001", "secret-of-another-child.example:443",
		"no valid IP found among resolved addresses - 0.0.0.0 denied by rule 'Deny: Not Global Unicast'")
	code, page := status("10.42.0.7:40002", statusURL)
	text, _ := json.Marshal(page)
	if code != 200 || !strings.Contains(string(text), `"host":"adult.example"`) ||
		!strings.Contains(string(text), reasonFamily) || strings.Contains(string(text), "another-child") {
		t.Fatalf("own status page = %d %s", code, text)
	}
	code, page = status("10.42.0.8:40003", statusURL+"?host=https://Secret-Of-Another-Child.example/path")
	text, _ = json.Marshal(page)
	if code != 200 || !strings.Contains(string(text), `"host":"secret-of-another-child.example"`) ||
		!strings.Contains(string(text), `"reason":"`+reasonFamily+`"`) || strings.Contains(string(text), "adult.example") {
		t.Fatalf("second workspace's status page = %d %s", code, text)
	}
	if code, page = status("10.42.0.9:40004", statusURL); code != 200 || len(page["blocked"].([]interface{})) != 0 {
		t.Fatalf("a workspace with no refusals = %d %v", code, page)
	}
	if code, _ = status("10.42.0.7:40005", "http://"+statusHost+"/elsewhere"); code != 404 {
		t.Fatalf("other path = %d", code)
	}
	// Over CONNECT the page is refused (the proxy terminates no TLS) and not recorded.
	response = refuse("10.42.0.7:40006", statusHost+":443", reasonStatus)
	if response.StatusCode != 403 || response.Header.Get("X-Sandbox-Blocked") != reasonStatusTLS {
		t.Fatalf("status over CONNECT = %d %v", response.StatusCode, response.Header)
	}
	if got := recent.recent("10.42.0.7", ""); len(got) != 1 {
		t.Fatalf("status requests were recorded as refusals: %v", got)
	}
}

type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// send writes one raw request to the proxy from the given local address and returns
// the status, the reason header and the body.
func send(t *testing.T, proxy, from, request string) (int, string, string) {
	t.Helper()
	dialer := net.Dialer{Timeout: 5 * time.Second}
	if from != "" {
		dialer.LocalAddr = &net.TCPAddr{IP: net.ParseIP(from)}
	}
	connection, err := dialer.Dial("tcp4", proxy)
	if err != nil {
		t.Skipf("cannot connect from %s: %v", from, err)
	}
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := io.WriteString(connection, request); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(connection)
	response, err := http.ReadResponse(reader, &http.Request{Method: strings.Fields(request)[0]})
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, response.Header.Get("X-Sandbox-Blocked"), string(body)
}

// The whole path through Smokescreen: refusals keep their 403 and reason header, the
// status page lists them for the client that made them, and it is neither logged nor
// counted.
func TestStatusPageThroughSmokescreen(t *testing.T) {
	format := &decisions{group: "family"}
	output := &lockedBuffer{}
	logger := logrus.New()
	logger.SetOutput(output)
	logger.SetFormatter(format)
	conf := smokescreen.NewConfig()
	conf.Log = logger
	conf.Network = "ip4"
	conf.ConnTracker = conntrack.NewTracker(conf.IdleTimeout, noop.NewNoOpMetricsClient(), logger, atomic.Value{}, nil)
	conf.RoleFromRequest = func(*http.Request) (string, error) { return "family", nil }
	conf.EgressACL = &policy{owner: domains{"owner.example": {}}}
	conf.RejectResponseHandlerWithCtx = reject(false, newDenials())
	server := httptest.NewServer(smokescreen.BuildProxy(conf))
	defer server.Close()
	proxy := strings.TrimPrefix(server.URL, "http://")

	code, reason, body := send(t, proxy, "", "CONNECT owner.example:443 HTTP/1.1\r\nHost: owner.example:443\r\n\r\n")
	if code != 403 || reason != reasonBlocklist {
		t.Fatalf("CONNECT to a listed name = %d %q %q", code, reason, body)
	}
	code, reason, body = send(t, proxy, "", "GET http://owner.example/secret?token=x HTTP/1.1\r\nHost: owner.example\r\nConnection: close\r\n\r\n")
	if code != 403 || reason != reasonBlocklist || body != "Blocked by the sandbox proxy: "+reasonBlocklist+".\n" {
		t.Fatalf("plain request to a listed name = %d %q %q", code, reason, body)
	}
	// Refused after the name resolves (loopback): the reason comes from the address check.
	code, reason, _ = send(t, proxy, "", "CONNECT localhost:9 HTTP/1.1\r\nHost: localhost:9\r\n\r\n")
	if code != 403 || reason != reasonPrivate {
		t.Fatalf("CONNECT to loopback = %d %q", code, reason)
	}
	code, reason, body = send(t, proxy, "", "GET "+statusURL+" HTTP/1.1\r\nHost: "+statusHost+"\r\nConnection: close\r\n\r\n")
	var page struct {
		Blocked []denialEntry `json:"blocked"`
	}
	if err := json.Unmarshal([]byte(body), &page); err != nil || code != 200 || reason != "" {
		t.Fatalf("status page = %d %q %q", code, reason, body)
	}
	if len(page.Blocked) != 2 || page.Blocked[0].Host != "localhost" || page.Blocked[0].Reason != reasonPrivate ||
		page.Blocked[1].Host != "owner.example" || page.Blocked[1].Reason != reasonBlocklist {
		t.Fatalf("status page lists %+v", page.Blocked)
	}
	if strings.Contains(body, "secret") || strings.Contains(body, "token") {
		t.Fatalf("status page carries request paths: %s", body)
	}
	code, reason, _ = send(t, proxy, "", "CONNECT "+statusHost+":443 HTTP/1.1\r\nHost: "+statusHost+":443\r\n\r\n")
	if code != 403 || reason != reasonStatusTLS {
		t.Fatalf("status page over CONNECT = %d %q", code, reason)
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 3 || strings.Contains(output.String(), statusHost) {
		t.Fatalf("log = %q, want the three refusals and no status requests", lines)
	}
	if format.denied.Load() != 3 || format.allowed.Load() != 0 {
		t.Fatalf("counted %d denied, %d allowed", format.denied.Load(), format.allowed.Load())
	}
	// Another workspace (another source address) sees none of it. Linux routes all of
	// 127/8 to loopback; elsewhere this part is skipped.
	t.Run("another workspace", func(t *testing.T) {
		code, _, body := send(t, proxy, "127.0.0.2", "GET "+statusURL+" HTTP/1.1\r\nHost: "+statusHost+"\r\nConnection: close\r\n\r\n")
		if err := json.Unmarshal([]byte(body), &page); err != nil || code != 200 || len(page.Blocked) != 0 {
			t.Fatalf("another workspace's status page = %d %q", code, body)
		}
	})
}
