# 部署到 VPS

> 目标域名：`cut.holyhbc.eu.org`（已解析到 64.110.73.90，VPS 已有 nginx）
> 适用：`webapp/` 扫码报工服务
> 最后更新：2026-10-09

---

## 0. 架构与前提

```
局域网 (192.168.0.x)                      VPS (64.110.73.90)
┌──────────────────────────┐            ┌─────────────────────────┐
│ hbc-print 打印标签        │            │ nginx :443 (你已有)     │
│   生成含二维码的标签       │            │   └─> scanapp :10080     │
│                          │            │                         │
│ sync_snapshot.py 定时运行  │──HTTPS───>│ app.py                  │
│   读 SQL Server(只读)     │  POST      │   └─> SQLite /root/cutting-app/webapp│
└──────────────────────────┘  /api/     │       /scan.db          │
                              snapshot  │                         │
                                         └─────────────────────────┘
   工人用微信扫标签 -> 直连 VPS 扫码页 -> 报工暂存本地 SQLite
                                        （阶段2 再由局域网同步回 SQL Server）
```

**关键原则（D-009）**：VPS **不连** SQL Server。所有数据由局域网电脑主动出站推送。

**已实测**：全量快照 22.74 MB / 23.7 万行，推送耗时 **7.4 秒**，落地后库 28 MB。

---

## 1. VPS 上传代码

在 VPS 上执行（**实际部署路径就是 `/root/cutting-app/webapp`**）：

```bash
sudo mkdir -p /root/cutting-app/webapp
sudo chown $USER:$USER /root/cutting-app/webapp

# 方式一：git（若 VPS 上有这份仓库）
cd /root/cutting-app/webapp
git clone <你的仓库地址> .

# 方式二：直接拷贝 webapp 目录（只有 4 个 .py + 1 个 .sql，不需要仓库）
scp -r webapp/{app.py,db.py,rules.py,schema.sql,sync_snapshot.py} user@64.110.73.90:/root/cutting-app/webapp/
```

> 只依赖 Python 3 标准库，**不需要 pip install 任何东西**。

确认：
```bash
cd /root/cutting-app/webapp && python3 -c "import app; print('OK')"
```

---

## 2. 生成同步令牌

```bash
# 在 VPS 上生成，保存到权限 600 的文件
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | tee /root/cutting-app/webapp/.sync_token
chmod 600 /root/cutting-app/webapp/.sync_token
```

⚠️ 这个令牌**局域网电脑推送时也要用**，妥善保存，别提交进 git。

---
Zv1ASD8Qx5-Mmx3cuWhsfzReamlsrCGba02ZQlc1mqs
## 3. systemd 服务

```bash
sudo tee /etc/systemd/system/scanapp.service > /dev/null <<'EOF'
[Unit]
Description=扫码报工服务
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/root/cutting-app/webapp
Environment=PORT=10080
Environment=SCAN_DB=/root/cutting-app/webapp/scan.db
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /root/cutting-app/webapp/app.py
Restart=always
RestartSec=5
# 令牌从文件读，避免出现在 ps 输出里
EnvironmentFile=-/root/cutting-app/webapp/.env

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/root/cutting-app/webapp

[Install]
WantedBy=multi-user.target
EOF
```

把令牌写进 `.env`（systemd 的 `EnvironmentFile` 只认 `KEY=VALUE`）：

```bash
echo "SYNC_TOKEN=$(cat /root/cutting-app/webapp/.sync_token)" | sudo tee /root/cutting-app/webapp/.env
sudo chmod 600 /root/cutting-app/webapp/.env
```

### 主管白名单 `SUPERVISORS`（D-007）

`ygzl` 没有可靠的职务字段，主管身份只能靠环境变量指定。
**不配这个变量，所有人都只是普通工人**（看不到改量审批页和全员工资）。

```bash
echo "SUPERVISORS=C001,A001,A003" | sudo tee -a /root/cutting-app/webapp/.env
sudo systemctl restart cutapp.service
```

验证（`worker_login` 应为 1）：

```bash
curl -s https://cut.holyhbc.eu.org/health
```

> ⚠️ 改了 `.env` **必须重启**才生效（systemd 只在启动时读一次）。

### 建一个登录账号

```bash
# 数据库路径会自动找脚本同级的 scan.db，不用手动指定
cd /root/cutting-app/webapp
python3 add_test_supervisor.py \
  --ygno C001 --name 测试主管 --phone 13844445555 --pin 5555
```

