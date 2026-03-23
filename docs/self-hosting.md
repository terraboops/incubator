# Self-hosting

## Daemon mode

The simplest way to run trellis in the background:

```bash
trellis serve --background
```

This spawns a detached process and writes the PID to `pool/trellis.pid`.
Logs go to `pool/trellis.log`.

To stop:

```bash
trellis serve --stop
```

## macOS (launchd)

Create `~/Library/LaunchAgents/com.trellis.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.trellis</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/trellis</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/your/project</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/path/to/your/project/pool/trellis.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/your/project/pool/trellis.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.trellis.plist
```

## Linux (systemd)

Create `/etc/systemd/system/trellis.service`:

```ini
[Unit]
Description=Trellis Pipeline
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/your/project
ExecStart=/path/to/venv/bin/trellis serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now trellis
```

## Reverse proxy (nginx)

To put trellis behind nginx with TLS:

```nginx
server {
    listen 443 ssl;
    server_name trellis.example.com;

    ssl_certificate     /etc/letsencrypt/live/trellis.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trellis.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Set `WEB_HOST=127.0.0.1` in your `.env` so the dashboard only listens on
localhost when behind a proxy.
