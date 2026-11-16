// The egress proxy for assistant coding workspaces: Stripe Smokescreen used as a
// library, one fixed group per process. A workspace may reach any public host except
// the ones a blocklist names. Smokescreen still refuses private, loopback, link-local
// and CGNAT addresses after it resolves the name, so no name can lead to the LAN or
// the cluster.
//
// This file adds what the stock binary lacks:
//   - a group the client cannot choose (stock takes it from a client certificate);
//   - a decision by blocklist: the owner's short list, and for the family group
//     maintained public lists baked into the image and refreshed while running;
//   - optional filtering resolvers, so the family group's names are resolved by a
//     family DNS service and a filtered answer is a refusal;
//   - refusals as a plain 403 with a short reason;
//   - a status page, http://sandbox-egress.invalid/blocked, where a workspace reads
//     its own recent refusals: Chromium shows only ERR_TUNNEL_CONNECTION_FAILED for a
//     refused HTTPS tunnel, never the reason;
//   - a log that carries nothing but the group, the domain and the decision.
//
// Built inside Smokescreen's own module (cmd/sandbox-egress) from its vendored
// dependencies.
package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/stripe/smokescreen/pkg/smokescreen"
	acl "github.com/stripe/smokescreen/pkg/smokescreen/acl/v1"
)

const (
	reasonBlocklist = "blocked by the sandbox blocklist"
	reasonFamily    = "blocked by the family filter"
	reasonLiteral   = "address literals are not allowed for this group"
	reasonPrivate   = "private address"
	reasonOther     = "refused by the sandbox proxy"
	// The proxy's own status page. A .invalid name never resolves (RFC 6761), so it
	// shadows no real site. The proxy answers it itself: it is never dialled, logged
	// or counted.
	statusHost = "sandbox-egress.invalid"
	statusPath = "/blocked"
	statusURL  = "http://" + statusHost + statusPath
	// The decision that routes a request to the status page; never shown to a client.
	reasonStatus    = "sandbox proxy status page"
	reasonStatusTLS = "the proxy status page is plain HTTP: " + statusURL
	// What a workspace can read back about its own refusals: the newest few, briefly.
	denialWindow     = 5 * time.Minute
	denialsPerClient = 20
	denialClients    = 256
	// A list larger than this is not a list this proxy should hold in memory.
	maxListBytes = 32 << 20
)

var hostname = regexp.MustCompile(`^[a-z0-9_]([a-z0-9_.-]{0,251}[a-z0-9])?$`)

type domains map[string]struct{}

// blocked reports whether host or any parent domain of it is in the set.
func (d domains) blocked(host string) bool {
	for {
		if _, ok := d[host]; ok {
			return true
		}
		dot := strings.IndexByte(host, '.')
		if dot < 0 {
			return false
		}
		host = host[dot+1:]
	}
}

// parseList reads a domain list: one name per line, or hosts-file lines
// ("0.0.0.0 name"), with optional "*." or adblock "||name^" decoration. Anything that is
// not a domain with at least one dot is skipped, so a damaged file yields a short
// list, which the caller's size check then rejects.
func parseList(r io.Reader) (domains, error) {
	set := domains{}
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 4096), 4096)
	for scanner.Scan() {
		line := scanner.Text()
		if i := strings.IndexAny(line, "#!"); i >= 0 {
			line = line[:i]
		}
		fields := strings.Fields(line)
		if len(fields) == 0 || len(fields) > 2 {
			continue
		}
		name := strings.ToLower(fields[len(fields)-1])
		name = strings.TrimSuffix(strings.TrimPrefix(strings.TrimPrefix(name, "||"), "*."), "^")
		name = strings.TrimSuffix(name, ".")
		if !strings.Contains(name, ".") || !hostname.MatchString(name) || net.ParseIP(name) != nil {
			continue
		}
		set[name] = struct{}{}
	}
	return set, scanner.Err()
}

func loadList(path string) (domains, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return parseList(io.LimitReader(file, maxListBytes))
}

// source is one maintained list: a copy baked into the image at a pinned revision,
// and the address its maintainer updates, read again while the proxy runs.
type source struct {
	Name       string `json:"name"`
	File       string `json:"file"`
	RefreshURL string `json:"refresh_url"`
	Entries    int    `json:"entries"`

	current   atomic.Pointer[domains]
	refreshed atomic.Int64
	failures  atomic.Uint64
}

