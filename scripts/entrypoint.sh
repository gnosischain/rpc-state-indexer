#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    set -- daemon
fi

exec rpc-state-indexer "$@"
