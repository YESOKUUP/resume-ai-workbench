# -*- coding: utf-8 -*-
"""简历渲染器 —— 把解析结果（JSON）套进内置模板库，输出可直接用的 HTML / PDF

用法
----
  # 先解析 PDF 再套模板（一步到位）
  python tools/render.py 简历.pdf -t classic -o out/

  # 三套模板一次全出，直接对比
  python tools/render.py 简历.pdf --all -o out/

  # 已经有了 JSON，直接渲染（不重新解析）
  python tools/render.py 简历.json -t modern

  # 顺便导出 PDF（用本机 Chrome 无头模式）
  python tools/render.py 简历.pdf --all --pdf -o out/

  # 看有哪些模板
  python tools/render.py --list

设计原则
--------
解析与渲染彻底分离：本工具**不改一个字**，只负责把结构化数据排进模板。
所以换模板、改样式，永远不影响内容的准确性。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import templates  # noqa: E402


# ----------------------------------------------------------------- 浏览器探测

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    """优先复用产品里那套（server/docio.py 已经踩过坑），找不到再自己扫。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "server"))
        from docio import find_browser as _fb  # type: ignore
        b = _fb()
        if b:
            return b
    except Exception:
        pass
    for p in _CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("chrome", "msedge", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    return None


def html_to_pdf(html_path, pdf_path, browser=None):
    """无头浏览器打印 PDF。返回 (ok, 页数或错误信息)。"""
    browser = browser or find_browser()
    if not browser:
        return False, "没找到 Chrome / Edge，无法导出 PDF"
    # 带空格 / 中文的本地路径必须 URL 编码，否则 Chrome 会解析出错页
    url = "file:///" + urllib.parse.quote(os.path.abspath(html_path).replace("\\", "/"))
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=4000",
        "--print-to-pdf=" + os.path.abspath(pdf_path), url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        return False, "调用浏览器失败：%s" % e
    if not os.path.exists(pdf_path):
        msg = (r.stderr or b"").decode("utf-8", "replace")[-300:]
        return False, "PDF 没生成：%s" % (msg or "未知原因")
    try:
        from pypdf import PdfReader
        n = len(PdfReader(pdf_path).pages)
        return True, n
    except Exception:
        return True, None


# ----------------------------------------------------------------- 解析入口

def parse_pdf(path):
    """复用 pdf2html 的解析链路（几何解析，不调 AI）。"""
    import pdfstruct  # noqa: E402
    res = pdfstruct.parse(path)
    if not res.get("ok"):
        q = res.get("quality") or {}
        raise RuntimeError("解析失败：%s（%s）" % (q.get("hint") or "结构为空", ", ".join(q.get("reasons") or [])))
    return res["resume"], res


def load_resume(path):
    """接受 .pdf 或 .json，统一返回 resume 数据。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        d = json.load(io.open(path, encoding="utf-8"))
        return (d.get("resume") or d), d
    if ext == ".pdf":
        return parse_pdf(path)
    raise RuntimeError("只认 .pdf 或 .json，收到：%s" % ext)


# ----------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把简历解析结果套进内置模板库，输出 HTML / PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", nargs="?", help="简历文件（.pdf 或 .json）")
    ap.add_argument("-t", "--template", default="classic",
                    help="模板名（默认 classic，用 --list 看全部）")
    ap.add_argument("--all", action="store_true", help="三套模板一次全渲染")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认与输入同级）")
    ap.add_argument("--stem", default=None, help="输出文件名前缀（默认取输入文件名）")
    ap.add_argument("--theme", default=None,
                    help="配色：预设名（slate/ink/teal/wine/amber）或逐项覆盖，"
                         "如 accent=#b45309,deep=#0f172a")
    ap.add_argument("--side", default=None,
                    help="哪些章节进侧栏（仅 modern 模板用），逗号分隔。"
                         "默认：技能,证书,荣誉,奖项,其他,自我,兴趣,语言")
    ap.add_argument("--pdf", action="store_true", help="同时导出 PDF")
    ap.add_argument("--fit", action="store_true",
                    help="自动压到一页：内容超出 A4 时整体缩小（下限 62%%）")
    ap.add_argument("--list", action="store_true", help="列出可用模板")
    args = ap.parse_args(argv)

    if args.list:
        print("可用模板：")
        for t in templates.list_templates():
            print("  %-14s %-10s %s" % (t["id"], t["name"], t.get("desc", "")))
        print("\n可用配色（--theme NAME）：")
        for k, v in templates.THEME_PRESETS.items():
            desc = "默认（灰蓝）" if not v else "  ".join("%s=%s" % (a, b) for a, b in v.items())
            print("  %-10s %s" % (k, desc))
        print("\n也可以逐项覆盖：--theme accent=#b45309")
        print("可覆盖的 key：%s" % ", ".join(templates.DEFAULT_THEME))
        return 0

    if not args.input:
        ap.print_help()
        return 1

    src = os.path.abspath(args.input)
    if not os.path.exists(src):
        print("找不到文件：%s" % src)
        return 1

    stem = args.stem or os.path.splitext(os.path.basename(src))[0]
    outdir = os.path.abspath(args.out) if args.out else os.path.dirname(src)
    os.makedirs(outdir, exist_ok=True)

    print("解析  %s" % os.path.basename(src))
    try:
        resume, raw = load_resume(src)
    except Exception as e:
        print("  × %s" % e)
        return 2

    n_sec = len(resume.get("sections") or [])
    n_blk = sum(len(s.get("blocks") or []) for s in (resume.get("sections") or []))
    n_bul = sum(len(b.get("bullets") or []) for s in (resume.get("sections") or []) for b in (s.get("blocks") or []))
    print("  → 章节 %d · 条目 %d · 要点 %d" % (n_sec, n_blk, n_bul))

    names = [t["id"] for t in templates.list_templates()] if args.all else [args.template]
    bad = [n for n in names if n not in [t["id"] for t in templates.list_templates()]]
    if bad:
        print("未知模板：%s" % ", ".join(bad))
        return 1

    browser = find_browser() if args.pdf else None
    if args.pdf and not browser:
        print("  ! 没找到 Chrome / Edge，跳过 PDF 导出")

    theme = templates.resolve_theme(args.theme)
    side_keys = [x.strip() for x in args.side.split(",") if x.strip()] if args.side else None
    if theme:
        print("  配色覆盖：%s" % ", ".join("%s=%s" % (k, v) for k, v in theme.items()))
    if side_keys:
        print("  侧栏章节：%s" % ", ".join(side_keys))

    # 文件名带上主题标签，避免不同配色互相覆盖
    tag = ""
    if args.theme:
        tag = "-" + (args.theme if args.theme in templates.THEME_PRESETS else "custom")

    def render_one(name, sc=None):
        return templates.render(resume, name,
                                title="%s 的简历" % (resume.get("name") or "").strip(),
                                theme=theme, side_keys=side_keys, scale=sc)

    def fit_one(name, html):
        """超出 A4 就按比例缩。量高度要跑一次无头浏览器 —— 只有它知道换行后真实占多高。"""
        if not args.fit or not args.pdf:
            return html, None
        try:
            sys.path.insert(0, os.path.join(ROOT, "server"))
            import docio
        except Exception:
            return html, None
        h = docio.measure_html_height(html)
        if not h or h <= docio.A4_PX_H:
            return html, None
        sc = max(0.62, docio.A4_PX_H / float(h))
        if sc >= 0.999:
            return html, None
        return render_one(name, sc), round(sc, 3)

    print("\n渲染  共 %d 套模板%s" % (len(names), "（自动压页已开）" if args.fit else ""))
    for name in names:
        hp = os.path.join(outdir, "%s-%s%s.html" % (stem, name, tag))
        html, sc = fit_one(name, render_one(name))
        io.open(hp, "w", encoding="utf-8", newline="\n").write(html)
        if sc:
            print("    ↳ %s 内容超出 A4，已整体缩到 %d%% 压进一页" % (name, round(sc * 100)))
        line = "  ✓ %-28s %s" % (os.path.basename(hp), "%d 字节" % os.path.getsize(hp))
        if args.pdf and browser:
            pp = os.path.join(outdir, "%s-%s%s.pdf" % (stem, name, tag))
            ok, info = html_to_pdf(hp, pp, browser)
            if ok:
                pages = ("%d 页" % info) if info else "已生成"
                flag = "  ⚠ 超过 1 页" if (info and info > 1) else ""
                line += "  + PDF %s%s" % (pages, flag)
            else:
                line += "  ! PDF 失败：%s" % info
        print(line)

    print("\n输出目录：%s" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