func loadSources(dir string) ([]*source, error) {
	raw, err := os.ReadFile(filepath.Join(dir, "sources.json"))
	if err != nil {
		return nil, err
	}
	var sources []*source
	if err := json.Unmarshal(raw, &sources); err != nil {
		return nil, err
	}
	for _, s := range sources {
		target, err := url.Parse(s.RefreshURL)
		if err != nil || target.Scheme != "https" || target.Host == "" || target.User != nil || filepath.Base(s.File) != s.File {
			return nil, fmt.Errorf("list %q needs a plain file name and an https refresh address", s.Name)
		}
		set, err := loadList(filepath.Join(dir, s.File))
		if err != nil {
			return nil, err
		}
		if s.Entries < 1 || len(set) != s.Entries {
			return nil, fmt.Errorf("list %q holds %d entries, its record says %d", s.Name, len(set), s.Entries)
		}
		s.current.Store(&set)
	}
	return sources, nil
}

// refresh replaces a list with its maintainer's current copy. The new copy must be
// reachable at a public address, within the size bound, and between half and four
// times the baked size; otherwise the list in use stays.
func (s *source) refresh(client *http.Client) error {
	response, err := client.Get(s.RefreshURL)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d", response.StatusCode)
	}
	body := io.LimitReader(response.Body, maxListBytes+1)
	counted := &countingReader{reader: body}
	set, err := parseList(counted)
	if err != nil {
		return err
	}
	if counted.n > maxListBytes {
		return errors.New("list exceeds the size bound")
	}
	if len(set) < s.Entries/2 || len(set) > s.Entries*4 {
		return fmt.Errorf("%d entries is outside the expected range", len(set))
	}
	s.current.Store(&set)
	s.refreshed.Store(time.Now().Unix())
	return nil
}

type countingReader struct {
	reader io.Reader
	n      int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.reader.Read(p)
	c.n += int64(n)
	return n, err
}

func public(ip net.IP) bool {
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback()
}

