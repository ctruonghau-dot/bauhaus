#!/bin/bash
cd "$(dirname "$0")"
echo ""
echo "  正在启动 BAUHAUS AI …"
exec /usr/bin/env python3 server.py
