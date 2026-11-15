# -*- coding: utf-8 -*-
"""
LLM 客户端 —— 纯标准库实现（urllib），OpenAI 兼容接口。

为什么不用 openai / httpx：本产品定位是"双击即用"，装依赖会让门槛陡增。
urllib 足够覆盖 chat/completions（含 SSE 流式）。

关键设计：
  · 温度按「归一化值 → 服务商量程」折算（OpenAI/DeepSeek 0–2，Claude 0–1）
  · 推理型模型（R1 / o1 系列）不传 temperature
  · 错误分类明确，供前端展示可执行的下一步
  · NDJSON 增量解析器：LLM 每吐完一行就回调一次
"""

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class LLMError(Exception):
    """带分类的 LLM 错误，前端据此给出「可执行的下一步」。"""

    def __init__(self, kind, message, hint="", status=None):
        super().__init__(message)
        self.kind = kind          # auth | rate_limit | timeout | network | bad_request | server | format
        self.message = message
        self.hint = hint
        self.status = status

    def to_dict(self):
        return {"kind": self.kind, "message": self.message, "hint": self.hint, "status": self.status}


# 各服务商的温度量程上限（用于归一化折算）
PROVIDER_MAX_TEMP = {
    "deepseek": 2.0,
    "openai": 2.0,
    "claude": 1.0,
    "moonshot": 1.0,
    "zhipu": 1.0,
    "qwen": 2.0,
    "custom": 2.0,
}

# 不接受 temperature 的模型特征（推理型）
_NO_TEMP_PATTERNS = [r"deepseek-reasoner", r"deepseek-r1", r"^o1", r"^o3", r"^o4", r"thinking", r"qwq"]


def _is_reasoning_model(model):
    m = (model or "").lower()
    return any(re.search(p, m) for p in _NO_TEMP_PATTERNS)


def resolve_temperature(normalized, provider, model):
    """归一化温度（0–1）→ 该服务商的绝对值；推理型模型返回 None（不传该参数）。"""
    if _is_reasoning_model(model):
        return None
    if normalized is None:
        return None
    mx = PROVIDER_MAX_TEMP.get((provider or "custom").lower(), 2.0)
    return round(max(0.0, min(1.0, float(normalized))) * mx, 3)


# 各服务商的单次输出上限（tokens）。
# 取值说明：结构重建/评估的**输出体积与简历正文同量级**（NDJSON 还要多带结构字符），
# 所以给小了必然被截断。这里取各家文档里的常见上限，宁可大一点。
PROVIDER_MAX_OUTPUT = {
    "deepseek": 8192,
    "openai": 16384,
    "claude": 8192,
    "moonshot": 8192,
    "zhipu": 8192,
    "qwen": 8192,
    "custom": 16384,      # 自建网关 / 中转：各家差异大，给宽一些（被拒会自动去掉参数重试）
}

# 推理型模型会把输出额度花在"思考"上，真正落到答案上的余量更小 → 要留余量
_REASONING_OUTPUT_FLOOR = 32768


# 网关卡对 max_tokens 的报错文案差异很大，这里做一轮宽松识别。
# 只在"请求带过 max_tokens 且收到 400"时才用到，误判代价只是多重试一次。
_MAX_TOKENS_ERR_PAT = re.compile(
    r"max[_\s-]?tokens?|max\s*output|maximum\s*(new\s*)?tokens"
    r"|最大长度|最大长度限制|输出长度|输出上限|长度上限|超出上限",
    re.I)


def _looks_like_max_tokens_error(message):
    return bool(_MAX_TOKENS_ERR_PAT.search(str(message or "")))


def resolve_max_tokens(cfg, requested=None):
    """决定这次调用带多大的 max_tokens。

    优先级：显式配置 > 推理模型的宽上限 > 服务商默认
    """
    cfg = cfg or {}
    if requested:
        return int(requested)
    explicit = cfg.get("max_tokens")
    if explicit:
        try:
            v = int(explicit)
            if v > 0:
                return v
        except Exception:
            pass
    provider = (cfg.get("provider") or "custom").lower()
    base = PROVIDER_MAX_OUTPUT.get(provider, 16384)
    if _is_reasoning_model(cfg.get("model")):
        return max(base, _REASONING_OUTPUT_FLOOR)
    return base



# ------------------------------------------------------------------ HTTP

