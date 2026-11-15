# -*- coding: utf-8 -*-
"""
简历 AI 工作台 · 本地服务

零第三方依赖（纯标准库）。职责：
  1. 保管 API Key（只在本机 config.json，不出本机）
  2. 代理 LLM 调用（OpenAI 兼容），流式转发给前端
  3. 本地规则护栏：数字守恒校验 / 用量记账
  4. PDF 提取（可选 pypdf）与 PDF 导出（本机浏览器无头）

启动：  python server/app.py            （或双击 start.bat）
访问：  http://127.0.0.1:8420/
"""

import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server import docio, llm, prompts, rules, store
else:
    from . import docio, llm, prompts, rules, store

VERSION = "2.0.0-phase2"
HOME = os.environ.get("RW_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
PORT = int(os.environ.get("RW_PORT") or 8420)
BIND = os.environ.get("RW_BIND") or "127.0.0.1"

_START_TS = time.time()


# ================================================================== 业务逻辑

#: 这些章节不参与「对照 JD 改写」—— 教育 / 荣誉 / 证书属于事实性内容，改了反而假
_JD_EXEMPT_KEYWORDS = ("教育", "荣誉", "奖项", "证书", "资质", "其他")


def _is_jd_exempt(title):
    return any(k in (title or "") for k in _JD_EXEMPT_KEYWORDS)


#: 「深度重写」额外排除的章节（只影响深度重写，不影响逐条评估）：
#: 工作经历是关键履历，用户明确要求整段不动 —— 改写容易加「项目介绍/项目工作」
#: 这类不属于工作经历的结构，风险大于收益。
_DEEP_EXTRA_EXEMPT_KEYWORDS = ("工作经历", "工作经验", "工作履历")


def _is_deep_exempt(title):
    t = title or ""
    return _is_jd_exempt(t) or any(k in t for k in _DEEP_EXTRA_EXEMPT_KEYWORDS)


def _originals_map(resume, skip_exempt=False):
    """从简历结构里取出 {bid: 原文}，用于数字守恒比对。

    skip_exempt=True 时跳过教育 / 荣誉 / 证书 / 其他章节 —— 这些不参与 JD 改写，
    模型即便硬输出这些 bid 也会被忽略。
    """
    out = {}
    for sec in (resume or {}).get("sections", []):
        if skip_exempt and _is_jd_exempt(sec.get("title")):
            continue
        for blk in sec.get("blocks", []):
            for b in blk.get("bullets", []):
                if b.get("bid"):
                    out[b["bid"]] = b.get("text", "")
    return out


def _sanitize_order(obj, resume):
    """校验模型的「顺序建议」：只允许重排**已有**的章节 / 条目 id。

    模型可能编造 id、漏项、重复 —— 这里统一清理：
    非法项丢掉、重复去掉、没列到的按原顺序补在后面。
    返回 {"sections":[...], "blocks":[...]}；与当前顺序一致则返回 None。
    """
    secs = [s.get("id") for s in (resume.get("sections") or []) if s.get("id")]
    blocks = [b.get("id") for s in (resume.get("sections") or [])
              for b in (s.get("blocks") or []) if b.get("id")]
    if not secs:
        return None

    def _clean(raw, valid):
        out, seen = [], set()
        for x in (raw or []):
            if isinstance(x, str) and x in valid and x not in seen:
                out.append(x)
                seen.add(x)
        out += [x for x in valid if x not in seen]
        return out

    so = _clean((obj or {}).get("sections"), secs)
    bo = _clean((obj or {}).get("blocks"), blocks)
    if so == secs and bo == blocks:
        return None
    return {"sections": so, "blocks": bo}


def _compact_resume(resume, only_bids=None):
    """把简历压成给 LLM 的紧凑 JSON。

    带上 sid / id / intro / subhead，模型才能：
      · 按 JD 建议重排章节与条目（要求返回 id 顺序）
      · 在深度重写时保留「项目介绍 → 项目工作 → 二级要点」的结构
    """
    secs = []
    for sec in (resume or {}).get("sections", []):
        ex = _is_jd_exempt(sec.get("title"))
        blocks = []
        for blk in sec.get("blocks", []):
            bullets = []
            for b in blk.get("bullets", []):
                if not b.get("bid"):
                    continue
                item = {"bid": b["bid"], "text": b.get("text", "")}
                if b.get("level"):
                    item["level"] = b["level"]
                bullets.append(item)
            if bullets:
                item_blk = {
                    "id": blk.get("id", ""),
                    "heading": blk.get("heading", ""),
                    "meta": blk.get("meta", ""),
                    "intro": blk.get("intro", ""),
                    "subhead": blk.get("subhead", ""),
                    "bullets": bullets,
                }
                if ex:
                    item_blk["exempt"] = True     # 教育 / 荣誉 / 证书：不要改
                blocks.append(item_blk)
        if blocks:
            s = {"sid": sec.get("id", ""), "title": sec.get("title", ""), "blocks": blocks}
            if ex:
                s["exempt"] = True
            secs.append(s)
    return {"intent": (resume or {}).get("intent", ""), "sections": secs}


def _norm_inferred(raw):
    """规范化模型给出的「AI 推断事实」清单（放开改写档位用）。

    模型可能给字符串数组、对象数组、或干脆不给 —— 统一成
    [{"span": "约 30%", "why": "..."}]，丢掉空项。前端拿它生成
    「需要你核实的事实清单」，所以宁可少也不要脏。
    """
    out = []
    for x in (raw or []):
        if not x:
            continue
        if isinstance(x, str):
            s = x.strip()
            if s:
                out.append({"span": s, "why": ""})
            continue
        if isinstance(x, dict):
            span = str(x.get("span") or x.get("text") or "").strip()
            if span:
                out.append({"span": span, "why": str(x.get("why") or x.get("reason") or "").strip()})
    return out


def run_evaluate(body, emit):
    """评估 + 改写的完整链路，边跑边通过 emit 推送 SSE 事件。"""
    cfg = store.load_config()
    t0 = time.time()
    client = llm.LLMClient(cfg["llm"])
    resume = body.get("resume") or {}
    jd_text = (body.get("jd") or "").strip()
    # level 优先取前端显式传入（已按全局「AI 改写风格」折算）；否则读配置里的 rewrite_style，兼容旧 ai_level
    _style = cfg["llm"].get("rewrite_style") or cfg["llm"].get("ai_level") or "heavy"
    level = body.get("level") or _style
    if level not in ("free", "heavy", "light"):
        level = "heavy"
    skip_ai = bool(body.get("skip_ai"))   # 纯规则体检：只跑本机规则，不调模型
    dirty = body.get("dirty_bids")
    rejected = body.get("rejected") or []
    blacklist = [w for w in (body.get("blacklist") or cfg.get("blacklist") or []) if w]

    if not jd_text:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "还没有粘贴目标 JD",
                                          "hint": "在工作台右侧贴入 JD 文本后再评估。"}})
        return
    # 教育 / 荣誉 / 证书 / 其他不参与 JD 改写（改了反而假），只作为上下文
    bullets = _originals_map(resume, skip_exempt=True)
    if not bullets:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "简历里没有可评估的内容",
                                          "hint": "先导入简历（或载入样例）再评估。"}})
        return
    exempt_titles = [s.get("title") or "" for s in (resume.get("sections") or [])
                     if _is_jd_exempt(s.get("title"))]
    if blacklist:
        jd_text = jd_text + "\n\n【用户声明：以下内容他明确没有做过，绝不允许出现】\n" + "、".join(blacklist)

    # ① 规则层先行 —— 本地毫秒级，用户不必干等
    report = rules.local_rules_report(resume)   # 与前端同源的规则实现（见 rules.py）
    emit({"stage": "rules", "report": report})

    # 纯规则体检：只跑本机规则、不调 AI（"评估时仅纯规则体检"开关）
    if skip_ai:
        emit({"stage": "done", "suggestions": 0, "usage": {}, "cost": 0,
              "currency": cfg["llm"].get("currency") or "¥", "elapsed": round(time.time() - t0, 1),
              "skipped_ai": True, "note": "纯规则体检：未调用 AI"})
        _finish_usage(client, "evaluate(rules-only)", {}, 0, t0)
        return

    # ② LLM 调用
    temp = llm.resolve_temperature(
        body.get("temperature", cfg["llm"].get("temperature")),
        client.provider, client.model)
    prompt = prompts.build_evaluate_prompt(
        jd_text, json.dumps(_compact_resume(resume), ensure_ascii=False),
        level=level, dirty_bids=dirty, rejected=rejected, exempt_titles=exempt_titles)

    emit({"stage": "start", "provider": client.provider, "model": client.model,
          "temperature": temp, "level": level,
          "prompt_chars": len(prompt["user"]), "bullets_total": len(bullets)})

    parser = llm.NDJSONStream()
    jd_duties = []
    suggestions = []
    coverage = {}
    order = None
    usage = {}
    chunks = 0

    try:
        for piece in client.chat(prompt["system"], prompt["user"], temperature=temp, stream=True,
                                 max_tokens=llm.resolve_max_tokens(cfg["llm"])):
            chunks += 1
            for obj in parser.feed(piece):
                t = obj.get("type")
                if t == "jd":
                    jd_duties = obj.get("duties") or []
                    emit({"stage": "jd", "role": obj.get("role", ""), "duties": jd_duties})
                elif t == "sug":
                    bid = obj.get("bid")
                    if bid not in bullets:
                        continue                    # 忽略幻觉出来的 bid
                    orig = bullets.get(bid, "")
                    rewritten = obj.get("rewritten", "")
                    added = rules.number_diff(orig, rewritten)
                    ignored = [w for w in (obj.get("ai_added_spans") or []) if w and w in orig]
                    sug = {
                        "bid": bid,
                        "verdict": obj.get("verdict") or "rewrite",
                        "original": orig,
                        "rewritten": rewritten,
                        "ai_added_spans": [w for w in (obj.get("ai_added_spans") or []) if w not in orig],
                        "reason": obj.get("reason", ""),
                        "jd_ref": obj.get("jd_ref") or [],
                        "inferred_facts": _norm_inferred(obj.get("inferred_facts")),
                        "status": "pending",
                        "check": {"numbers_added": added, "is_stale": False},
                    }
                    if ignored:
                        # LLM 把老词误报成"新增"，纠正它，避免 UI 错标
                        sug["check"]["spans_corrected"] = ignored
                    if any(w and w in orig for w in (obj.get("ai_added_spans") or [])):
                        sug["check"]["spans_corrected"] = True
                    suggestions.append(sug)
                    emit({"stage": "sug", "sug": sug,
                          "progress": {"done": len(suggestions), "total": len(bullets)}})
                elif t == "coverage":
                    coverage = obj
                    emit({"stage": "coverage",
                          "covered": obj.get("covered") or [], "missing": obj.get("missing") or [],
                          "intent": obj.get("intent") or ""})
                elif t == "order":
                    od = _sanitize_order(obj, resume)
                    if od and not order:
                        order = od
                        emit({"stage": "order", "order": od})
                elif t == "done":
                    pass
        for obj in parser.flush():
            t = obj.get("type")
            if t == "sug":
                bid = obj.get("bid")
                if bid in bullets and not any(s["bid"] == bid for s in suggestions):
                    orig = bullets.get(bid, "")
                    rewritten = obj.get("rewritten", "")
                    sug = {"bid": bid, "verdict": obj.get("verdict") or "rewrite", "original": orig,
                           "rewritten": rewritten,
                           "ai_added_spans": [w for w in (obj.get("ai_added_spans") or []) if w not in orig],
                           "reason": obj.get("reason", ""), "jd_ref": obj.get("jd_ref") or [],
                           "inferred_facts": _norm_inferred(obj.get("inferred_facts")),
                           "status": "pending",
                           "check": {"numbers_added": rules.number_diff(orig, rewritten), "is_stale": False}}
                    suggestions.append(sug)
                    emit({"stage": "sug", "sug": sug})
            elif t == "jd" and not jd_duties:
                jd_duties = obj.get("duties") or []
                emit({"stage": "jd", "role": obj.get("role", ""), "duties": jd_duties})
            elif t == "coverage" and not coverage:
                coverage = obj
                emit({"stage": "coverage", "covered": obj.get("covered") or [],
                      "missing": obj.get("missing") or [], "intent": obj.get("intent") or ""})
            elif t == "order" and not order:
                od = _sanitize_order(obj, resume)
                if od:
                    order = od
                    emit({"stage": "order", "order": od})
    except llm.LLMError as e:
        emit({"stage": "error", "error": e.to_dict(),
              "partial": {"suggestions": len(suggestions), "done": True}})
        _finish_usage(client, "evaluate(partial)", usage, len(suggestions), t0)
        return
    except Exception as e:
        traceback.print_exc()
        emit({"stage": "error", "error": {"kind": "server", "message": "本地服务内部错误：%s" % e,
                                          "hint": "看服务窗口的日志。"},
              "partial": {"suggestions": len(suggestions), "done": True}})
        return

    usage = getattr(client, "last_usage", None) or {}
    cost = store.estimate_cost(usage, cfg)
    _finish_usage(client, "evaluate", usage, len(suggestions), t0)
    emit({"stage": "done", "suggestions": len(suggestions), "usage": usage, "cost": cost,
          "currency": cfg["llm"].get("currency") or "¥", "elapsed": round(time.time() - t0, 1),
          "chunks": chunks, "duties": len(jd_duties),
          "truncated": bool(client.truncated)})


