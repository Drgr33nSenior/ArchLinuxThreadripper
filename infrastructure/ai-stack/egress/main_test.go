package main

import (
	"net"
	"strings"
	"testing"
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
	} {
		if got := family.reason(host); got != want {
			t.Errorf("family reason(%q) = %q, want %q", host, got, want)
		}
	}
	for host, want := range map[string]string{"adult.example": "", "192.0.2.7": "", "owner.example": reasonBlocklist} {
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
