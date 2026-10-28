"""把结构化解答排版成 A4 PDF，再转成 PNG。"""

from __future__ import annotations

import logging
import re


log = logging.getLogger("qq-deepseek-bot.render")

_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

class RenderError(RuntimeError):
    """LaTeX 编译或 PDF 转图片失败。"""


def escape_text(text: str) -> str:
    text = "".join(_SPECIAL.get(ch, ch) for ch in (text or ""))
    text = re.sub(r"\n\s*\n", r"\\par ", text)
    return text.replace("\n", " ").strip()


_MATH_COMMANDS = set("""
frac dfrac tfrac cfrac sqrt sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh
exp log ln lg lim limsup liminf sup inf max min sum prod coprod int iint iiint oint
oplus otimes bigoplus bigcup bigcap cup cap partial nabla infty cdot times div pm mp ast star
circ bullet cdots ldots vdots ddots dots le leq ge geq neq ne approx sim simeq equiv
cong propto ll gg prec succ in notin ni mid subset subseteq supset supseteq emptyset
varnothing forall exists neg land lor wedge vee implies iff to rightarrow leftarrow
Rightarrow Leftarrow leftrightarrow Leftrightarrow Longrightarrow Longleftarrow Longleftrightarrow longleftrightarrow mapsto longrightarrow longleftarrow
uparrow downarrow lvert rvert lVert rVert vert Vert langle rangle lbrace rbrace
lfloor rfloor lceil rceil left right middle big Big bigg Bigg bigl bigr Bigl Bigr
biggl biggr Biggl Biggr alpha beta gamma delta epsilon varepsilon zeta eta theta
vartheta iota kappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon
phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbb mathcal mathfrak mathrm mathbf mathit mathsf mathtt mathnormal boldsymbol bm
operatorname text textrm textbf textit overline underline overbrace underbrace
widehat widetilde hat tilde bar vec dot ddot dddot acute grave breve check boxed
phantom hphantom vphantom quad qquad enspace thinspace medspace thickspace
displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits underset
overset stackrel substack binom dbinom tbinom begin end hline cr det dim ker deg
gcd Pr mod bmod pmod arg Re Im ell hbar angle triangle perpendicular perp parallel
therefore because not smallsetminus setminus coloneqq colon backslash top bot prime
lnot restriction vdash models
""".split())
_MATH_ENVIRONMENTS = {"aligned", "alignedat", "gathered", "split", "cases", "matrix",
                      "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "array"}


def sanitize_math(math: str) -> str:
    math = (math or "").strip()
    math = re.sub(r"^\s*(\$+|\\\[|\\\()\s*", "", math)
    math = re.sub(r"\s*(\$+|\\\]|\\\))\s*$", "", math)
    if len(math) > 12000 or "^^" in math or re.search(r"(?<!\\)\$", math):
        raise RenderError("公式长度或特殊语法不受支持")
    for command in re.findall(r"\\([A-Za-z@]+|[^A-Za-z@])", math):
        if (command[0].isalpha() or "@" in command) and command not in _MATH_COMMANDS:
            raise RenderError(f"公式含不支持的命令：{command}")
    for environment in re.findall(r"\\(?:begin|end)\s*\{([^{}]*)\}", math):
        if environment not in _MATH_ENVIRONMENTS:
            raise RenderError("公式含不支持的环境")
    math = re.sub(r"(?<!\\)%", r"\\%", math)
    math = re.sub(r"(?<!\\)#", r"\\#", math)
    return math


_MATH_TOKEN = re.compile(
    r"(\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$[^$\n]+?\$)",
    re.S,
)
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_MATH_CMD_RE = re.compile(r"\\[a-zA-Z]+")


def _looks_like_math(text: str) -> bool:
    return bool(_MATH_CMD_RE.search(text)) and not _CHINESE_RE.search(text)


def _find_paren_end(text: str, start: int) -> int:
    depth = 1
    i = start + 2
    while i < len(text):
        if text.startswith(r"\(", i):
            depth += 1
            i += 2
        elif text.startswith(r"\)", i):
            depth -= 1
            if depth == 0:
                return i
            i += 2
        else:
            i += 1
    return -1


_CHINESE_PUNCT = set("，。；：！？、（）【】《》“”‘’")
_MATH_LEFT = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "+-*/=<>|!'.,;:()[]^_&%#~ "
)
_MATH_RIGHT = _MATH_LEFT


