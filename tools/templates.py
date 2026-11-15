# -*- coding: utf-8 -*-
"""内置 HTML 简历模板库

设计原则
--------
1. **数据与样式彻底分离**：模板只吃规整后的 resume 数据。
   解析层怎么变、以后接不接视觉模型，模板都不用改。
2. **样式是我们的资产**：不从原 PDF 还原样式，用户换来换去都是这套质量。
3. **打印即所得**：A4 尺寸 + @page 规则，无头 Chrome 直接 print-to-pdf。
4. **bid 全程保留**：每个要点带 data-bid，工作台可以反向定位回原数据。

三套模板
--------
  classic  经典单栏 —— 最通用，ATS 友好，中规中矩不抢戏
  compact  紧凑单页 —— 内容多时压成一页，字号小但层次清楚
  modern   现代分栏 —— 左侧信息栏 + 右侧经历，视觉更"设计感"

用法
----
  from templates import render
  html = render(resume_dict, template='classic')
"""

from __future__ import annotations

import html as _html
import re

# ----------------------------------------------------------------- 数据规整

#: 常见城市名 —— 解析器有时把「地点」塞进 role 字段，需要识别出来
_CITIES = set("""
北京 上海 广州 深圳 杭州 成都 武汉 西安 南京 天津 重庆 苏州 长沙 郑州 青岛
大连 厦门 福州 合肥 济南 宁波 无锡 佛山 东莞 沈阳 昆明 哈尔滨 长春 石家庄
太原 南昌 贵阳 兰州 银川 西宁 乌鲁木齐 呼和浩特 南宁 海口 拉萨 珠海 中山
常州 温州 绍兴 嘉兴 泉州 烟台 潍坊 徐州 洛阳 唐山 保定 临沂 淄博 威海 远程
""".split())

#: 职位关键词 —— 命中则认为 role 是真角色而不是地点
_JOB_WORDS = (
    "工程师", "经理", "助理", "专员", "主管", "总监", "顾问", "设计师", "运营",
    "开发", "实习", "leader", "Leader", "负责人", "架构师", "分析师", "产品",
    "销售", "客服", "编辑", "策划", "老师", "教师", "讲师", "研究员", "主任",
)


def _is_location(s):
    s = (s or "").strip()
    if not s or len(s) > 6:
        return False
    if any(w in s for w in _JOB_WORDS):
        return False
    return s in _CITIES or s.replace("市", "") in _CITIES


def _looks_like_contact(s):
    s = s or ""
    return "@" in s or sum(c.isdigit() for c in s) >= 7


def _find_city(s):
    """在一段文字里找城市名（子串匹配，而非要求整段就是城市）。"""
    for c in _CITIES:
        if c and c in s:
            return c
    return ""


def _split_meta(rest):
    """从「去掉日期后的 meta 剩余」里抠出 地点 / 角色 / 残余。

    返回 (place, role, leftover)。原则：**绝不丢用户的文字** ——
    任何认不出来的残余都留在 leftover，由调用方兜底进 place 原样呈现。
    """
    place, role, leftover = "", "", (rest or "").strip()
    city = _find_city(leftover)
    if city:
        place = city
        leftover = leftover.replace(city, " ").strip(" 　·|｜-–—~～")
    # 角色：剩余文本里只要含职位关键词，整段当作角色（地点已先抠出）
    if leftover and any(w in leftover for w in _JOB_WORDS):
        role = leftover
        leftover = ""
    return place, role, leftover


#: 默认哪些章节归到侧栏（modern 模板的左栏）—— 可用 side_keys 覆盖
_SIDE_KEYS = ("技能", "证书", "荣誉", "奖项", "其他", "自我", "兴趣", "语言")

#: 从 meta 里抠日期区间。工作台（前端）的数据模型没有独立的 date 字段，
#: 日期是塞在 meta 里的，形如「北京 2023.07 – 至今」「2023年至今」「2021-2024」。
_META_DATE_RE = re.compile(
    r"(?:"
    r"\d{4}\s*[.\-/]\s*\d{1,2}"        # 2023.07 / 2023-07
    r"|\d{4}年\d{1,2}月?"              # 2023年7月 / 2023年7
    r"|\d{4}年?"                       # 2023年 / 2023
    r")\s*[-–—~～至到]\s*"
    r"(?:"
    r"\d{4}\s*[.\-/]\s*\d{1,2}"        # 2024.08
    r"|\d{4}年\d{1,2}月?"              # 2024年8月
    r"|\d{4}年?"                       # 2024年
    r"|\d{1,2}月?"                     # 8月 / 8
    r"|至今|现在|今|present"            # 至今 / 现在 / 今
    r")", re.I)

#: 默认配色。模板 CSS 里所有颜色都走这些变量，换主题只需覆盖对应 key。
DEFAULT_THEME = {
    "ink": "#111827",        # 标题 / 强调文字
    "deep": "#1f2937",       # 深色侧栏底、强分隔线
    "body": "#374151",       # 正文
    "sub": "#4b5563",        # 次要文字
    "meta": "#6b7280",       # 日期 / 地点
    "faint": "#9ca3af",      # 圆点 / 极弱信息
    "line": "#d1d5db",         # 分隔线
    "accent": "#1d4ed8",       # 强调色（图标徽章）
    "accent-soft": "#e8effc",  # 强调色的浅底（图标底、时间轴）
    "panel": "#f7f9fc",        # 浅灰面板底
    "warm": "#d97757",         # 暖色点缀（第二强调色）
    "side-text": "#e5e7eb",    # 深色侧栏上的文字
    "canvas": "#f3f4f6",       # 纸张外的底色
}

