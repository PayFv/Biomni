# Mastra 接入 Biomni Step API

Biomni 对 Mastra 暴露的是**分步作业接口**，不是 Gradio，也不是 SSE。

Mastra 负责：拆任务、让用户确认、按步调用、展示进度。  
Biomni 负责：出计划、在本机环境执行某一步、把结果写到 session 目录。

基址：

```text
https://biomni.test0.dev
```

HTTP 会 301 到 HTTPS。Mastra 里请用 HTTPS，并允许跟随重定向。

当前**没有鉴权**。进程同时只跑一个 plan 或 step。

---

## 1. 交互模型

```text
health == 200
  → GET /status == 200（空闲）
  → POST /plan { prompt }            → 202 { session_id }
  → 轮询 GET /plan/result/:id        → 始终 200
        status=running 时读 description
        status=done 时把 result.steps 给用户确认
  → 用户改计划？
        POST /plan { prompt, session_id }   # 同一工作区重做计划
  → 用户批准某一步？
        POST /step { prompt, session_id }   → 202
  → 轮询 GET /step/result/:id
        done 后把 result.summary / output 给用户
  → 继续下一步，或再改计划
```

`session_id` 是**一个研究任务的工作区**，不是一次性作业号。

- 新建计划：不带 `session_id`
- 改计划：带上一次的 `session_id`
- 执行步骤：`session_id` **必填**，从该目录里的计划和上次结果接着干

---

## 2. 接口一览

| 方法 | 路径 | 成功码 | 作用 |
|---|---|---|---|
| `GET` | `/health` | 200 | 进程是否已启动 |
| `GET` | `/status` | 200 空闲 / **202 忙碌** | 全局能不能接新任务 |
| `POST` | `/plan` | **202** | 开始或修订计划 |
| `POST` | `/step` | **202** | 在指定 session 执行一步 |
| `GET` | `/plan/result/{session_id}` | **200** | 读计划进度/结果；`?log=full` 附带历史 `log` |
| `GET` | `/step/result/{session_id}` | **200** | 读步骤进度/结果；`?log=full` 附带历史 `log` |

其它码：

| 码 | 何时 |
|---|---|
| 400 | 参数不合法 |
| 404 | `session_id` 不存在 |
| 409 | 已有作业在跑，或服务未 boot 完 |
| 422 | 缺字段（例如 step 没带 `session_id`） |

`POST` 立刻返回，**不要把客户端超时设成等整轮分析**。整轮可能数分钟；用 result 轮询。

---

## 3. 请求 / 响应

### `GET /health`

```json
{ "ok": true, "state": "idle" }
```

`ok: false` 或连不上：不要提交任务。

### `GET /status`

```json
{
  "state": "idle",
  "busy": false,
  "type": null,
  "session_id": null,
  "booted": true
}
```

忙碌时 HTTP **202**：

```json
{
  "state": "busy",
  "busy": true,
  "type": "plan",
  "session_id": "s_ab12cd34ef56",
  "booted": true
}
```

Mastra 在 `POST` 前应把 **202 当成「占用中」**，等待或提示用户，不要当失败重试出第二个任务。

### `POST /plan`

```json
{ "prompt": "查询人类胰岛素蛋白公开信息，给出 3 个可确认步骤。" }
```

修订：

```json
{
  "prompt": "在上一版基础上去掉文献检索，只留数据库查询。",
  "session_id": "s_ab12cd34ef56"
}
```

返回 `202`：

```json
{ "session_id": "s_ab12cd34ef56" }
```

### `POST /step`

```json
{
  "session_id": "s_ab12cd34ef56",
  "prompt": "只执行计划中的第一步：在 UniProt 查询人类胰岛素。不要做后续步骤。"
}
```

返回同样是 `202 { "session_id" }`。

`prompt` 写成用户确认过的那一步，范围要写死。

### `GET /plan/result/:id` 与 `GET /step/result/:id`

HTTP **始终 200**（id 不存在才 404）。忙闲看 JSON 里的 `status`。

默认只返回**瞬时快照**，没有 `log` 字段：

```json
{
  "session_id": "s_ab12cd34ef56",
  "type": "plan",
  "status": "running",
  "prompt": "……",
  "description": "正在检索相关资源",
  "result": null,
  "error": null,
  "output": []
}
```

`GET /plan/result/:id?log=full`（step 同理）额外返回 `log`：本次作业按时间顺序的进度文本列表。条目已去掉 `--------AI Message--------` 等横幅。`log=full` 以外的值都当瞬时。

```json
{
  "description": "当前完整进度文本",
  "log": [
    "planning",
    "retrieving relevant resources",
    "writing plan",
    "plan ready"
  ]
}
```

`status` 只有 `running` | `done`。

`running`：把 `description` 展示给用户（LLM 会更新）。  
`done` 且 `error` 为空：读 `result`。  
`done` 且 `error` 有值：这一次失败。

计划完成时：

```json
{
  "status": "done",
  "result": {
    "steps": [
      { "id": "s1", "title": "查询 UniProt 人类胰岛素", "why": "……" }
    ]
  },
  "output": [
    { "name": "plan.md", "content_type": "md", "content": "# Plan\n……" },
    { "name": "plan.json", "content_type": "json", "content": "{…}" }
  ]
}
```

步骤完成时：

```json
{
  "status": "done",
  "result": { "summary": "……" },
  "output": [
    {
      "name": "plot.png",
      "content_type": "png",
      "encoding": "base64",
      "content": "iVBORw0KGgo..."
    }
  ]
}
```

文本文件：`content` 为原文。  
png / pdf 等：`encoding` 为 `base64`。  
单文件超过 5MB：`content` 为空，带 `error`。

