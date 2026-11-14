# 岸站 AIS 可信航迹（离线分析）

FastAPI + SQLite 网页应用：海事数据员上传两处岸站收取的 AIS 定位 CSV 原档，
系统按船生成可信航迹、隔离不可信电文，并支持下载可信航迹与对应原档。
同秒异位的冲突秒在船舶页列出全部来源与原始行，数据员可**人工采信一条原始定位**
触发航迹重判；采信点仍须通过 60 节速度门限。原档与判定**同一批次同时发布**；
并发上传、写盘失败或服务重启后，页面与下载只会看到完整版本，失败批次不会覆盖
此前可用的航迹。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://127.0.0.1:8000
```

数据保存在命名卷 `ais-data` 中（`/data/ais.sqlite3` 与 `/data/originals/`）。
`./samples` 以只读方式挂载进容器为 `/samples`，便于在容器内直接取样例文件：

```bash
# 宿主机上传样例（两个批次，验证重投、冲突、迟到点）
curl -F 'file=@samples/batch1_base.csv' http://127.0.0.1:8000/upload
curl -F 'file=@samples/batch2_late_arrivals.csv' http://127.0.0.1:8000/upload
```

停止并保留数据：`docker compose down`；连同卷一起删除：`docker compose down -v`。

> 若本机没有 Docker（本开发环境就没有），可直接用 venv 运行，见下节；
> 两种方式使用完全相同的代码、环境变量与数据布局。

### 本地 venv

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt      # 运行
pip install -r requirements-dev.txt  # 另加测试依赖
uvicorn app.main:app --reload --port 8000
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIS_DATA_DIR` | `./data` | SQLite 与 `originals/` 根目录 |
| `AIS_MAX_UPLOAD_BYTES` | 67108864 (64 MiB) | 单次上传上限，超限直接拒绝且不改动航迹 |
| `AIS_PORT` | 8000 | 仅 Compose 使用的宿主端口 |

## 输入格式

CSV 至少包含五列（表头大小写/别名均可，支持 `station/shore_station`、`utc/utc_seconds`、
`lat/latitude`、`lon/lng/longitude`），分隔符自动识别 `, ; \t |`：

```csv
station,mmsi,utc,lat,lon
SZ01,413000001,1767225600,22.500000,114.200000
```

* `utc` 为 UTC 秒（Unix epoch，2000–2100 年之间）；
* `mmsi` 为纯数字；
* 纬/经度有限且在合法范围内，且不是 (0,0)；
* 坏行不会中断整批，而是逐行记录在批次页的“解析拒绝行”中。

## 判定规则（`app/ais.py`）

按 MMSI 全量重算，与批次到达顺序无关：

1. **同秒同位置（重投）**：同一船同一 UTC 秒内所有候选位置一致时，只保留一个
   可信候选（先到者胜），其余标记 `duplicate` 隔离。重投不会制造伪轨迹。
2. **同秒异位（冲突）**：该秒只要出现两个不同位置，**该秒所有记录**都标记
   `conflict` 隔离。
3. **速度门限**：候选按 UTC 排序，与**上一可信点**计算的航速超过 **60 节**即
   标记 `speed_exceeded` 隔离；被隔离的点不参与后续“上一可信点”的计算。
4. **迟到电文**：后到的早期记录会触发该船**全部已发布行**的重算，旧点的
   可信/隔离结论及后续速度基准都可能改变；每次重算生成新的船舶 `version`。
5. 位置按 6 位小数（约 0.11 m）比较，恰好覆盖同一次定位的重投抖动。

## 冲突秒人工采信复核（`app/store.py` / `app/ais.py`）

同秒异位时整秒默认隔离，数据员可在船舶页“冲突秒人工复核”区指定一条原始定位采信：

1. **页面展示完整集合**：每个冲突秒列出该秒**全部**原始行（岸站、批次、原档行号、
   坐标与当前判定），表单内嵌两个令牌——当前船舶 `version` 与该记录集合的
   `set_fingerprint`（全部 raw row id 排序后取 SHA-256）。
2. **旧页面提交被拒绝**：提交时若船舶版本已变（任何重算）或集合指纹已变
   （迟到电文增删该秒记录），返回 409 且**不写入任何内容**，要求刷新后重选。
3. **采信点仍过 60 节门限**：采信后该秒仅选中点进入速度门限计算，其余冲突行保持
   `conflict` 隔离。若选中点相对上一可信点超过 60 节，整个采信事务回滚
   （返回 409），采信记录不落库、隔离状态与航迹完全不变。
