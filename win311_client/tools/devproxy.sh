#!/usr/bin/env bash
# Bring up a proxy for Windows-client development: the repo's mock LLM
# feeding a real proxy on a scratch port, with a scratch data dir.
#
# No VICE, no emulator, no GPU - this is the loop for working on the
# client itself. Ctrl-C stops both.
#
#   ./tools/devproxy.sh [port] [bind-address]
#
# Then, in another shell:   make run PORT=<port>
#
# The bind address is there for VMs and real machines. A QEMU guest on
# user-mode networking reaches the host at 10.0.2.2, and slirp rewrites
# that to the host's loopback, so the 127.0.0.1 default is enough. A
# bridged VM, a second box on the LAN, or a real 3.11 machine is not on
# the loopback and needs 0.0.0.0 - at which point the proxy is exposed
# to the whole network, which is why it is not the default.

set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
port="${1:-6410}"
bind="${2:-127.0.0.1}"
art="$here/../build/dev"
mkdir -p "$art/data"

# The proxy's venv is gitignored, so a fresh worktree does not have one.
# Borrow the main checkout's interpreter in that case - the code under
# test still comes from this tree, only the dependencies are shared.
py="$repo/llm64_proxy/.venv/bin/python"
if [ ! -x "$py" ]; then
    main="$(git -C "$repo" worktree list --porcelain 2>/dev/null \
            | awk '/^worktree /{print $2; exit}')"
    [ -n "${main:-}" ] && py="$main/llm64_proxy/.venv/bin/python"
fi
[ -x "$py" ] || py="$(command -v python3)"

mock_port=$(python3 - <<'EOF'
import socket
s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()
EOF
)

echo "mock LLM on :$mock_port"
python3 "$repo/emu/mock_llm.py" --port "$mock_port" >"$art/mock_llm.log" 2>&1 &
mock_pid=$!
trap 'kill $mock_pid $proxy_pid 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 50); do
    (echo >/dev/tcp/127.0.0.1/"$mock_port") >/dev/null 2>&1 && break
    sleep 0.1
done

echo "proxy on $bind:$port  (data in $art/data)"
cd "$repo/llm64_proxy"
OPENAI_API_KEY=mock-key \
OPENAI_API_BASE="http://127.0.0.1:$mock_port/v1" \
OPENAI_MODEL=mock \
LLM64_DATA_DIR="$art/data" \
LLM64_CARDS_DIR="$repo/emu/fixtures" \
LLM64_IMG_FIXTURE="$repo/emu/fixtures/scene.png" \
LLM64_PRINTER_BACKEND=c64 \
    "$py" -m src.main --host "$bind" --port "$port" -v \
    2>&1 | tee "$art/proxy.log" &
proxy_pid=$!

wait $proxy_pid