def _finish_usage(client, step, usage, n_sug, t0, quiet=False):
    if usage:
        store.add_usage(step, client.model, usage)
        try:
            txt_in = int((usage or {}).get("prompt_tokens") or 0)
            txt_out = int((usage or {}).get("completion_tokens") or 0)
            store.log("调用 AI（%s）" % step,
                      "模型 %s · 输入 %s tok · 输出 %s tok · 耗时 %.1fs · 生成建议 %d 条"
                      % (client.model, txt_in, txt_out, time.time() - t0, n_sug),
                      scope="联网", extra={"tokens_in": txt_in, "tokens_out": txt_out})
        except Exception:
            pass


REBUILD_MAX_ROUNDS = 4      # 截断后最多续写几轮（防死循环）


def run_rebuild(body, emit):
    """纯文本 → 结构化简历（结构重建）。

    两个真实世界的坑，这里都处理掉：

    ① **输出会被长度上限截断**。NDJSON 的输出体积和简历正文同量级，两页简历就能顶到上限；
       碰上推理型模型（MiniMax-M3 / DeepSeek-R1 等），思考过程还要先吃掉一部分额度。
       所以截断不是异常，是**正常的分段**：拿到已还原的部分后自动续写，直到 done 或轮次用尽。

    ② **部分成功也要能用**。哪怕最终没跑完，已还原的章节也照样返回给前端，
       由用户决定「先用已还原的部分」。绝不让已生成的内容白跑。
    """
    cfg = store.load_config()
    raw = (body.get("text") or "").strip()
    if len(raw) < 30:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "文本太短，无法解析简历结构",
                                          "hint": "粘贴完整的简历文本。"}})
        return
    client = llm.LLMClient(cfg["llm"])
    temp = llm.resolve_temperature(0.0, client.provider, client.model)   # 结构重建要确定性
    max_tokens = llm.resolve_max_tokens(cfg["llm"])

    # 前端可以带着"已还原的章节"回来要求继续补齐（截断兜底的出口）
    seed = body.get("continue_from") or []
    head, sections, confs = {}, [], []
    for s in seed:
        if isinstance(s, dict) and s.get("sid"):
            sections.append(s)
    emit({"stage": "start", "model": client.model, "temperature": temp})

    parser = llm.NDJSONStream()
    t0 = time.time()
    usage_total = {}
    rounds = 0
    had_truncation = False      # 是否发生过截断（决定要不要告诉用户"分了几段"）
    still_truncated = False     # 最后一轮是否仍被截断（决定结果是否可能不完整）
    err = None

    def absorb(objs):
        """把解析出的对象并入结果（section 按 sid 去重）。"""
        for obj in objs:
            t = obj.get("type")
            if t == "head":
                if not head:
                    head.update(obj)
                    emit({"stage": "head", "head": obj})
            elif t == "section":
                sid = obj.get("sid")
                if sid and any(s.get("sid") == sid for s in sections):
                    continue                      # 续写时模型重复输出了已有章节 → 丢弃
                sections.append(obj)
                emit({"stage": "section", "section": obj})
            elif t == "conf":
                key = obj.get("key")
                if key and any(c.get("key") == key for c in confs):
                    continue
                confs.append(obj)
                emit({"stage": "conf", "conf": obj})

    while True:
        rounds += 1
        parser = llm.NDJSONStream()
        sections_before = len(sections)
        if rounds == 1 and not seed:
            prompt = prompts.build_rebuild_prompt(raw)
        else:
            prompt = prompts.build_rebuild_continue_prompt(raw, sections)
        try:
            for piece in client.chat(prompt["system"], prompt["user"], temperature=temp,
                                     stream=True, max_tokens=max_tokens):
                absorb(parser.feed(piece))
            absorb(parser.flush())
        except llm.LLMError as e:
            err = e
            break
        except Exception as e:
            traceback.print_exc()
            err = llm.LLMError("server", "本地服务内部错误：%s" % e, "看服务窗口的日志。")
            break

        u = getattr(client, "last_usage", None) or {}
        for k, v in u.items():
            if isinstance(v, int):
                usage_total[k] = usage_total.get(k, 0) + v

        still_truncated = client.truncated
        if still_truncated:
            had_truncation = True
        _finish_usage(client, "rebuild#%d" % rounds, u, len(sections), t0, quiet=rounds > 1)
        if not still_truncated:
            break
        # 续写没有拿到新章节 → 再问也是原样重复，停
        if rounds > 1 and len(sections) == sections_before:
            break
        # 轮次用尽 → 停
        if rounds >= REBUILD_MAX_ROUNDS:
            break
        emit({"stage": "progress", "round": rounds, "sections": len(sections),
              "message": "输出达上限，正在接着还原第 %d 部分…" % (rounds + 1)})

    # ---- 收尾：部分成功也返回结果，不用错误态把已还原内容盖掉 ----
    if not sections:
        e = err or llm.LLMError("format", "AI 没有返回可用的结构", "换一个模型试试，或先用「规则推断」。")
        emit({"stage": "error", "error": e.to_dict() if hasattr(e, "to_dict") else dict(e)})
        return

    if err is not None and not still_truncated:
        # 有内容但中途报错（限流/网络）→ 明确告诉前端"这是部分结果"
        emit({"stage": "partial", "error": err.to_dict(), "sections": len(sections)})
    if had_truncation:
        emit({"stage": "notice", "kind": "truncated",
              "complete": not still_truncated, "rounds": rounds, "sections": len(sections),
              "message": ("简历较长，模型一次输出装不下，已分 %d 段还原完 %d 个章节。"
                          % (rounds, len(sections))) if not still_truncated else
                         ("简历较长，已分 %d 段还原出 %d 个章节，但可能还有内容没还原完。"
                          % (rounds, len(sections)))})

    emit({"stage": "done", "sections": len(sections), "confs": len(confs),
          "usage": usage_total, "cost": store.estimate_cost(usage_total, cfg),
          "head": head, "conf_list": confs,
          "truncated": bool(still_truncated),      # true = 结果可能不完整，需提醒用户
          "split_rounds": rounds,                  # >1 = 分了几段才还原完
          "rounds": rounds, "elapsed": round(time.time() - t0, 1)})


