# -*- coding: utf-8 -*-
"""几何驱动的 PDF 简历结构解析器。

## 为什么不能靠文字流

`pypdf` / `pdfjs` 这类只给「纯文本」的提取器，输出顺序是**内容流顺序**，不是视觉顺序。
由此产生的典型故障（都在真实简历上复现过）：

  · 折行被拆散 —— 一条 bullet 折成 3 行，就被当成 3 条（最致命，直接毁掉结构）
  · 看不见的装饰字符混入正文 —— 蓝底白字的公司 logo 字符「联」被拼进单位名
  · 分不清标题和正文 —— 没有字号/字重，只能靠硬编码章节名表，遇到「个人优势」就废
  · 双栏 PDF 顺序反了 —— 左右栏内容交叉
  · 右对齐的日期被当成正文的一部分 —— 无法拆出「单位 / 角色 / 日期」

## 本模块的做法

用 PyMuPDF 的 `get_text("dict")` 拿到每个 span 的**坐标 + 字号 + 字体 + 颜色**，
然后**按几何重建结构**（不调 AI，纯确定性）：

  1. 按 y0 重排（视觉顺序）
  2. 按 x 间隙把一行拆成若干「部件」（单位名 / 角色 / 日期）
  3. 按「同缩进 + 行距吻合」把折行合并回一段
  4. 按字号/颜色/缩进/字重判定角色：章节标题 / 条目标题 / 角色 / 日期 / 要点 / 简介 / 图标
  5. 分组成 sections → blocks → bullets，输出工作台可直接消费的 JSON

只在最后一步（可选）才需要 AI：判断某段经历该归哪个章节 —— 而且仅当 PDF 没有章节标题时。
"""

from __future__ import annotations

import math
import os
import re
import time
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)

#: PyMuPDF 搜索路径（按优先级）：环境变量 → 项目自带 libs/ → 本机通用库目录 → 系统已装
_LIB_CANDIDATES = [
    os.environ.get("PDF_PYLIBS"),
    os.path.join(_PROJ, "libs"),
    r"D:\workbuddy\temp\pylibs",
]
for _p in _LIB_CANDIDATES:
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

try:
    import pymupdf
except Exception:                                    # pragma: no cover
    try:
        import fitz as pymupdf                       # 老版本兼容
    except Exception as e:
        raise RuntimeError(
            "需要 PyMuPDF。装一次即可（约 53MB，不装也能跑，但解析质量会退回旧方案）：\n"
            "  python -m pip install --target D:\\workbuddy\\temp\\pylibs pymupdf\n"
            "也可以放到 <项目>/libs/ 让它随项目走。"
        ) from e


# ============================================================ 常量 / 词表

#: 常见章节名（**仅作辅助信号**，真正的判定靠几何；这张表用于给标题归类、排序）
SECTION_NAMES = [
    "工作经历", "工作经验", "工作履历", "职业经历", "项目经历", "项目经验", "主要项目",
    "科研课题", "科研课题经历", "科研经历", "实习经历", "实习经验",
    "教育经历", "教育背景", "教育", "学习经历",
    "荣誉奖项", "荣誉与技能", "荣誉", "获奖情况", "奖项",
    "专业技能", "技能", "技能特长", "个人技能",
    "自我评价", "个人简介", "个人优势", "自我介绍", "其他", "补充信息", "证书",
]
#: 章节标准顺序（用户看到的简历顺序应符合阅读习惯）
SECTION_ORDER = {n: i for i, n in enumerate([
    "工作经历", "工作经验", "工作履历", "职业经历",
    "项目经历", "项目经验", "主要项目", "科研课题", "科研课题经历", "科研经历",
    "实习经历", "实习经验",
    "教育经历", "教育背景", "教育", "学习经历",
    "专业技能", "技能", "技能特长", "个人技能",
    "荣誉奖项", "荣誉与技能", "荣誉", "获奖情况", "奖项",
    "自我评价", "个人简介", "个人优势", "自我介绍", "证书", "其他", "补充信息",
])}

#: 日期：2023.07 / 2023-07 / 2023年7月 / 至今 / present
DATE_RE = re.compile(
    r"(19|20)\d{2}\s*[.\-/年]\s*\d{0,2}\s*月?"
    r"(\s*[-–~—至]\s*((19|20)\d{2}\s*[.\-/年]\s*\d{0,2}\s*月?|至今|现在|今|present|Present|NOW|now))?"
)
DATE_RANGE_RE = re.compile(
    r"((19|20)\d{2}[.\-/年]\s*\d{0,2}\s*月?)\s*[-–~—至]\s*"
    r"((19|20)\d{2}[.\-/年]\s*\d{0,2}\s*月?|至今|现在|今|present|Present)"
)
#: 行首项目符号
BULLET_RE = re.compile(r"^\s*[·•∙▪●○◦‣※·\-\*\uFF0D\u2022\u25CF\u25AA]\s*")
#: 「条目简介」型开头（这些是介绍段落，不是工作要点）
INTRO_RE = re.compile(r"^\s*(项目介绍|项目背景|项目描述|项目简介|项目概述|项目说明|公司介绍|项目综述)\s*[:：]")
#: 中文标点里表示"这句还没说完"
SOFT_END = "，、：；,;:（(「『《"


def _hex(color) -> str:
    v = int(color)
    return "#%02x%02x%02x" % ((v >> 16) & 255, (v >> 8) & 255, v & 255)


