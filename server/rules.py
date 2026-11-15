# -*- coding: utf-8 -*-
"""
服务端规则引擎 —— 与前端 index.html 里的实现**逐条等价**。

为什么要两份：前端要即时（用户敲一个字就刷新），服务端要独立复算（不信任前端、
也不信任 LLM 的自我声明）。两份用同一套常量与算法，前端测试用例可原样搬过来验证。

核心理念（设计文档 §2.1）：**"包装"与"编造"的唯一不可逾越边界是数字**。
"""

import re

# ---------------------------------------------------------------- 常量（与前端一致）

CN_D = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2, "三": 3, "叁": 3,
        "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
        "八": 8, "捌": 8, "九": 9, "玖": 9}
CN_U = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
UNIT_MULTI = ["省分", "百分", "以上", "以下", "多个"]
UNIT_ONE = "个项条人次款套省家份页张周天月年万亿元台门场轮版级名位倍步处组类种层模块系统功能"
NUM_UNIT_SKIP_LEAD = ["第", "首", "初", "星", "周", "期", "礼", "一"]

CN_CHARS = "〇零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟"
_RE_CN = re.compile("[" + CN_CHARS + "]+")
_RE_AR = re.compile(r"\d+(?:[.,]\d+)?")
_RE_DATE = re.compile(r"^\d{4}[.\-/]\d{1,2}")
_RE_LATIN = re.compile(r"[A-Za-z]")
_RE_TILDE = re.compile(r"[约近余多上]")
_RE_FULLDIGIT = re.compile(r"[０-９]")

ACT_VERBS = ["主导", "负责", "牵头", "统筹", "推动", "组织", "带领", "独立完成", "设计", "规划",
             "定义", "搭建", "制定", "建模", "拆解", "输出方案", "完成", "落地", "交付", "实施",
             "推进", "输出", "编写", "沉淀", "固化", "分析", "调研", "评估", "复盘", "优化",
             "调优", "归因", "排查", "支撑", "对接"]
RESULT_WORDS = ["提升", "降低", "减少", "增长", "达成", "覆盖", "缩短", "节约", "上线", "验收",
                "签约", "采纳", "复用", "推广", "沉淀为", "获评", "节省", "通过验收"]

TONE_A = ["赋能", "赋能业务", "全面赋能", "深度赋能", "抓手", "拉通", "对齐", "闭环", "形成闭环",
          "链路", "全链路", "范式", "方法论", "体系建设", "生态", "生态位", "沉淀复用", "打造",
          "助力", "保驾护航", "强力支撑", "高度契合", "深度融合", "充分挖掘", "精细化运营",
          "全方位", "立体化", "多维度", "颗粒度", "打法", "组合拳", "护城河", "破局", "出圈"]
TONE_B = ["沉淀", "打通", "中台", "复刻", "量化", "复盘", "渗透", "复用", "流程化", "标准化",
          "机制化", "工具化"]
TONE_EXEMPT = ["SOP", "Workflow", "RAG", "渗透率", "交付", "验收", "迭代", "归因", "技能", "工作流"]

HARD_SIGNALS = ["日期解析失败", "必填区块为空"]


# ---------------------------------------------------------------- 数字提取

def _to_half_width(s):
    return _RE_FULLDIGIT.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), s)


def unit_at(text, j):
    """单位识别：长单位优先；若遇到量词（个/项/条…），再看它后面是否跟多字单位。

    —— 这样「5 个省分」归一化成 "5省分"，而不是断成 "5个"。
    """
    n = len(text)
    for u in UNIT_MULTI:
        if text.startswith(u, j):
            return u
    ch1 = text[j] if j < n else ""
    if ch1 and ch1 in UNIT_ONE:
        for u in UNIT_MULTI:
            if text.startswith(u, j + 1):
                return u
        return ch1
    if ch1 and re.match(r"[%¥+％]", ch1):
        return ch1
    if ch1 == " " and j + 1 < n and re.match(r"[%¥+％]", text[j + 1]):
        return text[j + 1]
    return ""


