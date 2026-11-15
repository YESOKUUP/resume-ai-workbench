# -*- coding: utf-8 -*-
"""
文档 IO —— PDF 文本/照片提取（可选依赖 pypdf）+ 调本机浏览器导出 PDF。

设计原则：
  · PDF 库优先用**项目自带的 libs/**：这样用户机器上随便哪个 Python 都能读 PDF，
    不必去动系统环境（也不用 pip install）。没有 libs 时才回退到系统已装的库。
  · 都没有的话不报死：返回明确指引（走「粘贴文本」入口），不让整个产品挂掉。
  · 导出 PDF 复用已验证的链路：本机 Edge/Chrome 无头 --print-to-pdf。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

# ---------------------------------------------------------------- 可选库挂载
# 项目根的 libs/ 里带着 pypdf（纯 Python，约 1.6MB）。挂上它，
# 无论 start.bat 选中哪个 Python，PDF 功能都可用。
_LIBS_DIR = os.environ.get("RW_LIBS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs")
if os.path.isdir(_LIBS_DIR) and _LIBS_DIR not in sys.path:
    sys.path.insert(0, _LIBS_DIR)


def load_templates():
    """挂载内置模板库（tools/templates.py）并返回模块对象。

    模板库跟服务本体一样是纯标准库实现，零第三方依赖。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tools = os.path.join(root, "tools")
    if not os.path.isdir(tools):
        raise RuntimeError("找不到 tools/ 目录：%s" % tools)
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import templates  # noqa: E402
    return templates


def libs_dir():
    """当前挂载的可选库目录（若存在）。"""
    return _LIBS_DIR if os.path.isdir(_LIBS_DIR) else ""


def pypdf_source():
    """pypdf 是从哪来的：项目自带 / 系统已装。"""
    try:
        import pypdf
    except Exception:
        return ""
    mod_path = getattr(pypdf, "__file__", "") or ""
    try:
        if libs_dir() and os.path.abspath(mod_path).startswith(os.path.abspath(libs_dir())):
            return "项目自带"
    except Exception:
        pass
    return "系统已装"


# ---------------------------------------------------------------- 依赖探测

def has_pdf_lib():
    try:
        import pypdf  # noqa: F401
        return True, "pypdf"
    except Exception:
        pass
    try:
        import PyPDF2  # noqa: F401
        return True, "PyPDF2"
    except Exception:
        pass
    try:
        import fitz  # noqa: F401
        return True, "pymupdf"
    except Exception:
        return False, ""


_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser():
    env = os.environ.get("CHROME_PATH") or os.environ.get("EDGE_PATH")
    if env and os.path.exists(env):
        return env
    for p in _BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


# ---------------------------------------------------------------- PDF 提取

def extract_pdf(path):
    """提取 PDF 的文本与内嵌图片。

    返回 {ok, pages, text, chars_per_page, is_scanned, images:[{name,mime,data_b64}], warning, error}
    """
    ok, lib = has_pdf_lib()
    if not ok:
        return {
            "ok": False, "error": "not_installed",
            "message": "找不到 PDF 解析组件（pypdf）",
            "hint": "项目的 libs/ 目录里应该自带 pypdf —— 如果它被误删了，"
                    "可以重新放回去（详见 README）；也可以先用「粘贴简历文本」入口。",
        }

    try:
        if lib == "pymupdf":
            return _extract_pymupdf(path)
        return _extract_pypdf(path, lib)
    except Exception as e:
        return {"ok": False, "error": "parse_failed", "message": "PDF 解析失败：%s" % e,
                "hint": "文件可能加密或损坏。试试解除密码，或用「粘贴简历文本」。"}


