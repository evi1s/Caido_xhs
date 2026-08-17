#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
token_fetcher.py — 通过 PAT 自动获取 Caido access token（Device Authorization Flow）
用法：python3 token_fetcher.py
说明：
  - PAT 从 config.yaml 读取（auth.pat），永久有效
  - 每次运行都会重新获取 access_token + refresh_token，写入 tokens.json
  - 监听器会在 token 到期前自动调用本脚本续期（auto_renew: true）
"""
import json
import os
import time
import urllib.parse
import urllib.request

import yaml
import websocket

# ---- 读取配置 ----
CONFIG_PATH = os.environ.get("CONFIG_FILE", "/app/config.yaml")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}

PAT = CFG.get("auth", {}).get("pat", "")
INSTANCE = CFG.get("caido", {}).get("url", "http://127.0.0.1:8080")
TOKENS_FILE = CFG.get("auth", {}).get("tokens_file", "/app/tokens.json")
CLOUD = "https://api.caido.io"
GRAPHQL_URL = INSTANCE + "/graphql"
WS_URL = INSTANCE.replace("http", "ws") + "/ws/graphql"

if not PAT:
    print("ERROR: config.yaml 中 auth.pat 为空，请填写 Caido Personal Access Token")
    raise SystemExit(1)

START_FLOW = """mutation StartAuthFlow {
  startAuthenticationFlow {
    request { id userCode }
    error { ... on AuthenticationUserError { code reason } }
  }
}"""

AUTH_TOKEN_SUB = """subscription CreatedAuthToken($requestId: ID!) {
  createdAuthenticationToken(requestId: $requestId) {
    token { accessToken expiresAt refreshToken scopes }
    error { ... on AuthenticationUserError { code reason } }
  }
}"""


def http_graphql(query, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers=headers)
    r = urllib.request.urlopen(req, timeout=15)
    d = json.loads(r.read().decode())
    if d.get("errors"):
        raise RuntimeError("GraphQL errors: " + json.dumps(d["errors"])[:300])
    return d.get("data") or {}


def cloud_req(method, path, query_params):
    url = CLOUD + path + "?" + urllib.parse.urlencode(query_params)
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": "Bearer " + PAT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def ws_subscribe_token(request_id, timeout_s=20):
    """用 websocket-client 手动实现 graphql-transport-ws，订阅 createdAuthenticationToken"""
    ws = websocket.create_connection(WS_URL, timeout=timeout_s,
                                     header=["Sec-WebSocket-Protocol: graphql-transport-ws"])
    try:
        ws.send(json.dumps({"type": "connection_init", "payload": {}}))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("type") == "connection_ack":
                ws.send(json.dumps({
                    "type": "subscribe",
                    "id": "1",
                    "payload": {
                        "query": AUTH_TOKEN_SUB,
                        "variables": {"requestId": request_id},
                    },
                }))
            elif msg.get("type") == "next":
                data = msg.get("payload", {}).get("data", {}) or {}
                auth = data.get("createdAuthenticationToken") or {}
                if auth.get("token"):
                    return auth["token"]
                if auth.get("error"):
                    return {"error": auth["error"]}
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


def main():
    for attempt in range(1, 8):
        try:
            print(f"[try {attempt}] start flow...")
            data = http_graphql(START_FLOW)
            auth_flow = data.get("startAuthenticationFlow") or {}
            req = auth_flow.get("request") or {}
            request_id = req.get("id")
            user_code = req.get("userCode", "")
            if not request_id:
                print("start flow error:", json.dumps(auth_flow.get("error", {}))[:200])
                time.sleep(3)
                continue

            print(f"[try {attempt}] approve (user_code={user_code})...")
            status, resp = cloud_req(
                "POST",
                "/api/instances/device-authorization/approve",
                {"request_id": request_id},
            )
            print(f"[try {attempt}] approve status={status}")
            if status != 200:
                print("approve resp:", json.dumps(resp)[:300])
                time.sleep(3)
                continue

            print(f"[try {attempt}] waiting for auth token...")
            token = ws_subscribe_token(request_id)
            if token and token.get("accessToken"):
                now = int(time.time())
                out = {
                    "access_token": token["accessToken"],
                    "refresh_token": token.get("refreshToken", ""),
                    "expires_at": token.get("expiresAt", ""),
                    "fetched_at": datetime_now_iso(),
                }
                with open(TOKENS_FILE, "w") as f:
                    json.dump(out, f, indent=2)
                print(f"SUCCESS: token saved to {TOKENS_FILE}, expires: {out['expires_at']}")
                return
            print("[try %d] token not received yet, retry..." % attempt)
        except Exception as e:
            print(f"[try {attempt}] error: {str(e)[:200]}")
        time.sleep(3)
    print("FAILED: could not get token after retries")
    raise SystemExit(1)


def datetime_now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