def cjk2num(s):
    """中文数字 → 阿拉伯。'三'→3、'十二'→12、'上万'→10000（「万」单独出现隐含 1）。"""
    total = 0
    section = 0
    num = 0
    for ch in s:
        if ch in CN_D:
            num = CN_D[ch]
        elif ch in CN_U:
            section += (num if num else 1) * CN_U[ch]
            num = 0
        elif ch == "万":
            section += num
            if not section:
                section = 1
            total += section * 10000
            section = 0
            num = 0
        elif ch == "亿":
            section += num
            if not section:
                section = 1
            total += section * 100000000
            section = 0
            num = 0
    return total + section + num


def extract_numbers(text):
    """提取归一化数字集合 → [{v, raw, idx}]。v 形如 '3项' / '~10000条' / '30+'。"""
    text = _to_half_width(str(text or ""))
    out = []

    def is_model_or_version(i, ln):
        before = text[i - 1] if i > 0 else ""
        after = text[i + ln] if i + ln < len(text) else ""
        if _RE_DATE.match(text[i:]):
            return True
        if _RE_LATIN.match(before or " ") or _RE_LATIN.match(after or " "):
            return True
        if before == "." or after == ".":
            return True
        return False

    # 1) 阿拉伯数字
    for m in _RE_AR.finditer(text):
        i, raw = m.start(), m.group(0)
        if is_model_or_version(i, len(raw)):
            continue
        if i > 0 and text[i - 1] == "第":
            continue
        if re.search(r"[vV]$", text[max(0, i - 2):i]):
            continue
        j = i + len(raw)
        if j < len(text) and text[j] == " ":
            j += 1
        unit = unit_at(text, j)
        tilde = "~" if (i > 0 and _RE_TILDE.match(text[i - 1])) else ""
        out.append({"v": tilde + raw + unit, "raw": raw + unit, "idx": i})

    # 2) 中文数字
    for m in _RE_CN.finditer(text):
        i, raw = m.start(), m.group(0)
        before = text[i - 1] if i > 0 else ""
        if before in NUM_UNIT_SKIP_LEAD:
            continue
        j = i + len(raw)
        if j < len(text) and text[j] == " ":
            j += 1
        unit = unit_at(text, j)
        if not unit:
            continue
        n = cjk2num(raw)
        if not n:
            continue
        tilde = "~" if (before and _RE_TILDE.match(before)) else ""
        out.append({"v": tilde + str(n) + unit, "raw": raw + unit, "idx": i})

    return out


def number_diff(original, rewritten):
    """数字守恒：Set(改写) − Set(原文)，非空即告警。"""
    a = set(x["v"] for x in extract_numbers(original))
    return [x for x in extract_numbers(rewritten) if x["v"] not in a]


# ---------------------------------------------------------------- AI 腔（分级）

def _count_tone(words, masked):
    hits = []
    work = sorted(words, key=len, reverse=True)      # 长词优先，避免重复计数
    m = masked
    for w in work:
        n = m.count(w)
        if n > 0:
            hits.append({"w": w, "n": n})
            m = m.replace(w, "§" * len(w))
    return hits


def find_tone(text):
    t = str(text or "")
    masked = t
    for w in TONE_EXEMPT:                            # C 级豁免先占位
        masked = masked.replace(w, "§" * len(w))
    return {"A": _count_tone(TONE_A, masked), "B": _count_tone(TONE_B, masked)}


# ---------------------------------------------------------------- 长度波动（§7.8）