# 这些地址绝不该走系统代理：本机回环 + 内网私有网段。
# 典型场景：用户接 Ollama / LM Studio / one-api 等本地或内网网关，
# 而本机（或公司）配了系统代理 —— 一旦被代理截走，请求会直接失败或返回 503，
# 且报错信息完全看不出原因。
_NO_PROXY_NETS = (
    re.compile(r"^127\."),
    re.compile(r"^10\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),
    re.compile(r"^::1$"),
    re.compile(r"^localhost$", re.I),
    re.compile(r"^0\.0\.0\.0$"),
)


def _needs_proxy_bypass(url):
    """判断该 URL 是否应绕过系统代理。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    h = host.strip("[]")
    return any(p.search(h) for p in _NO_PROXY_NETS)


def _opener_for(url, context=None):
    """按目标地址构造 opener。

    · 本机/内网地址 → 显式装上空的 ProxyHandler（绕过系统代理）
    · 公网地址     → 不装，沿用系统代理（用户可能需要它）
    · context      → 通过 HTTPSHandler 传入。注意：OpenerDirector.open()
                     本身不接受 context 参数（只有模块级 urlopen 接受），
                     所以必须挂在这里。
    """
    handlers = []
    if _needs_proxy_bypass(url):
        handlers.append(urllib.request.ProxyHandler({}))
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def _post_stream(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    return _opener_for(url, ctx).open(req, timeout=timeout)


def _classify(exc, base_url):
    """把 urllib 异常翻译成带可执行提示的 LLMError。"""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = ""
        try:
            detail = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            detail = body
        if code in (401, 403):
            return LLMError("auth", "API Key 无效或无权限（HTTP %d）" % code,
                            "检查设置里的 API Key 是否填对、是否已过期、是否与所选服务商匹配。", code)
        if code == 402:
            return LLMError("auth", "账户余额不足（HTTP 402）", "去服务商后台充值后再试。", code)
        if code == 429:
            return LLMError("rate_limit", "请求过于频繁或已达配额上限（HTTP 429）",
                            "已完成的建议不会丢。稍等一会儿再点「继续评估」，或换一个服务商。", code)
        if code == 404:
            return LLMError("bad_request", "接口地址或模型名不对（HTTP 404）",
                            "检查设置 → 高级选项里的 Base URL 与模型名（常见错法：多写或少写 /v1）。", code)
        if 400 <= code < 500:
            hint = "多为模型名不支持、参数不被接受，或输入超出该模型的上下文上限。可在设置里换模型，或先精简简历。"
            if _looks_like_max_tokens_error(detail):
                hint = "模型或网关不接受当前的输出长度参数，已自动去掉该参数重试；若仍失败请换模型。"
            return LLMError("bad_request", "请求被拒绝（HTTP %d）：%s" % (code, detail), hint, code)
        return LLMError("server", "服务商返回错误（HTTP %d）：%s" % (code, detail),
                        "这是对方服务的问题，稍后重试。", code)

    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "timed out" in text.lower() or isinstance(reason, TimeoutError):
            return LLMError("timeout", "连接超时", "检查网络 / 代理是否可用；也可在设置里换服务商。")
        return LLMError("network", "无法连接到服务商：%s" % text,
                        "常见原因：① 需要代理才能访问该服务商；② Base URL 写错；③ 公司网络拦截。")

    if isinstance(exc, TimeoutError):
        return LLMError("timeout", "连接超时", "检查网络 / 代理是否可用。")

    return LLMError("network", "请求失败：%s" % exc, "检查网络与服务商地址。")


def _headers(api_key, extra=None):
    h = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % (api_key or ""),
        "Accept": "text/event-stream",
        "User-Agent": "resume-workbench/2.0",
    }
    if extra:
        h.update(extra)
    return h


# ------------------------------------------------------------------ NDJSON 增量解析

class NDJSONStream:
    """从 LLM 的输出文本流里切出完整 JSON 对象。

    策略是**大括号配平扫描**（不是按行切），所以这些情况都能吃下：
      · 标准 NDJSON（一行一个对象）
      · 大对象被美化输出成多行（glm 系列常见）
      · 该换行没换行、多个对象挤在一起
      · 用 ```json 包裹、前后带废话
    """

    def __init__(self):
        self.buf = ""
        self.objects = []

    def feed(self, chunk):
        """喂入增量文本，返回本次新解析出的对象列表。

        用**大括号配平**扫描整个缓冲区，而不是按行切。原因：模型可能把一个大对象
        （如「深度重写」的整段 rewrite）**美化输出成多行**；按行切会把这种对象的每一行
        都当噪声丢掉，整个对象再也拼不回来 —— 实测 glm 系列会这样输出，症状是
        "模型没有返回可用的改写结果"。配平扫描对单行 NDJSON / 多行美化 / 该换行没换行 /
        被 ``` 包裹一律通吃。
        """
        self.buf += chunk
        return self._drain()

    def _drain(self):
        """把缓冲区里已配平的对象全部抠出来，保留未配平的尾部。

        注意：**不保留跨调用状态** —— 每次都对当前缓冲区从头扫一遍。历史实现用实例变量
        记 _start/_depth 跨调用复用，遇到"一个对象跨多个 chunk"时深度会被重复累加，
        永远配不平，整个对象被丢掉。
        """
        text = self.buf
        out = []
        depth, start, last_end = 0, -1, -1
        in_str, esc = False, False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0:
                        obj = _try_parse_json(text[start:i + 1])
                        if obj is not None:
                            out.append(obj)
                            self.objects.append(obj)
                        last_end = i + 1
                        start = -1
        if depth > 0 and start >= 0:
            self.buf = text[start:]        # 未配平：保留（其前的噪声一并丢掉）
        elif last_end >= 0:
            self.buf = text[last_end:]     # 丢掉已消费部分
        elif "{" not in text:
            self.buf = ""                  # 全是噪声，没有待拼的对象
        if len(self.buf) > 200000:
            self.buf = self.buf[-50000:]
        return out

    def flush(self):
        """流结束，把残留的完整对象也捞出来。"""
        out = self._drain()
        rest = self.buf
        if rest.strip():
            for seg in _extract_all_objects(rest):
                o = _try_parse_json(seg)
                if o is not None:
                    out.append(o)
                    self.objects.append(o)
        self.buf = ""
        return out


def _try_parse_json(text):
    t = (text or "").strip()
    if not t or t[0] != "{":
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def _try_parse_line(line):
    t = (line or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    if not t.startswith("{"):
        m = re.search(r"\{.*\}", t)
        if not m:
            return None
        t = m.group(0)
    return _try_parse_json(t)


def _extract_all_objects(text):
    """粗粒度提取：扫出所有 { 配平片段。"""
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:i + 1])
                start = -1
    return out


# ------------------------------------------------------------------ 调用

# 进程内缓存：记录"哪个 (base_url, model) 拒绝过 max_tokens"。
# 每次请求都新建一个 LLMClient，如果只存在实例上，就会每次先撞一次 400 再重试 —— 白白多一次往返。
# 这里放到模块级，一次服务运行内只需付一次代价。
_MAX_TOKENS_UNSUPPORTED = set()


class LLMClient(object):
    def __init__(self, cfg):
        self.cfg = cfg or {}
        # 上一次调用的结束原因（"stop" / "length" / ...）。
        # 关键：**截断不是异常，是数据** —— 截断时已经生成的内容仍然有效
        # （结构重建能"先用已还原的部分"，评估能保留已完成的建议），
        # 所以这里只记录，不抛错，由调用方决定怎么处理。
        self.last_finish_reason = None
        self.last_usage = None
        self.last_end_was_abrupt = False     # 流被"悄悄断开"（无 finish_reason 也无 [DONE]）
        # 服务商是否拒绝过 max_tokens（记下来，后续调用不再带）
        self._max_tokens_rejected = False

    @property
    def base_url(self):
        return (self.cfg.get("base_url") or "").rstrip("/")

    @property
    def api_key(self):
        return self.cfg.get("api_key") or ""

    @property
    def model(self):
        return self.cfg.get("model") or "deepseek-chat"

    @property
    def provider(self):
        return (self.cfg.get("provider") or "deepseek").lower()

    @property
    def truncated(self):
        """上一次调用是否因输出长度上限被截断。"""
        return self.last_finish_reason == "length"

    def chat(self, system, user, temperature=None, max_tokens=None, stream=True, timeout=None, retries=1):
        """调用 chat/completions。

        stream=True  -> 生成器，逐块 yield 文本增量
        stream=False -> 返回完整文本
        """
        if not self.base_url:
            raise LLMError("bad_request", "尚未配置服务商地址", "去「设置 → AI 服务」填写。")
        if not self.api_key:
            raise LLMError("auth", "尚未填写 API Key", "去「设置 → AI 服务」填写你的 API Key。")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": bool(stream),
        }
        temp = temperature
        if temp is not None:
            payload["temperature"] = temp
        _mt_key = (self.base_url, self.model)
        if max_tokens and not self._max_tokens_rejected and _mt_key not in _MAX_TOKENS_UNSUPPORTED:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}

        url = self.base_url + "/chat/completions"
        headers = _headers(self.api_key)
        to = timeout or int(self.cfg.get("timeout") or 120)

        last_err = None
        for attempt in range(retries + 2):
            self.last_finish_reason = None
            self.last_end_was_abrupt = False
            try:
                # 关键：**请求必须在这里"立刻"发出去**，错误才会落进本 try。
                # 之前是把生成器交给调用方、由调用方迭代时才发请求 ——
                # 那样异常发生在 try 之外，重试逻辑（限流重试 / max_tokens 回退）**从来没生效过**。
                resp = self._open(url, payload, headers, to)
            except LLMError as e:
                last_err = e
                # 有些网关/模型不接受 max_tokens（或要求特定取值范围）→ 去掉参数重试一次。
                # 这是真实存在的：自建网关与中转服务对参数校验的宽严差异很大，
                # 报错文案也不统一（有的是 "max_tokens"，有的是"最大长度"/"输出上限"）。
                if (e.kind == "bad_request"
                        and "max_tokens" in payload
                        and _looks_like_max_tokens_error(e.message)):
                    self._max_tokens_rejected = True
                    _MAX_TOKENS_UNSUPPORTED.add(_mt_key)     # 进程内记住，后续请求不再重试这条弯路
                    payload.pop("max_tokens", None)
                    continue
                # 限流/服务端错误值得重试
                if e.kind in ("rate_limit", "server") and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            # 连接已建立 —— 之后流中途出错就不该重试了（内容已经吐出去一部分）
            if not stream:
                return self._read_all(resp)
            return self._iter_sse(resp)
        raise last_err

    def _open(self, url, payload, headers, timeout):
        """发出请求并拿到响应对象（错误在这里抛，才能被重试逻辑接住）。"""
        try:
            return _post_stream(url, payload, headers, timeout)
        except Exception as e:
            raise _classify(e, url)

    def _read_all(self, resp):
        """非流式：读完整个响应体并解析。"""
        try:
            raw = resp.read().decode("utf-8", "replace")
        except Exception as e:
            raise _classify(e, self.base_url)
        try:
            data = json.loads(raw)
        except Exception:
            raise LLMError("format", "服务商返回了无法解析的内容", "换个模型试试（部分模型不兼容 OpenAI 格式）。")
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            raise LLMError("server", msg, "服务商报错，稍后重试或换模型。")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("format", "服务商返回内容为空", "换个模型试试。")
        if data.get("usage"):
            self.last_usage = data["usage"]
        self.last_finish_reason = choices[0].get("finish_reason") or "stop"
        return choices[0].get("message", {}).get("content", "")

    def _iter_sse(self, resp):
        """流式：逐块解析 SSE，yield 文本增量。"""
        buf = b""
        got_content = False
        while True:
            try:
                chunk = resp.read(4096)
            except Exception as e:
                raise _classify(e, self.base_url)
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl].strip()
                buf = buf[nl + 1:]
                if not line:
                    continue
                if line.startswith(b"data:"):
                    line = line[5:].strip()
                if line == b"[DONE]":
                    got_content = True     # 正常收尾哨兵
                    return
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                if obj.get("usage"):
                    self.last_usage = obj["usage"]
                for ch in (obj.get("choices") or []):
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        got_content = True
                        yield piece
                    fr = ch.get("finish_reason")
                    if fr:
                        self.last_finish_reason = fr
                        # 截断（length）不抛错：已经生成的内容是有效的，
                        # 结构重建可以"先用已还原的部分"，评估可以保留已完成的建议。
                        # 调用方用 client.truncated 判断，并决定续写 / 提示。
                        if fr != "stop":
                            return
        # 走到这里 = 连接关掉了，但既没有 finish_reason 也没有 [DONE] 哨兵。
        # 有些网关（自建/中转）就是这样"悄悄断流"的 —— 不识别的话，
        # 我们会把**被截断的输出当成完整结果**，静默丢掉后半段。
        if got_content and self.last_finish_reason is None:
            self.last_finish_reason = "length"
            self.last_end_was_abrupt = True
        return

    def test(self):
        """测试连接：发一个极小的请求，返回 {ok, model, temperature, ms}。"""
        t0 = time.time()
        temp = resolve_temperature(self.cfg.get("temperature"), self.provider, self.model)
        txt = self.chat("你是一个测试助手。", "只回复两个字：可用", temperature=temp,
                        max_tokens=16, stream=False, timeout=25, retries=0)
        return {
            "ok": True,
            "model": self.model,
            "temperature": temp,
            "temperature_note": "该模型不接受 temperature 参数，已自动省略" if temp is None else "",
            "ms": int((time.time() - t0) * 1000),
            "reply": (txt or "").strip()[:40],
        }
