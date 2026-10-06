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

### `GET /healthz`

健康检查，返回 `200 {"status": "ok"}`。

## 运行

```bash
docker compose up --build app          # 默认映射宿主机 8080
APP_PORT=9090 docker compose up app    # 宿主机端口由环境变量配置
```

## 验证（一次性 verify 服务）

`verify` 服务在应用健康检查后启动，依次执行：构建检查（全部源文件字节
码编译）、单元测试、API 冒烟（含 2³³ 回绕样例与稳定错误码断言），并以
退出码报告结果：

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