#: 现成配色，`--theme 名字` 直接选
THEME_PRESETS = {
    "slate": {},                                        # 默认：灰蓝
    "ink":   {"accent": "#111827", "deep": "#0f172a"},  # 纯黑灰，最稳
    "teal":  {"accent": "#0f766e", "deep": "#134e4a"},  # 青绿
    "wine":  {"accent": "#9f1239", "deep": "#4c0519"},  # 酒红
    "amber": {"accent": "#b45309", "deep": "#3f2d0a"},  # 琥珀
}


def normalize(resume, side_keys=None):
    """把解析结果规整成模板可用的形状。

    主要处理三类脏数据：
      · name 里混了空格（PDF 按字距拆开，实测「张 三」）
      · role 字段里其实是地点（实测「北京」）
      · 联系方式落在 extra 里（脱敏 PDF 的 `138****8000` 匹不上正则）
    """
    r = dict(resume or {})
    name = (r.get("name") or "").strip()
    # 姓名：去掉字间空格，但保留「·」分隔的少数民族姓名
    if name and "·" not in name and len(name.replace(" ", "")) <= 6:
        name = name.replace(" ", "")
    r["name"] = name

    contacts, others = [], []
    for x in [r.get("phone"), r.get("email")]:
        if x:
            contacts.append(x.strip())
    for x in (r.get("extra") or []):
        x = (x or "").strip()
        if not x:
            continue
        if _looks_like_contact(x) and not any(x in c for c in contacts):
            contacts.append(x)
        elif x not in ("照片", "Photo", "photo"):
            others.append(x)
    r["_contacts"] = contacts
    r["_notes"] = others

    secs = []
    for sec in (r.get("sections") or []):
        blocks = []
        for b in (sec.get("blocks") or []):
            nb = dict(b)
            role = (b.get("role") or "").strip()
            date = (b.get("date") or "").strip()
            # 工作台传来的数据没有独立 date/role/place 字段，日期/角色/地点全混在 meta 里。
            # 这里兜底拆出来，但**绝不丢文字**：任何认不出的残余都进 place，原样呈现。
            # （否则改了角色/部门后，内容会被整段塞进右侧 date 再因 nowrap 被裁掉。）
            if not date and b.get("meta"):
                m = _META_DATE_RE.search(b["meta"])
                if m:
                    date = m.group(0).strip()
                    nb["date"] = date
                    rest = (b["meta"][:m.start()] + " " + b["meta"][m.end():]).strip(" 　·|｜-–—~～")
                else:
                    # 没有可识别的「年-月」日期（如「2023年至今」）→ 整段交给我们拆，
                    # 不要再整段塞进 date（那样会因 nowrap 被裁掉、用户以为改了没生效）。
                    rest = (b["meta"] or "").strip()
                place_r, role_r, leftover = _split_meta(rest)
                if not nb.get("place"):
                    nb["place"] = place_r
                if not nb.get("role"):
                    nb["role"] = role_r
                if leftover:                      # 残余兜底进 place，保证用户改的内容不丢
                    nb["place"] = ((nb.get("place") or "") + " " + leftover).strip()
            # role 是地点 → 挪出角色位，改记为地点（仅当 role 字段本身是个纯地点时）
            if _is_location(role):
                nb["role"] = ""
                nb["place"] = role
            else:
                # 用 setdefault 而不是直接赋值 —— 上面从 meta 拆出来的地点
                # 不能被这里无条件覆盖掉。
                nb.setdefault("place", "")
            # heading 里若已含日期，去掉重复
            if date and nb.get("heading", "").endswith(date):
                nb["heading"] = nb["heading"][:-len(date)].strip(" ·-—–|｜")
            nb["bullets"] = [x for x in (b.get("bullets") or []) if (x.get("text") or "").strip()]
            if nb.get("heading") or nb["bullets"] or nb.get("intro"):
                blocks.append(nb)
        if blocks:
            secs.append({"title": sec.get("title") or "", "blocks": blocks})
    r["_sections"] = secs

    # 侧栏类章节 —— modern 模板会放到左栏（关键词可配置）
    keys = tuple(side_keys) if side_keys else _SIDE_KEYS

    def _is_side(t):
        return any(k in (t or "") for k in keys)
    r["_side_sections"] = [s for s in secs if _is_side(s["title"])]
    r["_main_sections"] = [s for s in secs if not _is_side(s["title"])]
    return r


# ----------------------------------------------------------------- 共用片段

def esc(s):
    return _html.escape(str(s or ""))


def _merge_ranges(rs):
    """合并/规范化字符区间 [[s,e],...]（重叠相邻合一，非法项丢弃）。"""
    a = []
    for r in (rs or []):
        if isinstance(r, (list, tuple)) and len(r) == 2:
            try:
                s, e = int(r[0]), int(r[1])
            except Exception:
                continue
            if e > s:
                a.append([s, e])
    a.sort(key=lambda x: x[0])
    out = []
    for s, e in a:
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _rich_text(text, ranges):
    """把「用户手动加粗的字符区间」渲染成带 <b> 的 HTML。

    区间以外的文字一律按原文输出 —— 加粗只影响呈现，不改一个字。
    """
    t = text or ""
    n = len(t)
    rs = [(max(0, s), min(n, e)) for s, e in _merge_ranges(ranges) if s < n and e > 0]
    if not rs:
        return esc(t)
    out, i = [], 0
    for s, e in rs:
        if s > i:
            out.append(esc(t[i:s]))
        out.append("<b>%s</b>" % esc(t[s:e]))
        i = e
    if i < n:
        out.append(esc(t[i:]))
    return "".join(out)