def _extract_pypdf(path, lib):
    import base64
    import io

    mod = __import__(lib)
    reader = mod.PdfReader(path)
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            return {"ok": False, "error": "encrypted", "message": "这个 PDF 有密码保护",
                    "hint": "请先用阅读器解除密码，或改用「粘贴简历文本」。"}

    texts, images = [], []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        texts.append(t)
        # 内嵌图片（用于照片）
        try:
            for im in (getattr(page, "images", None) or []):
                data = im.data
                if len(data) < 3000:          # 跳过图标/线条
                    continue
                name = getattr(im, "name", "image-%d.png" % len(images))
                ext = os.path.splitext(name)[1].lstrip(".").lower() or "png"
                if ext == "jpg":
                    ext = "jpeg"
                images.append({
                    "name": name,
                    "mime": "image/" + ext,
                    "size": len(data),
                    "page": i + 1,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                })
        except Exception:
            pass

    text = "\n".join(texts)
    pages = len(reader.pages)
    stripped = text.strip()
    cpp = len(stripped) / max(1, pages)
    is_scanned = cpp < 40          # 每页不足 40 字 → 基本可判定为扫描件
    out = {
        "ok": True, "lib": lib, "pages": pages, "text": stripped,
        "chars_per_page": round(cpp, 1), "is_scanned": is_scanned,
        "images": images[:6], "image_count": len(images),
    }
    if is_scanned:
        out["warning"] = "这个 PDF 几乎没有文字层（扫描件 / 图片型），提取到的文字可能不完整"
        out["hint"] = "建议改用「粘贴简历文本」，或选择尝试 OCR。"
    return out


def _extract_pymupdf(path):
    import base64
    import fitz
    doc = fitz.open(path)
    texts, images = [], []
    for i, page in enumerate(doc):
        texts.append(page.get_text() or "")
        for j, info in enumerate(page.get_images(full=True)):
            try:
                xref = info[0]
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                if pix.width * pix.height < 4000:
                    continue
                data = pix.tobytes("png")
                images.append({"name": "p%d-%d.png" % (i + 1, j + 1), "mime": "image/png",
                               "size": len(data), "page": i + 1,
                               "data_b64": base64.b64encode(data).decode("ascii")})
            except Exception:
                pass
    text = "\n".join(texts)
    pages = len(doc)
    stripped = text.strip()
    cpp = len(stripped) / max(1, pages)
    is_scanned = cpp < 40
    out = {"ok": True, "lib": "pymupdf", "pages": pages, "text": stripped,
           "chars_per_page": round(cpp, 1), "is_scanned": is_scanned,
           "images": images[:6], "image_count": len(images)}
    if is_scanned:
        out["warning"] = "这个 PDF 几乎没有文字层（扫描件 / 图片型）"
        out["hint"] = "建议改用「粘贴简历文本」。"
    return out


# ---------------------------------------------------------------- 导出 PDF

def export_pdf(html, out_path, timeout=60):
    """把一段自包含 HTML 渲染成 PDF（本机浏览器无头模式）。"""
    browser = find_browser()
    if not browser:
        return {"ok": False, "error": "no_browser",
                "message": "没找到可用的浏览器（Edge / Chrome）",
                "hint": "安装 Microsoft Edge 或 Google Chrome 后重试。"}

    tmpdir = tempfile.mkdtemp(prefix="rw-pdf-")
    html_path = os.path.join(tmpdir, "resume.html")
    with open(html_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)

    # 用 file:/// 形式；Windows 路径的反斜杠要转成 /
    file_url = "file:///" + html_path.replace("\\", "/").lstrip("/")
    tmp_pdf = os.path.join(tmpdir, "out.pdf")
    cmd = [
        browser,
        "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-pdf-header-footer",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=3000",
        "--print-to-pdf-no-header",
        "--print-to-pdf=" + tmp_pdf,
        file_url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "message": "导出超时（浏览器无响应）",
                "hint": "重试一次；若持续失败，改用「导出 HTML」再手动打印。"}

    if not os.path.exists(tmp_pdf) or os.path.getsize(tmp_pdf) == 0:
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        return {"ok": False, "error": "render_failed",
                "message": "浏览器没能生成 PDF" + (("：" + err) if err else ""),
                "hint": "检查浏览器是否可正常启动；也可先用「预览」确认排版。"}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    shutil.copyfile(tmp_pdf, out_path)
    size = os.path.getsize(out_path)
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
    return {"ok": True, "path": out_path, "size": size, "browser": os.path.basename(browser)}


def pdf_page_count(path):
    ok, lib = has_pdf_lib()
    if not ok:
        return None
    try:
        if lib == "pymupdf":
            import fitz
            return len(fitz.open(path))
        mod = __import__(lib)
        return len(mod.PdfReader(path).pages)
    except Exception:
        return None


# ---------------------------------------------------------------- 结构化解析
# 几何驱动的结构解析（tools/pdfstruct.py）。需要 PyMuPDF。
# 为什么要它：pypdf 只给「文字流」，会折行拆散、把图标字符混进正文、
# 分不清标题和正文。pdfstruct 用坐标+字号+颜色重建结构，**不调 AI**。

_PDFSTRUCT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")