> ⚠️ 本项目实际部署路径是 `/root/cutting-app`（不是 `/root/cutting-app/webapp`）。
> 脚本按「脚本同级 → `/root/cutting-app/webapp` → `~/cutting-app/webapp`」顺序自动探测，
> 也可用 `--db-path` 或 `SCAN_DB` 环境变量强制指定。

脚本会检查花名册里有没有这个人，**没有就警告并告诉你先推快照**。
PIN 只存哈希+盐，不存明文。重复执行幂等。

> ⚠️ 手机号**不在**花名册同步范围内（`sync_employees` 只推 `ygno/ygname/ygout`），
> 手机号只存在本地 `worker_login` 表，所以这一步必须在 VPS 上执行。

> systemd 同一段里 `Environment` 和 `EnvironmentFile` 的先后顺序不影响生效，
> 但 `.env` 文件必须在服务**启动前**存在，否则环境变量读不到。

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scanapp
sudo systemctl status scanapp
journalctl -u scanapp -f        # 看日志
```

自检（应返回 `{"ok":true,...}`）：

```bash
curl -s http://127.0.0.1:10080/health
```

---

## 4. nginx 反向代理

在**已有的** server 块里加一个 location（如果已有 `cut.holyhbc.eu.org` 的 server 块，直接改它）：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name cut.holyhbc.eu.org;

    # certbot 会自动改成 301 跳转 HTTPS
    location /.well-known/acme-challenge/ { root /var/www/html; }

    location / {
        # ⚠️ 必须加：nginx 默认只允许 1m 请求体，
        # 而全量快照推送是 22.7MB，不加会返回 413 Request Entity Too Large。
        # 64m 与代码里的 MAX_SNAPSHOT_BYTES 一致。
        client_max_body_size 64m;

        proxy_pass http://127.0.0.1:10080;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 5. HTTPS 证书

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d cut.holyhbc.eu.org
# 按提示选： redirect（自动跳HTTPS）
sudo certbot renew --dry-run
```

**微信扫标签必须 HTTPS**，明文 HTTP 会被拦。

验证：
```bash
curl -sI https://cut.holyhbc.eu.org/health | head -1     # 期望 HTTP/2 200
curl -s  https://cut.holyhbc.eu.org/health               # 期望 {"ok":true,...}
```

> ⚠️ **排查时用 `curl -s`，别用 `curl -sI`**。
> `-I` 发的是 HEAD 请求。早期版本没实现 `do_HEAD`，
> `BaseHTTPRequestHandler` 会回 `501 Unsupported method ('HEAD')`，
> 很容易被误判成"服务没起来"或"nginx 配错了"。
> 2026-10-09 已补上 `do_HEAD`，现在两者状态码一致。

---

## 6. 防火墙

服务默认**只监听 `127.0.0.1:10080`**，由 nginx 代理对外提供 HTTPS。
`10080` 端口本身**不需要**、也**不应该**放行到公网：

```bash
sudo ufw status
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 10080/tcp      # 兜底，确保 10080 不对外
```

### 监听地址

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | **保持默认即可**，只有 nginx 需要访问它 |
| `PORT` | `10080` | 与 nginx 的 `proxy_pass` 保持一致 |

只有在**车间内网自测**、需要让手机直接连这台机器时，才设 `BIND_HOST=0.0.0.0`。
启动日志里会打出「⚠️ 警告：正监听所有网卡」，提醒你确认防火墙。

```bash
# 内网自测时才这样起
BIND_HOST=0.0.0.0 PORT=10080 python3 /root/cutting-app/webapp/app.py
```

systemd 配置里也建议显式写上，避免以后误改：

```ini
Environment=BIND_HOST=127.0.0.1
Environment=PORT=10080
```


---

## 7. 局域网电脑：推送快照

在**能连 SQL Server 的那台 Windows 电脑**上：

```bash
# 设置令牌（一次性，或写进系统环境变量）
setx SYNC_TOKEN "粘贴第2步生成的令牌"

# 首次全量推送（清空后重灌）
python webapp\sync_snapshot.py --push https://cut.holyhbc.eu.org --replace

# 之后按需推单个定单
python webapp\sync_snapshot.py --zdno "9951背心2024-1-19" --push https://cut.holyhbc.eu.org
```

**首次全量**大约 7~10 秒、22 MB。

