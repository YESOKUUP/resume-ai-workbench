# -*- coding: utf-8 -*-
"""PDF → HTML 工具（阶段 1：导入）

产出三样东西，都在输出目录里：

  <名字>.json          结构化的简历（sections → blocks → bullets，带稳定 bid）
                       这份就是「可编辑 HTML 简历」的数据源，工作台可直接消费
  <名字>.html          单文件网页：左「PDF 原页渲染」右「解析结果」，
                       带「版面核对」视图 —— 每个文字块按其被判定的角色上色，
                       一眼看出哪一行被认成了章节标题 / 条目 / 要点 / 图标
  <名字>.txt           纯文本对照（便于 diff / 快速扫读）

用法：
  python tools/pdf2html.py <pdf> [-o 输出目录] [--dpi 110] [--json-only]

设计原则：**只解析，不改写**。不调用任何 AI；结构完全由几何推出。
"""

from __future__ import annotations

import argparse
import base64
import html as _html
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdfstruct  # noqa: E402

try:
    import pymupdf
except Exception:                                     # pragma: no cover
    try:
        import fitz as pymupdf
    except Exception as e:
        raise RuntimeError(
            "需要 PyMuPDF：python -m pip install --target D:\\workbuddy\\temp\\pylibs pymupdf"
        ) from e


# 角色 → 配色（版面核对视图用）
ROLE_STYLE = {
    "section": ("#6366f1", "rgba(99,102,241,.16)", "章节标题"),
    "entry":   ("#0f766e", "rgba(15,118,110,.14)", "条目标题"),
    "role":    ("#b45309", "rgba(180,83,9,.14)", "角色"),
    "date":    ("#1d4ed8", "rgba(29,78,216,.12)", "日期"),
    "bullet":  ("#374151", "rgba(55,65,81,.07)", "要点"),
    "note":    ("#6b7280", "rgba(107,114,128,.10)", "简介"),
    "icon":    ("#be123c", "rgba(190,18,60,.18)", "图标字符"),
}


def page_png_b64(doc, pno, dpi=110):
    zoom = dpi / 72.0
    pix = doc[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return base64.b64encode(pix.tobytes("png")).decode("ascii"), pix.width, pix.height


def build_overlay_boxes(res, doc, dpi):
    """按角色给每个文字行画定位框（版面核对视图）。"""
    zoom = dpi / 72.0
    out = []
    for l in res["lines"]:
        role = l.get("role") or l.get("kind") or "bullet"
        color, bg, _ = ROLE_STYLE.get(role, ROLE_STYLE["bullet"])
        out.append({
            "page": l["page"],
            "x": l["x0"] * zoom, "y": l["y0"] * zoom,
            "w": max(2.0, (l["x1"] - l["x0"]) * zoom),
            "h": max(2.0, (l["y1"] - l["y0"]) * zoom),
            "color": color, "bg": bg, "role": role,
            "text": l["text"],
        })
    return out


def esc(s):
    return _html.escape(s or "")


def render_resume_html(res):
    """语义化渲染解析结果 —— 这就是「可编辑 HTML 简历」的骨架。"""
    r = res["resume"]
    p = []
    p.append('<div class="paper-head">')
    p.append('  <div class="ph-main">')
    p.append('    <div class="rname">%s</div>' % esc(r["name"]))
    contact = "　|　".join(x for x in [r["phone"], r["email"]] if x)
    if contact:
        p.append('    <div class="rcontact">%s</div>' % esc(contact))
    if r["intent"]:
        p.append('    <div class="rintent">求职意向：<b>%s</b></div>' % esc(r["intent"]))
    p.append('  </div>')
    p.append('</div>')

    for extra in r.get("extra", [])[:1]:
        p.append('<div class="rextra">%s</div>' % esc(extra))

    for si, sec in enumerate(r["sections"]):
        p.append('<section class="sec">')
        p.append('  <h2 class="sec-t"><span class="sec-idx">%d</span>%s</h2>' % (si + 1, esc(sec["title"])))
        for b in sec["blocks"]:
            p.append('  <div class="blk" data-bid="%s">' % esc(b["id"]))
            head = []
            if b.get("icon"):
                head.append('<span class="icon" title="图标字符（原 PDF 里的徽章）">%s</span>' % esc(b["icon"]))
            if b.get("heading"):
                head.append('<span class="bh">%s</span>' % esc(b["heading"]))
            if b.get("role"):
                head.append('<span class="brole">%s</span>' % esc(b["role"]))
            if b.get("date"):
                head.append('<span class="bdate">%s</span>' % esc(b["date"]))
            if head:
                p.append('    <div class="blk-h">%s</div>' % "".join(head))
            if b.get("intro"):
                p.append('    <div class="bintro">%s</div>' % esc(b["intro"]))
            if b["bullets"]:
                p.append('    <ul class="bls">')
                for x in b["bullets"]:
                    bp = ('<b class="bp">%s</b>' % esc(x["bold_prefix"])) if x.get("bold_prefix") else ""
                    body = esc(x["text"])
                    if x.get("bold_prefix") and body.startswith(x["bold_prefix"]):
                        body = bp + body[len(x["bold_prefix"]):]
                    p.append('      <li data-bid="%s"><span class="bid">%s</span>%s</li>'
                             % (esc(x["bid"]), esc(x["bid"]), body))
                p.append('    </ul>')
            p.append('  </div>')
        p.append('</section>')
    return "\n".join(p)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e5e7eb;--line2:#d1d5db;--t1:#111827;--t2:#4b5563;--t3:#9ca3af;
      --brand:#6366f1;--brand-soft:#eef2ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--t1);
     font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
     font-size:13px;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.94);backdrop-filter:blur(10px);
       border-bottom:1px solid var(--line);padding:12px 22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:14px;margin:0;font-weight:700}
