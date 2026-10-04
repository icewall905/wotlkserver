#!/bin/bash
# Send a console command to the AzerothCore worldserver and print its reply.
# Prefers SOAP (credentials from .env); falls back to attaching to the console.
CMD="$*"
if [ -z "$CMD" ]; then
    echo "Usage: $0 '<command>'"
    echo "Example: $0 'account create myuser mypass'"
    exit 1
fi
CMD="${CMD#.}"
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }

if [ -n "$SOAP_USER" ]; then
    ESCAPED=$(printf '%s' "$CMD" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
    REPLY=$(docker exec -i ac-manager python3 -c '
import sys, requests, re, html
r = requests.post("http://ac-worldserver:7878/", auth=(sys.argv[1], sys.argv[2]), timeout=15,
    data=("<?xml version=\"1.0\"?><SOAP-ENV:Envelope xmlns:SOAP-ENV=\"http://schemas.xmlsoap.org/soap/envelope/\" xmlns:ns1=\"urn:AC\">"
          "<SOAP-ENV:Body><ns1:executeCommand><command>" + sys.argv[3] + "</command></ns1:executeCommand></SOAP-ENV:Body></SOAP-ENV:Envelope>"))
m = re.search(r"<result>(.*?)</result>|<faultstring>(.*?)</faultstring>", r.text, re.S)
print(html.unescape((m.group(1) if m.group(1) is not None else m.group(2)) if m else r.text).strip())
' "$SOAP_USER" "$SOAP_PASS" "$ESCAPED" 2>/dev/null) && { printf "%s\n" "$REPLY" | tr -d "\r"; exit 0; }
fi

# Fallback: type into the console through a pseudo-terminal, then detach (Ctrl-P Ctrl-Q).
{ sleep 1; printf '%s\r' "$CMD"; sleep 2; printf '\x10\x11'; sleep 1; } \
    | timeout 15 script -qfc "docker attach --sig-proxy=false ac-worldserver" /dev/null \
    | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -d '\r' | grep -av '^AC> *$' | tail -n +2
