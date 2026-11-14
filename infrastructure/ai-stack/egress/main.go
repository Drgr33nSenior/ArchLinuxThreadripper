// The egress proxy for assistant coding workspaces: Stripe Smokescreen used as a
// library, one fixed group per process. Smokescreen decides by domain (the ACL file)
// and refuses private, loopback, link-local and CGNAT addresses after it resolves the
// name, so an allowed name that points at the LAN or the cluster is still denied.
//
// This file adds only what the stock binary lacks: a role that the client cannot
// choose (stock takes it from a client certificate), and a log that carries nothing
// but the group, the domain and the decision. Built inside Smokescreen's own module
// (cmd/sandbox-egress) from its vendored dependencies.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"github.com/sirupsen/logrus"
	"github.com/stripe/smokescreen/pkg/smokescreen"
)

var hostname = regexp.MustCompile(`^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$`)

// decisions formats Smokescreen's one line per request as {group, domain, decision}
// and counts it. Every other field (client address, sizes, timings, trace and request
// identifiers, errors) and every other log line is dropped; a failed start is kept.
type decisions struct {
	group           string
	allowed, denied atomic.Uint64
}

func domain(value interface{}) string {
	host, _ := value.(string)
	if name, _, err := net.SplitHostPort(host); err == nil {
		host = name
	}
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	// Client-supplied text: anything that is not a plain host name is not echoed.
	if !hostname.MatchString(host) {
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
	decision := "deny"
	if allow, _ := entry.Data[smokescreen.LogFieldAllow].(bool); allow {
		decision = "allow"
		d.allowed.Add(1)
	} else {
		d.denied.Add(1)
	}
	line, err := json.Marshal(map[string]string{"group": d.group, "decision": decision,
		"domain": domain(entry.Data[smokescreen.LogFieldRequestedHost])})
	return append(line, '\n'), err
}

func (d *decisions) metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# TYPE workstation_sandbox_egress_requests_total counter\n"+
		"workstation_sandbox_egress_requests_total{group=%q,decision=\"allow\"} %d\n"+
		"workstation_sandbox_egress_requests_total{group=%q,decision=\"deny\"} %d\n",
		d.group, d.allowed.Load(), d.group, d.denied.Load())
}

func main() {
	group := flag.String("group", "", "the ACL service this proxy enforces (one per process)")
	aclFile := flag.String("acl", "/config/acl.yaml", "Smokescreen v1 ACL file")
	deny := flag.String("deny-ranges", "", "comma-separated CIDRs refused in addition to Smokescreen's built-in private ranges")
	port := flag.Uint("port", 4750, "proxy port")
	metricsAddr := flag.String("metrics", ":9810", "metrics listen address")
	flag.Parse()
	if !regexp.MustCompile(`^[a-z]{1,16}$`).MatchString(*group) || flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "usage: sandbox-egress --group <name> [--acl file] [--deny-ranges cidr,...]")
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

	conf := smokescreen.NewConfig()
	conf.Log = logger
	conf.Port = uint16(*port)
	conf.ExitTimeout = 5 * time.Second
	conf.AdditionalErrorMessageOnDeny = "Coding workspaces may only reach the reviewed list of sites for this account."
	// The group is this process's, never the request's: a workspace cannot pick another list.
	conf.RoleFromRequest = func(*http.Request) (string, error) { return *group, nil }
	if err := conf.SetupEgressAcl(*aclFile); err != nil {
		logger.Fatalf("cannot load the egress ACL: %v", err)
	}
	if *deny != "" {
		if err := conf.SetDenyRanges(strings.Split(*deny, ",")); err != nil {
			logger.Fatalf("invalid --deny-ranges: %v", err)
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", format.metrics)
	go func() {
		server := &http.Server{Addr: *metricsAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
		logger.Fatalf("metrics listener stopped: %v", server.ListenAndServe())
	}()
	smokescreen.StartWithConfig(conf, nil)
}
