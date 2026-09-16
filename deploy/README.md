# Deploying reachy-bridge always-on

Goal: the bridge runs 24/7 and restarts itself (and the tunnel) after
reboots and crashes. Pick **one** host — your Mac, a Mac mini, a Raspberry
Pi, anything that's always on and can reach the robot.

Because the robot is on your Tailscale network, the bridge host doesn't even
need to be on the robot's LAN: set `BRIDGE_MINI_URL` to the robot's Tailscale
IP (e.g. `http://100.64.0.12:8000/api` — find it with `tailscale status`).
Stable, no mDNS flakiness.

## 0. One-time setup (if you haven't)

```bash
git clone https://github.com/sgosselin/muse-reachy-bridge.git
cd muse-reachy-bridge
./scripts/setup.sh     # paste your agent's public key when asked
```

## 1. Install the services

Replace `/path/to/muse-reachy-bridge` with your checkout path everywhere below.

### macOS (launchd)

```bash
cd muse-reachy-bridge
sed -i '' 's|/path/to/muse-reachy-bridge|'"$PWD"'|g' deploy/launchd/*.plist
# check the tailscale binary path first:
which tailscale   # if it's not /usr/local/bin/tailscale, fix it in the funnel plist
cp deploy/launchd/com.reachy.bridge.plist ~/Library/LaunchAgents/
cp deploy/launchd/com.reachy.bridge.funnel.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.reachy.bridge.plist
launchctl load -w ~/Library/LaunchAgents/com.reachy.bridge.funnel.plist
```

Also stop the Mac from sleeping: System Settings → Energy → "Prevent automatic
sleeping when the display is off" (on a laptop, keep it plugged in). Or run
this on a Mac mini / Pi instead.

### Linux / Raspberry Pi (systemd)

```bash
cd muse-reachy-bridge
sudo sed 's|/path/to/muse-reachy-bridge|'"$PWD"'|g' \
  deploy/systemd/reachy-bridge.service > /etc/systemd/system/reachy-bridge.service
sudo cp deploy/systemd/reachy-bridge-funnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reachy-bridge reachy-bridge-funnel
```

## 2. Get the public URL

```bash
tailscale funnel status    # shows your https://<...>.ts.net URL
```

If `funnel` errors about permissions, enable it in the Tailscale admin
console for your tailnet, then reload the funnel service.

## 3. Verify from the outside

From your phone (off Wi-Fi) or ask your agent:

```bash
python client/bridge_client.py --url https://<your-funnel-url> \
  --key ~/.ssh/your_bridge_key health
```

You should see `"ok": true` and your robot type. Then try the panel at
`https://<your-funnel-url>/panel` with the panel token from `.env`.

## 4. Day-to-day

- **Update:** `git pull` in the checkout, then restart the service
  (`launchctl kickstart -k gui/$UID/com.reachy.bridge` on macOS,
  `sudo systemctl restart reachy-bridge` on Linux).
- **Logs:** `bridge.log` / `funnel.log` in the checkout dir.
- **Audit:** `audit.log` records every API call with the caller id — skim it
  after remote sessions. Rotate it occasionally; it's gitignored.
- **Revoke an agent:** `./scripts/add-client.sh` to rotate,
  `rm clients/<name>.pub` + service restart to revoke.
- **Standing down:** unload/disable the funnel service when you don't want
  the robot reachable; the bridge keeps running locally.