# ================================================================ 深度重写

def _project_section_index(resume):
    """定位「项目经历」章节：先按标题含"项目"，找不到就取要点最多的章节。"""
    secs = (resume or {}).get("sections") or []
    for i, s in enumerate(secs):
        if "项目" in (s.get("title") or ""):
            return i
    best_n, best_i = -1, -1
    for i, s in enumerate(secs):
        n = sum(len(b.get("bullets") or []) for b in (s.get("blocks") or []))
        if n > best_n:
            best_n, best_i = n, i
    return best_i


def _section_text(sec):
    """把整段章节拼成一段纯文本 —— 用于数字守恒比对（判断新数字是否原文已有）。"""
    parts = []
    for blk in (sec.get("blocks") or []):
        parts.append(blk.get("heading") or "")
        parts.append(blk.get("role") or "")
        parts.append(blk.get("meta") or "")
        for b in (blk.get("bullets") or []):
            parts.append(b.get("text") or "")
    return " ".join(parts)


_DEEP_TAGS = ("真实", "换角度", "推断数字", "推测经历")

# heading 归一化：去掉空白与常见分隔符，用于「模型块 ↔ 原块」的认回匹配
_HEAD_SEP = re.compile(r"[\s\|｜·・\-—–_/,，、（）()【】\[\]：:]+")


def _norm_head(s):
    return _HEAD_SEP.sub("", str(s or "")).lower()


def _match_orig_index(head, orig_blocks, used):
    """把一个模型 block 认回原 block 的下标；认不回返回 -1。

    先规范化后精确匹配；不行再退化到「包含」（要求 >= 4 字，避免短名误配）。
    模型被要求原样保留 heading，但实测量化/截断都可能把角色后缀吃掉，
    所以包含匹配能救回「智慧园区平台」vs「智慧园区平台 | 产品经理」这类。
    """
    nh = _norm_head(head)
    if nh:
        for i, ob in enumerate(orig_blocks):
            if i in used:
                continue
            if _norm_head(ob.get("heading")) == nh:
                return i
    if len(nh) >= 4:
        for i, ob in enumerate(orig_blocks):
            if i in used:
                continue
            oh = _norm_head(ob.get("heading"))
            if len(oh) >= 4 and (oh in nh or nh in oh):
                return i
    return -1


def _restore_block(ob):
    """把原 block 整段还原成结果 block（bullets 标「真实」并注明未改写）。"""
    return {
        "heading": str(ob.get("heading") or "").strip(),
        "meta": str(ob.get("meta") or "").strip(),
        # 事实字段原样带回（图标 / 角色 / 时间 / 地点）
        "icon": str(ob.get("icon") or "").strip(),
        "role": str(ob.get("role") or "").strip(),
        "date": str(ob.get("date") or "").strip(),
        "place": str(ob.get("place") or "").strip(),
        "intro": str(ob.get("intro") or "").strip(),
        "subhead": str(ob.get("subhead") or "").strip(),
        "bullets": [{
            "text": str(b.get("text") or "").strip(),
            "tag": "真实",
            "note": "模型未改写这一条，已保留原文",
            "jd_ref": [], "added_numbers": [], "level": 1,
        } for b in (ob.get("bullets") or []) if str(b.get("text") or "").strip()],
    }


