#!/usr/bin/env python3
"""Healthcheck de contenedor: GET https://127.0.0.1:<puerto>/health verificando contra la CA."""
import json
import ssl
import sys
import urllib.request

port, ca = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "/pki/ca.pem"
ctx = ssl.create_default_context(cafile=ca)
ctx.check_hostname = False  # el healthcheck consulta 127.0.0.1; la CA sigue verificándose
try:
    with urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=ctx, timeout=3) as r:
        sys.exit(0 if json.load(r).get("ok") else 1)
except Exception as e:  # noqa: BLE001
    print(e, file=sys.stderr)
    sys.exit(1)
