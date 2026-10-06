# HLS 字幕时间轴归一化服务

直播归档场景下，HLS 分段 WebVTT 字幕通过 `X-TIMESTAMP-MAP` 绑定到 33 位
MPEG-TS（90 kHz）时钟。当时钟回绕（越过 2³³）后，直接按 MPEGTS 排序会让
字幕跳回节目开头。本服务把一组连续分段的字幕还原到统一的绝对 90 kHz
时间轴上，跨回绕边界的字幕仍保持连续先后关系。

仅依赖 Python 标准库，镜像构建无需访问包管理源。

## API

### `POST /api/subtitles/normalize`

请求体（JSON）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `anchorTicks` | 整数 ≥ 0 | 首段绝对锚点：首段 `X-TIMESTAMP-MAP` 映射点在绝对 90 kHz 时间轴上的位置。必须满足 `anchorTicks ≡ MPEGTS₀ (mod 2³³)`，否则锚点不相容 |
| `maxAnchorIntervalTicks` | 整数 ≥ 0 | 相邻两段映射点之间允许的最大间隔（tick） |
| `regionPolicy` | 字符串，可选 | 省略时行为与响应结构与以往完全一致；仅接受 `"resolve"`，其他取值返回 `INVALID_REQUEST` |
| `segments` | 数组，1–64 个 | 字幕段，序号必须连续 |
| `segments[].sequence` | 整数 ≥ 0 | 段序号 |
| `segments[].content` | 字符串 | UTF-8 WebVTT 文本；全部段合计 ≤ 1 MiB |

每个分段必须恰含一个位于头部块（首个空行之前）的
`X-TIMESTAMP-MAP=LOCAL:<毫秒时间>,MPEGTS:<0..2³³-1>`。

成功响应 `200`：

```json
{
  "cues": [
    {"segment": 0, "index": 0, "startTicks": 8589930000, "endTicks": 8589966000, "text": "before wrap"},
    {"segment": 1, "index": 0, "startTicks": 8589982592, "endTicks": 8590072592, "text": "across wrap"}
  ]
}
```

`cues` 按（绝对起点 `startTicks`、段序号、段内次序）稳定排列；
`startTicks` / `endTicks` 为绝对 90 kHz 时间轴上的整数 tick。

### 区域解析（`regionPolicy=resolve`）

启用后解析正文（首个空行之后）中的 `REGION` 块以及提示计时行上的
`region:<id>` 设置。每个 REGION 块的设置可写在 `REGION` 同一行，也可写在
后续行（每行一个 `name:value`）：

| 设置 | 合法形式 | 缺省值 |
| --- | --- | --- |
| `id` | 非空、不含换行或 `-->` 的字符串标记（必填） | — |
| `width` | `0..100` 的整数百分比（`40%`） | `100%` |
| `lines` | 正整数 | `3` |
| `regionanchor` | `x%,y%`，各分量 0..100 | `0%,100%` |
| `viewportanchor` | `x%,y%`，各分量 0..100 | `0%,100%` |
| `scroll` | 仅允许 `up`，缺省表示不滚动 | `null` |

跨段中 `id` 与定义完全相同的 REGION 声明合并为一条；`id` 相同而任何
设置不同即冲突。REGION 块必须位于正文；同一段内同一 `id` 重复声明、
同一设置在一个块内重复、未知 / 非法字段、提示引用了不存在的 `id` 都是
错误。成功响应额外携带：

- `regions`：按**首次声明顺序**（段序号、段内次序）排列的规范区域列表，
  缺省设置已补全；
- 每条提示的 `regionId`：命中的区域 id，无引用时为 `null`。

```json
{
  "regions": [
    {"id": "top", "width": 40, "lines": 2,
     "regionAnchor": {"x": 10, "y": 20},
     "viewportAnchor": {"x": 30, "y": 40},
     "scroll": "up"}
  ],
  "cues": [
    {"segment": 0, "index": 0, "startTicks": 90000, "endTicks": 180000,
     "text": "hello", "regionId": "top"}
  ]
}
```

时间轴展开与 `cues` 的绝对时间排序不因区域解析改变，跨 2³³ 回绕仍保持
稳定。任何区域错误都会整体失败：HTTP 400 响应不包含 `cues` / `regions`，
不会返回部分归一化结果。

### 回绕展开规则

- 第 0 段映射点的绝对位置 = `anchorTicks`；
- 第 i 段映射点的绝对位置取候选值 `MPEGTSᵢ + k·2³³`（k ≥ 0，位置不为负）
  中唯一落在 `上一映射点 ± maxAnchorIntervalTicks` 窗口内的那个；