4. **迟到电文使旧采信自动失效**：重算时发现已采信秒的集合指纹变化，该采信自动
   置为 `pending_review`（原因 `set_changed`），航迹恢复整秒冲突隔离；采信点在
   重算中未过门限则置 `pending_review`（原因 `speed_exceeded`）。待复核状态在
   总览与船舶列表以“待复核”角标提示，可在船舶页按新集合重新采信。
5. **并发安全**：采信与上传都在单连接 `BEGIN IMMEDIATE` 事务内串行发布，读请求
   固定版本快照，因此不可能出现互相矛盾的判定与可信航迹；失败永远保留此前完整版本。
6. **重启保留**：采信记录持久化在 SQLite `adjudications` 表，重启后有效采信继续
   生效、待复核状态继续提示。同一冲突秒重复采信为 upsert，只保留最新一条。

## 发布与一致性（`app/store.py` / `app/db.py`）

* 原档先写入 `originals/.staging-*/` 并 `fsync`，再原子 `rename` 到
  `originals/<batch_id>/original.csv`，随后才在**单个 `BEGIN IMMEDIATE` 事务**
  中写入批次、原始行、拒绝行、每船判定与可信点并提交。
* 因此“已提交”即“原档已落盘且判定完整”；失败上传整体回滚并删除该目录，
  既有已发布航迹完全不变。
* 查询与下载在 `BEGIN`（WAL 快照）内固定船舶 `version`，并发重算不会让一次
  请求混入新旧两版数据；旧版本在事务内被裁剪。
* 启动时 `cleanup_staging` 清理崩溃遗留的 `.staging-*` 和无批次引用的目录，
  已发布批次永不删除。

## 页面与下载

| 路径 | 作用 |
| --- | --- |
| `GET /` | 批次列表与船舶列表 |
| `POST /upload` | 上传原档（multipart 字段名 `file`） |
| `GET /batches/{id}` | 本批原始行逐行判定 + 解析拒绝明细 |
| `GET /batches/{id}/original.csv` | 下载该批原档（响应头 `X-Content-SHA256`） |
| `GET /vessels/{mmsi}` | 可信点（含相对上一点航速）、隔离原因、冲突秒采信复核区 |
| `POST /vessels/{mmsi}/adjudicate` | 采信冲突秒的一条原始定位（表单含 `utc`、`selected_row_id`、`expected_version`、`set_fingerprint`） |
| `GET /vessels/{mmsi}/track.csv` | 可信航迹 CSV（按 UTC 排序） |
| `GET /vessels/{mmsi}/isolated.csv` | 隔离记录与原因 CSV |
| `GET /healthz` | 健康检查 |

## 测试与样例

```bash
. .venv/bin/activate
pytest -q            # 规则 + HTTP/并发/重启/失败回滚 + 人工采信复核
python tests/smoke.py  # 用 samples/ 跑一遍并打印每船判定
```

`samples/README.md` 详细说明了三个样例场景。手工验证顺序：

1. 上传 `samples/batch1_base.csv`：413000001 出现 `duplicate`，413000002 整秒
   `conflict`，413000003 出现 `speed_exceeded`，413000004 的坏行进入拒绝明细；
   413000009 的 00:00 冲突秒进入人工复核区。
2. 打开 `/vessels/413000009`：先选 HK02 远点（22.9/114.8）提交 → 被 60 节门限
   拒绝（409，采信不落库）；再选 SZ01 近点（22.301/114.101）→ 采信发布，该秒
   进入可信航迹，其余冲突行仍隔离。保持页面不刷新可再演示旧页面 409 拒绝。
3. 上传 `samples/batch2_late_arrivals.csv`：TW03 给 413000009 的同一冲突秒补入
   第三个位置，集合改变 → 旧采信自动变为“待复核”，航迹恢复整秒隔离；刷新船舶页
   按新集合重新采信即可恢复。413000005 补入迟到点后被重算，413000006 的 01:00
   旧点变为隔离，413000001/413000002 的重投结果保持稳定，同时出现新船 413000008。
4. 对比每次操作后 `/vessels/{mmsi}` 与 `track.csv`，并重启服务确认采信与待复核
   状态均保留；旧批次原档仍可完整下载。

## 目录

```
app/
  ais.py        CSV 解析、校验、纯函数航迹判定（含人工采信参数）
  db.py         SQLite schema、WAL 连接、读写事务
  store.py      原档原子落盘 + 单事务发布 + 全船重算 + 采信事务与失效
  main.py       FastAPI 路由、冲突秒页面与 CSV 导出
  templates/    网页（总览/批次/船舶/错误，含冲突秒采信表单）
samples/        重投、冲突、迟到点、坏行、人工复核样例
tests/          规则单测、HTTP/并发/重启/回滚/采信复核测试、smoke
```