# ---------------------------------------------------------------- 版面测量
# A4 在 96dpi 下的像素尺寸 —— Chrome 排版和打印都按这个基准。
A4_PX_H = 1123
A4_PX_W = 794

#: 注入页面用来回传高度的探针
_FIT_PROBE = (
    "<script>window.addEventListener('load',function(){"
    "var p=document.querySelector('.page');if(!p)return;"
    "document.title='RW_FIT='+Math.round(p.getBoundingClientRect().height);"
    "});</script>"
)


def measure_html_height(html, browser=None):
    """量出这段 HTML 渲染后的实际高度（px）。量不出来返回 None。

    为什么非要跑浏览器：只有它知道换行之后真实占多高 —— 同样的字数，
    中英文混排、标点避头尾，算出来的行数都不一样。
    """
    browser = browser or find_browser()
    if not browser or not html:
        return None
    tmp = tempfile.mkdtemp(prefix="rw-fit-")
    try:
        f = os.path.join(tmp, "probe.html")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(html.replace("</body>", _FIT_PROBE + "</body>"))
        # 带空格 / 中文的本地路径必须 URL 编码
        url = "file:///" + urllib.parse.quote(f.replace("\\", "/"))
        r = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--dump-dom", "--virtual-time-budget=2500", url],
            capture_output=True, timeout=90)
        m = re.search(r"RW_FIT=(\d+)", (r.stdout or b"").decode("utf-8", "replace"))
        return int(m.group(1)) if m else None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def structured_available():
    """结构化解析是否可用（需 PyMuPDF）。返回 (可用, 说明)。"""
    try:
        if _PDFSTRUCT_DIR not in sys.path:
            sys.path.insert(0, _PDFSTRUCT_DIR)
        import pdfstruct          # noqa: F401
        return True, "pymupdf"
    except Exception as e:
        return False, str(e)


def load_pdfstruct():
    """挂载并返回 tools/pdfstruct.py。

    AI 判构用的辅助函数（ai_worksheet / validate_line_map / build_from_line_map）
    都在里面 —— 它们需要拿"几何提取出来的每一行"，所以跟几何解析同源。
    """
    if _PDFSTRUCT_DIR not in sys.path:
        sys.path.insert(0, _PDFSTRUCT_DIR)
    import pdfstruct  # noqa: E402
    return pdfstruct


def extract_structured(path):
    """返回 {ok, resume, metrics, lines, image_count} / {ok:False, error, message, hint}

    resume 的 shape 与前端 state.resume 一致（sections/blocks/bullets + 稳定 bid），
    可直接交给「转换核对页」。失败时给出可执行提示，不阻断（前端会回退到旧路径）。
    """
    try:
        if _PDFSTRUCT_DIR not in sys.path:
            sys.path.insert(0, _PDFSTRUCT_DIR)
        import pdfstruct
    except Exception:
        return {"ok": False, "error": "no_pymupdf",
                "message": "没装几何解析组件 PyMuPDF",
                "hint": "装一次即可：python -m pip install --target D:\\workbuddy\\temp\\pylibs pymupdf"
                        "（装完重启服务）。不装也能用，但会退回旧解析 —— 折行会被拆散、标题识别不准。"}
    try:
        res = pdfstruct.parse(path)
        # parse() 会自己判断"结构能不能用"：0 章节 / 0 条目一律标 ok=False，
        # 绝不能把空结构当成功传给前端（真实事故：用户看到一片空白）。
        if not res.get("ok"):
            return {
                "ok": False,
                "error": res.get("error") or "no_structure",
                "message": res.get("message") or "没能识别出简历结构",
                "hint": res.get("hint") or "",
                "quality": res.get("quality"),
            }
        return {"ok": True, "resume": res["resume"], "metrics": res["metrics"],
                "lines": res["lines"], "quality": res.get("quality"),
                "image_count": len(res.get("images") or [])}
    except Exception as e:
        return {"ok": False, "error": "struct_failed", "message": "结构解析失败：%s" % e}


def env_report():
    ok, lib = has_pdf_lib()
    sok, _ = structured_available()
    b = find_browser()
    return {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "pdf_lib": lib if ok else None,
        "pdf_lib_ok": ok,
        "pdf_lib_source": pypdf_source() if ok else "",
        "structured_ok": sok,
        "libs_dir": libs_dir(),
        "browser": b,
        "browser_name": os.path.basename(b) if b else None,
    }
