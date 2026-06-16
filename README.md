# Enterprise V2Ray Proxy

单镜像企业代理节点，包含 V2Ray Core、LDAP 登录、SQLite 用户映射、定时离职清理和 Clash 订阅服务。

## 快速启动

```bash
docker compose up -d --build
```

访问 `http://localhost:8080`，使用 LDAP 账号登录。登录不会创建代理 UUID，只有点击“生成订阅 URL 和二维码”后才会创建 UUID、写入 `/etc/v2ray/config.json` 并向 `v2ray` 发送 `SIGHUP`。

管理员后台入口：

```text
http://localhost:8080/admin/login
```

默认账号是 `admin2`，默认密码是 `Mdt123456!`，可通过 `config.yaml` 的 `admin.username` / `admin.password` 或环境变量覆盖。

## 配置

应用只读取 `config.yaml`。示例配置内支持 `${ENV_NAME:-default}` 插值，敏感值建议通过 `docker-compose.yml` 的 environment 注入。

LDAP 配置对应字段：

- `ldap.server_uri`: LDAP 地址，例如 `ldap://devops.matlas.cn:389`
- `ldap.bind_dn`: 绑定 DN，例如 `cn=admin,dc=idatatlas,dc=com`
- `ldap.bind_password`: 绑定密码
- `ldap.user_ou`: 用户 OU 或搜索根，例如 `dc=idatatlas,dc=com`
- `ldap.user_filter`: 用户过滤器，例如 `(cn=%(user)s)`
- `ldap.attributes`: 用户属性映射，默认 `username=cn`、`name=sn`、`email=mail`

V2Ray、订阅服务端口、Gunicorn worker/thread、LDAP 同步 cron 表达式也都在 `config.yaml` 中维护。supervisord 只负责进程管理，启动参数由 Python launcher 从同一份配置读取。

`subscription.public_base_url` 和 `subscription.public_host` 默认是 `auto`：

- 订阅 URL 会从请求头 `X-Forwarded-Proto`、`X-Forwarded-Host`、`Host` 推导。
- Clash/VLESS 节点的 server 会从 `X-Forwarded-Host` 或 `Host` 去掉端口后推导。
- `subscription.public_port` 仍需要配置为 VLESS 对外端口，例如 `10086`。

Nginx 反代建议：

```nginx
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Host $http_host;
proxy_set_header X-Forwarded-Proto $scheme;
```

Clash 规则由后端下发，配置在 `subscription.routing`：

```yaml
subscription:
  routing:
    mode: "cn_direct"
    custom_rules: []
```

内置模式：

- `cn_direct`: 内网和中国大陆 IP 直连，其余走 `Proxy`
- `global_proxy`: 全部走 `Proxy`
- `direct`: 全部直连
- `custom`: 使用 `custom_rules` 原样下发

自定义示例：

```yaml
subscription:
  routing:
    mode: "custom"
    custom_rules:
      - "GEOSITE,CN,DIRECT"
      - "GEOIP,CN,DIRECT"
      - "MATCH,Proxy"
```

## 持久化目录

`docker-compose.yml` 默认挂载：

- `./data:/data`: SQLite 数据库 `users.db`
- `./runtime/v2ray:/etc/v2ray`: 生成的 V2Ray 配置
- `./config.yaml:/app/config.yaml:ro`: 应用配置中心

## 进程

容器内由 `supervisord` 管理：

- `v2ray`: `/usr/bin/v2ray run -c /etc/v2ray/config.json`
- `subscription`: Flask 订阅服务
- `cron`: 每 5 分钟执行一次 LDAP active 用户清理

## 离职清理

定时任务会遍历 SQLite 中 `active` LDAP 用户，逐个在 LDAP 中搜索。搜索不到时标记为 `revoked`，有变更才重新生成 clients，并重启 V2Ray 以立即断开已撤权用户的现有连接：

```bash
pkill -TERM v2ray
```

LDAP 连接或查询异常会让同步任务失败退出，不会把临时 LDAP 故障误判为用户离职。
