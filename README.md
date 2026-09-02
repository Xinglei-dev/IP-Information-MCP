# IP-Info MCP Server

基于 db-ip Lite 免费数据库的 MCP Server，通过 Streamable HTTP 对外提供离线 IP 地理定位与 ASN 查询。数据库按需在线更新，进程不需要重启。

> 本仓库不包含 DB-IP 数据库文件，也不包含任何 IP 查询结果。公开部署仅支持 Docker。

## 数据与授权

本项目使用 [DB-IP Lite](https://db-ip.com/db/lite.php) 免费数据库，数据按
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 授权分发。使用本服务或派生数据时需保留对 DB-IP 的署名：

> IP Geolocation by [DB-IP](https://db-ip.com)

## MCP 工具

| 工具 | 说明 |
|---|---|
| `query_ip` | 查询单个 IPv4/IPv6，返回国家、地区、城市、经纬度、ASN、AS 组织 |
| `batch_query_ips` | 批量查询，最多 100 个 IP，按输入顺序返回 |
| `update_databases` | 检查 db-ip 最新月版；有新版时后台下载并原子切换 |
| `get_update_status` | 查看数据库版本、更新状态和最近一次更新结果 |

查询样例：

```json
{
  "ok": true,
  "data": {
    "ip": "8.8.8.8",
    "country": "United States",
    "country_code": "US",
    "region": "California",
    "city": "Mountain View",
    "latitude": 37.422,
    "longitude": -122.085,
    "asn": 15169,
    "as_organization": "Google LLC",
    "is_eu": false
  }
}
```

更新期间 `query_ip` 与 `batch_query_ips` 会返回 `数据库正在更新，请稍后再试`。

## 构建与启动

数据库文件**不进入镜像**。镜像只包含代码和依赖，Country/City/ASN 三个 MMDB 位于挂载目录 `/data/dbip`。

固定文件名：

```text
dbip-country-lite.mmdb
dbip-city-lite.mmdb
dbip-asn-lite.mmdb
```

### 首次启动

镜像内 `/data/dbip` 是空卷时，容器会在打开 MCP 端口前先连接 db-ip 下载三个最新数据库并完成校验。任何一步失败都会使容器以非零状态退出，方便编排层重试。

推荐使用 Docker named volume，首次运行会自动初始化：

```bash
docker build -t ip-info-mcp .

docker volume create ip-info-data

docker run -d --name ip-info-mcp \
  -p 8010:8010 \
  -v ip-info-data:/data/dbip \
  -e MCP_AUTH_TOKEN='replace-with-a-long-token' \
  ip-info-mcp
```

如果使用宿主机目录挂载，需要让容器内用户（uid/gid `10001`）对该目录有写权限：

```bash
mkdir -p /srv/ip-info/dbip
sudo chown 10001:10001 /srv/ip-info/dbip

docker run -d --name ip-info-mcp \
  -p 8010:8010 \
  -v /srv/ip-info/dbip:/data/dbip \
  -e MCP_AUTH_TOKEN='replace-with-a-long-token' \
  ip-info-mcp
```

### 运行期更新

不重建镜像、不重启容器。任意 MCP 客户端调用 `update_databases` 后，服务会：

1. 检查 db-ip 最新月版；
2. 有新版则后台下载三个 `.gz` 文件；
3. 解压并用 `maxminddb` 校验；
4. 全部通过后原子替换数据文件并切换查询句柄。

更新期间查询工具返回“正在更新”，旧库在失败时继续可用。

## MCP 客户端接入

- Endpoint: `http://<host>:8010/mcp`
- Header: `Authorization: Bearer <MCP_AUTH_TOKEN>`

通用配置示例：

```yaml
servers:
  ip-info:
    url: http://127.0.0.1:8010/mcp
    headers:
      Authorization: Bearer <MCP_AUTH_TOKEN>
```

本机开发调试不通过直接运行源码对外提供；如需内部调试，请在 `data/dbip` 准备私有 MMDB 数据后自行运行测试。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_AUTH_TOKEN` | 无 | 必填。所有 MCP 请求的 Bearer Token |
| `DATA_DIR` | `/data/dbip` | 三个 MMDB 所在目录 |
| `MCP_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `MCP_PORT` | `8010` | HTTP 监听端口 |
| `MCP_PATH` | `/mcp` | Streamable HTTP 路径 |

## 开发测试

公开仓库不包含 MMDB 测试数据。`tests/test_client.py` 会在缺少本地私有数据时自动跳过，其他测试不依赖网络。

内部开发环境准备数据后，可在项目根目录运行：

```bash
python -m unittest tests.test_client tests.test_updater -v
```