header .file{font-size:12px;color:var(--t2);font-family:ui-monospace,Consolas,monospace}
header .spacer{flex:1}
.stats{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--t2)}
.stats b{color:var(--t1);font-weight:650}
.seg{display:inline-flex;background:#fff;border:1px solid var(--line2);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;padding:5px 12px;font-size:12px;cursor:pointer;color:var(--t2)}
.seg button.on{background:var(--brand);color:#fff}
main{padding:20px 22px 60px;max-width:1500px;margin:0 auto}
.view{display:none}.view.on{display:block}

/* ---- 解析结果 ---- */
.paper{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:34px 40px;
       max-width:794px;margin:0 auto;box-shadow:0 1px 3px rgba(16,24,40,.05)}
.paper-head{display:flex;gap:18px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:20px}
.ph-main{flex:1}
.rname{font-size:24px;font-weight:700;letter-spacing:.5px}
.rcontact{font-size:12px;color:var(--t2);margin-top:6px}
.rintent{font-size:12px;color:var(--t2);margin-top:4px}
.rintent b{color:#4338ca}
.rextra{font-size:11.5px;color:var(--t2);margin:-8px 0 12px}
.sec{margin-bottom:22px}
.sec-t{font-size:13px;color:var(--brand);font-weight:700;margin:0 0 11px;padding-bottom:6px;
       border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
.sec-idx{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:5px;
         background:var(--brand);color:#fff;font-size:10.5px;font-weight:700;flex:0 0 auto}
.blk{margin-bottom:15px;padding-left:10px;border-left:2px solid #f1f2f6}
.blk:hover{border-left-color:var(--brand-soft)}
.blk-h{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:5px}
.bh{font-size:13.5px;font-weight:650}
.brole{font-size:11.5px;color:#b45309}
.bdate{font-size:11.5px;color:var(--t3);margin-left:auto}
.icon{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 4px;
      border-radius:5px;background:#1d4ed8;color:#fff;font-size:11px;font-weight:700;
      transform:translateY(1px)}
.bintro{font-size:12.5px;color:var(--t2);line-height:1.8;margin:2px 0 6px}
.bls{margin:6px 0 0;padding-left:16px}
.bls li{font-size:12.5px;line-height:1.85;margin-bottom:3px}
.bp{color:#111827}
.bid{font-family:ui-monospace,Consolas,monospace;font-size:9.5px;color:#c3c8d2;margin-right:6px;user-select:none}

/* ---- 原文对照 ---- */
.raw{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:26px 30px;
     max-width:900px;margin:0 auto;font-size:12.5px;line-height:1.9;white-space:pre-wrap;
     font-family:ui-monospace,Consolas,"Microsoft YaHei",monospace;color:var(--t2)}

/* ---- 版面核对 ---- */
.legend{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 14px;font-size:11.5px;color:var(--t2)}
.legend span{display:inline-flex;align-items:center;gap:5px}
.sw{width:11px;height:11px;border-radius:3px;border:1px solid rgba(0,0,0,.12)}
.pages{display:flex;flex-direction:column;align-items:center;gap:18px}
.pagewrap{position:relative;background:#fff;border:1px solid var(--line);border-radius:8px;
          box-shadow:0 1px 3px rgba(16,24,40,.06);overflow:hidden;max-width:100%}
.pagewrap img{display:block;width:100%;height:auto}
.ovl{position:absolute;border-radius:2px;outline:1px solid transparent;transition:outline-color .12s}
.pagewrap:hover .ovl{outline-color:currentColor}
.ovl:hover{outline-width:1.5px;z-index:5}
.pglabel{position:absolute;top:8px;right:10px;background:rgba(17,24,39,.72);color:#fff;font-size:10.5px;
         padding:2px 7px;border-radius:5px}
footer{padding:16px 22px 40px;text-align:center;color:var(--t3);font-size:11.5px}
"""

JS = """
function seg(btn){
  document.querySelectorAll('.seg button').forEach(function(b){ b.classList.toggle('on', b === btn); });
  var v = btn.dataset.view;
  document.querySelectorAll('.view').forEach(function(x){ x.classList.toggle('on', x.id === 'v-' + v); });
  if (history.replaceState) history.replaceState(null, '', '#' + v);
  fitOverlays();
}
/* 支持 #parsed / #check / #raw 直达（便于分享与截图） */
function initView(){
  var h = (location.hash || '').replace('#', '');
  var btn = document.querySelector('.seg button[data-view="' + h + '"]');
  if (btn) seg(btn);
}
document.querySelectorAll('.seg button').forEach(function(b){
  b.addEventListener('click', function(){ seg(b); });
});
/* 版面核对：把叠加框按图片实际宽度缩放 */
function fitOverlays(){
  document.querySelectorAll('.pagewrap').forEach(function(w){
    var img = w.querySelector('img'); if (!img) return;
    var native = parseFloat(w.dataset.nw) || img.naturalWidth || 1;
    var scale = img.clientWidth / native;
    w.querySelectorAll('.ovl').forEach(function(o){
      o.style.left   = (parseFloat(o.dataset.x) * scale) + 'px';
      o.style.top    = (parseFloat(o.dataset.y) * scale) + 'px';
      o.style.width  = (parseFloat(o.dataset.w) * scale) + 'px';
      o.style.height = (parseFloat(o.dataset.h) * scale) + 'px';
    });
  });
}
window.addEventListener('resize', fitOverlays);
window.addEventListener('load', function(){ initView(); fitOverlays(); });
"""


def build_html(res, pdf_path, dpi=110):
    doc = pymupdf.open(pdf_path)
    try:
        pages = []
        for pno in range(len(doc)):
            b64, w, h = page_png_b64(doc, pno, dpi)
            pages.append({"b64": b64, "w": w, "h": h, "no": pno + 1})
    finally:
        doc.close()

    boxes = build_overlay_boxes(res, None, dpi)
    r = res["resume"]
    m = res["metrics"]

    ovl_by_page = {}
    for b in boxes:
        ovl_by_page.setdefault(b["page"], []).append(b)

    page_html = []
    for pg in pages:
        idx = pg["no"] - 1
        ovls = "".join(
            '<div class="ovl" data-x="%.1f" data-y="%.1f" data-w="%.1f" data-h="%.1f" '
            'style="background:%s;color:%s" title="%s | %s"></div>'
            % (o["x"], o["y"], o["w"], o["h"], o["bg"], o["color"], o["role"], esc(o["text"][:60]))
            for o in ovl_by_page.get(idx, [])
        )
        page_html.append(
            '<div class="pagewrap" data-nw="%d"><span class="pglabel">第 %d 页</span>'
            '<img alt="第 %d 页" src="data:image/png;base64,%s">%s</div>'
            % (pg["w"], pg["no"], pg["no"], pg["b64"], ovls)
        )

    legend = "".join(
        '<span><i class="sw" style="background:%s"></i>%s</span>' % (v[0], v[2])
        for v in ROLE_STYLE.values()
    )

    bins = sum(len(s["blocks"]) for s in r["sections"])
    blts = sum(len(b["bullets"]) for s in r["sections"] for b in s["blocks"])

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF 解析核对 · %(fname)s</title>
<style>%(css)s</style></head>
<body>
<header>
  <h1>PDF → HTML 解析核对</h1>
  <span class="file">%(fname)s</span>
  <span class="spacer"></span>
  <div class="stats">
    <span><b>%(pages)d</b> 页</span>
    <span>正文 <b>%(body).1f</b>pt</span>
    <span>行距 <b>%(gap).1f</b></span>
    <span>章节 <b>%(secs)d</b></span>
    <span>条目 <b>%(bins)d</b></span>
    <span>要点 <b>%(blts)d</b></span>
  </div>
  <div class="seg">
    <button data-view="parsed" class="on">解析结果</button>
    <button data-view="check">版面核对</button>
    <button data-view="raw">纯文本</button>
  </div>
</header>
<main>
  <div class="view on" id="v-parsed"><div class="paper">%(resume)s</div></div>
  <div class="view" id="v-check">
    <div class="legend">%(legend)s</div>
    <div class="pages">%(pages_html)s</div>
  </div>
  <div class="view" id="v-raw"><div class="raw">%(raw)s</div></div>
</main>
<footer>由 tools/pdf2html.py 生成 · 结构完全由文字几何推出，未调用 AI · 中间产物，不参与投递</footer>
<script>%(js)s</script>
</body></html>""" % {
        "fname": esc(res["meta"]["path"]),
        "css": CSS, "js": JS,
        "pages": res["meta"]["pages"],
        "body": m["body_size"], "gap": m["line_gap"],
        "secs": len(r["sections"]), "bins": bins, "blts": blts,
        "resume": render_resume_html(res),
        "legend": legend,
        "pages_html": "".join(page_html),
        "raw": esc(pdfstruct.to_plain_text(res)),
    }


def main():
    ap = argparse.ArgumentParser(description="PDF → HTML（结构解析，不调 AI）")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认与 PDF 同级的 pdf2html-out/）")
    ap.add_argument("--dpi", type=int, default=110, help="版面核对视图的渲染精度")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--stem", default=None, help="输出文件名（不含扩展名）")
    a = ap.parse_args()

    pdf = os.path.abspath(a.pdf)
    if not os.path.exists(pdf):
        print("找不到文件：%s" % pdf)
        return 1
    outdir = a.out or os.path.join(os.path.dirname(pdf), "pdf2html-out")
    os.makedirs(outdir, exist_ok=True)
    stem = a.stem or os.path.splitext(os.path.basename(pdf))[0]

    res = pdfstruct.parse(pdf)
    jpath = os.path.join(outdir, stem + ".json")
    with io.open(jpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("JSON  %s" % jpath)

    if not a.json_only:
        hpath = os.path.join(outdir, stem + ".html")
        with io.open(hpath, "w", encoding="utf-8", newline="\n") as f:
            f.write(build_html(res, pdf, a.dpi))
        print("HTML  %s" % hpath)
        tpath = os.path.join(outdir, stem + ".txt")
        with io.open(tpath, "w", encoding="utf-8", newline="\n") as f:
            f.write(pdfstruct.to_plain_text(res))
        print("TXT   %s" % tpath)

    print("-" * 58)
    print(pdfstruct.summary(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