def _bigrams(s):
    s = re.sub(r"\s+", "", str(s or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _cover_ratio(orig, cand):
    """原要点被某条输出覆盖的比例（字符二元组召回率）。"""
    a = _bigrams(orig)
    if not a:
        return 0.0
    return len(a & _bigrams(cand)) / float(len(a))


def _bullet_label(t):
    """取要点「：」前的短标签（无冒号则取前 8 字），用于认回原要点。"""
    s = str(t or "").strip()
    i = s.find("：")
    if i < 0:
        i = s.find(":")
    if 0 < i <= 24:
        return _norm_head(s[:i])
    return _norm_head(s[:8])


def _conserve_bullets(out_bullets, orig_block, stats):
    """要点守恒：一个 block 内的原要点，模型可以合并 / 改写，但不能整条消失。

    实测事故：工作经历一个 block 2 条要点，模型只回 1 条 → 另一条被整章替换吞掉。
    判定「被覆盖」的信号（任一）：
      1) 原要点的短标签（「：」前）出现在某条输出里；
      2) 与该输出的字符二元组覆盖率 >= 0.5（合并 / 大幅改写时仍能命中）。
    仅当**输出条数少于原条数**时才做还原 —— 相等或更多视为模型做了拆分 / 扩写，
    避免把正常改写误判成「漏条」而重复补回。
    """
    orig_bullets = [b for b in (orig_block.get("bullets") or [])
                    if str(b.get("text") or "").strip()]
    if not orig_bullets or len(out_bullets) >= len(orig_bullets):
        return out_bullets

    texts = [str(b.get("text") or "") for b in out_bullets]
    norms = [_norm_head(t) for t in texts]
    restored = 0
    result = list(out_bullets)
    for oi, ob in enumerate(orig_bullets):
        ot = str(ob.get("text") or "").strip()
        label = _bullet_label(ot)
        covered = False
        for t, nt in zip(texts, norms):
            if label and len(label) >= 4 and label in nt:
                covered = True
                break
            if _cover_ratio(ot, t) >= 0.5:
                covered = True
                break
        if covered:
            continue
        rb = {"text": ot, "tag": "真实", "note": "模型未改写这一条，已保留原文",
              "jd_ref": [], "added_numbers": [], "level": 1}
        at = min(oi, len(result))
        result.insert(at, rb)
        texts.insert(min(at, len(texts)), ot)
        norms.insert(min(at, len(norms)), _norm_head(ot))
        restored += 1
    if restored:
        stats["restored_bullets"] = stats.get("restored_bullets", 0) + restored
    return result


def _conserve_deep_blocks(out_blocks, orig_blocks, stats):
    """条目守恒安全网：原章节有几个条目，结果就必须有几个条目。

    实测事故：项目经历原本 3 个项目，模型只回 1 个；前端是「整章替换」，
    另外 2 个项目被静默丢弃。这里在回传前做一次对账：

    · 模型块按 heading 认回原块（允许重排）；
    · 原块被漏掉 → 整段保留原文（不丢项目/公司），计入 stats["restored"]；
    · 模型块有 heading 却一条要点都没给 → 用原文要点补回（不留空条目）；
    · 模型块的 heading 在原章节里找不到 → 视为「凭空多出条目」，丢弃（红线），计入 stats["dropped"]。

    返回：模型顺序为主、缺失原块就近插回的新列表。
    """
    if not orig_blocks:
        return out_blocks

    assembled = []          # 最终 block 列表
    pos_of_orig = {}        # 原块下标 -> assembled 下标
    used = set()
    dropped = 0
    restored = 0

    for ob in out_blocks:
        mi = _match_orig_index(ob.get("heading"), orig_blocks, used)
        if mi < 0:
            dropped += 1    # 原章节没有 → 不采信，防伪造
            continue
        used.add(mi)
        # heading / meta 是事实（项目名、公司名、时间）：一律以原文为准，
        # 防止模型擅自改名/改时间（即便它按约定不该动）。
        ob["heading"] = str(orig_blocks[mi].get("heading") or ob.get("heading") or "").strip()
        ob["meta"] = str(orig_blocks[mi].get("meta") or ob.get("meta") or "").strip()
        # 事实字段（图标 / 角色 / 时间 / 地点）也原样带回，模型不负责输出它们
        for _k in ("icon", "role", "date", "place"):
            if orig_blocks[mi].get(_k):
                ob[_k] = orig_blocks[mi][_k]
        # 认出原块但要点被清空：用原文要点补回，避免「项目还在、内容没了」
        if not (ob.get("bullets") or []):
            refill = _restore_block(orig_blocks[mi])
            ob["bullets"] = refill["bullets"]
            ob["intro"] = ob.get("intro") or refill["intro"]
            ob["subhead"] = ob.get("subhead") or refill["subhead"]
            restored += 1
        # 要点级守恒：块还在，但模型只回了一部分要点 → 把漏掉的原文要点补回
        ob["bullets"] = _conserve_bullets(ob.get("bullets") or [], orig_blocks[mi], stats)
        pos_of_orig[mi] = len(assembled)
        assembled.append(ob)

    # 把被漏掉的原块就近插回（保持原章节相对顺序）
    for mi in range(len(orig_blocks)):
        if mi in used:
            continue
        rb = _restore_block(orig_blocks[mi])
        if not rb["heading"] and not rb["bullets"]:
            continue
        ins = 0
        for j in range(mi - 1, -1, -1):
            if j in pos_of_orig:
                ins = pos_of_orig[j] + 1
                break
        assembled.insert(ins, rb)
        for k, v in list(pos_of_orig.items()):
            if v >= ins:
                pos_of_orig[k] = v + 1
        pos_of_orig[mi] = ins
        restored += 1

    if restored:
        stats["restored"] = stats.get("restored", 0) + restored
    if dropped:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    return assembled


def _sanitize_deep_result(res, orig_text, orig_blocks=None):
    """给模型产出**加一道本机安全网**：模型可能自称"真实"却塞了新数字。

    规则：一条 bullet 只要引入了原文（整段项目经历）里没有的数字，
    就强制把 tag 降级为「推断数字」并补一句说明 —— 宁可错标成需要核实，
    也不能让用户以为某个数字是原文就有的。

    同时**守住三层结构**：项目介绍（intro）→ 项目工作（subhead）→ 二级要点（level=2）。
    模型漏给 intro/subhead 时，从原 block 兜底补回，避免改写后结构被压平。
    """
    stats = {t: 0 for t in _DEEP_TAGS}
    stats["corrected"] = 0
    orig_blocks = orig_blocks or []
    orig_by_head = {(b.get("heading") or "").strip(): b for b in orig_blocks}
    out_blocks = []
    for bi, blk in enumerate(res.get("blocks") or []):
        heading = str(blk.get("heading") or "").strip()
        ob = orig_by_head.get(heading) or (orig_blocks[bi] if bi < len(orig_blocks) else {})
        subhead = str(blk.get("subhead") or ob.get("subhead") or "").strip()
        intro = str(blk.get("intro") or ob.get("intro") or "").strip()
        nb = {"heading": heading, "meta": str(blk.get("meta") or "").strip(),
              "intro": intro, "subhead": subhead, "bullets": []}
        for b in (blk.get("bullets") or []):
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            tag = b.get("tag") if b.get("tag") in _DEEP_TAGS else "换角度"
            note = str(b.get("note") or "").strip()
            added = rules.number_diff(orig_text, text)
            if added and tag in ("真实", "换角度"):
                tag = "推断数字"
                stats["corrected"] += 1
                if not note:
                    note = "含原文没有的数字：" + "、".join(x["v"] for x in added[:3])
            stats[tag] = stats.get(tag, 0) + 1
            nb["bullets"].append({
                "text": text, "tag": tag, "note": note,
                "jd_ref": b.get("jd_ref") or [],
                "added_numbers": [x["v"] for x in added],
                # 缩进由「二级列表」样式统一给（有 subhead 时列表整体缩进）
                "level": 1,
            })
        if nb["bullets"] or nb["heading"]:
            out_blocks.append(nb)
    # 条目守恒：模型漏掉的项目/公司必须补回，绝不能静默丢条目
    out_blocks = _conserve_deep_blocks(out_blocks, orig_blocks, stats)
    return {"title": str(res.get("title") or "项目经历"), "blocks": out_blocks, "stats": stats}


def _dump_deep_raw(raw, usage, client):
    """把模型原始输出落盘，便于排查"没按格式返回"这类问题。返回文件路径。"""
    try:
        d = os.path.join(HOME, "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "deep-rewrite-raw.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("finish_reason=%s\ntruncated=%s\ntokens_out=%s\nusage=%s\n\n"
                    % (client.last_finish_reason, client.truncated,
                       (usage or {}).get("completion_tokens"), usage))
            f.write(raw[:200000])
        return path
    except Exception:
        return ""

def _select_deep_sections(sections, requested):
    """选出要深度重写的章节索引。

    requested: 前端传来的章节索引列表（用户勾选的）。为空 → 默认「除教育/荣誉/奖项/证书/资质/其他/工作经历外、且内容够长」的全部章节。
    返回去重、保序的索引列表。
    """
    n = len(sections or [])
    if requested:
        out = []
        for x in requested:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and not _is_deep_exempt((sections[i] or {}).get("title")):
                out.append(i)
        seen = set()
        return [i for i in out if not (i in seen or seen.add(i))]
    out = []
    for i, s in enumerate(sections or []):
        if _is_deep_exempt(s.get("title")):
            continue
        if len(_section_text(s).strip()) < 20:
            continue
        out.append(i)
    return out


def _run_deep_section(client, cfg, jd_text, intensity, temp, sec, title, background_payload):
    """对单个章节跑一次 LLM 调用 + 本机安全网。

    返回 (result_or_None, duties, role, error_dict_or_None, truncated_bool)。
    result 是 _sanitize_deep_result 的产出（含 orig / title）。
    """
    prompt = prompts.build_deep_rewrite_prompt(
        jd_text,
        json.dumps(_compact_resume({"sections": [sec]}), ensure_ascii=False),
        intensity=intensity, section_title=title, background_payload=background_payload)
    parser = llm.NDJSONStream()
    duties, role, rewrite = [], "", None
    raw_parts = []
    usage = {}
    try:
        for piece in client.chat(prompt["system"], prompt["user"], temperature=temp, stream=True,
                                 max_tokens=llm.resolve_max_tokens(cfg["llm"])):
            raw_parts.append(piece)
            for obj in parser.feed(piece):
                t = obj.get("type")
                if t == "jd":
                    duties = obj.get("duties") or []
                    role = obj.get("role", "")
                elif t == "rewrite":
                    rewrite = obj
        for obj in parser.flush():
            t = obj.get("type")
            if t == "rewrite" and rewrite is None:
                rewrite = obj
            elif t == "jd" and not duties:
                duties = obj.get("duties") or []
                role = obj.get("role", "")
    except llm.LLMError as e:
        return None, duties, role, e.to_dict(), False
    except Exception as e:
        traceback.print_exc()
        return None, duties, role, {"kind": "server", "message": "本地服务内部错误：%s" % e,
                                   "hint": "看服务窗口的日志。"}, False
    usage = getattr(client, "last_usage", None) or {}
    if not rewrite or not (rewrite.get("blocks") or []):
        truncated = bool(client.truncated) or client.last_finish_reason == "length"
        raw_path = _dump_deep_raw("".join(raw_parts), usage, client)
        if truncated:
            err = {"kind": "format", "message": "模型输出被长度上限截断，整段结果没写完",
                   "hint": "这一章内容较长。可试：① ⚙ 设置里换一个输出上限更大的模型；② 关掉该模型的思考 / 推理模式。"}
        else:
            err = {"kind": "format", "message": "模型没有按约定格式返回整段结果",
                   "hint": "原始输出已存到 logs/deep-rewrite-raw.txt，可据此排查（也可把它发给开发者）。"}
        return None, duties, role, err, truncated
    sec_text = _section_text(sec)
    result = _sanitize_deep_result(rewrite, sec_text, sec.get("blocks"))
    result["orig"] = [{
        "heading": blk.get("heading") or "",
        "meta": blk.get("meta") or "",
        "bullets": [str(b.get("text") or "") for b in (blk.get("bullets") or [])],
    } for blk in (sec.get("blocks") or [])]
    result["title"] = title
    return result, duties, role, None, False


def run_deep_rewrite(body, emit):
    """「章节逐章重写」：识别除教育/荣誉/奖项/证书/资质/其他/工作经历外的章节，逐一重写。

    工作经历是关键履历，用户明确要求整段不动（改写会加「项目介绍/项目工作」等不属于
    工作经历的结构），因此深度重写默认跳过它；逐条评估不受影响。

    每章独立一次 LLM 调用 → 单次输出长度可控，彻底规避「整段超长被截断」的风险。
    与 /api/evaluate 的区别：产出的是**整段新结构**（可重排项目顺序、可拆并 bullet），
    落到前端一个独立的「AI 改写版」快照。三档强度靠提示词区分，红线由本机护栏兜底。
    """
    cfg = store.load_config()
    resume = body.get("resume") or {}
    jd_text = (body.get("jd") or "").strip()
    intensity = body.get("intensity") or cfg["llm"].get("rewrite_style") or "standard"
    if intensity not in prompts.DEEP_INTENSITIES:
        intensity = "standard"

    if not jd_text:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "还没有粘贴目标 JD",
                                          "hint": "先在工作台右侧贴入 JD 文本，再点深度重写。"}})
        return

    sections = resume.get("sections") or []
    targets = _select_deep_sections(sections, body.get("sections"))
    if not targets:
        emit({"stage": "error", "error": {"kind": "bad_request",
                                          "message": "没有可重写的章节",
                                          "hint": "教育/荣誉/奖项/证书/其他/工作经历不参与；且章节需有至少 20 字内容。可勾选其它章节再试。"}})
        return

    client = llm.LLMClient(cfg["llm"])
    # 三档各自给一个合适的"火候"：标准偏确定，激进偏放开；不改变"允许改什么"（那由提示词管）。
    norm = {"standard": 0.25, "bold": 0.45, "aggressive": 0.6}.get(intensity, 0.25)
    temp = llm.resolve_temperature(norm, client.provider, client.model)
    full_payload = json.dumps(_compact_resume(resume), ensure_ascii=False)  # 背景：完整简历

    emit({"stage": "start", "provider": client.provider, "model": client.model,
          "temperature": temp, "intensity": intensity,
          "intensity_name": prompts.DEEP_INTENSITIES[intensity]["name"],
          "total": len(targets),
          "section_titles": [((sections[i] or {}).get("title") or "章节") for i in targets],
          "prompt_chars": 0})

    results = []
    overall = {t: 0 for t in _DEEP_TAGS}
    overall["corrected"] = 0
    any_truncated = False
    duties_all, role_all = [], ""
    total_usage = {}
    total_bullets = 0
    t0 = time.time()

    for pos, idx in enumerate(targets):
        sec = sections[idx]
        title = sec.get("title") or "章节"
        emit({"stage": "section_start", "index": idx, "title": title,
              "pos": pos, "total": len(targets)})
        result, duties, role, err, truncated = _run_deep_section(
            client, cfg, jd_text, intensity, temp, sec, title, full_payload)
        if err:
            any_truncated = any_truncated or truncated
            emit({"stage": "section_error", "index": idx, "title": title,
                  "pos": pos, "total": len(targets), "error": err})
            store.log("深度重写失败（%s）" % title,
                      "模型 %s · %s" % (client.model, (err or {}).get("message", "")), scope="联网")
            continue
        for k, v in (result.get("stats") or {}).items():
            overall[k] = overall.get(k, 0) + v
        if not duties_all and duties:
            duties_all, role_all = duties, role
        u = getattr(client, "last_usage", None) or {}
        for kk in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if u.get(kk) is not None:
                total_usage[kk] = total_usage.get(kk, 0) + u[kk]
        total_bullets += sum(len(b.get("bullets") or []) for b in result.get("blocks") or [])
        results.append({"index": idx, "title": title, "result": result})
        emit({"stage": "section_done", "index": idx, "title": title,
              "pos": pos, "total": len(targets), "result": result,
              "duties": duties, "role": role,
              "intensity": intensity,
              "intensity_name": prompts.DEEP_INTENSITIES[intensity]["name"]})

    if not results:
        emit({"stage": "error", "error": {"kind": "format",
                                          "message": "所有章节都重写失败",
                                          "hint": "看服务窗口的日志，或换一个模型再试。"},
              "usage": total_usage})
        _finish_usage(client, "deep-rewrite", total_usage, 0, t0)
        return

    cost = store.estimate_cost(total_usage, cfg)
    _finish_usage(client, "deep-rewrite", total_usage, total_bullets, t0)
    store.log("深度重写（%s · 逐章 %d 章）" % (prompts.DEEP_INTENSITIES[intensity]["name"], len(results)),
              "章节 %s · 推断/推测 %d 处"
              % ("、".join(r["title"] for r in results),
                 overall.get("推断数字", 0) + overall.get("推测经历", 0)),
              scope="联网")
    emit({"stage": "done", "usage": total_usage, "cost": cost,
          "currency": cfg["llm"].get("currency") or "¥",
          "elapsed": round(time.time() - t0, 1),
          "sections": [{"index": r["index"], "title": r["title"],
                        "blocks": len(r["result"].get("blocks") or []),
                        "stats": r["result"].get("stats") or {}} for r in results],
          "total_sections": len(results),
          "stats": overall,
          "truncated": any_truncated})

