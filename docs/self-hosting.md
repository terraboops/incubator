# Self-hosting

## Daemon mode

The simplest way to run incubator in the background:

```bash
incubator serve --background
```

This spawns a detached process and writes the PID to `pool/incubator.pid`.
Logs go to `pool/incubator.log`.

To stop:

```bash
incubator serve --stop
```

## macOS (launchd)

Create `~/Library/LaunchAgents/com.incubator.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.incubator</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/incubator</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/your/project</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/path/to/your/project/pool/incubator.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/your/project/pool/incubator.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.incubator.plist
```

## Linux (systemd)

Create `/etc/systemd/system/incubator.service`:

```ini
[Unit]
Description=Incubator Pipeline
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/your/project
ExecStart=/path/to/venv/bin/incubator serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now incubator
```

## Reverse proxy (nginx)

To put incubator behind nginx with TLS:

```nginx
server {
    listen 443 ssl;
    server_name incubator.example.com;

    ssl_certificate     /etc/letsencrypt/live/incubator.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/incubator.example.com/privkey.pem;

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