def _bullet_li(x):
    """一条要点。加粗来自两处并合并渲染：
      ① 用户在工作台手动加粗的字符区间 x["bold"]；
      ② PDF 原文自带的行首加粗引导词 x["bold_prefix"]。

    ⚠️ 不做任何措辞加工 —— 模板层的职责是排版，一个字都不改。
    """
    t = x.get("text") or ""
    bid = x.get("bid") or ""
    ranges = list(x.get("bold") or [])
    pre = (x.get("bold_prefix") or "").strip()
    if pre and t.startswith(pre):
        ranges.append([0, len(pre)])
    # 层级：PDF 里缩进更深的子要点。用内联 margin 保证六套模板都生效。
    try:
        lv = int(x.get("level") or 1)
    except Exception:
        lv = 1
    lv = max(1, min(3, lv))
    lv_attr = ' class="lv%d" style="margin-left:%dpx"' % (lv, 16 * (lv - 1)) if lv > 1 else ''
    return '<li data-bid="%s"%s>%s</li>' % (esc(bid), lv_attr, _rich_text(t, ranges))


def _item_html(b, compact=False):
    """一条经历：标题行（单位｜角色｜日期）+ 地点 + 简介 + 要点。"""
    out = []
    head_l, head_r = [], []

    if b.get("icon"):
        head_l.append('<span class="ic">%s</span>' % esc(b["icon"]))
    if b.get("heading"):
        head_l.append('<span class="t">%s</span>' % esc(b["heading"]))
    if b.get("role"):
        head_l.append('<span class="ro">%s</span>' % esc(b["role"]))
    date = b.get("date") or ""
    place = b.get("place") or ""
    # 地点与日期合并到右侧：省一行，也更符合简历惯例
    meta_r = " · ".join(x for x in [place, date] if x)
    if meta_r:
        head_r.append('<span class="dt">%s</span>' % esc(meta_r))

    out.append('<div class="ih">')
    out.append('  <span class="il">%s</span>' % " ".join(head_l))
    out.append('  <span class="ir">%s</span>' % " ".join(head_r))
    out.append('</div>')
    if b.get("intro"):
        # 项目介绍也支持手动加粗（与要点同一套字符区间，导出时一并渲染）
        out.append('<p class="ii">%s</p>' % _rich_text(b["intro"], b.get("introBold") or []))
    if b.get("subhead"):
        # 「项目工作：」是**项目内的小标题**，不是章节标题 —— 不能用 .sh（那是「工作经历」
        # 这类大节的样式，带竖线/下划线，套上去层级会和模块一样大）。
        # 字号借用 `.ii`（项目介绍）保持一致，只加粗 + 去掉多余边距。
        out.append('<p class="ii" style="font-weight:600;color:inherit;border:0;padding:0;'
                   'margin:7px 0 2px">%s</p>' % esc(b["subhead"]))
    if b.get("bullets"):
        ul_attr = ' class="bs sub" style="padding-left:26px"' if b.get("subhead") else ' class="bs"'
        out.append('<ul%s>%s</ul>' % (ul_attr, "".join(_bullet_li(x) for x in b["bullets"])))
    return "".join(out)


def _section_html(sec, compact=False, cls="", icon="", wrap=False):
    """渲染一个章节。

    icon  章节标题前的图标 SVG（vibe 系模板用）
    wrap  给每个条目套一层 .blk —— 做时间轴 / 竖线这类样式时需要
    """
    cls = ("sec " + cls).strip()
    items = []
    for b in sec["blocks"]:
        inner = _item_html(b, compact)
        if wrap:
            items.append('<div class="blk" data-bid="%s">%s</div>'
                         % (esc(b.get("id") or ""), inner))
        else:
            items.append(inner)
    return '<section class="%s"><h2 class="sh">%s%s</h2>%s</section>' % (
        cls, icon, esc(sec["title"]), "".join(items))


def _contact_line(n, sep="　|　"):
    return sep.join(esc(x) for x in n["_contacts"])