- 窗口内没有候选 → `ANCHOR_INCOMPATIBLE`；多于一个候选 → `UNWRAP_NOT_UNIQUE`；
- 提示的绝对 tick = 段映射点位置 +（提示本地毫秒 − 映射 LOCAL 毫秒）× 90。

### 错误响应

统一为 `400`，携带稳定错误码与（适用时的）段序号：

```json
{"error": {"code": "WEBVTT_HEADER_INVALID", "message": "...", "segment": 4}}
```

| 错误码 | 含义 |
| --- | --- |
| `INVALID_REQUEST` | 请求体不是合法 JSON 对象或字段类型非法 |
| `SEGMENT_COUNT_OUT_OF_RANGE` | 段数不在 1–64 |
| `SEGMENTS_NOT_CONSECUTIVE` | 段序号不连续（含重复） |
| `PAYLOAD_TOO_LARGE` | 全部段文本合计超过 1 MiB |
| `WEBVTT_HEADER_INVALID` | 缺少 `WEBVTT` 头 |
| `TIMESTAMP_MAP_MISSING` | 段内没有 `X-TIMESTAMP-MAP` |
| `TIMESTAMP_MAP_DUPLICATE` | 段内出现多个 `X-TIMESTAMP-MAP` |
| `TIMESTAMP_MAP_INVALID` | `X-TIMESTAMP-MAP` 格式错误或不在头部块 |
| `TIMESTAMP_INVALID` | 毫秒时间戳格式非法（须为 `mm:ss.mmm` 或 `hh:mm:ss.mmm`） |
| `MPEGTS_OUT_OF_RANGE` | MPEGTS 超出 33 位范围（0..8589934591） |
| `CUE_TIMING_INVALID` | 提示块缺少或存在非法的 `-->` 计时行 |
| `CUE_INTERVAL_INVALID` | 提示结束时间不大于开始时间 |
| `ANCHOR_INCOMPATIBLE` | 锚点与首段 MPEGTS 不同余，或相邻段间隔超出上限无法衔接 |
| `UNWRAP_NOT_UNIQUE` | 回绕展开存在多个候选，无法唯一确定 |
| `REGION_BLOCK_INVALID` | REGION 块位于头部块，或块内后续行不是单个 `name:value` 标记（仅 `resolve`） |
| `REGION_FIELD_INVALID` | REGION 字段缺失 `id`、字段名未知、值非法，或提示的 `region:` 引用非法（仅 `resolve`） |
| `REGION_SETTING_DUPLICATE` | 一个 REGION 块内同一字段重复，或一条提示携带多个 `region` 设置（仅 `resolve`） |
| `REGION_DUPLICATE_ID` | 同一段内同一区域 `id` 出现多次声明（仅 `resolve`） |
| `REGION_CONFLICT` | 跨段同名区域的任何字段不一致（段序号为后来冲突的声明所在段） |
| `REGION_REFERENCE_UNKNOWN` | 提示的 `region:` 引用了任何段都未声明的 `id` |

### `GET /healthz`

健康检查，返回 `200 {"status": "ok"}`。

## 运行

```bash
docker compose up --build app          # 默认映射宿主机 8080
APP_PORT=9090 docker compose up app    # 宿主机端口由环境变量配置
```

## 验证（一次性 verify 服务）

`verify` 服务在应用健康检查后启动，依次执行：构建检查（全部源文件字节
码编译）、单元测试、API 冒烟（含 2³³ 回绕样例、区域解析成功样例——跨段
同名区域合并、首次声明顺序、回绕排序稳定——以及区域冲突、未知引用、非法
字段和省略 `regionPolicy` 的兼容样例，连同稳定错误码断言），并以退出码
报告结果：

```bash
docker compose up --build --exit-code-from verify verify
echo $?   # 0 = 全部通过，1 = 存在失败
```

## 本地开发

```bash
python3 -m unittest discover -s tests -v   # 单元测试
PORT=8080 python3 -m app.main              # 启动服务
APP_BASE_URL=http://127.0.0.1:8080 python3 -m app.verify  # 完整验证流水线
```

## 结构

```
app/
  main.py         HTTP 服务（路由、请求体限制、错误映射）
  webvtt.py       WebVTT 严格解析（头、X-TIMESTAMP-MAP、毫秒时间、提示区间）
  normalize.py    33 位回绕唯一展开与提示排序
  service.py      请求校验与编排（段数、序号、1 MiB 上限）
  healthcheck.py  容器健康检查
  verify.py       一次性验证流水线
tests/            单元测试（解析、展开、服务、HTTP）
```
