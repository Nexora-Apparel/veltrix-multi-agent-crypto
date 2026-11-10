# Cloudflare Tunnel origin for the VELTRIX dashboard API

This deploys the Python dashboard API on a persistent host while keeping its
listener bound to `127.0.0.1:8765`. `cloudflared` creates outbound tunnel
connections; no inbound API port is opened on the host. The public API
hostname is protected by a Cloudflare Access **Service Auth** policy. The
Cloudflare Worker authenticates to that hostname with an Access service token
and independently supplies the VELTRIX dashboard bearer token.

This is a host deployment guide, not evidence that a tunnel, hostname, Access
application or production host has already been provisioned.

## 1. Create the named tunnel

In the Cloudflare dashboard:

1. Open **Networking → Tunnels** and create a named tunnel, for example
   `veltrix-api-origin`.
2. Install the current `cloudflared` package on the persistent Python API
   host using Cloudflare's Linux instructions.
3. Download the tunnel credentials JSON for the created tunnel. Record its
   UUID and install the file as
   `/etc/cloudflared/<TUNNEL_UUID>.json`.
4. Create a dedicated service identity and lock down the credentials:

   ```sh
   useradd --system --home /var/lib/cloudflared --shell /usr/sbin/nologin cloudflared
   install -d -o root -g cloudflared -m 0750 /etc/cloudflared
   chown root:cloudflared /etc/cloudflared/<TUNNEL_UUID>.json
   chmod 0640 /etc/cloudflared/<TUNNEL_UUID>.json
   ```

Do not put the tunnel credentials in the repository, a dashboard asset, or a
browser environment.

## 2. Publish only the loopback API

Copy `deployment/cloudflared/config.yml.example` to
`/etc/cloudflared/config.yml`. Replace the tunnel UUID and hostname with the
real values. Keep the origin service as `http://127.0.0.1:8765`; the Python
dashboard service must remain loopback-bound. Keep the final catch-all
`http_status:404` ingress rule.

Validate the ingress rules before enabling the service:

```sh
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
```

Install and start the hardened service:

```sh
install -m 0644 deployment/systemd/cloudflared-veltrix.service /etc/systemd/system/cloudflared-veltrix.service
systemctl daemon-reload
systemctl enable --now cloudflared-veltrix.service
systemctl status cloudflared-veltrix.service
journalctl -u cloudflared-veltrix.service -f
```

Create the public hostname route in the Cloudflare tunnel dashboard, for
example `api.example.com`, mapped to this tunnel. Do not open port 8765 on
the host firewall or bind the Python API to a public interface.

## 3. Protect the API hostname with Access Service Auth

Create a Cloudflare Access self-hosted application for the API hostname and
add a policy whose action is **Service Auth**. Create a dedicated Access
service token for the Worker-to-origin request. Do not reuse a human login
session or the dashboard visitor's credentials.

Add these repository secrets for the guarded Worker deployment workflow:

- `CF_ACCESS_CLIENT_ID`: the Access service token Client ID.
- `CF_ACCESS_CLIENT_SECRET`: the Access service token Client Secret.
- `VELTRIX_API_ORIGIN`: the bare HTTPS origin, e.g.
  `https://api.example.com`, without a path.
- `VELTRIX_DASHBOARD_TOKEN`: the strong bearer token configured as
  `VELTRIX_DASHBOARD_TOKEN` in the Python dashboard service.
- `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`: scoped Worker deploy
  credentials.

The Worker injects both the Access service-token headers and the VELTRIX
bearer token server-side. The browser receives neither credential. The Python
API still checks its bearer token using constant-time comparison. Access
service-token authentication is an additional origin boundary, not a
replacement for the application bearer token.

Use a separate Access application/policy for human dashboard visitors. That
policy should require the intended user identity; do not grant all visitors
the origin's Service Auth policy.

## 4. Verify the complete origin path

From a trusted operator environment, check the API with both credentials:

```sh
curl --fail --silent --show-error \
  -H "Authorization: Bearer $VELTRIX_DASHBOARD_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  https://api.example.com/api/health
```

The guarded Worker deployment workflow performs the same authenticated
preflight before it deploys. It additionally requires a connected read-only
Turso binding, complete paper schema, live execution disabled, a fresh
healthy pipeline heartbeat and a successful candle refresh within five
minutes.

Verify that requests without either credential are rejected by the relevant
layer, the API listener is still loopback-only, the tunnel is healthy, and the
dashboard hostname separately requires human Access authentication.

## 5. Security and recovery constraints

- Keep `TURSO_AUTH_TOKEN` (pipeline write access) separate from the mandatory
  `TURSO_DASHBOARD_AUTH_TOKEN` (provider-issued read-only access).
- Keep `LIVE_TRADING=false`; the tunnel is an API transport, not an execution
  authorization path.
- The tunnel service account must not join the database-write group or read
  pipeline secrets.
- Keep the API and dashboard services loopback-bound. The tunnel is the only
  public path to the API hostname.
- Monitor `cloudflared-veltrix.service` and the Python pipeline/dashboard
  units independently. A healthy tunnel does not prove a healthy worker.
- Never use `noTLSVerify` as a permanent workaround for an HTTPS origin.

References:
- Cloudflare Tunnel configuration and ingress rules:
  https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/
- Run Cloudflare Tunnel as a Linux service:
  https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/
- Cloudflare Access service-token request headers:
  https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/
