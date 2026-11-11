#!/bin/sh
# Codex 登录状态由管理员通过 codexctl 统一管理，禁止用户自行 login / logout。
# 注意：logout 会在服务端吊销同一账号所有用户共用的 token。
REAL="${CODEX_REAL:-/opt/codex/bin/codex}"
case "$1" in
  login)
    if [ "$2" = "status" ]; then
      exec "$REAL" "$@"
    fi
    echo "Codex 账号由管理员统一分配，请勿自行 login。如需帮助请联系管理员。" >&2
    exit 1
    ;;
  logout)
    echo "Codex 账号由管理员统一分配，请勿自行 logout（会导致同账号所有用户掉线）。" >&2
    exit 1
    ;;
esac
exec "$REAL" "$@"
