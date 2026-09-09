#!/usr/bin/env bash
# Read-only connectivity checks. Never print mirror URLs (they may be sensitive).

bootstrap_network_tools() {
  local tool
  for tool in curl getent ip timedatectl pacman-conf bsdtar mktemp; do
    command -v "$tool" >/dev/null 2>&1 || {
      bootstrap_die "network prerequisite missing: $tool"
      return 1
    }
  done
  [[ $(timedatectl show --property=NTPSynchronized --value) == yes ]] || {
    bootstrap_die 'network/time: wait for clock synchronization before TLS/package checks'
    return 1
  }
}

bootstrap_network_route() {
  local host=$1 addresses address
  addresses=$(getent ahosts "$host") || {
    bootstrap_die 'network/DNS: host resolution failed'
    return 1
  }
  [[ -n $addresses ]] || {
    bootstrap_die 'network/DNS: empty resolution'
    return 1
  }
  while read -r address _; do
    if [[ $address == *:* ]]; then
      ip -6 route get "$address" >/dev/null 2>&1 && return 0
    else
      ip -4 route get "$address" >/dev/null 2>&1 && return 0
    fi
  done <<<"$addresses"
  bootstrap_die 'network/routing: no route to resolved host'
}

bootstrap_network_curl() {
  # Ignore curlrc, inherited API keys, proxy credentials and debug environment.
  env -i PATH="$PATH" curl -q --silent --fail --location --proto '=https' \
    --proto-redir '=https' --connect-timeout 10 --max-time 45 "$@"
}

bootstrap_network_mirrors() (
  # Isolate traps and own only this freshly allocated scratch directory.
  local repos repo servers server host scratch ok remote=0 core=0 extra=0
  bootstrap_network_tools || return 1
  repos=$(pacman-conf --repo-list) || {
    bootstrap_die 'network/mirrors: cannot read pacman configuration'
    return 1
  }
  [[ -n $repos ]] || {
    bootstrap_die 'network/mirrors: no configured repositories'
    return 1
  }
  umask 077
  scratch=$(mktemp -d) || return 1
  trap 'rm -rf -- "$scratch"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  while IFS= read -r repo; do
    [[ $repo =~ ^[A-Za-z0-9_-]+$ ]] || {
      bootstrap_die 'network/mirrors: invalid repository identity'
      return 1
    }
    [[ $repo != core ]] || core=1
    [[ $repo != extra ]] || extra=1
    servers=$(pacman-conf --repo "$repo" Server) || return 1
    [[ -n $servers ]] || {
      bootstrap_die "network/mirrors: no servers for $repo"
      return 1
    }
    ok=0
    while IFS= read -r server; do
      if [[ $server == file:///* && $server != *'..'* ]]; then
        if bsdtar -tf "${server#file://}/$repo.db" >"$scratch/list" 2>/dev/null && [[ -s $scratch/list ]]; then
          ok=1
          break
        fi
        continue
      fi
      # Reject credentials, substitutions, queries and non-HTTPS mirrors.
      [[ $server =~ ^https://([A-Za-z0-9.-]+)(:[0-9]+)?(/[A-Za-z0-9._~/%+-]*)?$ ]] || continue
      host=${BASH_REMATCH[1]}
      bootstrap_network_route "$host" || continue
      if bootstrap_network_curl --max-filesize 33554432 --output "$scratch/repo.db" "${server%/}/$repo.db" &&
        bsdtar -tf "$scratch/repo.db" >"$scratch/list" 2>/dev/null && [[ -s $scratch/list ]]; then
        ok=1
        remote=$((remote + 1))
        break
      fi
    done <<<"$servers"
    ((ok)) || {
      bootstrap_die "network/mirrors: no usable database for $repo (DNS/routing/TLS/HTTP/archive); no erasure"
      return 1
    }
    bootstrap_log "network: $repo database reachable (package signatures still required)"
  done <<<"$repos"
  ((core && extra && remote > 0)) || {
    bootstrap_die 'network/mirrors: require core, extra and an HTTPS mirror'
    return 1
  }
)

bootstrap_network_codex() {
  local mode=$1 host url code
  bootstrap_network_tools || return 1
  case $mode in
    api)
      host=api.openai.com
      url=https://api.openai.com/v1/models
      ;;
    device)
      host=auth.openai.com
      url=https://auth.openai.com/.well-known/openid-configuration
      ;;
    *) return 2 ;;
  esac
  bootstrap_network_route "$host" || return 1
  # A deliberate unauthenticated request. 401 is expected for the API endpoint.
  code=$(env -i PATH="$PATH" curl -q --silent --output /dev/null --write-out '%{http_code}' \
    --proto '=https' --connect-timeout 10 --max-time 20 "$url") || {
    bootstrap_die 'Codex connectivity: TLS/transport failed (not an authentication verdict)'
    return 1
  }
  if [[ $code == 200 || ($mode == api && $code == 401) ]]; then
    bootstrap_log 'Codex endpoint reachable; authentication, account entitlement and model access are NOT verified'
  else
    bootstrap_die "Codex endpoint refused probe (HTTP $code); no billing-method fallback"
    return 1
  fi
}
