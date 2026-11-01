"""Bounded public Flexible Octopus rate refresh; no account or meter credentials.

Produces tariff metadata only. It does not meter power, integrate money, change
PSU state, discover a customer's agreement or claim electricity-bill accuracy.
"""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request


MAX_BODY = 131072
MAX_PAGES = 4
MAX_CACHE_AGE = 26 * 3600
PUBLICATION_TTL = 2 * 3600
TARIFF = re.compile(r"E-1R-(VAR-[0-9]{2}-[0-9]{2}-[0-9]{2})-([A-HJ-NP])")
CONFIG_KEYS = {"schema", "tariff_code", "payment_method", "fallback_unit_pence", "fallback_standing_pence"}


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def number(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("rate outside parsing bounds")
    return float(value)


def config(value):
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS or type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("unsupported public tariff configuration")
    code, method = value["tariff_code"], value["payment_method"]
    if code is None:
        if method is not None:
            raise ValueError("tariff and payment method must be configured together")
    elif not isinstance(code, str) or not TARIFF.fullmatch(code) or method not in {"DIRECT_DEBIT", "NON_DIRECT_DEBIT"}:
        raise ValueError("require an exact single-rate Flexible tariff and payment method")
    number(value["fallback_unit_pence"], 1000)
    number(value["fallback_standing_pence"], 10000)
    return value


def epoch(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid effective date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("effective date needs timezone")
    return parsed.timestamp()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("tariff redirects are not permitted")


def fetch_json(url):
    # Do not inherit proxy credentials or send a customer identity/header.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "workstation-tariff/1"})
    with opener.open(request, timeout=8) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("unexpected tariff response")
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise ValueError("oversized tariff response")
    return decode(body)


def price(code, kind, method, now, fetch=fetch_json):
    product = TARIFF.fullmatch(code).group(1)
    path = f"/v1/products/{product}/electricity-tariffs/{code}/{kind}/"
    url = "https://api.octopus.energy" + path + "?page_size=100"
    candidates = []
    for _ in range(MAX_PAGES):
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        if (parsed.scheme != "https" or parsed.netloc != "api.octopus.energy" or parsed.path != path
                or parsed.fragment or set(query) - {"page", "page_size"}
                or any(len(values) != 1 or not re.fullmatch(r"[0-9]{1,3}", values[0]) for values in query.values())):
            raise ValueError("unexpected tariff page URL")
        page = fetch(url)
        if not isinstance(page, dict) or not isinstance(page.get("results"), list) or len(page["results"]) > 100:
            raise ValueError("invalid tariff page")
        for row in page["results"]:
            if not isinstance(row, dict):
                raise ValueError("invalid tariff row")
            start = epoch(row["valid_from"])
            end = None if row["valid_to"] is None else epoch(row["valid_to"])
            if end is not None and end <= start:
                raise ValueError("invalid tariff interval")
            if start <= now and (end is None or now < end) and row.get("payment_method") in {method, None}:
                candidates.append({"value": number(row["value_inc_vat"], 1000 if kind == "standard-unit-rates" else 10000),
                                   "from": start, "to": end})
        following = page.get("next")
        if following is None:
            break
        if not isinstance(following, str) or len(following) > 512:
            raise ValueError("invalid tariff pagination")
        url = following
    else:
        raise ValueError("tariff page budget exhausted")
    if len(candidates) != 1:
        raise ValueError("require one unambiguous currently effective price")
    return candidates[0]


def binding(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def valid_cache(cache, settings, now):
    try:
        return (isinstance(cache, dict) and type(cache["schema"]) is int and cache["schema"] == 1 and cache["binding"] == binding(settings)
                and 0 <= now - number(cache["fetched_at"], 1e12) <= MAX_CACHE_AGE
                and cache["from"] <= now and (cache["to"] is None or now < cache["to"])
                and number(cache["unit_gbp"], 10) >= 0 and number(cache["standing_gbp"], 100) >= 0)
    except (KeyError, TypeError, ValueError):
        return False


def refresh(settings, cache, now, fetch=fetch_json):
    settings = config(settings)
    mode, success, saved = 0, 0, None
    active = {"unit_gbp": settings["fallback_unit_pence"] / 100,
              "standing_gbp": settings["fallback_standing_pence"] / 100, "to": None}
    if settings["tariff_code"] is not None:
        try:
            unit = price(settings["tariff_code"], "standard-unit-rates", settings["payment_method"], now, fetch)
            standing = price(settings["tariff_code"], "standing-charges", settings["payment_method"], now, fetch)
            ends = [row["to"] for row in (unit, standing) if row["to"] is not None]
            saved = {"schema": 1, "binding": binding(settings), "fetched_at": now,
                     "unit_gbp": unit["value"] / 100, "standing_gbp": standing["value"] / 100,
                     "from": max(unit["from"], standing["from"]), "to": min(ends) if ends else None}
            active, mode, success = saved, 1, 1
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            if valid_cache(cache, settings, now):
                active, mode = cache, 2
    valid_until = min(now + PUBLICATION_TTL, active["to"]) if active["to"] is not None else now + PUBLICATION_TTL
    values = {"unit_price_gbp_per_kwh": active["unit_gbp"], "standing_charge_gbp_per_day": active["standing_gbp"],
              "mode": mode, "auto_configured": int(settings["tariff_code"] is not None),
              "refresh_success": success, "sample_timestamp_seconds": now, "valid_until_seconds": valid_until}
    if mode in (1, 2):
        values["last_success_timestamp_seconds"] = active["fetched_at"]
    return values, saved


def read_file(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe tariff file")
        value = os.read(descriptor, maximum + 1)
        if len(value) > maximum:
            raise ValueError("oversized tariff file")
        return decode(value)
    finally:
        os.close(descriptor)


def write_file(directory, name, text, mode):
    target = directory / name
    if target.is_symlink():
        raise ValueError("symlink tariff output")
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ValueError("unsafe tariff output")
    descriptor, temporary = tempfile.mkstemp(prefix=".tariff-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(config_path, directory, now=None, fetch=fetch_json):
    directory = Path(directory)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("tariff directory must be caller-owned and not writable by others")
    lock = os.open(directory / ".refresh.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode) or os.fstat(lock).st_nlink != 1:
            raise ValueError("unsafe tariff lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        settings = config(read_file(config_path, 4096))
        try:
            cache = read_file(directory / ".last-good.json", 4096)
        except FileNotFoundError:
            cache = None
        values, saved = refresh(settings, cache, time.time() if now is None else now, fetch)
        # Refuse unsafe publication before replacing the last-good cache.
        if (directory / "tariff.prom").is_symlink():
            raise ValueError("symlink tariff output")
        if saved is not None:
            write_file(directory, ".last-good.json", json.dumps(saved, allow_nan=False) + "\n", 0o600)
        write_file(directory, "tariff.prom", "".join(f"workstation_tariff_{key} {value:.12g}\n" for key, value in values.items()), 0o644)
        return values
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("directory")
    args = parser.parse_args()
    try:
        run(args.config, args.directory)
    except (OSError, ValueError, KeyError, TypeError):
        print("FAILED: tariff refresh/publication; prior files retained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