def _is_chinese(ch: str) -> bool:
    return bool(_CHINESE_RE.match(ch))


def _skip_group(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    i = start
    while i < len(text):
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _scan_bare_end(text: str, start: int) -> int:
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                i += 1
                continue
            nxt = text[i + 1]
            if nxt.isalpha():
                i += 2
                while i < len(text) and text[i].isalpha():
                    i += 1
                continue
            if nxt in ",;:! ":
                i += 2
                continue
            if nxt == "\\":
                i += 2
                continue
            i += 2
            continue
        if ch == "{":
            i = _skip_group(text, i, "{", "}")
            continue
        if ch == "[":
            i = _skip_group(text, i, "[", "]")
            continue
        if ch == "\n" or ch == "$" or _is_chinese(ch) or ch in _CHINESE_PUNCT:
            break
        if ch in _MATH_RIGHT:
            i += 1
            continue
        break
    return i


def _wrap_bare_latex(text: str) -> str:
    """模型偶尔不写 $...$，直接把 \\frac、\\int 混在中文里；这里补上包裹。"""
    if "\\" not in (text or ""):
        return text or ""

    out: list[str] = []
    last = 0
    i = 0
    length = len(text)
    while i < length:
        end = -1
        if text.startswith("$$", i):
            pos = text.find("$$", i + 2)
            if pos >= 0:
                end = pos + 2
        elif text.startswith(r"\[", i):
            pos = text.find(r"\]", i + 2)
            if pos >= 0:
                end = pos + 2
        elif text.startswith(r"\(", i):
            pos = _find_paren_end(text, i)
            if pos >= 0:
                end = pos + 2
        elif text[i] == "$":
            pos = text.find("$", i + 1)
            if pos >= 0:
                end = pos + 1

        if end >= 0:
            out.append(text[last:end])
            last = end
            i = end
            continue

        if text[i] == "\\" and i + 1 < length and text[i + 1].isalpha():
            start = i
            while start > last and text[start - 1] in _MATH_LEFT:
                start -= 1
            while start < i and text[start].isspace():
                start += 1

            finish = _scan_bare_end(text, i)
            while finish > start and text[finish - 1].isspace():
                finish -= 1

            segment = text[start:finish]
            if "\\" in segment:
                out.append(text[last:start])
                out.append("$" + segment + "$")
                last = finish
                i = finish
                continue

        # 没有反斜杠的简单公式：x^2、a_n、n=2、F'(x)=x^2
        if text[i] in "^_=":
            start = i
            while start > last and text[start - 1] in _MATH_LEFT:
                start -= 1
            while start < i and text[start].isspace():
                start += 1

            finish = _scan_bare_end(text, i)
            while finish > start and text[finish - 1].isspace():
                finish -= 1

            segment = text[start:finish]
            if (
                segment
                and not segment.startswith(("=", "^", "_"))
                and re.search(r"[A-Za-z0-9]", segment)
            ):
                out.append(text[last:start])
                out.append("$" + segment + "$")
                last = finish
                i = finish
                continue

        # 简单数字区间 [0,1]
        if text[i] == "[":
            match = re.match(r"\[\s*\d+\s*,\s*\d+\s*\]", text[i:])
            if match:
                segment = match.group(0)
                out.append(text[last:i])
                out.append("$" + segment + "$")
                last = i + len(segment)
                i = last
                continue

        i += 1

    out.append(text[last:])
    return "".join(out)


def render_rich(text: str, display_fallback: bool = False) -> str:
    text = _wrap_bare_latex(text or "")
    if not _MATH_TOKEN.search(text) and display_fallback and _looks_like_math(text):
        return "\\[\n" + sanitize_math(text) + "\n\\]"

    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(escape_text("".join(buffer)))
            buffer.clear()

    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            end = text.find("$$", i + 2)
            if end >= 0:
                flush()
                out.append("\\[\n" + sanitize_math(text[i + 2 : end]) + "\n\\]")
                i = end + 2
                continue
        if text.startswith(r"\[", i):
            end = text.find(r"\]", i + 2)
            if end >= 0:
                flush()
                out.append("\\[\n" + sanitize_math(text[i + 2 : end]) + "\n\\]")
                i = end + 2
                continue
        if text.startswith(r"\(", i):
            end = _find_paren_end(text, i)
            if end >= 0:
                inner = text[i + 2 : end].replace(r"\(", "").replace(r"\)", "")
                flush()
                out.append("\\(" + sanitize_math(inner) + "\\)")
                i = end + 2
                continue
        if text[i] == "$":
            end = text.find("$", i + 1)
            if end >= 0:
                flush()
                out.append("\\(" + sanitize_math(text[i + 1 : end]) + "\\)")
                i = end + 1
                continue
        buffer.append(text[i])
        i += 1

    flush()
    return "".join(out)


def render_inline(text: str) -> str:
    return render_rich(text, display_fallback=False)


def render_field(text: str, display: bool = False) -> str:
    text = text or ""
    if not _MATH_TOKEN.search(text) and _looks_like_math(text):
        inner = sanitize_math(text)
        return ("\\[\n" + inner + "\n\\]") if display else ("\\(" + inner + "\\)")
    return render_rich(text, display_fallback=False)


def render_math(math: str) -> str:
    math = sanitize_math(math)
    if not math:
        return ""
    if "\\begin{" not in math and ("&" in math or "\\\\" in math):
        math = "\\begin{aligned}" + math + "\\end{aligned}"
    return "\\[\n" + math + "\n\\]"


def _header(title: str, fontsize: str = "11pt", margin: str = "1.7cm") -> str:
    title_tex = render_inline(title)
    return rf"""\documentclass[{fontsize},a4paper,fontset=fandol]{{ctexart}}
\usepackage[margin={margin}]{{geometry}}
\usepackage{{amsmath,amssymb,mathtools,bm}}
\xeCJKDeclareCharClass{{CJK}}{{"2460->"2473}}
\usepackage{{amsthm,graphicx}}
\usepackage{{xcolor}}
\usepackage[most]{{tcolorbox}}
\usepackage{{fancyhdr}}
\pagestyle{{fancy}}
\fancyhf{{}}
\fancyfoot[C]{{\small\thepage}}
\renewcommand{{\headrulewidth}}{{0pt}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0.4em}}
\linespread{{1.15}}
\begin{{document}}
\begin{{center}}
{{\LARGE\bfseries {title_tex}}}\\[0.4em]
{{\small\color{{gray}} 由 AI 生成，仅供参考}}
\end{{center}}
"""


def build_tex(doc: dict) -> str:
    title = str(doc.get("title") or "解题过程")
    steps = doc.get("steps", [])
    if not isinstance(steps, list):
        steps = []

    total_len = len(str(doc.get("problem_latex") or "")) + len(str(doc.get("answer_latex") or ""))
    total_len += sum(
        len(str(step.get("text") or "")) + len(str(step.get("math") or ""))
        for step in steps
        if isinstance(step, dict)
    )
    compact = total_len > 6000 or len(steps) > 16
    fontsize = "10pt" if compact else "11pt"
    margin = "1.4cm" if compact else "1.7cm"

    problem = render_field(str(doc.get("problem_latex") or ""), display=True)
    answer = render_field(str(doc.get("answer_latex") or ""), display=False)
    check = render_field(str(doc.get("check_latex") or ""), display=False)

    lines = [_header(title, fontsize, margin)]
    lines.append(
        r"\begin{tcolorbox}[breakable,colback=gray!5,colframe=gray!45,"
        r"title=题目,fonttitle=\bfseries]"
    )
    lines.append(problem)
    lines.append(r"\end{tcolorbox}")
    lines.append(r"\vspace{0.2em}")

    for step in steps:
        if not isinstance(step, dict):
            continue
        text = render_inline(str(step.get("text") or ""))
        math = render_math(str(step.get("math") or ""))
        if text:
            lines.append(text)
        if math:
            lines.append(math)
        lines.append(r"\par\vspace{0.55em}")

    if answer:
        lines.append(
            r"\begin{tcolorbox}[breakable,colback=green!4,colframe=green!45!black,"
            r"title=答案,fonttitle=\bfseries]"
        )
        lines.append(answer)
        lines.append(r"\end{tcolorbox}")

    if check:
        lines.append(
            r"\begin{tcolorbox}[breakable,colback=blue!3,colframe=blue!35!black,"
            r"title=检验 / 易错点,fonttitle=\bfseries]"
        )
        lines.append(check)
        lines.append(r"\end{tcolorbox}")

    lines.append(r"\end{document}")
    return "\n".join(lines)