// refreshClient fetches lists directly (never through the proxy itself), resolving
// with the proxy's resolver, dialling only public addresses and following no redirect.
func refreshClient(resolver *net.Resolver) *http.Client {
	dialer := &net.Dialer{Timeout: 15 * time.Second}
	transport := &http.Transport{
		Proxy:               nil,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout: 15 * time.Second,
		DialContext: func(ctx context.Context, _, address string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(address)
			if err != nil || port != "443" {
				return nil, errors.New("list refresh is https only")
			}
			ips, err := resolver.LookupIP(ctx, "ip4", host)
			if err != nil {
				return nil, err
			}
			for _, ip := range ips {
				if public(ip) {
					return dialer.DialContext(ctx, "tcp4", net.JoinHostPort(ip.String(), port))
				}
			}
			return nil, errors.New("list host has no public address")
		},
	}
	return &http.Client{Transport: transport, Timeout: 3 * time.Minute,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

// policy decides one request by name. Addresses are Smokescreen's job, after this.
type policy struct {
	owner        domains
	sources      []*source
	denyLiterals bool
}

func normalize(host string) string {
	if name, _, err := net.SplitHostPort(host); err == nil {
		host = name
	}
	return strings.ToLower(strings.TrimSuffix(strings.Trim(host, "[]"), "."))
}

func (p *policy) reason(host string) string {
	host = normalize(host)
	if host == statusHost {
		return reasonStatus
	}
	if net.ParseIP(host) != nil {
		if p.denyLiterals {
			return reasonLiteral
		}
		return ""
	}
	if p.owner.blocked(host) {
		return reasonBlocklist
	}
	for _, s := range p.sources {
		if s.current.Load().blocked(host) {
			return reasonFamily
		}
	}
	return ""
}

func (p *policy) Decide(args acl.DecideArgs) (acl.Decision, error) {
	if reason := p.reason(args.Host); reason != "" {
		return acl.Decision{Reason: reason, Result: acl.Deny, Project: "sandbox"}, nil
	}
	return acl.Decision{Reason: "not on a blocklist", Result: acl.Allow, Project: "sandbox"}, nil
}

// refusal turns Smokescreen's reason into the short one the client is shown. A
// filtering resolver answers 0.0.0.0 for a name it blocks; Smokescreen refuses that
// address like any other that is not public.
func refusal(reason string, filtered bool) string {
	switch {
	case reason == reasonBlocklist || reason == reasonFamily || reason == reasonLiteral:
		return reason
	case filtered && (strings.Contains(reason, " 0.0.0.0 denied by rule") || strings.Contains(reason, " :: denied by rule")):
		return reasonFamily
	case strings.Contains(reason, "denied by rule"):
		return reasonPrivate
	}
	return reasonOther
}

var domainText = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$`)

// decisions formats Smokescreen's one line per request as {group, domain, decision}
// and counts it. Every other field (client address, sizes, timings, trace and request
// identifiers, errors) and every other log line is dropped; a failed start is kept.
type decisions struct {
	group           string
	allowed, denied atomic.Uint64
}

func domain(value interface{}) string {
	host, _ := value.(string)
	host = normalize(host)
	// Client-supplied text: anything that is not a plain host name is not echoed.
	if !domainText.MatchString(host) {
		return "invalid"
	}
	return host
}

func (d *decisions) Format(entry *logrus.Entry) ([]byte, error) {
	if entry.Message != smokescreen.CanonicalProxyDecision {
		if entry.Level > logrus.FatalLevel {
			return nil, nil
		}
		line, err := json.Marshal(map[string]string{"group": d.group, "fatal": entry.Message})
		return append(line, '\n'), err
	}
	host := domain(entry.Data[smokescreen.LogFieldRequestedHost])
	if host == statusHost {
		return nil, nil
	}
	decision := "deny"
	if allow, _ := entry.Data[smokescreen.LogFieldAllow].(bool); allow {
		decision = "allow"
		d.allowed.Add(1)
	} else {
		d.denied.Add(1)
	}
	line, err := json.Marshal(map[string]string{"group": d.group, "decision": decision, "domain": host})
	return append(line, '\n'), err
}

// denials remembers each workspace's recent refusals so that it can ask why a request
// failed. A workspace is known by its pod address, the source of its connections to
// the proxy, and is only ever shown its own entries. An entry is the logged domain and
// the short reason. Entries live in memory only and are dropped once older than
// denialWindow: on the next refusal or status request, or the minute sweep.
type denial struct {
	host, reason string
	at           time.Time
}

type denials struct {
	mu      sync.Mutex
	now     func() time.Time
	clients map[string][]denial // newest first
}

func newDenials() *denials {
	return &denials{now: time.Now, clients: map[string][]denial{}}
}

// client is the address a request came from, without its port; "" if there is none.
func client(r *http.Request) string {
	if r == nil {
		return ""
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || net.ParseIP(host) == nil {
		return ""
	}
	return host
}

func (d *denials) add(client, host, reason string) {
	if client == "" {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	now := d.now()
	d.expire(now)
	if _, known := d.clients[client]; !known && len(d.clients) >= denialClients {
		d.evict()
	}
	kept := []denial{{host, reason, now}}
	for _, e := range d.clients[client] {
		if len(kept) == denialsPerClient {
			break
		}
		if e.host != host {
			kept = append(kept, e)
		}
	}
	d.clients[client] = kept
}

// expire drops every entry older than denialWindow, and clients left with none.
// The caller holds the lock.
func (d *denials) expire(now time.Time) {
	for c, entries := range d.clients {
		current := 0
		for current < len(entries) && now.Sub(entries[current].at) < denialWindow {
			current++
		}
		if current == 0 {
			delete(d.clients, c)
		} else if current < len(entries) {
			d.clients[c] = entries[:current:current]
		}
	}
}

// sweep expires old entries even when no refusal or status request arrives.
func (d *denials) sweep() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.expire(d.now())
}

// evict drops the client whose newest entry is the oldest. The caller holds the lock.
func (d *denials) evict() {
	oldest, at := "", time.Time{}
	for c, entries := range d.clients {
		if oldest == "" || entries[0].at.Before(at) {
			oldest, at = c, entries[0].at
		}
	}
	delete(d.clients, oldest)
}

type denialEntry struct {
	Host       string `json:"host"`
	Reason     string `json:"reason"`
	SecondsAgo int64  `json:"seconds_ago"`
}

// recent lists one client's current entries, newest first, optionally for one host.
func (d *denials) recent(client, host string) []denialEntry {
	d.mu.Lock()
	defer d.mu.Unlock()
	now := d.now()
	d.expire(now)
	result := []denialEntry{}
	if client == "" {
		return result
	}
	for _, e := range d.clients[client] {
		if host == "" || e.host == host {
			result = append(result, denialEntry{e.host, e.reason, int64(now.Sub(e.at) / time.Second)})
		}
	}
	return result
}

// reply replaces a refusal response with the proxy's own answer.
func reply(response *http.Response, code int, header http.Header, text string) {
	response.StatusCode, response.Status = code, fmt.Sprintf("%d %s", code, http.StatusText(code))
	response.Header = header
	response.Body = io.NopCloser(strings.NewReader(text))
	response.ContentLength = int64(len(text))
}

// answer serves the status page: the requesting workspace's refusals, never another's.
// It is plain HTTP only; the proxy terminates no TLS, so a CONNECT to it is refused.
func (d *denials) answer(proxyType string, response *http.Response) {
	request := response.Request
	text := "text/plain; charset=utf-8"
	if proxyType == "connect" || request == nil || request.URL == nil {
		reply(response, http.StatusForbidden, http.Header{"Content-Type": {text}, "X-Sandbox-Blocked": {reasonStatusTLS}},
			"Blocked by the sandbox proxy: "+reasonStatusTLS+".\n")
		return
	}
	if request.URL.Path != statusPath {
		reply(response, http.StatusNotFound, http.Header{"Content-Type": {text}},
			"Not found. This workspace's recent refusals are at "+statusURL+"\n")
		return
	}
	if request.Method != http.MethodGet {
		reply(response, http.StatusMethodNotAllowed, http.Header{"Content-Type": {text}, "Allow": {http.MethodGet}},
			"Use GET "+statusURL+"\n")
		return
	}
	host := ""
	if value := request.URL.Query().Get("host"); value != "" {
		if target, err := url.Parse(value); err == nil && target.Host != "" {
			value = target.Host
		}
		host = domain(value)
	}
	body, err := json.Marshal(map[string]interface{}{
		"blocked":        d.recent(client(request), host),
		"window_seconds": int(denialWindow / time.Second),
		"about": "Requests from this workspace that the sandbox egress proxy refused, newest first. " +
			"The reasons are the sandbox's policy; retrying will not help. Add ?host=NAME to ask about one site.",
	})
	if err != nil {
		body = []byte(`{"blocked":[]}`)
	}
	reply(response, http.StatusOK, http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}},
		string(body)+"\n")
}

// reject turns Smokescreen's refusal into a plain 403 with one short reason, and
// remembers it for the client's status page. Smokescreen's own text names the
// addresses a host resolved to, and its 407 reads as a login prompt to browsers.
func reject(filtered bool, recent *denials) func(*smokescreen.SmokescreenContext, *http.Response) {
	return func(sctx *smokescreen.SmokescreenContext, response *http.Response) {
		if response.StatusCode != http.StatusProxyAuthRequired {
			return
		}
		reason, host := reasonOther, ""
		if sctx != nil {
			host = sctx.RequestedHost
			if sctx.Decision != nil {
				if sctx.Decision.Reason == reasonStatus {
					recent.answer(sctx.ProxyType, response)
					return
				}
				reason = refusal(sctx.Decision.Reason, filtered)
			}
		}
		recent.add(client(response.Request), domain(host), reason)
		reply(response, http.StatusForbidden,
			http.Header{"Content-Type": {"text/plain; charset=utf-8"}, "X-Sandbox-Blocked": {reason}},
			"Blocked by the sandbox proxy: "+reason+".\n")
	}
}

func metrics(d *decisions, sources []*source) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "# TYPE workstation_sandbox_egress_requests_total counter\n"+
			"workstation_sandbox_egress_requests_total{group=%q,decision=\"allow\"} %d\n"+
			"workstation_sandbox_egress_requests_total{group=%q,decision=\"deny\"} %d\n",
			d.group, d.allowed.Load(), d.group, d.denied.Load())
		if len(sources) == 0 {
			return
		}
		// refreshed 0: still the copy baked into the image.
		fmt.Fprint(w, "# TYPE workstation_sandbox_egress_blocklist_entries gauge\n"+
			"# TYPE workstation_sandbox_egress_blocklist_refreshed_timestamp_seconds gauge\n"+
			"# TYPE workstation_sandbox_egress_blocklist_refresh_failures_total counter\n")
		for _, s := range sources {
			fmt.Fprintf(w, "workstation_sandbox_egress_blocklist_entries{group=%q,list=%q} %d\n"+
				"workstation_sandbox_egress_blocklist_refreshed_timestamp_seconds{group=%q,list=%q} %d\n"+
				"workstation_sandbox_egress_blocklist_refresh_failures_total{group=%q,list=%q} %d\n",
				d.group, s.Name, len(*s.current.Load()), d.group, s.Name, s.refreshed.Load(),
				d.group, s.Name, s.failures.Load())
		}
	}
}

func main() {
	group := flag.String("group", "", "the group this proxy serves (one per process)")
	blocklist := flag.String("blocklist", "", "the owner's blocked domains, one per line")
	maintained := flag.String("maintained", "", "directory of maintained lists (sources.json and its files)")
	refresh := flag.Duration("refresh", 24*time.Hour, "how often to reread the maintained lists; 0 keeps the baked copies")
	resolvers := flag.String("resolvers", "", "comma-separated filtering DNS servers (ip:port) used instead of the system resolver")
	denyLiterals := flag.Bool("deny-literals", false, "refuse address literals, which no name filter can judge")
	deny := flag.String("deny-ranges", "", "comma-separated CIDRs refused in addition to Smokescreen's built-in private ranges")
	port := flag.Uint("port", 4750, "proxy port")
	metricsAddr := flag.String("metrics", ":9810", "metrics listen address")
	maxConcurrent := flag.Int("max-concurrent", 256, "requests and tunnels served at once, for the whole proxy")
	maxRate := flag.Float64("max-rate", 100, "new requests per second, for the whole proxy")
	flag.Parse()
	if !regexp.MustCompile(`^[a-z]{1,16}$`).MatchString(*group) || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "usage: sandbox-egress --group <name> [--blocklist file] [--maintained dir] [--resolvers ip:port,...]")
		os.Exit(2)
	}
	format := &decisions{group: *group}
	logger := logrus.New()
	logger.SetOutput(os.Stdout)
	logger.SetFormatter(format)
	// goproxy and net/http write request details (client addresses, hosts) to the standard
	// logger and to a logger goproxy builds on os.Stderr. Nothing this process means to
	// say goes there: decisions and a failed start go to stdout. Go's own crash output
	// writes to descriptor 2 directly and is unaffected.
	log.SetOutput(io.Discard)
	logrus.SetOutput(io.Discard)
	if null, err := os.OpenFile(os.DevNull, os.O_WRONLY, 0); err == nil {
		os.Stderr = null
	}

	rules := &policy{owner: domains{}, denyLiterals: *denyLiterals}
	if *blocklist != "" {
		owner, err := loadList(*blocklist)
		if err != nil {
			logger.Fatalf("cannot load the blocklist: %v", err)
		}
		rules.owner = owner
	}
	if *maintained != "" {
		sources, err := loadSources(*maintained)
		if err != nil {
			logger.Fatalf("cannot load the maintained lists: %v", err)
		}
		rules.sources = sources
	}

	conf := smokescreen.NewConfig()
	conf.Log = logger
	conf.Port = uint16(*port)
	conf.Network = "ip4"
	conf.ExitTimeout = 5 * time.Second
	// The group is this process's, never the request's: a workspace cannot pick another policy.
	conf.RoleFromRequest = func(*http.Request) (string, error) { return *group, nil }
	conf.EgressACL = rules
	resolver := net.DefaultResolver
	filtered := *resolvers != ""
	if filtered {
		servers := strings.Split(*resolvers, ",")
		for _, server := range servers {
			if host, _, err := net.SplitHostPort(server); err != nil || net.ParseIP(host) == nil {
				logger.Fatalf("invalid --resolvers entry %q", server)
			}
		}
		resolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, network, servers[rand.IntN(len(servers))])
		}}
		conf.Resolver = resolver
	}
	if *deny != "" {
		if err := conf.SetDenyRanges(strings.Split(*deny, ",")); err != nil {
			logger.Fatalf("invalid --deny-ranges: %v", err)
		}
	}
	// Bounds for the whole proxy, shared by the few workspaces behind it.
	if err := conf.SetRateLimits(*maxConcurrent, *maxRate, -1); err != nil {
		logger.Fatalf("invalid limits: %v", err)
	}
	conf.MaxConcurrentConnectTunnels = *maxConcurrent
	// A refusal is a plain 403 with one short reason; the status page answers in its place.
	recent := newDenials()
	conf.RejectResponseHandlerWithCtx = reject(filtered, recent)
	go func() {
		for range time.Tick(time.Minute) {
			recent.sweep()
		}
	}()

	if len(rules.sources) > 0 && *refresh > 0 {
		client := refreshClient(resolver)
		go func() {
			time.Sleep(30 * time.Second)
			for {
				for _, s := range rules.sources {
					if err := s.refresh(client); err != nil {
						s.failures.Add(1)
					}
				}
				time.Sleep(*refresh)
			}
		}()
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", metrics(format, rules.sources))
	go func() {
		server := &http.Server{Addr: *metricsAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
		logger.Fatalf("metrics listener stopped: %v", server.ListenAndServe())
	}()
	smokescreen.StartWithConfig(conf, nil)
}