# ----------------------------------------------------------------- 基础 CSS

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
/* 跨页留白：@page 的上下边距是**每一页**都会生效的（.page 的 padding 只在首尾） */
@page{size:A4;margin:10mm 0}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{
  font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC",
              "Noto Sans CJK SC",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:var(--c-deep);background:var(--c-canvas);padding:18px 0;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
}
.page{
  width:210mm;min-height:297mm;margin:0 auto;background:#fff;position:relative;
  box-shadow:0 1px 3px rgba(0,0,0,.14);
}
ul{list-style:none}
@media print{
  body{background:#fff;padding:0}
  .page{box-shadow:none;margin:0;width:auto;min-height:auto}
}
"""


def base_css(theme=None):
    """把主题变量拼在基础 CSS 前面。

    模板 CSS 全部引用这些变量，所以换配色不用动模板本身。
    """
    t = dict(DEFAULT_THEME)
    t.update(theme or {})
    head = ":root{%s}" % "".join("--c-%s:%s;" % (k, v) for k, v in t.items())
    return head + BASE_CSS


def fit_css(scale):
    """把内容整体缩放后压进一页。

    用 zoom 而不是缩字号：zoom 会连带间距一起缩，而且把宽度按同比例放大之后，
    **布局与原来完全等价** —— 高度就是原高度的 scale 倍，可预测。
    （只缩字号试过：scale 0.8 才降 11%，因为 margin/padding 没跟着缩，不够用。）

    必须拼在模板 CSS **之后**：模板里也有 .page{} 规则（比如 modern 的 min-height），
    放前面会被覆盖掉。
    """
    if not scale or abs(scale - 1.0) < 1e-4:
        return ""
    s = max(0.5, min(1.15, float(scale)))
    return (".page{zoom:%.4f;width:calc(210mm/%.4f);min-height:calc(297mm/%.4f)}"
            % (s, s, s))


def typography_css(font=None, line_height=None):
    """用户自定义排版：字体 / 行距。

    必须拼在模板 CSS 之后 —— 模板里有 `p{line-height:1.4}` 这类规则。
    """
    css = ""
    if font:
        css += ".page,.page *{font-family:%s}\n" % font
    if line_height:
        css += (".page p,.page li,.page div{line-height:%s}\n" % line_height)
    return css


#: 兜底重写规则 —— 拼在模板 CSS **之后**，确保「标题行能换行、左右两栏能收缩」，
#: 任何过长的内容都换行呈现而不是被裁掉（之前整段 meta 因 nowrap 溢出丢失的根因）。
OVERRIDE_CSS = """
.ih{flex-wrap:wrap}
.ih>.il,.ih>.ir,.blk .ih>.il,.blk .ih>.ir,.mn .ih>.il,.mn .ih>.ir{min-width:0}
.ih .dt{white-space:normal;word-break:break-word}
"""


def resolve_theme(spec):
    """解析 `--theme` 取值。

    两种写法都支持：
      · 预设名     slate / ink / teal / wine / amber
      · 逐项覆盖   accent=#b45309,deep=#0f172a

    返回 dict 或 None。
    """
    if not spec:
        return None
    spec = spec.strip()
    if spec in THEME_PRESETS:
        return dict(THEME_PRESETS[spec])
    out = {}
    for kv in spec.split(","):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip().lstrip("-").replace("c-", "", 1).replace("_", "-")
        if k in DEFAULT_THEME:
            out[k] = v.strip()
    return out or None


# ----------------------------------------------------------------- 模板一：经典单栏

CSS_CLASSIC = """
.page{padding:16mm 15mm 14mm}
.hd{text-align:center;padding-bottom:7px;border-bottom:1.6px solid var(--c-deep);margin-bottom:11px}
.nm{font-size:22px;font-weight:600;letter-spacing:.06em;color:var(--c-ink);line-height:1.3}
.ct{font-size:10.5px;color:var(--c-sub);margin-top:5px;word-break:break-all}
.it{font-size:11px;color:var(--c-ink);margin-top:5px}
.it b{font-weight:600}
.nt{font-size:10px;color:var(--c-meta);margin-top:3px}
.sec{margin-bottom:10px}
.sh{font-size:12.5px;font-weight:600;color:var(--c-ink);letter-spacing:.05em;
    padding-bottom:3px;border-bottom:.8px solid var(--c-line);margin-bottom:6px}
.ih{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.il{font-size:11px;font-weight:600;color:var(--c-ink)}
.il .ro{font-weight:400;color:var(--c-sub);margin-left:5px}
.il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:8.5px;font-weight:600;
        padding:1px 3px;border-radius:3px;margin-right:4px;vertical-align:1px}
.ir .dt{font-size:10px;color:var(--c-meta);white-space:normal;word-break:break-word}
.is{font-size:10px;color:var(--c-meta);margin-top:1px}
.ii{font-size:10.5px;color:var(--c-body);line-height:1.65;margin-top:3px}
.bs{margin-top:3px}
.bs li{font-size:10.5px;line-height:1.65;color:var(--c-body);padding-left:11px;position:relative;margin-bottom:1.5px}
.bs li:before{content:"";position:absolute;left:2px;top:7.2px;width:3.4px;height:3.4px;
              border-radius:50%;background:var(--c-meta)}
.bs li b{font-weight:600;color:var(--c-ink)}
.sec>.ih,.sec>div:first-of-type{margin-top:0}
"""

# ----------------------------------------------------------------- 模板二：紧凑单页

CSS_COMPACT = """
.page{padding:12mm 13mm 10mm}
.hd{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;
    padding-bottom:6px;border-bottom:1.4px solid var(--c-deep);margin-bottom:8px}
.nm{font-size:17px;font-weight:600;letter-spacing:.05em;color:var(--c-ink);line-height:1.2}
.it{font-size:9.5px;color:var(--c-body);margin-top:3px}
.it b{font-weight:600}
.hd-r{text-align:right;font-size:9px;color:var(--c-sub);line-height:1.55;word-break:break-all}
.nt{font-size:9px;color:var(--c-meta);margin-top:2px}
.sec{margin-bottom:6.5px}
.sh{font-size:10.5px;font-weight:600;color:var(--c-ink);letter-spacing:.08em;
    padding-bottom:2px;border-bottom:.7px solid var(--c-line);margin-bottom:4px}
.ih{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.il{font-size:9.8px;font-weight:600;color:var(--c-ink)}
.il .ro{font-weight:400;color:var(--c-sub);margin-left:4px}
.il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:8px;font-weight:600;
        padding:0 3px;border-radius:2px;margin-right:3px}
.ir .dt{font-size:9px;color:var(--c-meta);white-space:normal;word-break:break-word}
.ii{font-size:9.5px;color:var(--c-body);line-height:1.5;margin-top:2px}
.bs li{font-size:9.5px;line-height:1.52;color:var(--c-body);padding-left:10px;position:relative;margin-bottom:1px}
.bs li:before{content:"";position:absolute;left:2px;top:6.4px;width:3px;height:3px;
              border-radius:50%;background:var(--c-faint)}
.bs li b{font-weight:600;color:var(--c-ink)}
"""

# ----------------------------------------------------------------- 模板三：现代分栏

CSS_MODERN = """
.page{padding:0 0 0 64mm;min-height:297mm;position:relative}
.page:before{content:"";position:absolute;left:0;top:0;bottom:0;width:64mm;background:var(--c-deep)}
.sd{position:absolute;left:0;top:0;width:64mm;padding:14mm 8mm 12mm;color:var(--c-side-text)}
.mn{padding:14mm 11mm 12mm 10mm}
.nm{font-size:19px;font-weight:600;letter-spacing:.08em;color:#fff;line-height:1.25}
.it{font-size:9.5px;color:var(--c-faint);margin-top:7px;padding-bottom:8px;border-bottom:1px solid var(--c-body)}
.it b{font-weight:600;color:var(--c-side-text)}
.ct{font-size:9px;color:var(--c-faint);line-height:1.8;margin-top:8px;word-break:break-all}
.ct div{margin-bottom:1px}
.nt{font-size:8.5px;color:var(--c-meta);line-height:1.7;margin-top:7px}
.sd .sec{margin-top:15px}
.sd .sh{font-size:9.5px;font-weight:600;color:#fff;letter-spacing:.12em;
        text-transform:none;padding-bottom:3px;border-bottom:1px solid var(--c-sub);margin-bottom:6px}
.sd .bs li{font-size:9px;line-height:1.6;color:var(--c-line);padding-left:9px;position:relative;margin-bottom:2px}
.sd .bs li:before{content:"";position:absolute;left:1px;top:6px;width:2.6px;height:2.6px;border-radius:50%;background:var(--c-faint)}
.sd .ih{display:block}
.sd .il{font-size:9.5px;font-weight:600;color:var(--c-canvas)}
.sd .il .ro{display:block;font-weight:400;color:var(--c-faint);margin:1px 0 0}
.sd .ir .dt{font-size:8.5px;color:var(--c-faint)}
.mn .sec{margin-bottom:9px}
.mn .sh{font-size:12px;font-weight:600;color:var(--c-ink);letter-spacing:.06em;
        padding-bottom:3px;border-bottom:1.4px solid var(--c-deep);margin-bottom:6px}
.mn .ih{display:flex;align-items:baseline;justify-content:space-between;gap:9px}
.mn .il{font-size:10.5px;font-weight:600;color:var(--c-ink)}
.mn .il .ro{font-weight:400;color:var(--c-sub);margin-left:5px}
.mn .il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:8.5px;font-weight:600;
            padding:1px 3px;border-radius:3px;margin-right:4px;vertical-align:1px}
.mn .ir .dt{font-size:9.5px;color:var(--c-meta);white-space:normal;word-break:break-word}
.mn .is{font-size:9.5px;color:var(--c-meta);margin-top:1px}
.mn .ii{font-size:10px;color:var(--c-body);line-height:1.62;margin-top:3px}
.mn .bs{margin-top:3px}
.mn .bs li{font-size:10px;line-height:1.62;color:var(--c-body);padding-left:11px;position:relative;margin-bottom:1.5px}
.mn .bs li:before{content:"";position:absolute;left:2px;top:7px;width:3.2px;height:3.2px;border-radius:50%;background:var(--c-faint)}
.mn .bs li b{font-weight:600;color:var(--c-ink)}
"""


# ----------------------------------------------------------------- 渲染入口

def _head_classic(n):
    p = ['<header class="hd">']
    p.append('<div class="nm">%s</div>' % esc(n["name"]))
    if n["_contacts"]:
        p.append('<div class="ct">%s</div>' % _contact_line(n))
    if n.get("intent"):
        p.append('<div class="it">求职意向：<b>%s</b></div>' % esc(n["intent"]))
    p.append("</header>")
    if n["_notes"]:
        p.append('<div class="nt">%s</div>' % esc("　·　".join(n["_notes"])))
    return "".join(p)


def _head_compact(n):
    p = ['<header class="hd">']
    p.append('<div class="hd-l"><div class="nm">%s</div>' % esc(n["name"]))
    if n.get("intent"):
        p.append('<div class="it">求职意向：<b>%s</b></div>' % esc(n["intent"]))
    p.append("</div>")
    right = list(n["_contacts"]) + list(n["_notes"])
    if right:
        p.append('<div class="hd-r">%s</div>' % "<br>".join(esc(x) for x in right))
    p.append("</header>")
    return "".join(p)


def _head_modern(n):
    p = ['<aside class="sd">']
    p.append('<div class="nm">%s</div>' % esc(n["name"]))
    if n.get("intent"):
        p.append('<div class="it">%s</div>' % esc(n["intent"]))
    if n["_contacts"]:
        p.append('<div class="ct">%s</div>' % "".join("<div>%s</div>" % esc(x) for x in n["_contacts"]))
    if n["_notes"]:
        p.append('<div class="nt">%s</div>' % esc("　·　".join(n["_notes"])))
    for sec in n["_side_sections"]:
        p.append(_section_html(sec, compact=True))
    p.append("</aside>")
    p.append('<main class="mn">%s</main>' % "".join(_section_html(s) for s in n["_main_sections"]))
    return "".join(p)


# ---------------------------------------------------------------- 图标
# 取自 vibe-resume（24×24 线性风格 sprite）。章节标题按**关键词**猜类型选图标 ——
# 模板不认识章节名，所以命不中就退回通用文件图标。

ICON_SPRITE = (
    '<svg aria-hidden="true" style="display:none" xmlns="http://www.w3.org/2000/svg">'
    '<symbol id="ic-school" viewBox="0 0 24 24">'
    '<path d="M22 10 12 5 2 10l10 5 10-5z"/>'
    '<path d="M6 12.5V17c0 1.7 2.7 3 6 3s6-1.3 6-3v-4.5M22 10v6"/></symbol>'
    '<symbol id="ic-briefcase" viewBox="0 0 24 24">'
    '<path d="M10 6V5a2 2 0 0 1 2-2h0a2 2 0 0 1 2 2v1"/>'
    '<path d="M3 8h18v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 13h18M9 13v2h6v-2"/></symbol>'
    '<symbol id="ic-code" viewBox="0 0 24 24">'
    '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6M14 4l-4 16"/></symbol>'
    '<symbol id="ic-tool" viewBox="0 0 24 24">'
    '<path d="M14.7 6.3a4 4 0 0 0 5 5L11 20l-5-5 8.7-8.7z"/><path d="m7 17-3 3"/></symbol>'
    '<symbol id="ic-file" viewBox="0 0 24 24">'
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 2v6h6M8 13h8M8 17h6"/></symbol>'
    "</svg>"
)

_ICON_RULES = (
    (("教育", "学历", "学习", "院校"), "ic-school"),
    # 「项目」规则必须排在「工作」前面：两者都含"经历"，
    # 顺序反了「项目经历」会被误判成工作图标。
    (("项目", "作品", "实践", "开发"), "ic-code"),
    (("工作", "实习", "职业", "任职", "经历"), "ic-briefcase"),
    (("技能", "证书", "荣誉", "奖项", "其他", "工具", "语言"), "ic-tool"),
    (("科研", "研究", "论文", "学术", "课题"), "ic-file"),
)


def icon_for(title):
    for keys, icon in _ICON_RULES:
        if any(k in (title or "") for k in keys):
            return icon
    return "ic-file"


def _icon_svg(title):
    return '<svg class="si"><use href="#%s"/></svg>' % icon_for(title)


def _is_skill_sec(title):
    """技能类章节 —— 部分模板把这类排成两列更省空间。"""
    return any(k in (title or "") for k in ("技能", "证书", "语言", "工具"))


def _hdr_block(n, p):
    """三套 vibe 模板共用的头部渲染（结构一样，外观交给各自的 CSS）。"""
    p.append('<header class="hd">')
    p.append('<div class="nm">%s</div>' % esc(n["name"]))
    if n.get("intent"):
        p.append('<div class="it">%s</div>' % esc(n["intent"]))
    if n["_contacts"]:
        p.append('<div class="ct">%s</div>'
                 % "".join('<span>%s</span>' % esc(x) for x in n["_contacts"]))
    if n["_notes"]:
        p.append('<div class="nt">%s</div>' % esc("　·　".join(n["_notes"])))
    p.append("</header>")


# ================================================================ 模板四 · vibe 标准单页
# 对应 vibe-resume: internship-employment/standard-one-page
# 特征：图标章节标题 + 渐变横线、经历左侧时间轴圆点、技能双列

CSS_VIBE_STD = """
.page{padding:12mm 10mm 10mm}
.hd{border:1px solid var(--c-line);border-top:3px solid var(--c-ink);border-radius:3px;
    padding:6mm 6mm 5mm;margin-bottom:6mm}
.nm{font-size:21pt;font-weight:800;color:var(--c-ink);line-height:1.05}
.it{font-size:9.5pt;font-weight:700;color:var(--c-sub);margin-top:2px}
.ct{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;font-size:8.5pt}
.ct span{background:var(--c-panel);border:1px solid var(--c-line);border-radius:3px;
         padding:2px 6px;color:var(--c-deep)}
.nt{font-size:8pt;color:var(--c-sub);margin-top:4px}
.sec{margin-bottom:5.5mm}
.sh{display:flex;align-items:center;gap:5px;font-size:12pt;font-weight:800;
    color:var(--c-ink);line-height:1.15;margin-bottom:4px}
.sh:after{content:"";flex:1;height:1.5px;margin-left:4px;
          background:linear-gradient(90deg,var(--c-accent),transparent)}
.si{width:15px;height:15px;flex:0 0 15px;padding:3px;border-radius:5px;
    background:var(--c-accent-soft);color:var(--c-accent);
    fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.blk{border-left:2px solid var(--c-accent-soft);padding-left:8px;position:relative;margin-bottom:3mm}
.blk:last-child{margin-bottom:0}
.blk:before{content:"";position:absolute;left:-4.5px;top:2.5px;width:5px;height:5px;
            border-radius:50%;background:var(--c-accent);border:2px solid #fff}
.blk .ih{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.blk .il{font-size:10pt;font-weight:800;color:var(--c-ink)}
.blk .il .ro{font-weight:700;color:var(--c-sub);margin-left:5px;font-size:9pt}
.blk .il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:7.5pt;
             font-weight:700;padding:1px 3px;border-radius:3px;margin-right:4px;vertical-align:1px}
.blk .ir .dt{font-size:9pt;font-weight:700;color:var(--c-sub);white-space:normal;word-break:break-word}
.blk .ii{font-size:9pt;line-height:1.45;color:var(--c-body);margin-top:2px}
.blk .bs{margin-top:2px}
.blk .bs li{font-size:9pt;line-height:1.45;color:var(--c-body);padding-left:11px;
            position:relative;margin-bottom:1px}
.blk .bs li:before{content:"";position:absolute;left:3px;top:6.2px;width:3.4px;height:3.4px;
                   border-radius:50%;background:var(--c-accent)}
.blk .bs li b{font-weight:800;color:var(--c-ink)}
.sec.cols .blk{border-left:0;padding-left:0}
.sec.cols .blk:before{display:none}
.sec.cols .blk .bs{columns:2;column-gap:14px}
.sec.cols .blk .bs li{break-inside:avoid}
.sec.cols .blk .bs li:before{background:var(--c-warm);width:3.6px;height:3.6px}
"""


def _body_vibe_std(n):
    p = [ICON_SPRITE]
    _hdr_block(n, p)
    for sec in n["_sections"]:
        p.append(_section_html(sec, cls="cols" if _is_skill_sec(sec["title"]) else "",
                               icon=_icon_svg(sec["title"]), wrap=True))
    return "".join(p)


# ================================================================ 模板五 · vibe 高密度
# 对应 vibe-resume: internship-employment/dense-two-page
# 特征：蓝色章节标题 + 细分隔线，字号紧、信息密度高

CSS_VIBE_DENSE = """
.page{padding:10mm 9mm 8mm}
.hd{padding-bottom:4px;border-bottom:2px solid var(--c-deep);margin-bottom:5px}
.nm{font-size:16pt;font-weight:800;color:var(--c-ink);line-height:1.1}
.it{font-size:8.5pt;font-weight:700;color:var(--c-accent);margin-top:2px}
.ct{font-size:8pt;color:var(--c-sub);margin-top:3px}
.nt{font-size:7.5pt;color:var(--c-sub);margin-top:2px}
.sec{margin-bottom:3.5mm}
.sh{font-size:10.5pt;font-weight:800;color:var(--c-accent);line-height:1.2;
    padding-bottom:2px;border-bottom:1.5px solid var(--c-accent-soft);margin-bottom:3px}
.blk{margin-bottom:2.2mm}
.blk:last-child{margin-bottom:0}
.blk .ih{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.blk .il{font-size:9.5pt;font-weight:800;color:var(--c-deep)}
.blk .il .ro{font-weight:700;color:var(--c-sub);margin-left:5px;font-size:8.5pt}
.blk .il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:7.5pt;
             font-weight:700;padding:0 3px;border-radius:2px;margin-right:3px}
.blk .ir .dt{font-size:8.5pt;color:var(--c-sub);white-space:normal;word-break:break-word}
.blk .ii{font-size:8.5pt;line-height:1.4;color:var(--c-body);margin-top:1px}
.blk .bs{margin-top:1px}
.blk .bs li{font-size:8.5pt;line-height:1.42;color:var(--c-body);padding-left:10px;
            position:relative;margin-bottom:.5px}
.blk .bs li:before{content:"";position:absolute;left:2px;top:5.8px;width:3px;height:3px;
                   border-radius:50%;background:var(--c-accent)}
.blk .bs li b{font-weight:800;color:var(--c-deep)}
.sec.cols .blk .bs{columns:2;column-gap:12px}
.sec.cols .blk .bs li{break-inside:avoid}
"""


def _body_vibe_dense(n):
    p = ['<header class="hd">']
    p.append('<div class="nm">%s</div>' % esc(n["name"]))
    if n.get("intent"):
        p.append('<div class="it">%s</div>' % esc(n["intent"]))
    if n["_contacts"]:
        p.append('<div class="ct">%s</div>' % esc("　|　".join(n["_contacts"])))
    if n["_notes"]:
        p.append('<div class="nt">%s</div>' % esc("　·　".join(n["_notes"])))
    p.append("</header>")
    for sec in n["_sections"]:
        p.append(_section_html(sec, cls="cols" if _is_skill_sec(sec["title"]) else "",
                               wrap=True))
    return "".join(p)


# ================================================================ 模板六 · vibe 科研申请
# 对应 vibe-resume: research-application/research-classic
# 特征：深蓝主色、少装饰、左侧细竖线，突出研究工作

CSS_VIBE_RESEARCH = """
.page{padding:14mm 12mm 12mm}
.hd{padding-bottom:5px;border-bottom:2px solid var(--c-accent);margin-bottom:6mm}
.nm{font-size:19pt;font-weight:800;color:var(--c-ink);letter-spacing:.02em;line-height:1.1}
.it{font-size:9.5pt;font-weight:700;color:var(--c-accent);margin-top:3px}
.ct{font-size:8.5pt;color:var(--c-sub);margin-top:5px}
.nt{font-size:8pt;color:var(--c-sub);margin-top:3px}
.sec{margin-bottom:5mm}
.sh{font-size:11.5pt;font-weight:800;color:var(--c-accent);letter-spacing:.04em;
    padding-bottom:2px;border-bottom:1px solid var(--c-line);margin-bottom:4px}
.blk{margin-bottom:3mm;padding-left:8px;border-left:2px solid var(--c-accent-soft)}
.blk:last-child{margin-bottom:0}
.blk .ih{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.blk .il{font-size:10pt;font-weight:800;color:var(--c-ink)}
.blk .il .ro{font-weight:700;color:var(--c-sub);margin-left:5px;font-size:9pt}
.blk .il .ic{display:inline-block;background:var(--c-accent);color:#fff;font-size:7.5pt;
             font-weight:700;padding:1px 3px;border-radius:2px;margin-right:4px}
.blk .ir .dt{font-size:9pt;color:var(--c-sub);white-space:normal;word-break:break-word}
.blk .ii{font-size:9pt;line-height:1.5;color:var(--c-body);margin-top:2px}
.blk .bs{margin-top:2px}
.blk .bs li{font-size:9pt;line-height:1.5;color:var(--c-body);padding-left:11px;
            position:relative;margin-bottom:1.5px}
.blk .bs li:before{content:"";position:absolute;left:2px;top:6.4px;width:3.4px;height:3.4px;
                   border-radius:50%;background:var(--c-warm)}
.blk .bs li b{font-weight:800;color:var(--c-ink)}
"""


def _body_vibe_research(n):
    p = ['<header class="hd">']
    p.append('<div class="nm">%s</div>' % esc(n["name"]))
    if n.get("intent"):
        p.append('<div class="it">%s</div>' % esc(n["intent"]))
    if n["_contacts"]:
        p.append('<div class="ct">%s</div>' % esc("　|　".join(n["_contacts"])))
    if n["_notes"]:
        p.append('<div class="nt">%s</div>' % esc("　·　".join(n["_notes"])))
    p.append("</header>")
    for sec in n["_sections"]:
        p.append(_section_html(sec, wrap=True))
    return "".join(p)


# ---------------------------------------------------------------- 渲染入口

def _body_classic(n):
    return _head_classic(n) + "".join(_section_html(s) for s in n["_sections"])


def _body_compact(n):
    return _head_compact(n) + "".join(_section_html(s) for s in n["_sections"])


def _body_modern(n):
    return _head_modern(n)


#: 模板注册表。
#: theme = 该模板自带的默认配色（会被 render(theme=...) 显式覆盖）。
_TPL = {
    "classic": {
        "name": "经典单栏",
        "desc": "姓名居中，通用、ATS 友好，不抢戏",
        "css": CSS_CLASSIC, "body": _body_classic,
    },
    "compact": {
        "name": "紧凑单页",
        "desc": "姓名左 / 联系方式右，字号小，内容多时压得进一页",
        "css": CSS_COMPACT, "body": _body_compact,
    },
    "modern": {
        "name": "现代分栏",
        "desc": "左侧深色信息栏（联系方式 + 技能荣誉）+ 右侧主体经历",
        "css": CSS_MODERN, "body": _body_modern,
    },
    "vibe-standard": {
        "name": "Vibe 标准单页",
        "desc": "图标章节标题 + 渐变横线、经历时间轴、技能双列",
        "css": CSS_VIBE_STD, "body": _body_vibe_std,
        "theme": {"accent": "#2563eb", "accent-soft": "#eaf1ff", "ink": "#141414",
                  "sub": "#667085", "line": "#dbe2ea", "panel": "#f7f9fc",
                  "warm": "#d97757"},
    },
    "vibe-dense": {
        "name": "Vibe 高密度双页",
        "desc": "紧凑高密度，蓝色章节标题 + 细分隔线",
        "css": CSS_VIBE_DENSE, "body": _body_vibe_dense,
        "theme": {"accent": "#1768e5", "deep": "#173a86", "ink": "#171717",
                  "sub": "#5f6672", "accent-soft": "#eef6ff", "line": "#dbe2ea"},
    },
    "vibe-research": {
        "name": "Vibe 科研申请",
        "desc": "深蓝稳重、少装饰，突出研究与论文",
        "css": CSS_VIBE_RESEARCH, "body": _body_vibe_research,
        "theme": {"accent": "#1e3a5f", "accent-soft": "#eef3fa", "ink": "#1a1a1a",
                  "sub": "#5f6b76", "line": "#c9cfd6", "panel": "#f7f9fc",
                  "warm": "#a6622b"},
    },
}


def list_templates():
    return [{"id": k, "name": v["name"], "desc": v.get("desc", "")} for k, v in _TPL.items()]


def render(resume, template="classic", title=None, theme=None, side_keys=None,
           scale=None, font=None, line_height=None, font_scale=None):
    """把 resume 数据渲染成完整的自包含 HTML 文档。

    theme      配色覆盖 {key: value}（key 见 DEFAULT_THEME）。
               优先级：这里的覆盖 > 模板自带默认 > 全局默认
    side_keys  哪些章节进侧栏（仅 modern 用），None 用 _SIDE_KEYS
    scale      整体字号比例（1.0 = 原样）。用于把内容压进一页；
               与 theme 里的 fs 冲突时以 scale 为准。
    font       正文字体族（CSS font-family 值）
    line_height 正文行距（如 "1.7"）
    font_scale 用户手动放大/缩小字号（1.0 = 原样），与 scale 相乘
    """
    if template not in _TPL:
        raise ValueError("未知模板 %r，可选：%s" % (template, ", ".join(_TPL)))
    t = _TPL[template]
    n = normalize(resume, side_keys)
    merged = dict(t.get("theme") or {})
    merged.update(theme or {})

    total = None
    if scale or font_scale:
        total = float(scale or 1.0) * float(font_scale or 1.0)

    doc_title = title or (n["name"] + " 的简历" if n["name"] else "简历")
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>%s</title>\n<style>%s\n%s%s%s%s</style>\n</head>\n"
        '<body>\n<div class="page" data-template="%s">%s</div>\n</body>\n</html>\n'
    ) % (esc(doc_title), base_css(merged), t["css"],
         typography_css(font, line_height), fit_css(total), OVERRIDE_CSS,
         template, t["body"](n))


def render_all(resume, title=None, theme=None, side_keys=None):
    """所有模板一次全渲染 —— 用于"换模板看效果"。"""
    return {k: render(resume, k, title, theme=theme, side_keys=side_keys) for k in _TPL}