def length_check(original, rewritten):
    """按 bullet 原长度分三档；短 bullet 用绝对值兜底。"""
    o = len(str(original or ""))
    r = len(str(rewritten or ""))
    delta = r - o
    pct = round(delta / o * 100) if o else 0
    level, hint, strong = "ok", "", False
    if o < 20:
        if abs(delta) > 30:
            level, strong = "strong", True
        elif abs(delta) > 15:
            level = "hint"
    elif o <= 60:
        if abs(pct) >= 60:
            level, strong = "strong", True
        elif abs(pct) >= 30:
            level = "hint"
    else:
        if abs(pct) >= 50:
            level, strong = "strong", True
        elif abs(pct) >= 25:
            level = "hint"
    if level == "ok":
        return {"level": level, "delta": delta, "pct": pct, "hint": ""}
    if delta > 0:
        hint = "变长 %d 字（+%d%%）· 表述偏冗长，注意页数" % (delta, pct)
    else:
        lost = len(extract_numbers(original)) - len(extract_numbers(rewritten))
        if lost > 0:
            hint = "变短 %d 字，且数字减少 %d 个" % (-delta, lost)
        else:
            hint = "变短 %d 字 · 仅删减修饰，正常" % (-delta)
            if lost == 0 and level == "hint":
                return {"level": "ok", "delta": delta, "pct": pct, "hint": "精简修饰，正常"}
    return {"level": level, "delta": delta, "pct": pct, "hint": hint, "strong": strong}


# ---------------------------------------------------------------- STAR 启发式（§7.5）

def star_check(text):
    """三档判定，**永远不判"不合格"** —— AI 不该要求用户必须有数字。"""
    t = str(text or "")
    action = next((v for v in ACT_VERBS if v in t), "")
    numbers = extract_numbers(t)
    result = next((w for w in RESULT_WORDS if w in t), "")
    missing = []
    if not action:
        missing.append("动作")
    if not numbers:
        missing.append("数字")
    if not result:
        missing.append("结果")
    level = "full" if not missing else ("partial" if len(missing) == 1 else "missing")
    return {"level": level, "action": action, "number": numbers[0]["raw"] if numbers else "",
            "result": result, "missing": missing}


# ---------------------------------------------------------------- 置信度三色（§7.4）

def conf_level(c):
    if not c:
        return "ok"
    sigs = c.get("signals") or []
    conf = c.get("conf") if isinstance(c.get("conf"), (int, float)) else 0.95
    hard = sum(1 for s in sigs if any(h in s for h in HARD_SIGNALS))
    if conf < 0.5 or hard >= 2:
        return "needfix"
    if conf < 0.85 or len(sigs) >= 1:
        return "warn"
    return "ok"


def conf_calc(signal_weights, llm_self=None):
    """LLM 自报只能压低、不能抬高（自报置信度校准很差）。"""
    conf = 1 - min(0.9, sum(signal_weights or []) * 0.15)
    return min(conf, llm_self if isinstance(llm_self, (int, float)) else conf)


# ---------------------------------------------------------------- 汇总

def local_rules_report(resume):
    """规则层体检：不依赖 LLM，毫秒级出分。"""
    bullets = []
    for sec in (resume or {}).get("sections", []):
        for blk in sec.get("blocks", []):
            for b in blk.get("bullets", []):
                t = b.get("text", "")
                st = star_check(t)
                tone = find_tone(t)
                bullets.append({
                    "bid": b.get("bid"), "text": t, "star": st,
                    "tone": {"a": sum(x["n"] for x in tone["A"]),
                             "b": sum(x["n"] for x in tone["B"]),
                             "words": [x["w"] for x in tone["A"]]},
                    "length": "too_long" if len(t) > 140 else ("too_short" if len(t) < 8 else "ok"),
                })
    return {
        "bullets": bullets,
        "star_full": sum(1 for x in bullets if x["star"]["level"] == "full"),
        "star_total": len(bullets),
        "tone_a_total": sum(x["tone"]["a"] for x in bullets),
        "tone_hits": [{"bid": x["bid"], "words": x["tone"]["words"]} for x in bullets if x["tone"]["a"]],
        "long_bullets": [x["bid"] for x in bullets if x["length"] == "too_long"],
    }