def _lum(hex_color: str) -> float:
    """相对亮度 0~1，用于判断"白字/浅字"。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _is_bold(font: str, flags: int) -> bool:
    f = (font or "").lower()
    if "bold" in f or "black" in f or "heavy" in f or "semibold" in f or "demibold" in f:
        return True
    return bool(flags & 16)          # bit 4 = bold


def _median(xs, default=0.0):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else default


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _join_text(a: str, b: str) -> str:
    """把折行的两段拼回去：**中文之间不插空格**，英文/数字之间才补一个空格。

    真实故障：中文简历的折行若用空格连接，会多出可断行的空隙 ——
    浏览器可能就在「大应 / 用”体系」这种半截词上断开，看起来像"错行"。
    """
    a = (a or "").rstrip()
    b = (b or "").lstrip()
    if not a:
        return b
    if not b:
        return a
    if _CJK.search(a[-1]) or _CJK.search(b[0]):
        return a + b
    if a[-1] in "-/（(【[《“\"'" or b[0] in "，。、；：！？）》】]”\"'),.;:%":
        return a + b
    return a + " " + b


# ============================================================ ① 取行（含几何）

class Span:
    __slots__ = ("text", "x0", "y0", "x1", "y1", "size", "font", "color", "bold", "flags")

    def __init__(self, s, line_bbox):
        self.text = s.get("text", "")
        b = s.get("bbox") or line_bbox
        self.x0, self.y0, self.x1, self.y1 = b
        self.size = round(float(s.get("size") or 0), 2)
        self.font = (s.get("font") or "").split("+")[-1]
        self.color = _hex(s.get("color", 0))
        self.flags = int(s.get("flags") or 0)
        self.bold = _is_bold(self.font, self.flags)


class Line:
    """一条**视觉行**（同一基线）。含它由哪些 span 组成。"""

    def __init__(self, page, block_i, line_i, line):
        self.page = page
        self.block_i = block_i
        self.line_i = line_i
        b = line["bbox"]
        self.x0, self.y0, self.x1, self.y1 = b
        self.spans = [Span(s, b) for s in line.get("spans", []) if s.get("text")]
        self.text = "".join(s.text for s in self.spans)
        # 主字号 / 主颜色：取字符数最多的 span
        if self.spans:
            main = max(self.spans, key=lambda s: len(s.text.strip()) or 1)
            self.size = main.size
            self.color = main.color
        else:
            self.size, self.color = 0.0, "#000000"
        self.max_size = max((s.size for s in self.spans), default=0.0)
        self.any_bold = any(s.bold for s in self.spans)
        self.all_bold = all(s.bold for s in self.spans) if self.spans else False
        self.height = self.y1 - self.y0
        # 矢量画出来的项目符号（文字层里没有「•」字符）—— 由 annotate_bullets 标注。
        # 真实简历里大量 bullet 是"画"出来的小圆点/小方块，只靠文本正则永远识别不到。
        self.bullet = False
        self.marker_x = None

    @property
    def indent(self):
        return self.x0

    def __repr__(self):
        return "<Line y=%.1f x=%.1f sz=%.1f %r>" % (self.y0, self.x0, self.size, self.text[:24])


def page_markers(page) -> list:
    """抽取页内**小尺寸矢量图形** —— 列表的项目符号常是"画"出来的实心小圆/小方块，
    文字层里没有「•」字符，只靠文本正则永远识别不到。

    （思路借鉴「找工作助手」项目的 pdf_reconstruct.py：把画出来的符号也当作 bullet 信号。）
    返回 [(x0, x1, y0, y1), ...]。
    """
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        if 1.0 <= r.width <= 9.0 and 1.0 <= r.height <= 9.0:
            out.append((r.x0, r.x1, r.y0, r.y1))
    return out


def annotate_bullets(lines: list, markers: list) -> None:
    """把「左侧紧邻一个小圆点/小方块」的行标记为列表项（l.bullet = True）。

    判据（**全部相对该行自身**，不依赖页面版心，避免"bullet 文字比版心更靠右"时漏判）：
      · 符号的垂直中心落在该行的 y 范围内；
      · 符号紧贴在文字左界的左侧（不超过约 40pt）—— 排除页面左侧的装饰元素。
    """
    if not markers:
        return
    for l in lines:
        for mx0, mx1, my0, my1 in markers:
            my = (my0 + my1) / 2
            if not ((l.y0 - 2.0) <= my <= (l.y1 + 2.0)):
                continue
            if mx1 > l.x0 + 3.0:            # 符号必须在文字左界之外
                continue
            if mx0 < l.x0 - 40.0:           # 但也不能离得太远（那多半是页面装饰）
                continue
            l.bullet = True
            l.marker_x = mx0
            break


def extract_lines(doc) -> list:
    """取出所有视觉行并按 (页, y, x) 排序 —— PyMuPDF 给的是块顺序，必须自己重排。

    逐页顺带做一件事：把**矢量画出来的项目符号**标注到紧邻的行上（annotate_bullets）。
    """
    out = []
    for pno, page in enumerate(doc):
        page_lines = []
        d = page.get_text("dict")
        for bi, blk in enumerate(d.get("blocks", [])):
            if blk.get("type") != 0:
                continue                              # 图片另处理
            for li, line in enumerate(blk.get("lines", [])):
                ln = Line(pno, bi, li, line)
                if ln.text.strip():
                    page_lines.append(ln)
        if page_lines:
            annotate_bullets(page_lines, page_markers(page))
        out.extend(page_lines)
    out.sort(key=lambda l: (l.page, round(l.y0, 1), l.x0))
    return out


def doc_images(doc) -> list:
    """图片对象（含照片）。"""
    out = []
    for pno, page in enumerate(doc):
        for im in page.get_images(full=True):
            try:
                xref = im[0]
                rects = page.get_image_rects(xref)
                r = rects[0] if rects else None
                pix = pymupdf.Pixmap(doc, xref)
                out.append({
                    "page": pno, "xref": xref,
                    "w": pix.width, "h": pix.height,
                    "bbox": [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)] if r else None,
                })
            except Exception:
                pass
    return out


# ============================================================ ② 版面度量

class Metrics:
    """从数据本身推出来的度量（不写死字号）。"""

    def __init__(self, lines, page_h):
        self.page_h = page_h
        # 正文字号：用「字符覆盖量」而不是「行数」取众数。
        # 标题行短、正文行长 —— 按行数在样本稀疏时会跟标题打平甚至判错，
        # 按字符数则天然倾向正文（实测整份简历 90% 以上的字符都是正文字号）。
        cover = {}
        for l in lines:
            t = (l.text or "").strip()
            if not t:
                continue
            k = round(l.size, 1)
            cover[k] = cover.get(k, 0) + len(t)
        self.body_size = max(cover, key=cover.get) if cover else 12.0
        sizes = [round(l.size, 1) for l in lines if l.text.strip()]
        # 视觉行内 x0 的聚类（缩进层级）
        self.indents = sorted({round(l.x0, 0) for l in lines})
        self.base_indent = min(self.indents) if self.indents else 0.0
        # 行距：**行距是"同段内相邻行的 y0 差"，也是所有相邻 y0 差里最小的那一簇**。
        # 不能只按"同 block 内相邻行"算 —— 有些 PDF（vibe-resume 模板）把每一行
        # 都放进独立的 block，那样同 block 内根本没有相邻行，算法就退化了。
        # 改用「出现次数最多的那个较小差值」（众数，0.5pt 分箱）。
        diffs = []
        by_col = {}
        for l in lines:
            by_col.setdefault((l.page, round(l.x0 / 6.0)), []).append(l)
        for group in by_col.values():
            group.sort(key=lambda l: l.y0)
            for a, b in zip(group, group[1:]):
                d = b.y0 - a.y0
                if 1.0 < d < 80:
                    diffs.append(d)
        near = [d for d in diffs if d <= self.body_size * 2.2]
        if near:
            bins = {}
            for d in near:
                bins[round(d * 2) / 2] = bins.get(round(d * 2) / 2, 0) + 1
            self.line_gap = max(bins, key=lambda k: (bins[k], -k))
        else:
            self.line_gap = self.body_size * 1.45
        self.line_gap = min(max(self.line_gap, self.body_size * 1.05), self.body_size * 2.2)
        # 章节标题字号：明显大于正文的
        big = [s for s in set(sizes) if s >= self.body_size * 1.25]
        self.title_sizes = sorted(big)
        # 右对齐区（日期）的 x 阈值
        self.right_edge = max((l.x1 for l in lines), default=0)

    def is_body(self, size):
        return abs(size - self.body_size) < 0.6

    def describe(self):
        return {
            "body_size": self.body_size,
            "line_gap": round(self.line_gap, 2),
            "title_sizes": self.title_sizes,
            "indents": self.indents[:12],
            "base_indent": self.base_indent,
        }


# ============================================================ ③ 拆部件 / 分类

class Part:
    """一行里的一段连续文字（被明显的 x 间隙切开）。"""

    __slots__ = ("text", "x0", "y0", "x1", "y1", "size", "color", "bold", "any_bold", "all_bold", "span_w")

    def __init__(self, spans):
        self.text = "".join(s.text for s in spans)
        self.x0 = min(s.x0 for s in spans)
        self.y0 = min(s.y0 for s in spans)
        self.x1 = max(s.x1 for s in spans)
        self.y1 = max(s.y1 for s in spans)
        # 代表 span：**取字号最大的那个**（同字号则取最左）。
        # 不能取"字符最多的那个" —— 「单位名（9.4pt 粗） + 日期（8.2pt 细）」这种混排部件，
        # 日期往往字符更多，代表 span 会选错，整段就被误判成"小字号元信息"，
        # 实测导致：单位名被当成角色、日期重复出现两次。
        rep = max(spans, key=lambda s: (round(s.size, 1), -s.x0))
        self.size = rep.size
        self.color = rep.color
        self.bold = rep.bold
        self.any_bold = any(s.bold for s in spans)
        self.all_bold = all(s.bold for s in spans)
        self.span_w = self.x1 - self.x0

    def __repr__(self):
        return "<Part x=%.1f sz=%.1f%r %r>" % (self.x0, self.size, self.text[:18], self.text[-6:] if len(self.text) > 18 else "")


def strip_icon_spans(ll: "LogicalLine", metrics: Metrics):
    """把「图标字符」从逻辑行的 span 里摘出来。

    真实故障：单位名前有个**蓝底白字的圆角徽章**，里面是「联」字（公司 logo 用字符画的）。
    pypdf 把它当正文，于是单位名变成「联 某通信规划设计院…」。
    判据组合（缺一不可，因为白字也可能真是正文）：
      · 字符数 ≤ 2
      · 字号小于正文
      · 颜色很浅（白 / 近白）—— 说明它靠色块衬托，是图形化元素
      · 位于行首（徽章总在行首）
    """
    if not ll.parts:
        return "", ll.parts
    ordered = sorted(ll.parts, key=lambda s: s.x0)
    lead_x = ordered[0].x0
    icons, keep = [], []
    for s in ordered:
        t = s.text.strip()
        is_icon = (
            t and len(t) <= 2
            and s.size < metrics.body_size * 0.98
            and _lum(s.color) > 0.85
            and (s.x0 - lead_x) <= metrics.body_size * 2.5      # 行首那块
        )
        (icons if is_icon else keep).append(s)
    return "".join(s.text.strip() for s in icons), keep


def split_parts(line: Line, metrics: Metrics, gap_ratio=2.2) -> list:
    """把一条视觉行按 x 间隙拆成部件。

    典型简历一行里塞了三样东西：`单位名（左） 角色（中） 日期（右对齐）`。
    间隙 > 平均字宽 * gap_ratio 就认为是不同部件。
    """
    spans = sorted(line.spans, key=lambda s: s.x0)
    if not spans:
        return []
    groups, cur = [], [spans[0]]
    for a, b in zip(spans, spans[1:]):
        char_w = max(a.size, b.size) * 0.5 or 6.0
        gap = b.x0 - a.x1
        if gap > char_w * gap_ratio:
            groups.append(cur)
            cur = [b]
        else:
            cur.append(b)
    groups.append(cur)
    return [Part(g) for g in groups if "".join(s.text for s in g).strip()]


class LogicalLine:
    """**逻辑行**：同一 PyMuPDF block 内、垂直方向重叠的若干视觉行横拼而成。

    为什么必须横拼：同一行里字号不同的文字（单位名 12.8pt / 角色 9.8pt / 日期 10.5pt）
    基线不同，PyMuPDF 会把它们拆成 block 内不同的 line。
    若逐条 line 分类，「产品经理助理」「2023.07 - 2023.12」会被当成两个独立要点 —— 真实故障。
    """

    def __init__(self, page, block_i, lines):
        self.page = page
        self.block_i = block_i
        self.lines = sorted(lines, key=lambda l: l.x0)
        self.parts = []
        for ln in self.lines:
            self.parts.extend(sorted(ln.spans, key=lambda s: s.x0))
        self.text = "".join(s.text for s in self.parts)
        self.x0 = min(l.x0 for l in lines)
        self.x1 = max(l.x1 for l in lines)
        self.y0 = min(l.y0 for l in lines)
        self.y1 = max(l.y1 for l in lines)
        main = max(self.parts, key=lambda s: len(s.text.strip()) or 1)
        self.size = main.size
        self.color = main.color
        self.max_size = max((s.size for s in self.parts), default=0.0)
        self.any_bold = any(s.bold for s in self.parts)
        self.height = self.y1 - self.y0
        # 矢量 bullet 标记：组内任一行带标记，整条逻辑行就算列表项
        mks = [getattr(l, "marker_x", None) for l in lines if getattr(l, "bullet", False)]
        self.bullet = bool(mks)
        self.marker_x = min(mks) if mks else None

    def as_line(self) -> Line:
        """包成 Line 以便复用 split_parts。"""
        holder = type("H", (), {})()
        holder.page = self.page
        holder.block_i = self.block_i
        holder.spans = self.parts
        holder.x0, holder.y0, holder.x1, holder.y1 = self.x0, self.y0, self.x1, self.y1
        holder.text = self.text
        holder.size = self.size
        holder.color = self.color
        holder.max_size = self.max_size
        holder.any_bold = self.any_bold
        holder.all_bold = all(s.bold for s in self.parts)
        holder.height = self.height
        holder.bullet = self.bullet
        holder.marker_x = self.marker_x
        return holder

    def __repr__(self):
        return "<LogLine y=%.1f x=%.1f sz=%.1f %r>" % (self.y0, self.x0, self.size, self.text[:26])


def assemble_logical_lines(lines: list, y_tol=3.0) -> list:
    """把视觉行按 block 分组，块内垂直重叠的行横拼成逻辑行。

    块内的换行（y 不相交）保留为独立逻辑行 —— 它们是"折行"，交给 merge_wrapped 按行距合并。
    """
    by_block = {}
    for l in lines:
        by_block.setdefault((l.page, l.block_i), []).append(l)

    out = []
    for (page, bi), group in by_block.items():
        group.sort(key=lambda l: (l.y0, l.x0))
        bucket, cur = [], [group[0]]
        for prev, nxt in zip(group, group[1:]):
            overlap = min(cur[-1].y1, nxt.y1) - max(cur[-1].y0, nxt.y0) > y_tol
            close_base = abs(nxt.y0 - cur[-1].y0) <= y_tol
            if overlap or close_base:
                cur.append(nxt)
            else:
                bucket.append(cur)
                cur = [nxt]
        bucket.append(cur)
        for lineset in bucket:
            out.append(LogicalLine(page, bi, lineset))
    out.sort(key=lambda l: (l.page, l.y0, l.x0))
    return out


#: 行的角色
R_SECTION = "section"      # 章节标题（工作经历）
R_ENTRY = "entry"          # 条目标题（单位 / 项目 / 学校）
R_ROLE = "role"            # 角色（产品经理助理）
R_DATE = "date"            # 日期
R_BULLET = "bullet"        # 要点
R_NOTE = "note"            # 条目简介（顶格、无缩进）
R_ICON = "icon"            # 图标字符（如蓝底白字的 logo）


def classify(line: Line, metrics: Metrics, parts: list, icon: str = "") -> dict:
    """给一条视觉行判定角色 + 子部件。icon 由 strip_icon_spans 预先摘出。"""
    res = {"kind": R_BULLET, "date": "", "role": "", "icon": icon or "", "title": "", "text": ""}
    if not parts:
        res["kind"] = R_ICON
        return res

    # --- 日期：右对齐（x1 接近右边距）且含日期串 ---
    dates, rest = [], []
    for p in parts:
        t = p.text.strip()
        if not t:
            continue
        near_right = (metrics.right_edge - p.x1) < metrics.right_edge * 0.06
        if near_right and DATE_RE.search(t) and len(t) <= 32:
            dates.append(p)
        else:
            rest.append(p)
    if dates:
        res["date"] = DATE_RE.search(dates[0].text).group(0).strip()
        # 日期常和角色挤在同一个部件里（「解决方案交付工程师｜2023.07 - 至今」）
        # → 把非日期部分留作 role，别丢
        leftover = dates[0].text.replace(res["date"], "").strip(" \u3000|｜-—–~·")
        if leftover and not res["role"]:
            res["role"] = leftover
    # 日期也可能是行尾一部分（未被 x 间隙切开）
    if not res["date"] and rest:
        m = DATE_RANGE_RE.search(rest[-1].text[-40:]) or DATE_RE.search(rest[-1].text[-24:])
        if m and (metrics.right_edge - rest[-1].x1) < metrics.right_edge * 0.08:
            res["date"] = m.group(0).strip()

    if not rest:
        res["kind"] = R_DATE
        return res

    # --- 章节标题 ---
    # 实测三种模板，章节标题的"可判据"完全不同：
    #   A. 17.2pt 蓝字（正文 12pt）—— 字号 1.43 倍，最好判
    #   B. 10.5pt 黑字（正文 9pt）—— 字号仅 1.17 倍，无颜色线索
    #   C. 9.4pt 黑字（正文 9.4pt）—— **字号完全相同**，只能靠"加粗 + 短 + 命中章节名"
    #     且 C 的标题缩进(50.2)甚至比条目(42.0)更深 —— 所以"顶格"不能作为硬条件。
    # 共同前提：没有日期（带日期的短加粗行是条目标题，不是章节）。
    biggest = max(p.size for p in rest)
    title_txt = " ".join(p.text.strip() for p in rest).strip()
    lead = rest[0]
    if title_txt and not res["date"]:
        short = len(title_txt) <= 24
        clean_end = title_txt[-1] not in "：:。，,、；;！!？?"
        leftmost = lead.x0 <= metrics.base_indent + 1.5
        is_known = title_txt in SECTION_NAMES
        looks_section = (
            biggest >= metrics.body_size * 1.25
            or (biggest >= metrics.body_size * 1.06 and lead.any_bold and short and clean_end and leftmost)
            # 同字号兜底：命中已知章节名即可，**不要求顶格**
            or (lead.any_bold and short and clean_end and is_known)
        )
        if looks_section:
            res["kind"] = R_SECTION
            res["title"] = title_txt
            return res

    # --- 条目标题：字号略大于正文，或加粗 + 顶格 + 含日期/角色 ---
    lead = rest[0]
    lead_txt = lead.text.strip()
    indented = lead.x0 > metrics.base_indent + metrics.body_size * 0.5
    is_entry_size = lead.size > metrics.body_size + 0.4
    is_bold_lead = lead.any_bold      # 混排部件（粗标题 + 细元信息）整体 bold 会是 False，用 any_bold

    # 角色：比正文小、且和标题同行（"产品经理助理"）
    if len(rest) > 1 and rest[-1].size < metrics.body_size - 0.5 and len(rest[-1].text.strip()) <= 24:
        res["role"] = rest[-1].text.strip()
        rest_for_title = rest[:-1]
    else:
        res_for_title = [p for p in rest if p.size >= metrics.body_size - 0.5]
        small = [p for p in rest if p.size < metrics.body_size - 0.5]
        rest_for_title = res_for_title or rest
        if small and not res["role"]:
            res["role"] = " ".join(p.text.strip() for p in small).strip()

    title_txt = " ".join(p.text.strip() for p in rest_for_title).strip()
    # 日期/地点可能内嵌在条目标题尾部（如「某通信规划设计院 · 解决方案交付工程师2023.07 – 至今 · 北京」）。
    # 这种行右对齐判据不成立（标题后面还跟着日期，整体不贴右边距），所以退一步按"尾部模式"剥离，
    # 否则日期和地点会混进单位名。
    if title_txt:
        m = re.search(
            r"((?:19|20)\d{2}\s*[.\-/年]\s*\d{0,2}\s*月?\s*[-–—~至]\s*"
            r"(?:(?:19|20)\d{2}\s*[.\-/年]\s*\d{0,2}\s*月?|至今|现在|今|present|Present))"
            r"\s*(?:[·•|｜]\s*(.{1,14}))?$", title_txt)
        if m:
            if not res["date"]:
                res["date"] = m.group(1).strip()
            loc = (m.group(2) or "").strip()
            if loc:
                res["role"] = " ".join(x for x in [res.get("role", ""), loc] if x).strip()
            head_only = title_txt[:m.start()].strip(" \u3000·•|｜-—–")
            if head_only:
                title_txt = head_only
    # 标题行特征：有日期 或 字号更大 或（顶格 + 加粗 + 够短）
    looks_entry = bool(res["date"]) or is_entry_size or (is_bold_lead and not indented and len(title_txt) <= 60)
    # 「求职意向：…」是简历头部字段，不是条目标题
    if re.match(r"求职意向\s*[:：]", title_txt):
        looks_entry = False
    if looks_entry and title_txt:
        res["kind"] = R_ENTRY
        res["title"] = title_txt
        return res

    # --- 要点 / 简介 ---
    res["text"] = " ".join(p.text.strip() for p in rest).strip()
    if indented:
        res["kind"] = R_BULLET
    else:
        res["kind"] = R_NOTE           # 顶格且无日期 → 条目简介
    # 项目符号优先：文字层有「•」**或**左侧有矢量小圆点（画出来的符号）
    drawn = bool(getattr(line, "bullet", False))
    res["drawn_bullet"] = drawn
    if drawn or BULLET_RE.match(res["text"]):
        res["kind"] = R_BULLET
    # 「项目介绍：…」这类是**条目简介**，即使它跟要点一样有缩进
    if INTRO_RE.match(res["text"]):
        res["kind"] = R_NOTE
    return res


# ============================================================ ④ 合并折行

def merge_wrapped(items: list, metrics: Metrics) -> list:
    """把被折行切断的同一段合并回一条。

    ⚠️ 不能依赖 PyMuPDF 的 block 边界：实测有模板（vibe-resume）把**每一行都放进
    独立的 block**，此时 block 边界毫无段落信息。所以改成纯几何判据，全部满足才合并：

      · 同缩进（x0 差 ≤ 2.5pt）—— 折行的续行与首行左端对齐
      · y0 间距 ≈ 行距（≤ 1.18 倍）—— 段间距实测是行距的 1.25 倍以上，能分开
      · 上一行「没写完」（不以 。！？ 结尾）
      · 本行不是新条目的开头（不以日期区间 / 项目符号开头）
      · 同角色（要点续要点、简介续简介）

    实测校准：行距 14.2pt 的文档，段内续行 14.2~14.3，段间 18.0+；
    行距 17.2pt 的文档，段内续行 17.2，段间 18.0~26.4。
    """
    out = []
    for it in items:
        if out:
            prev = out[-1]
            # 简介 / 要点允许互相续接：折行的后一行会丢掉「项目介绍：」这个前缀，
            # 于是被独立判成「要点」，若严格要求同类就永远拼不回来 ——
            # 真实故障：项目介绍被切成「…能力体系为」+「用”体系。…」两段。
            same_kind = prev["kind"] in (R_BULLET, R_NOTE) and it["kind"] in (R_BULLET, R_NOTE)
            same_indent = abs(it["x0"] - prev["x0"]) <= 2.5
            dy = it["y0"] - prev.get("_last_y0", prev["y0"])
            tight = 0 < dy <= metrics.line_gap * 1.18
            prev_open = bool(prev["text"]) and prev["text"].rstrip()[-1:] not in "。！？.!?"
            starts_new = bool(DATE_RANGE_RE.match(it["text"].lstrip())) or bool(BULLET_RE.match(it["text"]))
            if same_kind and same_indent and tight and prev_open and not starts_new:
                prev["text"] = _join_text(prev["text"], it["text"])
                prev["y1"] = it["y1"]
                prev["x1"] = max(prev["x1"], it["x1"])
                prev["_last_y0"] = it["y0"]
                if it.get("bold_prefix") and not prev.get("bold_prefix"):
                    prev["bold_prefix"] = it["bold_prefix"]
                continue
        it["_last_y0"] = it["y0"]
        out.append(it)
    return out


def merge_bullet_continuations(items: list, metrics: Metrics) -> list:
    """把「同一条 bullet 折出来的行」接回去 —— 治「PDF 里是换行、却被解析成分段」这个最致命的错误。

    为什么不复用 merge_wrapped：它要求「同角色 + 同缩进（≤2.5pt）」，而 bullet 的折行
    有两个硬伤：
      · 续行往往与**正文左端**对齐，而首行的 x0 把前面的项目符号也算进去了 → 差出 5~10pt，
        被判成"不同缩进"；
      · 续行本身既没有符号、也不一定顶格或缩进，容易被判成另一种角色（NOTE）→ "不同角色"。
    两条叠加，长 bullet 就被切成了好几段。

    这里改成 bullet 专属的判据（借鉴「找工作助手」pdf_reconstruct.py 的续行合并）：
      · 首行是 bullet（文字符号 或 矢量符号），续行是 bullet / 简介 都行
      · 垂直：紧跟着（行距 ≤ 1.45 倍），不是新的段落间距
      · 水平：**宽松容差**（≤ max(6, 2.5×字号)）—— 接得住"符号位置 vs 正文位置"的偏差
      · 上一行"没写完"（不以 。！？ 结尾）
      · 本行不是新条目的开头（不以日期区间 / 项目符号开头，也不是章节或条目标题）
    """
    out = []
    tol = max(6.0, metrics.body_size * 2.5)
    for it in items:
        if out:
            prev = out[-1]
            if prev["kind"] == R_BULLET and it["kind"] in (R_BULLET, R_NOTE):
                dy = it["y0"] - prev.get("_last_y0", prev["y0"])
                tight = 0 < dy <= metrics.line_gap * 1.45
                hx_ok = abs(it["x0"] - prev["x0"]) <= tol
                prev_open = bool(prev["text"]) and prev["text"].rstrip()[-1:] not in "。！？.!?"
                starts_new = (
                    bool(DATE_RANGE_RE.match(it["text"].lstrip()))
                    or bool(BULLET_RE.match(it["text"]))
                    or bool(it.get("drawn_bullet"))
                    or it["kind"] in (R_SECTION, R_ENTRY)
                )
                if tight and hx_ok and prev_open and not starts_new:
                    prev["text"] = _join_text(prev["text"], it["text"])
                    prev["y1"] = it["y1"]
                    prev["x1"] = max(prev["x1"], it["x1"])
                    prev["_last_y0"] = it["y0"]
                    if it.get("bold_prefix") and not prev.get("bold_prefix"):
                        prev["bold_prefix"] = it["bold_prefix"]
                    continue
        it["_last_y0"] = it["y0"]
        out.append(it)
    return out


def bold_prefix_of(line: Line) -> str:
    """取行首那截加粗小标题（简历常见：「**售前支撑与 AI 场景识别**：…」）。"""
    spans = sorted(line.spans, key=lambda s: s.x0)
    if not spans or not spans[0].bold:
        return ""
    buf = ""
    for s in spans:
        if not s.bold:
            break
        buf += s.text
    buf = buf.strip()
    # 必须以「：」收尾才算小标题，否则可能是整行加粗的普通内容
    return buf if buf.endswith(("：", ":")) else ""


# ============================================================ ⑤ 组装结构

def _assign_levels(sections: list, body_size: float = 9.0) -> None:
    """给每个 block 里的 bullets 标 `level`（1 / 2 / 3）—— 依据同一 block 内的缩进聚类。

    真实简历常见两级：
        项目工作：              <- 1 级（x=37.7）
            市场与竞品分析：…    <- 2 级（x=52.7，缩进更深）
    PDF 只给 x0，不告诉层级；把它聚成 level，渲染端才能递进缩进，
    否则所有要点会被压平在同一行首，看起来"没对齐"。
    """
    step = max(6.0, float(body_size or 9.0) * 1.6)
    for sec in (sections or []):
        for blk in (sec.get("blocks") or []):
            xs = [b.get("_x0") for b in (blk.get("bullets") or []) if b.get("_x0") is not None]
            if not xs:
                continue
            base = min(xs)
            for b in (blk.get("bullets") or []):
                x = b.get("_x0")
                if x is None:
                    continue
                lvl = 1 + int(round((float(x) - float(base)) / step))
                if lvl > 1:
                    b["level"] = min(3, lvl)


#: 「项目工作：」这类**分组标题**（不是普通要点）——它是二级要点的标题行
_SUBHEAD_RE = re.compile(r"^\s*(项目工作|工作内容|主要工作|工作职责|项目职责|具体工作|主要职责)\s*[:：]\s*$")

#: 同样的分组标题被折行合并粘到了「项目介绍」结尾（旧解析产物常见的形态）
_SUBHEAD_TAIL_RE = re.compile(
    r"(项目工作|工作内容|主要工作|工作职责|项目职责|具体工作|主要职责)\s*[:：]?\s*$")

#: 「项目工作：后面直接跟正文」—— 标题要和正文拆成两行（标题独占一行，正文做二级要点）
_SUBHEAD_LINE_RE = re.compile(
    r"^\s*(项目工作|工作内容|主要工作|工作职责|项目职责|具体工作|主要职责)\s*[:：]\s*(.*)$")


def _split_subhead_from_intro(sections: list) -> None:
    """兜底：把粘在「项目介绍」末尾的「项目工作：」拆出来单独成行。

    intro 是整段说明，分组标题应当独占一行。旧解析 / 折行合并可能得到
    `intro = "…智能调优。项目工作："` 且 subhead 为空 —— 页面上就会看到
    「项目工作：」紧贴介绍最后一句，层级也丢了。这里就地拆开。
    """
    for s in sections:
        for b in (s.get("blocks") or []):
            if b.get("subhead") or not b.get("bullets"):
                continue
            intro = (b.get("intro") or "").strip()
            if not intro:
                continue
            m = _SUBHEAD_TAIL_RE.search(intro)
            if not m:
                continue
            b["intro"] = intro[:m.start()].strip()
            b["subhead"] = m.group(1) + "："


def _extract_subheads(sections: list) -> None:
    """把「项目工作：」从要点里摘出来，放到 block["subhead"]。

    简历的项目结构是固定的三层：
        项目介绍：…（一段说明）
        项目工作：（分组标题）
            · 市场与竞品分析：…（二级要点）
    如果把「项目工作：」当普通要点渲染，它会变成一条带圆点的条目，层级看起来就乱了。
    """
    for sec in (sections or []):
        for blk in (sec.get("blocks") or []):
            if blk.get("subhead"):
                continue
            bl = blk.get("bullets") or []
            picked = None
            for i, b in enumerate(bl):
                if (b.get("level") or 1) != 1:
                    continue
                t = (b.get("text") or "").strip()
                m = _SUBHEAD_LINE_RE.match(t)
                if not m:
                    continue
                rest = (m.group(2) or "").strip()
                if rest:
                    # 「项目工作：评估原有系统…」同行 —— 标题单独成行，正文留成一条二级要点
                    picked = (i, m.group(1).strip() + "：", rest)
                    break
                # 纯标题行：后面必须真有更深一级的要点，否则它可能只是一条普通内容
                if any((x.get("level") or 1) > 1 for x in bl[i + 1:]):
                    picked = (i, t, "")
                    break
            if not picked:
                continue
            i, head, rest = picked
            blk["subhead"] = head
            if rest:
                bl[i]["text"] = rest
            else:
                del bl[i]
            # 缩进由渲染端的「二级列表」统一给，这里把层级相对降一级
            for x in bl:
                if x.get("level"):
                    x["level"] = max(1, int(x["level"]) - 1)


def build_resume(lines: list, metrics: Metrics, doc_meta: dict) -> dict:
    """lines → {name, phone, email, intent, sections:[{title, blocks:[...]}]}"""

    # 1) 视觉行 → 逻辑行 → 逐条取部件 + 分类
    loglines = assemble_logical_lines(lines)
    items = []
    pending_icon = ""
    for ll in loglines:
        holder = ll.as_line()
        icon, clean = strip_icon_spans(ll, metrics)
        if clean:
            holder.spans = clean                 # 用净化后的 span 做部件切分
        parts = split_parts(holder, metrics)
        c = classify(holder, metrics, parts, icon)
        # 只有图标、没有正文的行（徽章独占一行）→ 挂到下一个条目标题上
        if c["kind"] == R_ICON and not parts:
            pending_icon = (pending_icon + c["icon"]).strip()
            continue
        if not parts:
            continue
        if pending_icon and not c["icon"]:
            c["icon"] = pending_icon
        pending_icon = ""
        items.append({
            "page": ll.page, "y0": ll.y0, "y1": ll.y1,
            "x0": min(p.x0 for p in parts) if parts else ll.x0,
            "x1": max(p.x1 for p in parts) if parts else ll.x1,
            "size": ll.size, "color": ll.color, "bold": ll.any_bold,
            "kind": c["kind"], "text": c["text"], "title": c["title"],
            "role": c["role"], "date": c["date"], "icon": c["icon"],
            "bold_prefix": bold_prefix_of(holder),
            "drawn_bullet": bool(c.get("drawn_bullet")),
            "indent": ll.x0,
            "_blk": (ll.page, ll.block_i),
        })

    # 2) 合并折行：
    #    ① 先把「同一 bullet 的折行」接回去 —— PDF 里是换行、却容易被当成分段，
    #       是最毁结构的一类错误（长 bullet 折成 3 行就变成 3 段）；
    #    ② 再按通用规则合并普通段落的折行。
    items = merge_bullet_continuations(items, metrics)
    items = merge_wrapped(items, metrics)

    # 3) 头部元信息（第一页、第一个「已知章节标题」之前的内容）
    #    注意：姓名/教育经历这类头部块的字号也可能很大，不能只看"最大字号=章节"。
    #    判据用「标题命中已知章节名」，找不到时退回"第一页第一个 section 且其后还有内容"。
    def is_known_section(it):
        return it["kind"] == R_SECTION and (it["title"] or "").strip() in SECTION_NAMES

    first_section = next((i for i, it in enumerate(items) if is_known_section(it)), None)
    if first_section is None:
        # 没有已知章节名 → 取第一页里"第一个 section 且不是第一行"的位置
        cands = [i for i, it in enumerate(items) if it["kind"] == R_SECTION and i > 0]
        first_section = cands[-1] if len(cands) == 1 else (cands[0] if cands else len(items))
    header_items = items[:first_section]
    body_items = items[first_section:]

    meta = {"name": "", "phone": "", "email": "", "intent": "", "extra": []}
    if header_items:
        # 名字 = 字号最大的那条
        nm = max(header_items, key=lambda it: it["size"])
        meta["name"] = (nm["title"] or nm["text"]).strip()
        allhead = " ".join((it["title"] or it["text"]) for it in header_items)
        # 手机号：允许 138 0013 8000 / 138-0013-8000 / 13800138000
        m = (re.search(r"1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}", allhead)
             or re.search(r"1[3-9]\d{9}", allhead))
        if m:
            meta["phone"] = m.group(0).strip()
        m = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", allhead)
        if m:
            meta["email"] = m.group(0)
        # 求职意向：取到分隔符「·」「|」或行尾（不能用 \s 截断，否则「AI 应用运营」会被切成「AI」）
        m = re.search(r"求职意向[:：]\s*(.+?)\s*(?:[·•\|｜]|$)", allhead)
        if m:
            meta["intent"] = m.group(1).strip()
        for it in header_items:
            t = (it["title"] or it["text"]).strip()
            if not t or t == meta["name"] or it["kind"] == R_ICON:
                continue
            # 已经提取进 phone/email/intent 的行不要再当"额外头信息"重复展示
            residue = t
            for v in (meta["phone"], meta["email"]):
                if v:
                    residue = residue.replace(v, "")
            residue = re.sub(r"求职意向\s*[:：].*", "", residue)
            residue = residue.strip(" \u3000|｜·•-—–")
            if not residue:
                continue
            # 只保留"去掉联系方式后剩下的那部分"（如「预备党员」），
            # 别把整行再放一遍，否则页面上手机号邮箱会出现两次
            meta["extra"].append(residue)

    # 4) 分组：section → entry(block) → bullets
    sections = []
    cur_sec = None
    cur_blk = None

    def ensure_sec(title="未命名章节"):
        nonlocal cur_sec
        if cur_sec is None:
            cur_sec = {"title": title, "blocks": []}
            sections.append(cur_sec)
        return cur_sec

    for it in body_items:
        k = it["kind"]
        if k == R_SECTION:
            cur_sec = {"title": it["title"] or "未命名章节", "blocks": []}
            sections.append(cur_sec)
            cur_blk = None
            continue
        if k == R_ENTRY:
            sec = ensure_sec()
            cur_blk = {
                "heading": it["title"] or "未命名",
                "icon": it.get("icon", ""),
                "role": it.get("role", ""),
                "date": it.get("date", ""),
                "meta": " ".join(x for x in [it.get("role", ""), it.get("date", "")] if x).strip(),
                "intro": "",
                "bullets": [],
                "_just_entry": True,
                "_entry_y": it["y0"],
            }
            sec["blocks"].append(cur_blk)
            continue
        if k == R_NOTE:
            sec = ensure_sec()
            if cur_blk is None:
                cur_blk = {"heading": "", "icon": "", "role": "", "date": "", "meta": "", "intro": "", "bullets": []}
                sec["blocks"].append(cur_blk)
            # 紧跟在条目标题下面、顶格、很短的一行 —— 它是**角色 / 地点**那一行，
            # 不是条目简介。（如「解决方案交付工程师 智慧城市设计院部门 北京」）
            just_entry = cur_blk.get("_just_entry") and not cur_blk["intro"] and not cur_blk["bullets"]
            dy = it["y0"] - cur_blk.get("_entry_y", it["y0"])
            if just_entry and len(it["text"]) <= 44 and 0 <= dy <= metrics.line_gap * 2.0:
                cur_blk["meta"] = " ".join(x for x in [cur_blk["meta"], it["text"]] if x).strip()
                cur_blk["_just_entry"] = False
            else:
                cur_blk["intro"] = _join_text(cur_blk["intro"], it["text"])
                cur_blk["_just_entry"] = False
            continue
        if k == R_BULLET:
            sec = ensure_sec()
            if cur_blk is None:
                cur_blk = {"heading": "", "icon": "", "role": "", "date": "", "meta": "", "intro": "", "bullets": []}
                sec["blocks"].append(cur_blk)
            cur_blk["_just_entry"] = False
            txt = BULLET_RE.sub("", it["text"]).strip()
            if txt:
                cur_blk["bullets"].append({
                    "text": txt,
                    "bold_prefix": it.get("bold_prefix", ""),
                    "_x0": it.get("x0"),          # 层级聚类用，产物里会删掉
                })
            continue
        # R_ICON / R_DATE 单独成行 → 忽略（已被 entry 吸收）

    # 5) 章节排序（按标准顺序；未知章节保持原顺序放后面）
    for i, s in enumerate(sections):
        s["_ord"] = i
    sections.sort(key=lambda s: (SECTION_ORDER.get(s["title"], 900 + s["_ord"]), s["_ord"]))
    for s in sections:
        s.pop("_ord", None)

    # 6) 稳定 bid（顺手清掉内部用的临时键，别把下划线字段带到产物里）
    #    先按缩进给要点标层级（1/2/3），再删掉用于聚类的 _x0
    _assign_levels(sections, metrics.body_size)
    _extract_subheads(sections)
    _split_subhead_from_intro(sections)
    for si, s in enumerate(sections):
        s["id"] = "s%d" % (si + 1)
        base = sum(len(x["blocks"]) for x in sections[:si])
        for bi, b in enumerate(s["blocks"]):
            b.pop("_just_entry", None)
            b.pop("_entry_y", None)
            b["id"] = "p%d" % (base + bi + 1)
            for xi, x in enumerate(b["bullets"]):
                x.pop("_x0", None)
                x["bid"] = "%s-b%d" % (b["id"], xi + 1)

    return {
        "id": "resume-" + str(doc_meta.get("mtime", ""))[:10].replace("-", "") or "resume-1",
        "name": meta["name"], "phone": meta["phone"], "email": meta["email"],
        "intent": meta["intent"], "extra": meta["extra"],
        "sections": sections,
    }


# ============================================================ ⑥ 对外入口

def debug_lines(lines: list, metrics: Metrics) -> list:
    """给「版面核对」视图用：每条逻辑行的角色判定（合并折行之前，逐行可见）。

    返回 [{page,y0,y1,x0,x1,size,color,bold,kind,icon,date,role,text}]
    """
    out = []
    for ll in assemble_logical_lines(lines):
        holder = ll.as_line()
        icon, clean = strip_icon_spans(ll, metrics)
        if clean:
            holder.spans = clean
        parts = split_parts(holder, metrics)
        c = classify(holder, metrics, parts, icon)
        kind = c["kind"]
        text = c["title"] or c["text"] or ("[%s]" % c["icon"] if c["icon"] else "")
        if c["kind"] in (R_ENTRY,) and (c.get("role") or c.get("date")):
            text = " · ".join(x for x in [c["title"], c.get("role"), c.get("date")] if x)
        out.append({
            "page": ll.page, "y0": round(ll.y0, 1), "y1": round(ll.y1, 1),
            "x0": round(ll.x0, 1), "x1": round(ll.x1, 1),
            "size": ll.size, "color": ll.color, "bold": ll.any_bold,
            "kind": kind, "icon": c["icon"], "date": c.get("date", ""),
            "role": c.get("role", ""), "text": text,
            # raw = 未加工的原文。AI 判构与组装都用它，
            # 保证「模型看到的字」与「组装进简历的字」是同一份。
            "raw": ll.text.strip(),
        })
    return out


# ================================================================== AI 判构
# 思路：几何负责**取字**（字符直接从 PDF 文本层读出，100% 准确、零幻觉），
#       模型只负责**判构**（回答"第几行是什么"），**一个字都不许它写**。
# 这样数字守恒从"事后检查"变成"结构上不可能违反"，换模板也不会再崩。

def ai_worksheet(res):
    """生成给模型看的「版面描述表」—— 纯文本、带行号、只含几何特征 + 原文。

    刻意**不带**几何规则已经判好的角色（kind）：让模型独立看版面，
    两边结论才能互相印证；把规则结果喂给它只会让错误同化。

    行号 = res["lines"] 的下标 + 1，与 build_from_line_map 用的是同一份，
    所以模型说"第 5 行是要点"，我们能原样取回来。
    """
    lines = res.get("lines") or []
    if not lines:
        return ""

    xs = [l.get("x0") or 0.0 for l in lines]
    base = min(xs) if xs else 0.0
    ys = [l.get("y0") for l in lines if l.get("y0") is not None]
    gaps = [b - a for a, b in zip(ys, ys[1:]) if 1 < (b - a) < 60]
    gap = sorted(gaps)[len(gaps) // 2] if gaps else 16.0

    out, prev_y = [], None
    for i, l in enumerate(lines, 1):
        txt = (l.get("raw") or l.get("text") or "").replace("\n", " ").strip()
        if not txt:
            continue
        y = l.get("y0")
        # 视觉上明显断开 → 空一行，给模型一个"段落边界"的信号
        if prev_y is not None and y is not None and (y - prev_y) > gap * 1.6:
            out.append("")
        if y is not None:
            prev_y = y
        off = (l.get("x0") or 0.0) - base
        ind = "顶格" if off < 2 else "+%.1f" % off
        out.append("[%d] %.1fpt %s %s %s 「%s」"
                   % (i, l.get("size") or 0.0, "粗" if l.get("bold") else "细",
                      ind, l.get("color") or "", txt))
    return "\n".join(out)


def _walk_line_refs(node, path, out):
    """把映射里的所有行号引用收集出来（顺便记录类型错误）。"""
    if isinstance(node, bool):
        out.append(("bad", path, node))
    elif isinstance(node, int):
        out.append(("ref", path, node))
    elif isinstance(node, (list, tuple)):
        for i, x in enumerate(node):
            _walk_line_refs(x, "%s[%d]" % (path, i), out)
    elif isinstance(node, dict):
        for k, v in node.items():
            _walk_line_refs(v, "%s.%s" % (path, k), out)
    elif node is None or node == "":
        pass
    else:
        out.append(("bad", path, node))


def validate_line_map(n, m, min_cover=0.45):
    """校验模型返回的行号映射。返回 (ok, issues, stats)。

    行号映射是后面组装简历的唯一依据 —— 一旦越界就会取到空或取错行，
    宁可判定失败、降级回几何规则，也不能把错结构交给用户。
    """
    issues, refs = [], []
    if not isinstance(m, dict):
        return False, ["返回的不是 JSON 对象（%r）" % type(m).__name__], {}

    _walk_line_refs(m, "$", refs)
    used = {}
    for kind, path, val in refs:
        if kind == "bad":
            issues.append("%s: 期望行号，实际 %r" % (path, val))
            continue
        if not (1 <= val <= n):
            issues.append("%s: 行号 %d 越界（合法范围 1..%d）" % (path, val, n))
            continue
        used[val] = used.get(val, 0) + 1

    secs = m.get("sections")
    if not isinstance(secs, list) or not secs:
        issues.append("sections 为空或不是数组")

    n_entries = 0
    for sec in (secs or []):
        if not isinstance(sec, dict):
            continue
        for it in (sec.get("entries") or []):
            if isinstance(it, dict):
                if (it.get("head") or it.get("bullets") or it.get("intro")
                        or it.get("meta")):
                    n_entries += 1
    if not n_entries:
        issues.append("一条经历都没标出来")

    cover = (len(used) / float(n)) if n else 0.0
    dup = sorted([k for k, v in used.items() if v > 1])
    stats = {"total": n, "used": len(used), "cover": round(cover, 3), "dup": dup}
    if cover < min_cover:
        issues.append("覆盖率过低（%.0f%% < %.0f%%），可能整块漏标"
                      % (cover * 100, min_cover * 100))
    return (not issues), issues, stats


def build_from_line_map(res, m, extra_meta=None):
    """按模型给的行号映射，从**原文**组装 resume。

    核心保证：所有文字都取自 lines[i]["raw"]，这里不生成任何一个字。
    行号越界/缺失一律当空处理（校验层已经拦过一遍，这里是最后一道兜底）。
    """
    lines = res.get("lines") or []
    n = len(lines)

    def T(v):
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= n):
            return ""
        return (lines[v - 1].get("raw") or lines[v - 1].get("text") or "").strip()

    def TL(vs):
        return [t for t in (T(v) for v in (vs or [])) if t]

    def X(v):
        """取该行的 x0（层级聚类用）。"""
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= n):
            return None
        return lines[v - 1].get("x0")

    name = T(m.get("name"))
    intent = T(m.get("intent"))
    if intent:
        intent = re.sub(r"^\s*求职意向\s*[:：]\s*", "", intent).strip()

    phone = email = ""
    extra = []
    for c in TL(m.get("contacts")):
        mm = re.search(r"1[3-9]\d{9}|1\d{2}[\s\-]?\d{4}[\s\-]?\d{4}", c)
        if mm and not phone:
            phone = mm.group(0)
            continue
        mm = re.search(r"[\w.\-+]+@[\w.\-]+\.\w+", c)
        if mm and not email:
            email = mm.group(0)
            continue
        extra.append(c)

    sections, si = [], 0
    for sec in (m.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        title = T(sec.get("title"))
        blocks = []
        for it in (sec.get("entries") or []):
            if not isinstance(it, dict):
                continue
            head = " · ".join(TL(it.get("head")))
            meta_parts = TL(it.get("meta"))
            intro = ""
            for t in TL(it.get("intro")):
                intro = _join_text(intro, t)
            bullets = []
            for v in (it.get("bullets") or []):
                t = T(v)
                if t:
                    bullets.append({"text": t, "_x0": X(v)})
            if not (head or bullets or intro or meta_parts):
                continue
            date = " · ".join(meta_parts)
            # 模型常把「公司 · 职位 · 日期」标成同一行 → 把日期剥出来单独放，
            # 这样标题干净、模板也能正确把日期排到右侧。
            if head:
                dm = DATE_RANGE_RE.search(head)
                if dm:
                    if not date:
                        date = dm.group(0).strip()
                    # 日期后面常粘着地点（"…工程师2023.07 – 至今 · 北京"），
                    # 一起移出标题 —— 否则标题里挂着一串日期和城市。
                    tail = head[dm.end():].strip(" 　·|｜-–—~～")
                    if tail:
                        date = (date + " · " + tail).strip(" ·")
                    head = head[:dm.start()].strip(" 　·|｜-–—~～")
            blocks.append({
                "id": "", "heading": head, "icon": "", "role": "",
                "date": date, "meta": date, "intro": intro, "bullets": bullets,
            })
        if not blocks:
            continue
        si += 1
        sections.append({"id": "s%d" % si,
                         "title": title or ("章节 %d" % si),
                         "blocks": blocks})

    _assign_levels(sections, (res.get("metrics") or {}).get("body_size") or 9.0)
    _extract_subheads(sections)
    _split_subhead_from_intro(sections)
    pi = 0
    for s in sections:
        for b in s["blocks"]:
            pi += 1
            b["id"] = "p%d" % pi
            for bi, x in enumerate(b["bullets"], 1):
                x.pop("_x0", None)
                x["bid"] = "%s-b%d" % (b["id"], bi)
                x["bold_prefix"] = ""

    return {
        "id": "resume-%d" % int(time.time()),
        "name": name,
        "phone": phone,
        "email": email,
        "intent": intent,
        "extra": extra,
        "sections": sections,
    }


def quality(res) -> dict:
    """给解析结果打分，用来判断"这次解析到底能不能用"。

    ⚠️ 为什么要这一步：真实事故 —— 解析器对某类模板返回了 `sections: []`，
    但调用方只看 `ok`，于是"0 章节"被当成功，用户看到一片空白 + 后面 AI 乱补的一坨。
    **"没解析出内容"必须当失败**，让上层能回退并给出可执行提示。
    """
    r = res.get("resume") or {}
    secs = r.get("sections") or []
    blocks = [b for s in secs for b in (s.get("blocks") or [])]
    bullets = [x for b in blocks for x in (b.get("bullets") or [])]
    chars = sum(len(x.get("text") or "") for x in bullets) + sum(len(b.get("intro") or "") for b in blocks)
    usable = bool(secs) and bool(blocks) and (len(bullets) >= 3 or chars >= 120)
    reasons = []
    if not secs:
        reasons.append("没有识别出任何章节标题")
    if not blocks:
        reasons.append("没有识别出任何经历/项目条目")
    if not bullets and chars < 120:
        reasons.append("没有识别出可编辑的要点")
    return {
        "usable": usable,
        "sections": len(secs), "blocks": len(blocks), "bullets": len(bullets),
        "chars": chars,
        "reasons": reasons,
    }


def parse(path, keep_images=True) -> dict:
    doc = pymupdf.open(path)
    try:
        lines = extract_lines(doc)
        page_h = doc[0].rect.height if len(doc) else 842
        metrics = Metrics(lines, page_h)
        meta = {
            "path": os.path.basename(path),
            "pages": len(doc),
            "page_size": [round(doc[0].rect.width, 1), round(doc[0].rect.height, 1)] if len(doc) else None,
            "mtime": os.path.getmtime(path),
        }
        resume = build_resume(lines, metrics, meta)
        out = {
            "ok": True,
            "meta": meta,
            "metrics": metrics.describe(),
            "images": doc_images(doc) if keep_images else [],
            "resume": resume,
            "lines": debug_lines(lines, metrics),
        }
        q = quality(out)
        out["quality"] = q
        # 结构不可用时**明确标成失败**，上层才能回退 —— 不要把空结构当成功传下去
        if not q["usable"]:
            out["ok"] = False
            out["error"] = "no_structure"
            out["message"] = "这份 PDF 没能识别出简历结构（%s）" % "；".join(q["reasons"])
            out["hint"] = ("常见原因：① 是扫描件/图片型（没有文字层）；② 排版特殊（如整页表格、"
                           "多栏混排），自动判据没覆盖；③ 内容不是简历。"
                           "可以先用「粘贴简历文本」入口，或把 PDF 另存为文本版再传。")
    finally:
        doc.close()
    return out


def to_plain_text(res) -> str:
    """便于对照的纯文本视图。图标用 [x] 标注，与正文区分开。"""
    r = res["resume"]
    out = [r["name"], " ".join(x for x in [r["phone"], r["email"]] if x),
           ("求职意向：" + r["intent"]) if r["intent"] else "", ""]
    for s in r["sections"]:
        out.append("【%s】" % s["title"])
        for b in s["blocks"]:
            icon = ("[%s] " % b["icon"]) if b.get("icon") else ""
            head = " ".join(x for x in [b["heading"], b.get("meta", "")] if x)
            if head.strip():
                out.append(icon + head)
            if b.get("intro"):
                out.append("　" + b["intro"])
            for x in b["bullets"]:
                out.append("· " + x["text"])
        out.append("")
    return "\n".join(out).strip()


def summary(res) -> str:
    r = res["resume"]
    m = res["metrics"]
    bins = sum(len(s["blocks"]) for s in r["sections"])
    bults = sum(len(b["bullets"]) for s in r["sections"] for b in s["blocks"])
    return ("%d 页 · 正文 %.1fpt · 行距 %.1f · 章节 %d · 条目 %d · 要点 %d · 图片 %d"
            % (res["meta"]["pages"], m["body_size"], m["line_gap"],
               len(r["sections"]), bins, bults, len(res["images"])))


if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    r = parse(sys.argv[1])
    print(summary(r))
    print("-" * 60)
    print(to_plain_text(r))
    if "--json" in sys.argv:
        print("-" * 60)
        print(json.dumps(r["resume"], ensure_ascii=False, indent=2))
