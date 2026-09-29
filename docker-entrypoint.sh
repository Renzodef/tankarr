#!/bin/sh
# Run Tankarr as the "tankarr" user, remapped to PUID/PGID when the container
# starts as root: the convention of the LinuxServer and hotio images the other
# *arr applications ship with. A container started with `--user` (or `user:`
# in Compose) never reaches the root branch and simply runs as that user.
set -eu

umask "${UMASK:-022}"

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    case "$PUID$PGID" in
        *[!0-9]*) echo "PUID and PGID must be numbers" >&2; exit 1 ;;
    esac
    if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
        echo "PUID and PGID must name a regular user: Tankarr never runs as root" >&2
        exit 1
    fi
    if [ "$(id -g tankarr)" != "$PGID" ]; then
        groupmod -o -g "$PGID" tankarr
    fi
    if [ "$(id -u tankarr)" != "$PUID" ]; then
        usermod -o -u "$PUID" tankarr
    fi
    # Only Tankarr's own data. The library, imports and downloads belong to
    # the operator and can be huge; their permissions are theirs to set.
    for directory in /config /home/tankarr; do
        if [ -d "$directory" ] && [ "$(stat -c %u:%g "$directory")" != "$PUID:$PGID" ]; then
            chown -R tankarr:tankarr "$directory" || true
        fi
    done
    export HOME=/home/tankarr
    exec setpriv --reuid=tankarr --regid=tankarr --init-groups "$@"
fi

# Started with --user: the uid may not exist in /etc/passwd, and HOME then
# points at a folder it cannot write. The data folder is the one it owns.
if [ ! -w "${HOME:-/}" ] && [ -w /config ]; then
    export HOME=/config
fi
exec "$@"