# ================================================================ 指令式改写
# 用户选中一段内容（一条要点 / 一段经历 / 一个章节）+ 给出自然语言修改要求，
# 只让 AI 改这一段 —— 像 vibe coding 一样改简历。
# 复用同一条流式解析 + 数字守恒安检；产出交给前端「对比 → 接受/拒绝/再改改」，
# 绝不自动改正文。

_SCOPE_KIND_NAME = {"bullet": "一条要点", "intro": "一段项目介绍",
                    "block": "一段经历", "section": "一个章节"}


def _scope_blocks(content):
    """把 scope 内容规整成统一的 blocks 形状： [{heading, meta, intro, bullets:[{bid,text}]}]。

    三种粒度传进来的形状不同，这里统一，后续 prompt / 数字守恒 / 前端都用同一套。
    """
    if not isinstance(content, dict):
        return []
    if content.get("blocks"):
        return [b for b in content["blocks"] if isinstance(b, dict)]
    if content.get("bullets") is not None or content.get("heading") is not None:
        return [content]
    if content.get("text") is not None:
        return [{"heading": "", "meta": "", "intro": "",
                 "bullets": [{"bid": content.get("bid") or "", "text": content.get("text") or ""}]}]
    return []


def _scope_text(blocks):
    """把选中范围拼成一段纯文本 —— 数字守恒比对的"原文"。"""
    parts = []
    for b in (blocks or []):
        parts.append(str(b.get("heading") or ""))
        parts.append(str(b.get("meta") or ""))
        parts.append(str(b.get("intro") or ""))
        parts.append(str(b.get("subhead") or ""))
        for x in (b.get("bullets") or []):
            parts.append(str(x.get("text") or ""))
    return " ".join(p for p in parts if p)


