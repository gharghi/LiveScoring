#!/usr/bin/env bash
#
# Forced command for the CI deploy key.  Installed at
# /usr/local/sbin/livescoring-deploy-ssh and referenced from
# /root/.ssh/authorized_keys as:
#
#   command="/usr/local/sbin/livescoring-deploy-ssh",no-agent-forwarding,\
#   no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA...
#
# The deploy key is stored in a GitHub secret, so it must not be able to do
# anything except deploy: if it leaks, the worst it buys is a redeploy of a
# branch that is already in the repo.  Everything else is refused here.
set -euo pipefail

cmd=${SSH_ORIGINAL_COMMAND:-}
ref=origin/main

if [ -n "$cmd" ]; then
    if [[ "$cmd" =~ deploy\.sh[[:space:]]+[\"\']?([A-Za-z0-9._/-]+)[\"\']?[[:space:]]*$ ]]; then
        ref="${BASH_REMATCH[1]}"
    else
        echo "refused: this key may only run the deploy script" >&2
        exit 1
    fi
fi

# Only refs that actually came from the remote; no local paths, no options.
case "$ref" in
    origin/*) ;;
    *) echo "refused: ref must be origin/<branch>, got '$ref'" >&2; exit 1 ;;
esac

exec /srv/livescoring/deploy/deploy.sh "$ref"