---

## 4. Mastra 推荐接法

不要把 Biomni 做成「一个大 tool 跑完全程」，也不要挂 224 个 MCP 工具。

用 **Workflow + 人工确认**：

```text
checkReady → createPlan → waitForPlan → suspend(用户改/批计划)
  → revisePlan? → waitForPlan
  → runStep → waitForStep → suspend(用户看结果)
  → 下一 step 或结束
```

轮询间隔建议 2–5 秒。单次作业超时建议 10–30 分钟。  
`POST` 超时 15 秒足够（只收 202）。

全局同时只能有一个作业。Workflow 里 plan/step **串行**，提交前先看 `/status`。

---

## 5. TypeScript 工具示例

```ts
const BASE = process.env.BIOMNI_BASE_URL ?? "https://biomni.test0.dev";

type JobType = "plan" | "step";

type BiomniResult = {
  session_id: string;
  type: JobType;
  status: "running" | "done";
  prompt: string;
  description: string;
  result: Record<string, unknown> | null;
  error: string | null;
  output: Array<{
    name: string;
    content_type: string;
    content: string;
    encoding?: string;
    error?: string;
  }>;
  log?: string[];
};

async function biomniFetch(path: string, init?: RequestInit) {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    redirect: "follow",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}

export async function assertReady() {
  const health = await biomniFetch("/health");
  if (health.status !== 200 || health.body.ok !== true) {
    throw new Error("Biomni is not ready");
  }
  const st = await biomniFetch("/status");
  if (st.status === 202 || st.body.busy) {
    throw new Error("Biomni is busy");
  }
}

export async function startPlan(prompt: string, sessionId?: string) {
  await assertReady();
  const { status, body } = await biomniFetch("/plan", {
    method: "POST",
    body: JSON.stringify({ prompt, session_id: sessionId }),
  });
  if (status === 409) throw new Error("Biomni is busy");
  if (status !== 202) throw new Error(body.detail ?? `plan failed: ${status}`);
  return body.session_id as string;
}

export async function startStep(prompt: string, sessionId: string) {
  await assertReady();
  const { status, body } = await biomniFetch("/step", {
    method: "POST",
    body: JSON.stringify({ prompt, session_id: sessionId }),
  });
  if (status === 409) throw new Error("Biomni is busy");
  if (status !== 202) throw new Error(body.detail ?? `step failed: ${status}`);
  return body.session_id as string;
}

export async function waitResult(
  type: JobType,
  sessionId: string,
  onProgress?: (description: string) => void,
) {
  const started = Date.now();
  while (Date.now() - started < 30 * 60 * 1000) {
    const { status, body } = await biomniFetch(`/${type}/result/${sessionId}`);
    if (status === 404) throw new Error(`unknown session: ${sessionId}`);
    const result = body as BiomniResult;
    if (result.status === "running") {
      onProgress?.(result.description);
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }
    if (result.error) throw new Error(result.error);
    return result;
  }
  throw new Error("Biomni result timed out");
}
```

挂到 Mastra：

```ts
import { createTool } from "@mastra/core/tools";
import { z } from "zod";

export const biomniPlanTool = createTool({
  id: "biomni_plan",
  description:
    "让 Biomni 为生物医学任务生成或修订可确认的步骤计划。不要用它执行分析。",
  inputSchema: z.object({
    prompt: z.string(),
    session_id: z.string().optional(),
  }),
  execute: async ({ prompt, session_id }) => {
    const id = await startPlan(prompt, session_id);
    const result = await waitResult("plan", id);
    return { session_id: id, steps: result.result, output: result.output };
  },
});

export const biomniStepTool = createTool({
  id: "biomni_step",
  description:
    "在已有 session 上执行用户批准的单步。prompt 必须是这一步的完整说明。",
  inputSchema: z.object({
    prompt: z.string(),
    session_id: z.string(),
  }),
  execute: async ({ prompt, session_id }) => {
    const id = await startStep(prompt, session_id);
    const result = await waitResult("step", id);
    return { session_id: id, summary: result.result, output: result.output };
  },
});
```

Agent 说明建议写清：

```text
遇到需要生物医学数据库、实验设计、组学分析、化合物预测的任务：
1. 先 biomni_plan，把 steps 展示给用户确认；
2. 用户要改计划就带同一 session_id 再 plan；
3. 用户批准后，一次只 biomni_step 一步；
4. 不要编造蛋白 / 变异 / 药物数据。
```

进度展示：在 `waitResult` 的 `onProgress` 里把 `description` 推到 Workflow 状态或前端。不要用 SSE。

---

## 6. 约束

1. **单作业**：全进程同时只能有一个 plan 或 step。`/status == 202` 时不要再 POST。
2. **不要打 Gradio**（旧的 `:7860` / `/generate_response`）。只走本文接口。
3. **不要用 MCP 全量工具**。计划与执行都通过 prompt。
4. step 的 prompt 写「只做这一步」，否则模型可能多做。
5. 无鉴权，仅内网 / 受控域名使用。
6. 生成文件可能含研究数据，不要把整段 base64 无必要地回灌给模型；摘要用 `result.summary`，文件按需给前端。

---

## 7. 本地自检

```bash
BASE=https://biomni.test0.dev

curl -sS -w '\n%{http_code}\n' $BASE/health
curl -sS -w '\n%{http_code}\n' $BASE/status

curl -sS -X POST $BASE/plan \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"查询人类胰岛素蛋白公开信息，给出3个可确认步骤。"}'
# 记下 session_id

curl -sS $BASE/plan/result/<session_id>
```

`status=done` 后再测 step 或带 `session_id` 的修订 plan。