def _sanitize_scope_result(res, orig_text):
    """给「指令式改写」的产出加本机安全网（与深度重写同源）。

    模型可能按用户要求补了量化数字 —— 只要某个数字在**选中范围的原文**里没有，
    就逐条列进 numbers_added，让前端高亮 + 提醒核实。宁可错标成"要核实"，
    也不能让用户以为某个数字是原文就有的。
    """
    out_blocks = []
    n_num = 0
    for blk in (res.get("blocks") or []):
        if not isinstance(blk, dict):
            continue
        nb = {
            "heading": str(blk.get("heading") or "").strip(),
            "meta": str(blk.get("meta") or "").strip(),
            "intro": str(blk.get("intro") or "").strip(),
            "subhead": str(blk.get("subhead") or "").strip(),
            "bullets": [],
        }
        for b in (blk.get("bullets") or []):
            if not isinstance(b, dict):
                continue
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            added = rules.number_diff(orig_text, text)
            if added:
                n_num += 1
            nb["bullets"].append({
                "ref": str(b.get("ref") or "").strip(),
                "text": text,
                "ai_added_spans": [w for w in (b.get("ai_added_spans") or []) if w],
                "inferred_facts": _norm_inferred(b.get("inferred_facts")),
                "note": str(b.get("note") or "").strip(),
                "numbers_added": [x["v"] for x in added],
            })
        # 项目介绍也是可单独改的一段 —— 同样做数字守恒
        nb["intro_numbers_added"] = []
        if nb["intro"]:
            added_i = rules.number_diff(orig_text, nb["intro"])
            if added_i:
                n_num += 1
            nb["intro_numbers_added"] = [x["v"] for x in added_i]
        if nb["bullets"] or nb["intro"] or nb["heading"]:
            out_blocks.append(nb)
    return {"blocks": out_blocks, "reason": str(res.get("reason") or "").strip(),
            "numbers_added": n_num}


def run_scope_rewrite(body, emit):
    """按用户指令，只改写他选中的那一段（bullet / block / section）。"""
    cfg = store.load_config()
    scope = body.get("scope") or {}
    instruction = (body.get("instruction") or "").strip()
    jd_text = (body.get("jd") or "").strip()
    style = body.get("style") or cfg["llm"].get("rewrite_style") or "standard"
    prev = body.get("prev") or None
    kind = scope.get("kind")

    if not instruction:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "还没有写修改要求",
                                          "hint": "告诉 AI 你想怎么改这一段，例如「缩短到两行以内」。"}})
        return
    blocks = _scope_blocks(scope.get("content"))
    if not blocks:
        emit({"stage": "error", "error": {"kind": "bad_request", "message": "选中的内容为空",
                                          "hint": "重新选中要修改的内容。"}})
        return
    orig_text = _scope_text(blocks)

    client = llm.LLMClient(cfg["llm"])
    temp = llm.resolve_temperature(
        body.get("temperature", cfg["llm"].get("temperature")), client.provider, client.model)
    prompt = prompts.build_scope_rewrite_prompt(
        {"kind": kind, "content": scope.get("content")}, instruction, jd_text, style=style, prev=prev)

    emit({"stage": "start", "provider": client.provider, "model": client.model,
          "temperature": temp, "kind": kind,
          "kind_name": _SCOPE_KIND_NAME.get(kind, "一段内容"),
          "instruction": instruction, "prompt_chars": len(prompt["user"])})

    parser = llm.NDJSONStream()
    result, t0 = None, time.time()
    raw_parts = []
    try:
        for piece in client.chat(prompt["system"], prompt["user"], temperature=temp, stream=True,
                                 max_tokens=llm.resolve_max_tokens(cfg["llm"])):
            raw_parts.append(piece)
            for obj in parser.feed(piece):
                if obj.get("type") == "scope":
                    result = obj
        for obj in parser.flush():
            if obj.get("type") == "scope" and result is None:
                result = obj
    except llm.LLMError as e:
        emit({"stage": "error", "error": e.to_dict()})
        _finish_usage(client, "rewrite-scope", {}, 0, t0)
        return
    except Exception as e:
        traceback.print_exc()
        emit({"stage": "error", "error": {"kind": "server", "message": "本地服务内部错误：%s" % e,
                                          "hint": "看服务窗口的日志。"}})
        return

    usage = getattr(client, "last_usage", None) or {}
    if not result or not (result.get("blocks") or []):
        truncated = bool(client.truncated) or client.last_finish_reason == "length"
        raw_path = _dump_deep_raw("".join(raw_parts), usage, client)
        if truncated:
            err = {"kind": "format", "message": "模型输出被长度上限截断，改写没写完",
                   "hint": "选中的范围可能偏大。可缩小范围（改成只选一段经历），或换输出上限更大的模型。"}
        else:
            err = {"kind": "format", "message": "模型没有按约定格式返回改写结果",
                   "hint": "原始输出已存到 logs/deep-rewrite-raw.txt，可据此排查（也可发给开发者）。"}
        store.log("指令式改写失败（格式）",
                  "模型 %s · 输出 %s tok · finish=%s · 原始输出 %s"
                  % (client.model, (usage or {}).get("completion_tokens"),
                     client.last_finish_reason, raw_path or "未落盘"), scope="联网")
        emit({"stage": "error", "error": err, "usage": usage})
        _finish_usage(client, "rewrite-scope", usage, 0, t0)
        return

    result = _sanitize_scope_result(result, orig_text)
    nb = sum(len(b["bullets"]) for b in result["blocks"])
    emit({"stage": "scope", "result": result, "kind": kind,
          "kind_name": _SCOPE_KIND_NAME.get(kind, "一段内容"),
          "instruction": instruction, "style": style})

    cost = store.estimate_cost(usage, cfg)
    _finish_usage(client, "rewrite-scope", usage, nb, t0)
    store.log("指令式改写（%s）" % _SCOPE_KIND_NAME.get(kind, "一段内容"),
              "改写 %d 条要点 · 新增数字 %d 处" % (nb, result.get("numbers_added", 0)), scope="联网")
    emit({"stage": "done", "usage": usage, "cost": cost,
          "currency": cfg["llm"].get("currency") or "¥",
          "elapsed": round(time.time() - t0, 1), "bullets": nb})


# ================================================================== HTTP

