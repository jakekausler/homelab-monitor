"""Reference mirror of the udm_parse VRL multi-format parser.

Tested against REAL captured UDM samples
(apps/monitor/tests/fixtures/udm_syslog_samples.txt). This reference ports the
udm_parse ALGORITHM (deploy/vector/vector.toml.template [transforms.udm_parse])
to pure Python so CI can exercise the parse WITHOUT a vector binary:

  1. Envelope strip: <PRI> + syslog ts + hostname + body; on standard lines the
     DOUBLED leading hostname ("UDM-Pro UDM-Pro") is stripped from the body.
  2. Three-way format branch on the (de-duplicated) body:
       - starts with "CEF:"        -> service="udm-audit"    udm_format="cef"
       - matches r'^\\[[A-Z]'       -> service="udm-firewall" udm_format="iptables"
       - otherwise (systemd/sshd/daemon) -> service="udm-system" udm_format="system"
  3. Unrecognized envelope         -> service="udm-other"    udm_format="unknown"
                                       parse_failed=1
  CEF extension parsing uses the same boundary pre-split (split on " KEY=" for a
  bounded key vocabulary) so SPACE-containing values (e.g. UNIFIadmin="Admin User",
  UNIFIsettingsSection="Firewall Group") survive intact.

The AUTHORITATIVE check is `vector validate` (the @pytest.mark.slow test in
test_vector_template.py) plus live Refinement against the real UDM Pro. Keep this
reference in sync with deploy/vector/vector.toml.template [transforms.udm_parse].

This module lives under apps/monitor/tests/ (OUTSIDE the homelab_monitor coverage
source), so its branches are NOT subject to the 100% branch gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --- Envelope regex (mirrors the VRL parse_regex envelope pattern) ---
_ENVELOPE = re.compile(
    r"^<(?P<pri>\d+)>"
    r"(?P<ts>[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<body>.*)$"
)

# NO-PRI fallback envelope (mirrors the VRL no-<PRI> parse_regex): some CEF audit
# lines arrive as "Mon DD HH:MM:SS host CEF:..." with no leading <PRI>.
_ENVELOPE_NOPRI = re.compile(
    r"^(?P<ts>[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<body>.*)$"
)

# --- CEF header field indices (split "CEF:0|vendor|product|ver|sig|name|sev|ext" on "|") ---
_CEF_IDX_SIGNATURE_ID = 4
_CEF_IDX_NAME = 5
_CEF_IDX_SEVERITY = 6
_CEF_IDX_EXTENSION = 7

# Bounded CEF-extension key vocabulary for the boundary pre-split (mirrors the VRL
# replace(ext, r' (UNIFI[A-Za-z]+|src|dst|spt|dpt|msg|cs\d|cn\d)=', "\n$$1=")).
_CEF_KEY_BOUNDARY = re.compile(r" (UNIFI[A-Za-z]+|src|dst|spt|dpt|msg|cs\d|cn\d)=")

# iptables branch discriminator (mirrors VRL match(body, r'^\[[A-Z]')).
_IPTABLES_HEAD = re.compile(r"^\[[A-Z]")
_FW_CHAIN = re.compile(r"^\[(?P<chain>[^\]]*)\]")
_FW_DESCR = re.compile(r'DESCR="(?P<descr>[^"]*)"')

# systemd/sshd/daemon proc extraction (mirrors the VRL proc parse_regex).
_PROC = re.compile(r"^(?P<proc>[A-Za-z0-9._-]+)(\[(?P<pid>\d+)\])?:\s+(?P<pmsg>.*)$")


def _parse_kv_newline(norm: str) -> dict[str, str]:
    """Parse the boundary-presplit CEF extension (newline-delimited key=value)."""
    out: dict[str, str] = {}
    for line in norm.split("\n"):
        if "=" in line:
            key, _, value = line.partition("=")
            if key:
                out[key] = value
    return out


def _fw_field(rest: str, key: str) -> str:
    r"""Extract one iptables key's value via its own regex (mirrors the VRL
    per-field parse_regex). Tolerant of empty values (OUT=) and bare flag tokens
    (DF/SYN); \b<KEY>=(?P<v>\S*) never swallows the following key."""
    m = re.search(r"\b" + re.escape(key) + r"=(?P<v>\S*)", rest)
    return m.group("v") if m is not None else ""


def parse_udm_line(raw: str) -> dict[str, str]:  # noqa: PLR0915
    """Pure-Python port of the udm_parse VRL. Returns the enriched event fields.

    Intentionally long/flat: this mirrors the multi-branch udm_parse VRL transform
    statement-for-statement so the two stay easy to diff. Splitting it into helpers
    would diverge from the VRL structure it must track.
    """
    result: dict[str, str] = {"parse_failed": "0"}
    # Mirror VRL strip_whitespace: real UDP datagrams carry a trailing "\n"
    # (or "\r\n"); strip BEFORE envelope matching else "$" rejects every line.
    raw = raw.strip()
    m = _ENVELOPE.match(raw)
    pri_present = True
    if m is None:
        m = _ENVELOPE_NOPRI.match(raw)
        pri_present = False
    if m is None:
        result["parse_failed"] = "1"
        result["message"] = raw
        result["source_type"] = "udm"
        result["service"] = "udm-other"
        result["udm_format"] = "unknown"
        return result

    if pri_present:
        pri = int(m.group("pri"))
        result["syslog_severity"] = str(pri % 8)
    else:
        result["syslog_severity"] = ""
    host = m.group("host")
    body = m.group("body")
    dup = host + " "
    if body.startswith(dup):
        body = body[len(dup) :]
    result["host"] = host
    result["source_type"] = "udm"

    if body.startswith("CEF:"):
        result["udm_format"] = "cef"
        result["service"] = "udm-audit"
        parts = body.split("|", _CEF_IDX_EXTENSION)
        result["cef_signature_id"] = (
            parts[_CEF_IDX_SIGNATURE_ID] if len(parts) > _CEF_IDX_SIGNATURE_ID else ""
        )
        result["cef_name"] = parts[_CEF_IDX_NAME] if len(parts) > _CEF_IDX_NAME else ""
        result["cef_severity_raw"] = (
            parts[_CEF_IDX_SEVERITY] if len(parts) > _CEF_IDX_SEVERITY else ""
        )
        ext = parts[_CEF_IDX_EXTENSION] if len(parts) > _CEF_IDX_EXTENSION else ""
        norm = _CEF_KEY_BOUNDARY.sub(lambda mo: "\n" + mo.group(1) + "=", ext)
        kv = _parse_kv_newline(norm)
        result["udm_category"] = kv.get("UNIFIcategory", "").lower()
        result["udm_subcategory"] = kv.get("UNIFIsubCategory", "")
        result["udm_admin"] = kv.get("UNIFIadmin", "")
        result["udm_access_method"] = kv.get("UNIFIaccessMethod", "")
        result["udm_settings_section"] = kv.get("UNIFIsettingsSection", "")
        result["udm_settings_entry"] = kv.get("UNIFIsettingsEntry", "")
        result["src"] = kv.get("src", "")
        cefmsg = kv.get("msg", "")
        result["message"] = cefmsg if cefmsg != "" else body
    elif _IPTABLES_HEAD.match(body):
        result["udm_format"] = "iptables"
        result["service"] = "udm-firewall"
        chain = _FW_CHAIN.match(body)
        if chain is not None:
            result["fw_chain"] = chain.group("chain")
        descr = _FW_DESCR.search(body)
        rest = body
        if descr is not None:
            result["fw_descr"] = descr.group("descr")
            rest = _FW_DESCR.sub("", body)
        result["fw_in"] = _fw_field(rest, "IN")
        result["fw_out"] = _fw_field(rest, "OUT")
        result["src"] = _fw_field(rest, "SRC")
        result["dst"] = _fw_field(rest, "DST")
        result["fw_proto"] = _fw_field(rest, "PROTO")
        result["fw_spt"] = _fw_field(rest, "SPT")
        result["fw_dpt"] = _fw_field(rest, "DPT")
        result["message"] = body
    else:
        result["udm_format"] = "system"
        result["service"] = "udm-system"
        proc = _PROC.match(body)
        if proc is not None:
            result["process"] = proc.group("proc")
            result["message"] = proc.group("pmsg")
        else:
            result["message"] = body
    return result


# --- Real captured fixtures (verbatim lines from udm_syslog_samples.txt) ---

_CEF_LOGIN = (
    "<30>Jun 19 18:17:32 UDM-Pro CEF:0|Ubiquiti|UniFi Network|10.4.57|544|"
    "Network Accessed|4|UNIFIcategory=Audit UNIFIhost=UDM Pro "
    "UNIFIaccessMethod=web UNIFIadmin=Admin User src=192.168.2.38 "
    "UNIFIutcTime=2026-06-19T22:17:32.276Z "
    "msg=Admin User accessed UniFi Network using the web. Source IP: 192.168.2.38"
)
_CEF_CONFIG_CHANGE = (
    "<30>Jun 19 18:18:56 UDM-Pro CEF:0|Ubiquiti|UniFi Network|10.4.57|546|"
    "Config Modified|5|UNIFIcategory=Audit UNIFIhost=UDM Pro "
    "UNIFIsettingsChanges=name: Pi-Hole DNS_test UNIFIaccessMethod=web "
    "UNIFIsettingsSection=Firewall Group UNIFIsettingsEntry=Pi-Hole DNS_test "
    "UNIFIadmin=Admin User src=192.168.2.38 "
    "UNIFIutcTime=2026-06-19T22:18:56.537Z "
    "msg=Admin User made a change to  in Pi-Hole DNS_test Firewall Group settings."
    " Source IP: 192.168.2.38"
)
_IPTABLES_UDP = (
    "<13>Jun 19 18:17:05 UDM-Pro UDM-Pro [LAN_LOCAL-RET-2147483647] "
    'DESCR="no rule description" IN=br0 OUT= '
    "MAC=ff:ff:ff:xx:xx:xx:e8:68:e7:xx:xx:xx:08:00 SRC=192.168.2.24 "
    "DST=255.255.255.255 LEN=216 TOS=00 PREC=0x00 TTL=255 ID=47698 "
    "PROTO=UDP SPT=49154 DPT=6667 LEN=196 MARK=1a0000"
)
_IPTABLES_TCP = (
    "<13>Jun 19 18:17:09 UDM-Pro UDM-Pro [LAN_LOCAL-RET-2147483647] "
    'DESCR="no rule description" IN=br0 OUT= '
    "MAC=6c:63:f8:xx:xx:xx:6c:63:f8:xx:xx:xx:08:00 SRC=192.168.2.111 "
    "DST=192.168.2.1 LEN=60 TOS=00 PREC=0x00 TTL=64 ID=34881 DF "
    "PROTO=TCP SPT=60856 DPT=8080 SEQ=726876871 ACK=0 WINDOW=64240 SYN "
    "URGP=0 MARK=1a0000"
)
_IPTABLES_NAMED_RULE = (
    "<13>Jun 19 18:17:24 UDM-Pro UDM-Pro [PREROUTING-DNAT-11] "
    'DESCR="PortForward DNAT [Nginx SSL]" IN=eth8 OUT= '
    "MAC=6c:63:f8:xx:xx:xx:2c:c1:f4:xx:xx:xx:08:00 SRC=198.51.100.80 "
    "DST=203.0.113.10 LEN=52 TOS=00 PREC=0x00 TTL=53 ID=35859 "
    "PROTO=TCP SPT=44596 DPT=443 SEQ=1402745621 ACK=0 WINDOW=65535 SYN "
    "URGP=0 MARK=1a0000"
)
_IPTABLES_ICMP = (
    "<13>Jun 19 18:17:25 UDM-Pro UDM-Pro [LAN_LOCAL-RET-2147483647] "
    'DESCR="no rule description" IN=br0 OUT= '
    "MAC=6c:63:f8:xx:xx:xx:88:29:bf:xx:xx:xx:08:00 SRC=192.168.2.139 "
    "DST=192.168.2.1 LEN=84 TOS=00 PREC=0x00 TTL=64 ID=19970 DF "
    "PROTO=ICMP TYPE=8 CODE=0 ID=56026 SEQ=61743 MARK=1a0000"
)
_IPTABLES_IPV6 = (
    "<13>Jun 19 18:17:06 UDM-Pro UDM-Pro [LAN_LOCAL-RET-2147483646] "
    'DESCR="no rule description" IN=br0 OUT= '
    "MAC=33:33:00:xx:xx:xx:36:34:d0:xx:xx:xx:86:dd "
    "SRC=fe80::3434:d0ff:feda:5b54 DST=ff02::fb LEN=95 TC=0 HOPLIMIT=255 "
    "FLOWLBL=0 PROTO=UDP SPT=5353 DPT=5353 LEN=55 MARK=1a0000"
)
_SYSTEMD = "<30>Jun 19 18:17:37 UDM-Pro UDM-Pro systemd[1]: Created slice User Slice of UID 0."
_SSHD = (
    "<38>Jun 19 18:17:37 UDM-Pro UDM-Pro sshd[1365787]: "
    "Accepted publickey for root from 192.168.2.148 port 40738 ssh2: "
    "ED25519 SHA256:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
)
_DNSMASQ = (
    "<30>Jun 19 18:17:59 UDM-Pro UDM-Pro dnsmasq[2956502]: "
    "inotify: /run/dnsmasq.dns.conf.d/hosts.d//leases new or modified"
)
_MALFORMED = "this is not a valid syslog envelope at all"


def test_cef_login_extracts_full_admin_and_message() -> None:
    p = parse_udm_line(_CEF_LOGIN)
    assert p["service"] == "udm-audit"
    assert p["udm_format"] == "cef"
    assert p["source_type"] == "udm"
    assert p["udm_category"] == "audit"
    assert p["udm_admin"] == "Admin User"  # space-truncation regression
    assert p["udm_access_method"] == "web"
    assert p["src"] == "192.168.2.38"
    assert p["cef_signature_id"] == "544"
    assert p["cef_name"] == "Network Accessed"
    assert (
        p["message"] == "Admin User accessed UniFi Network using the web. Source IP: 192.168.2.38"
    )
    assert p["parse_failed"] == "0"


def test_cef_config_change_extracts_settings_section_and_entry() -> None:
    p = parse_udm_line(_CEF_CONFIG_CHANGE)
    assert p["service"] == "udm-audit"
    assert p["udm_format"] == "cef"
    assert p["udm_settings_section"] == "Firewall Group"  # space value survives
    assert p["udm_settings_entry"] == "Pi-Hole DNS_test"
    assert p["udm_admin"] == "Admin User"
    assert "Pi-Hole DNS_test Firewall Group settings" in p["message"]


def test_iptables_udp() -> None:
    p = parse_udm_line(_IPTABLES_UDP)
    assert p["service"] == "udm-firewall"
    assert p["udm_format"] == "iptables"
    assert p["fw_proto"] == "UDP"
    assert p["src"] == "192.168.2.24"
    assert p["dst"] == "255.255.255.255"
    assert p["fw_dpt"] == "6667"
    assert p["fw_descr"] == "no rule description"
    assert p["fw_out"] == ""  # empty OUT= must NOT swallow the next key (SRC)
    assert p["fw_in"] == "br0"


def test_iptables_tcp() -> None:
    p = parse_udm_line(_IPTABLES_TCP)
    assert p["service"] == "udm-firewall"
    assert p["fw_proto"] == "TCP"
    assert p["fw_dpt"] == "8080"


def test_iptables_named_rule_descr_with_spaces_and_brackets() -> None:
    p = parse_udm_line(_IPTABLES_NAMED_RULE)
    assert p["service"] == "udm-firewall"
    assert p["fw_descr"] == "PortForward DNAT [Nginx SSL]"  # spaces + brackets survive
    assert p["fw_proto"] == "TCP"
    assert p["fw_chain"] == "PREROUTING-DNAT-11"


def test_iptables_icmp() -> None:
    p = parse_udm_line(_IPTABLES_ICMP)
    assert p["service"] == "udm-firewall"
    assert p["fw_proto"] == "ICMP"


def test_iptables_ipv6() -> None:
    p = parse_udm_line(_IPTABLES_IPV6)
    assert p["service"] == "udm-firewall"
    assert p["src"] == "fe80::3434:d0ff:feda:5b54"
    assert p["dst"] == "ff02::fb"


def test_systemd() -> None:
    p = parse_udm_line(_SYSTEMD)
    assert p["service"] == "udm-system"
    assert p["udm_format"] == "system"
    assert p["process"] == "systemd"
    assert p["message"] == "Created slice User Slice of UID 0."


def test_sshd() -> None:
    p = parse_udm_line(_SSHD)
    assert p["service"] == "udm-system"
    assert p["process"] == "sshd"
    assert p["message"].startswith("Accepted publickey for root")


def test_other_daemon_dnsmasq() -> None:
    p = parse_udm_line(_DNSMASQ)
    assert p["service"] == "udm-system"
    assert p["process"] == "dnsmasq"


def test_doubled_hostname_is_stripped() -> None:
    # Standard lines carry "UDM-Pro UDM-Pro"; the body must NOT start with the
    # second "UDM-Pro" after the dup-hostname strip.
    p = parse_udm_line(_SYSTEMD)
    assert not p["message"].startswith("UDM-Pro")
    assert p["process"] == "systemd"


def test_single_hostname_cef_not_overstripped() -> None:
    # CEF audit lines carry a SINGLE "UDM-Pro"; the dup strip must NOT consume the
    # "CEF:" body.
    p = parse_udm_line(_CEF_LOGIN)
    assert p["udm_format"] == "cef"
    assert p["cef_name"] == "Network Accessed"


def test_malformed_line_marks_parse_failed() -> None:
    p = parse_udm_line(_MALFORMED)
    assert p["parse_failed"] == "1"
    assert p["service"] == "udm-other"
    assert p["udm_format"] == "unknown"
    assert p["message"] == _MALFORMED


def test_nopri_cef_line_parses_to_audit_with_full_fields() -> None:
    # Regression for Issue 2: some CEF audit lines arrive with NO <PRI> prefix
    # ("Mon DD HH:MM:SS host CEF:..."). The no-PRI envelope fallback must still
    # bucket them as udm-audit with full CEF extraction; severity is unknowable.
    line = (
        "Jun 19 20:15:08 UDM-Pro CEF:0|Ubiquiti|UniFi Network|10.4.57|544|"
        "Network Accessed|4|UNIFIcategory=Audit UNIFIadmin=Admin User "
        "src=192.168.2.38 msg=hello world"
    )
    p = parse_udm_line(line)
    assert p["service"] == "udm-audit"
    assert p["udm_format"] == "cef"
    assert p["udm_admin"] == "Admin User"
    assert p["message"] == "hello world"
    assert p["src"] == "192.168.2.38"
    assert p["syslog_severity"] == ""  # no <PRI> -> severity unknowable
    assert p["parse_failed"] == "0"


def test_trailing_newline_still_parses() -> None:
    # Regression for Issue 1 (the bug that shipped): real UDP datagrams carry a
    # trailing "\n". Without strip_whitespace the envelope "$" rejects the line.
    p = parse_udm_line(_IPTABLES_UDP + "\n")
    assert p["service"] == "udm-firewall"
    assert p["udm_format"] == "iptables"
    assert p["fw_proto"] == "UDP"
    assert p["src"] == "192.168.2.24"
    assert p["parse_failed"] == "0"


# --- Parametrized sweep over EVERY real fixture line (no crash, sane service) ---

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "udm_syslog_samples.txt"
_VALID_SERVICES = {
    "udm-audit",
    "udm-firewall",
    "udm-system",
    "udm-other",
}


def _fixture_lines() -> list[str]:
    text = _FIXTURE_PATH.read_text(encoding="utf-8")
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("line", _fixture_lines())
def test_every_fixture_line_maps_to_a_sane_service(line: str) -> None:
    # Feed each fixture line WITH a trailing "\n" appended: real UDP syslog
    # datagrams carry one, and stripping it (splitlines) would hide the Issue 1
    # regression (envelope "$" rejecting trailing-newline lines). strip_whitespace
    # in the parser must absorb it.
    p = parse_udm_line(line + "\n")
    assert p["service"] in _VALID_SERVICES, f"unexpected service for: {line!r}"
    assert "message" in p
    # Real captured lines all carry a valid <PRI> envelope, so none should be
    # bucketed as the parse-failure fallback.
    assert p["parse_failed"] == "0", f"unexpected parse failure for: {line!r}"
