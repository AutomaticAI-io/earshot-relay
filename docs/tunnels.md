# Tunnels: letting the phone reach the relay

Sending needs nothing special: your scripts talk to the relay, the relay talks to the bucket, the phone talks to the bucket. A tunnel matters for two things:

- **Receipts.** The phone must reach `https://<host>/v1/beacons/<token>` over HTTPS. The app refuses plain `http`.
- **Remote callers** (CI, a cloud agent, a remote MCP client) that must reach `POST /v1/messages` and `/mcp`.

If you only send from the same machine or LAN and do not need receipts, skip this page.

**Expose as little as you can.** Set `EARSHOT_RELAY_BEACON_PORT=8081` to serve only the beacon endpoint and `/healthz` on a second port, and point the phone's tunnel at that port. The API and MCP stay on the main port, on localhost. Once the beacon port is set, the main port no longer answers beacons, so the second port must be reachable from the host:

```sh
# docker run: publish both ports on loopback
docker run -d --name earshot-relay --restart unless-stopped --env-file .env \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 -v earshot-relay-data:/data ghcr.io/automaticai-io/earshot-relay
```

With compose, uncomment the `127.0.0.1:8081:8081` line in `compose.yaml`. If the tunnel points at a port that is not published, beacons fail without an error on the relay and every receipt stays `stored`, then `unconfirmed`.

## Tailscale (private to your tailnet)

The phone runs Tailscale too.

```sh
# beacon-only listener on 8081 (EARSHOT_RELAY_BEACON_PORT=8081)
tailscale serve --bg --https=443 localhost:8081
tailscale serve status        # shows https://<machine>.<tailnet>.ts.net
```

Set `EARSHOT_RELAY_PUBLIC_URL=https://<machine>.<tailnet>.ts.net`, run `earshot-relay status`, paste the beacon URL into the app. Tailscale terminates TLS with a real certificate. `tailscale funnel` makes it public; you do not need that for the phone, and for the API you should not use it without the bearer token.

## Cloudflare Tunnel (public hostname)

Use a named tunnel with a stable hostname that forwards to the beacon port:

```yaml
# config.yml for cloudflared
tunnel: <tunnel id>
credentials-file: <path to the credentials json>
ingress:
  - hostname: earshot-beacons.example.com
    service: http://localhost:8081
  - service: http_status:404
```

```sh
cloudflared tunnel route dns <tunnel name> earshot-beacons.example.com
cloudflared tunnel run <tunnel name>
```

Set `EARSHOT_RELAY_PUBLIC_URL=https://earshot-beacons.example.com`. Quick Tunnels (`cloudflared tunnel --url ...`) get a random hostname with no uptime guarantee; use them for a smoke test only, because the beacon URL you paste into the phone breaks when the hostname changes.

## Any HTTPS reverse proxy

Caddy, Traefik or nginx with a real certificate in front of the relay works. Forward to the beacon port. Plain `http` is not accepted by the app.

## What the tunnel sees

The beacon token is in the URL path, so it appears in the phone's settings and in your proxy's access logs. A leaked beacon URL can forge receipts, but it cannot send messages, because the API token is a different secret. If you suspect a leak, change `EARSHOT_RELAY_BEACON_TOKEN` and paste the new URL into the app.

## Without a tunnel

Everything except receipts keeps working. The phone never depends on beacons to play a message.