### 定时自动推送（Windows 任务计划程序）

| 项 | 值 |
|---|---|
| 程序 | `pythonw.exe`（或 `python.exe`） |
| 参数 | `"G:\hbc\opencode\webapp\sync_snapshot.py" --push https://cut.holyhbc.eu.org` |
| 起始位置 | `G:\hbc\opencode\webapp` |
| 触发 | 每天 07:00（开工前） |
| 勾选 | 不论用户是否登录都运行 |

> 建议**只在开工前全量推一次**。数据量大但很快（7 秒），
> 没必要高频推；有变化时手动推指定定单即可。

---

## 7.1 排查：推送报 413

```
推送失败 HTTP 413: Request Entity Too Large
```

nginx 默认 `client_max_body_size` 只有 **1 MB**，全量快照是 **22.7 MB**，必被拦。
在 `location /` 里加 `client_max_body_size 64m;` 后 `nginx -t && systemctl reload nginx`。

（按单个定单推送时数据很小，不加也能过；但全量推送必须加。）

### 排查：推送报 401

`令牌不正确`。检查两处是否一致：
- VPS：`~/cutting-app/webapp/.env` 里的 `SYNC_TOKEN`（改完要 `systemctl restart`）
- 推送端：Windows 的 `SYNC_TOKEN`

Windows 侧注意：`set` 在 PowerShell 里是 `Set-Variable` 的别名，**不设环境变量**。
- PowerShell：`$env:SYNC_TOKEN = "xxx"`
- cmd：`set SYNC_TOKEN=xxx`
- 永久保存用 `setx`（但**只对新开的窗口生效**）

## 8. 打印标签：打开二维码

`hbc-print` 的条码配置文件里加三行：

```json
"label_qr_enabled": true,
"label_qr_size_mm": 13.0,
"label_qr_base_url": "https://cut.holyhbc.eu.org"
```

然后**先打样张、微信实扫**确认再正式印。

---

## 9. 部署后验收清单

```bash
# 1. 服务活着
curl -s https://cut.holyhbc.eu.org/health

# 2. 有数据（推送过之后）
curl -s https://cut.holyhbc.eu.org/health    # 看 scan_map / bundle 是否非 0

# 3. 某扎的扫码页能打开
curl -s https://cut.holyhbc.eu.org/s/<某个short_id> | head -5
```

- [ ] `/health` 返回 `ok:true`
- [ ] 局域网 `--push` 成功且 `scan_map` 非 0
- [ ] 打印样张，微信扫标签能打开页面
- [ ] 页面能选工序、填数量、提交成功
- [ ] 故意填超量，提示"超出剩余数量"
- [ ] 不存在的工号被拒
- [ ] `/admin/pending` 能看到刚提交的报工
- [ ] 断电重启后服务自动起来（`systemctl is-enabled scanapp`）

---

## 10. 运维备忘

| 操作 | 命令 |
|---|---|
| 看日志 | `sudo journalctl -u scanapp -f` |
| 重启 | `sudo systemctl restart scanapp` |
| 停止 | `sudo systemctl stop scanapp` |
| 备份库 | `sqlite3 /root/cutting-app/webapp/scan.db ".backup /root/cutting-app/webapp/scan-$(date +%F).db"` |
| 看库统计 | `curl -s https://cut.holyhbc.eu.org/health` |
| 看待同步 | 浏览器打开 `https://cut.holyhbc.eu.org/admin/pending` |

⚠️ **备份很重要**：`scan.db` 里存着工人已报但**还没同步回 SQL Server** 的数据
（阶段 2 之前）。丢了就得让工人重报。建议加个每日 cron：

```bash
sudo crontab -e
# 每天 02:00 备份，保留 14 天
0 2 * * * sqlite3 /root/cutting-app/webapp/scan.db ".backup /root/cutting-app/webapp/backup/scan-$(date +\%F).db" && find /root/cutting-app/webapp/backup -name '*.db' -mtime +14 -delete
```

---

## 11. 已知限制

- **报工只落在 VPS 本地 SQLite**，还没同步回 SQL Server 的 `jfgz`（阶段 2 的工作）
- **无断网兜底**：车间网络断了页面打不开就没法报工
- **跨车间定单**不出码（52/10295 个定单，0.5%），需阶段 1 补 CC 选择器
- **无鉴权**的扫码页：任何人都能扫标签报工，只能靠工号校验兜底