def _extract_json(text):
    """从模型输出里抠出 JSON —— 容忍 ```json 包裹和前后废话。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ResumeWorkbench/" + VERSION

    # ---------- 工具 ----------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, mime, code=200, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _body_json(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

    def _sse_send(self, obj):
        try:
            payload = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def log_message(self, fmt, *args):
        """只在服务窗口打印 API 调用，方便用户看到"正在发生什么"。"""
        try:
            if "/api/" in self.path:
                sys.stderr.write("  · %s\n" % self.requestline)
        except Exception:
            pass

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p == "/api/health":
                return self.api_health()
            if p == "/api/config":
                return self._json({"ok": True, "config": store.public_config(),
                                   "presets": store.PROVIDER_PRESETS})
            if p == "/api/usage":
                return self._json({"ok": True, "usage": store.read_usage_with_cost()})
            if p == "/api/logs":
                return self._json({"ok": True, "logs": store.read_logs()})
            if p == "/api/data":
                return self._json({"ok": True, "data": store.data_overview()})
            if p == "/api/env":
                return self._json({"ok": True, "env": docio.env_report()})
            if p == "/api/templates":
                return self.api_templates()
            if p in ("/", "/index.html"):
                return self.serve_index()
            if p == "/favicon.ico":
                return self._bytes(b"", "image/x-icon")
            return self._json({"ok": False, "error": "not_found", "path": p}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"ok": False, "error": "server", "message": str(e)}, 500)

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/logs":
            store.clear_logs()
            return self._json({"ok": True})
        if p == "/api/usage/session":
            store.reset_session_usage()
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/config":
                return self.api_config()
            if p == "/api/test-connection":
                return self.api_test_connection()
            if p == "/api/evaluate":
                return self.sse(lambda emit: run_evaluate(self._body_json(), emit))
            if p == "/api/deep-rewrite":
                return self.sse(lambda emit: run_deep_rewrite(self._body_json(), emit))
            if p == "/api/rewrite-scope":
                return self.sse(lambda emit: run_scope_rewrite(self._body_json(), emit))
            if p == "/api/rebuild":
                return self.sse(lambda emit: run_rebuild(self._body_json(), emit))
            if p == "/api/parse-pdf":
                return self.api_parse_pdf()
            if p == "/api/export-pdf":
                return self.api_export_pdf()
            if p == "/api/render":
                return self.api_render()
            if p == "/api/snapshot":
                body = self._body_json()
                name = store.save_snapshot(body)
                return self._json({"ok": True, "name": name})
            if p == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0))).start()
                return
            return self._json({"ok": False, "error": "not_found", "path": p}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"ok": False, "error": "server", "message": str(e)}, 500)

    # ---------- 各接口 ----------
    def serve_index(self):
        if not os.path.exists(INDEX):
            return self._json({"ok": False, "error": "index_missing",
                               "message": "找不到 index.html", "hint": INDEX}, 500)
        with open(INDEX, "rb") as f:
            data = f.read()
        return self._bytes(data, "text/html; charset=utf-8")

    def api_health(self):
        cfg = store.load_config()
        c = docio.env_report()
        return self._json({
            "ok": True,
            "service": "resume-workbench",
            "version": VERSION,
            "port": PORT,
            "uptime": int(time.time() - _START_TS),
            "ai_ready": bool(cfg["llm"].get("api_key")),
            "provider": cfg["llm"].get("provider"),
            "model": cfg["llm"].get("model"),
            "ai_level": cfg["llm"].get("ai_level"),
            "rewrite_style": cfg["llm"].get("rewrite_style") or cfg["llm"].get("ai_level") or "standard",
            "pure_rule": bool(cfg["llm"].get("pure_rule")),
            "temperature": cfg["llm"].get("temperature"),
            # 「项目经历整段重写」的三档强度（前端据此渲染选择，单一事实来源）
            "deep_intensities": prompts.DEEP_INTENSITIES,
            # 全局「AI 改写风格」四档（前端据此渲染设置面板，单一事实来源）
            "rewrite_styles": getattr(prompts, "REWRITE_STYLES", None),
            "pdf_lib_ok": c["pdf_lib_ok"],
            "pdf_lib": c["pdf_lib"],
            "pdf_lib_source": c.get("pdf_lib_source", ""),
            # 几何结构解析（PyMuPDF）是否可用 —— 决定 PDF 导入的结构质量
            "structured_ok": bool(c.get("structured_ok")),
            "browser_ok": bool(c["browser"]),
            "browser": c.get("browser_name") or "",
            # 解释器信息：用户报"上传 PDF 失败"时，第一件要确认的就是"跑的是哪个 Python"
            "python": c.get("python", ""),
            "python_exe": c.get("python_exe", ""),
            "home": HOME,
            # 构建标记：重启后可在 /api/health 确认「跑的是不是带修复的最新服务端」
            "build": "2026-11-10-deep-conserve-v3",
        })

    def api_config(self):
        body = self._body_json()
        if body.get("reset"):
            store.save_config({"llm": {"api_key": ""}})
            return self._json({"ok": True, "config": store.public_config()})
        cfg = store.save_config(body)
        store.log("更新设置", "服务商 %s · 模型 %s · 档位 %s · 温度 %.2f"
                  % (cfg["llm"].get("provider"), cfg["llm"].get("model"),
                     cfg["llm"].get("ai_level"), float(cfg["llm"].get("temperature") or 0)))
        return self._json({"ok": True, "config": store.public_config()})

    def api_test_connection(self):
        body = self._body_json()
        cfg = store.load_config()
        # 允许用表单里尚未保存的值直接测试
        merged = json.loads(json.dumps(cfg["llm"], ensure_ascii=False))
        for k in ("provider", "base_url", "model", "timeout"):
            if body.get(k):
                merged[k] = body[k]
        if body.get("api_key") and body.get("api_key") != "__KEEP__":
            merged["api_key"] = body["api_key"]
        if body.get("temperature") is not None and body.get("temperature") != "":
            merged["temperature"] = body["temperature"]
        client = llm.LLMClient(merged)
        try:
            res = client.test()
            store.log("测试连接成功", "模型 %s · 温度 %s · %dms"
                      % (res["model"], res["temperature"], res["ms"]), scope="联网")
            return self._json({"ok": True, "result": res})
        except llm.LLMError as e:
            store.log("测试连接失败", "%s：%s" % (e.kind, e.message), scope="联网")
            return self._json({"ok": False, "error": e.to_dict()})
        except Exception as e:
            return self._json({"ok": False, "error": {"kind": "server", "message": str(e), "hint": ""}})

    def sse(self, worker):
        """开一个 SSE 通道，把 worker 里 emit 的事件逐条推给前端。"""
        self._sse_open()
        closed = {"v": False}

        def emit(evt):
            if closed["v"]:
                return
            try:
                self._sse_send(evt)
            except (BrokenPipeError, ConnectionResetError):
                closed["v"] = True

        try:
            worker(emit)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            traceback.print_exc()
            emit({"stage": "error", "error": {"kind": "server", "message": str(e),
                                              "hint": "看服务窗口日志。"}})
        try:
            self._sse_send({"stage": "end"})
        except Exception:
            pass

    def api_parse_pdf(self):
        raw = self._body()
        if not raw:
            return self._json({"ok": False, "error": "empty", "message": "没有收到文件内容"})
        filename = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.pdf")
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix="rw-pdf-"), "in.pdf")
        with open(tmp, "wb") as f:
            f.write(raw)
        res = docio.extract_pdf(tmp)
        if res.get("ok"):
            # 几何驱动的结构解析（PyMuPDF）。成功时前端直接拿结构化结果，
            # 不必再走「AI 重建结构」—— 结构本来就是确定性的，交给 AI 反而更不稳。
            st = docio.extract_structured(tmp)
            if st.get("ok"):
                # 几何先把**每一行取准**（字符直接从 PDF 文本层读，不存在认错），
                # 再让模型判断"第几行是什么角色"—— 它只回行号、一个字都不写，
                # 于是既借到模型的版面理解，又不可能编造或写错内容。
                ai = self._ai_struct(st)
                if ai and ai.get("resume"):
                    st["resume"] = ai["resume"]
                    st["structured_by"] = "ai"
                    st["ai_stats"] = ai.get("stats")
                else:
                    st["structured_by"] = "rules"
                    if ai and ai.get("error"):
                        st["ai_note"] = ai["error"]

                res["structured"] = st
                res["struct_metrics"] = st.get("metrics")
                secs = st["resume"].get("sections") or []
                nb = sum(len(b.get("bullets") or []) for s in secs for b in (s.get("blocks") or []))
                by = "AI 判构" if st.get("structured_by") == "ai" else "几何规则"
                store.log("解析 PDF（%s）" % by,
                          "%s · %s 页 · 章节 %d · 条目 %d · 要点 %d"
                          % (filename, res["pages"], len(secs),
                             sum(len(s.get("blocks") or []) for s in secs), nb))
            else:
                res["structured_error"] = {"message": st.get("message"), "hint": st.get("hint"),
                                           "quality": st.get("quality")}
                store.log("解析 PDF（几何解析不可用）",
                          "%s · %s 页 · %s"
                          % (filename, res["pages"], (st.get("message") or "")[:40]))
            res["filename"] = filename
        try:
            os.remove(tmp)
        except Exception:
            pass
        return self._json({"ok": bool(res.get("ok")), "result": res})

    def _ai_struct(self, st):
        """让模型判断"第几行是什么角色"，只取行号，文字全部从原文取。

        返回 {"resume": ..., "stats": ...} 或 {"error": "人话原因"} 或 None（没配 Key）。
        任何失败都只返回错误、不抛异常 —— 调用方据此降级回几何规则，
        绝不因为 AI 出问题就让整个上传解析挂掉。
        """
        cfg = (store.load_config().get("llm") or {})
        if not cfg.get("api_key"):
            return {"error": "没配 API Key，本次用几何规则解析"}
        if cfg.get("struct_ai") is False:
            return None
        lines = st.get("lines") or []
        if len(lines) < 3:
            return {"error": "内容太少，不值得调用模型"}
        try:
            ps = docio.load_pdfstruct()
        except Exception as e:
            return {"error": "几何解析组件不可用：%s" % e}

        try:
            ws = ps.ai_worksheet(st)
            if not ws.strip():
                return {"error": "版面描述表为空"}
            client = llm.LLMClient(cfg)
            txt = client.chat(prompts.STRUCT_SYSTEM, prompts.build_struct_prompt(ws, len(lines)),
                              temperature=0, stream=False, timeout=90)
            m = _extract_json(txt)
            if m is None:
                return {"error": "模型没有返回可解析的 JSON"}
            ok, issues, stats = ps.validate_line_map(len(lines), m)
            if not ok:
                return {"error": "模型返回的结构不可用：" + "；".join(issues[:2]),
                        "stats": stats}
            resume = ps.build_from_line_map(st, m)
            # 组装结果再过一遍质量闸门：模型全标错的话这里会拦住
            secs = resume.get("sections") or []
            if not secs:
                return {"error": "模型没标出任何章节", "stats": stats}
            if not any((b.get("bullets") or b.get("heading"))
                       for s in secs for b in (s.get("blocks") or [])):
                return {"error": "模型标出的条目全是空的", "stats": stats}
            return {"resume": resume, "stats": stats}
        except Exception as e:
            return {"error": "AI 判构失败：%s" % e}

    def api_export_pdf(self):
        body = self._body_json()
        html = body.get("html") or ""
        if len(html) < 200:
            return self._json({"ok": False, "error": "bad_request", "message": "没有收到简历 HTML"})
        outdir = os.path.join(HOME, "export")
        name = body.get("filename") or ("resume-%s.pdf" % time.strftime("%Y%m%d-%H%M%S"))
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|')
        out_path = os.path.join(outdir, safe)
        res = docio.export_pdf(html, out_path)
        if not res.get("ok"):
            return self._json({"ok": False, "error": res.get("error"), "message": res.get("message"),
                               "hint": res.get("hint")}, 200)
        pages = docio.pdf_page_count(out_path)
        store.log("导出 PDF", "%s · %s 页 · %.0f KB" % (safe, pages if pages else "?", res["size"] / 1024))
        with open(out_path, "rb") as f:
            data = f.read()
        quoted = urllib.parse.quote(safe)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quoted)
        self.send_header("X-Page-Count", str(pages if pages else ""))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    # ---------- 模板库 ----------
    def api_templates(self):
        """列出内置模板与配色 —— 前端「换模板」面板用。"""
        try:
            tpl = docio.load_templates()
        except Exception as e:
            return self._json({"ok": False, "error": "templates_unavailable",
                               "message": str(e), "hint": "确认 tools/templates.py 还在"}, 200)
        return self._json({
            "ok": True,
            "templates": tpl.list_templates(),
            "themes": [{"id": k, "values": v} for k, v in tpl.THEME_PRESETS.items()],
            "keys": list(tpl.DEFAULT_THEME),
        })

    def api_render(self):
        """把简历数据套进内置模板，返回完整 HTML（不落盘）。

        导出 PDF 复用已有的 /api/export-pdf：前端拿到这里返回的 HTML 再传过去，
        PDF 链路不必写第二遍。
        """
        body = self._body_json()
        resume = body.get("resume") or {}
        template = body.get("template") or "classic"
        theme = body.get("theme") or None
        filename = body.get("filename") or ""

        try:
            tpl = docio.load_templates()
        except Exception as e:
            return self._json({"ok": False, "error": "templates_unavailable",
                               "message": str(e)}, 200)

        auto_fit = bool(body.get("auto_fit"))
        nm = (resume.get("name") or "").strip()
        title = (nm + " 的简历") if nm else None

        # 用户自定义排版（字体 / 行距 / 字号）—— 只接受安全字符，避免 CSS 注入
        font = str(body.get("font") or "").strip()
        if font and not re.match(r"^[A-Za-z0-9 \-,\.'\"_\u4e00-\u9fa5]+$", font):
            font = ""
        line_height = str(body.get("line_height") or "").strip()
        if line_height and not re.match(r"^[0-9.]+$", line_height):
            line_height = ""
        try:
            font_scale = float(body.get("font_scale"))
        except (TypeError, ValueError):
            font_scale = None
        if font_scale is not None:
            font_scale = max(0.8, min(1.2, font_scale))

        def build(sc=None):
            return tpl.render(resume, template, theme=theme, title=title, scale=sc,
                              font=(font or None), line_height=(line_height or None),
                              font_scale=font_scale)

        try:
            if isinstance(theme, str):
                theme = tpl.resolve_theme(theme)
            html = build()
        except ValueError as e:
            return self._json({"ok": False, "error": "unknown_template",
                               "message": str(e)}, 200)
        except Exception as e:
            traceback.print_exc()
            return self._json({"ok": False, "error": "render_failed",
                               "message": str(e)}, 200)

        # ---- 自动压页 ----
        # 内容超出 A4 就整体缩小（zoom 是按比例的，所以量一次高度就能算出比例）。
        # 下限 0.62：再小就不像简历了，宁可让用户换紧凑模板。
        scale, fit_note = None, ""
        if auto_fit:
            h0 = docio.measure_html_height(html)
            if h0 and h0 > docio.A4_PX_H:
                s = max(0.62, docio.A4_PX_H / float(h0))
                if s < 0.999:
                    html2 = build(s)
                    h1 = docio.measure_html_height(html2)
                    # 线性估算总有一点点误差，超出就用实测值再校一次
                    if h1 and h1 > docio.A4_PX_H:
                        s2 = max(0.62, s * docio.A4_PX_H / float(h1))
                        if s2 < s - 0.005:
                            html2, s = build(s2), s2
                    html, scale = html2, round(s, 3)
                    fit_note = "为压进一页整体缩到 %d%%" % round(s * 100)

        store.log("套模板渲染",
                  "%s · %s%s" % (template, filename or "未命名",
                                 (" · " + fit_note) if fit_note else ""))

        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if scale:
            self.send_header("X-Fit-Scale", str(scale))
        if fit_note:
            self.send_header("X-Fit-Note", urllib.parse.quote(fit_note))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)


# ================================================================== 启动

def banner():
    c = docio.env_report()
    cfg = store.load_config()
    lines = [
        "",
        "  简历 AI 工作台 · 本地服务  v" + VERSION,
        "  " + "-" * 52,
        "  访问地址   http://%s:%d/" % (BIND, PORT),
        "  工作目录   %s" % HOME,
        "  Python     %s" % c["python"],
        "  解释器     %s" % c["python_exe"],
        "  PDF 解析   %s" % (("✓ " + c["pdf_lib"] + "（" + (c.get("pdf_lib_source") or "已装") + "）")
                            if c["pdf_lib_ok"]
                            else "✗ 不可用（可用「粘贴简历文本」替代）"),
        "  导出 PDF   %s" % (("✓ " + c["browser_name"]) if c["browser"] else "✗ 未找到 Edge/Chrome"),
        "  AI 配置    %s" % (("✓ 已填写 API Key（%s / %s）" % (cfg["llm"].get("provider"), cfg["llm"].get("model")))
                              if cfg["llm"].get("api_key") else "○ 尚未填写 API Key（去页面右上角 ⚙ 设置）"),
        "  " + "-" * 52,
        "  关闭本窗口即停止服务。Ctrl+C 亦可。",
        "",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


class Server(ThreadingHTTPServer):
    """注意：Windows 下 SO_REUSEADDR 会**允许两个进程绑同一端口**，
    于是"重复双击 start.bat"会静默起两个服务、请求随机落到其中一个（极难排查）。
    这里显式关掉，让第二次绑定直接失败并给出提示。"""
    allow_reuse_address = False
    daemon_threads = True


def main():
    # 双击 start.bat 时，cmd 已切到 UTF-8 代码页；这里让 Python 也用 UTF-8 输出，
    # 中文自检信息才不会变成乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    banner()

    try:
        srv = Server((BIND, PORT), Handler)
    except OSError:
        sys.stdout.write(
            "\n  [启动失败] 端口 %d 已被占用 —— 很可能本服务已经在运行了。\n"
            "  处理办法：\n"
            "    ① 直接用已开着的那个窗口（浏览器打开 http://%s:%d/ 即可）；\n"
            "    ② 或关掉旧窗口后重试；\n"
            "    ③ 或换个端口启动：  set RW_PORT=8531 && start.bat\n\n"
            % (PORT, BIND, PORT))
        sys.stdout.flush()
        return 1

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\n  已停止。\n")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
