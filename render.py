"""把结构化解答排版成 A4 PDF，再转成 PNG。"""

from __future__ import annotations

import re

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


_GENERATED_PROBLEM_LABEL = re.compile(
    r"(?m)^(?:图片/PDF 转写（第 \d+ 批）|"
    r"(?:B\d{4,}|PDF 附件 \d+)（共 \d+ 页，第 [\d、]+ 页）|"
    r"附件题目|本次补充/追问材料（结合原题重新核对）|"
    r"用户补充或更正（以本段为准）|本次追问)：\r?\n"
)


def display_problem_text(text: str) -> str:
    """Show the problem content without source labels added by the bot."""
    text = _GENERATED_PROBLEM_LABEL.sub("", str(text or ""))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_PAGE_FURNITURE_ENV = re.compile(
    r"[ \t]*\\begin\{(flushright|center)\}(.*?)\\end\{\1\}[ \t]*\r?\n?", re.S)
_PAGE_FURNITURE_LINE = re.compile(
    r"(?mi)^[ \t]*(?:"
    r"第\s*\d{1,4}\s*页(?:\s*[/／、，,]?\s*共\s*\d{1,4}\s*页)?"
    r"|(?:page|p\.)\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?"
    r"|\d{1,4}\s*/\s*\d{1,4}"
    r"|[•·●▪]\s*\d{1,4}(?:\s*[•·●▪])?"
    r"|\d{1,4}\s*[•·●▪]"
    r"|[-–—]\s*\d{1,4}\s*[-–—]"
    r")[ \t]*(?=\r?\n|\Z)\r?\n?"
)
_PAGE_NUMBER_LINE = re.compile(r"[ \t]*\d{1,4}[ \t]*")


def _is_page_furniture_block(content: str) -> bool:
    """True when a flushright/center block only carries a page number."""
    body = re.sub(r"\\[A-Za-z]+", "", content)
    body = re.sub(r"[\\\s{}•·●▪~|/\-–—]+", "", body)
    return body == "" or body.isdigit()


def strip_page_furniture(text: str) -> str:
    """Drop page numbers, running heads and footers copied from the source page.

    The vision model is asked for a complete transcription, so printed page
    furniture can land in the problem text (typically a ``flushright`` block
    such as ``\\textbullet\\ 42 \\textbullet``). Those fragments fail the LaTeX
    whitelist and used to force the whole document into source-text mode.
    Only page-furniture shaped fragments are removed; content is untouched.
    """
    text = str(text or "")
    if not text.strip():
        return text
    text = _PAGE_FURNITURE_ENV.sub(
        lambda match: "\n" if _is_page_furniture_block(match.group(2)) else match.group(0), text)
    text = _PAGE_FURNITURE_LINE.sub("", text)
    lines = text.splitlines()
    numbered = [index for index, line in enumerate(lines) if line.strip()]
    if len(numbered) > 1:
        for index in {numbered[0], numbered[-1]}:
            if _PAGE_NUMBER_LINE.fullmatch(lines[index]) and sum(
                    bool(line.strip()) for line in lines) > 1:
                lines[index] = ""
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+\r?\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def escape_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\u2028\u2029]", " ", text or "")
    text = "".join(_SPECIAL.get(ch, ch) for ch in (text or ""))
    text = re.sub(r"\n\s*\n", r"\\par ", text)
    return text.replace("\n", " ").strip()


_MATH_COMMANDS = set("""
frac dfrac tfrac cfrac sqrt sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh
exp log ln lg lim limsup liminf sup inf max min sum prod coprod int iint iiint oint
oplus otimes bigoplus bigcup bigcap cup cap partial nabla infty cdot times div pm mp ast star
circ bullet cdots ldots vdots ddots dots le leq ge geq neq ne approx sim simeq equiv
cong propto ll gg prec succ preceq succeq in notin ni mid subset subseteq supset supseteq emptyset
varnothing forall exists neg land lor wedge vee implies iff to rightarrow leftarrow
Rightarrow Leftarrow leftrightarrow Leftrightarrow Longrightarrow Longleftarrow Longleftrightarrow longleftrightarrow mapsto longrightarrow longleftarrow
uparrow downarrow lvert rvert lVert rVert vert Vert langle rangle lbrace rbrace
lfloor rfloor lceil rceil left right middle big Big bigg Bigg bigl bigr Bigl Bigr
biggl biggr Biggl Biggr alpha beta gamma delta epsilon varepsilon zeta eta theta
vartheta iota kappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon
phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
mathbb mathcal mathfrak mathrm mathbf mathit mathsf mathtt mathnormal boldsymbol bm
operatorname text textrm textbf textit textbullet overline underline overbrace underbrace
widehat widetilde hat tilde bar vec dot ddot dddot acute grave breve check boxed
phantom hphantom vphantom quad qquad enspace thinspace medspace thickspace
displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits underset
overset stackrel substack binom dbinom tbinom begin end hline cr det dim ker deg
gcd Pr mod bmod pmod arg Re Im ell hbar angle triangle perpendicular perp parallel
therefore because not smallsetminus setminus coloneqq colon backslash top bot prime
lnot restriction vdash models
Arrowvert Bbbk Box Bumpeq Cap Cup Diamond Doteq Downarrow Finv Game Lleftarrow Lsh Rrightarrow
Rsh Subset Supset Uparrow Updownarrow Vdash Vvdash aleph amalg approxeq arrowvert asymp
backepsilon backprime backsim backsimeq barwedge beth between bigcirc bigodot bigotimes
bigsqcup bigstar bigtriangledown bigtriangleup biguplus bigvee bigwedge blacklozenge
blacksquare blacktriangle blacktriangledown blacktriangleleft blacktriangleright boxdot
boxminus boxplus boxtimes braceld bracelu bracerd braceru bracevert bumpeq cdotp centerdot
circeq circlearrowleft circlearrowright circledS circledast circledcirc circleddash clubsuit
complement curlyeqprec curlyeqsucc curlyvee curlywedge curvearrowleft curvearrowright dagger
daleth dashv ddagger diagdown diagup diamond diamondsuit digamma divideontimes doteqdot dotplus
doublebarwedge doublecap doublecup downdownarrows downharpoonleft downharpoonright eqcirc eqsim
eqslantgtr eqslantless eth fallingdotseq flat frown geqq geqslant gets ggg gggtr gimel gnapprox
gneq gneqq gnsim gtrapprox gtrdot gtreqless gtreqqless gtrless gtrsim gvertneqq heartsuit
hslash imath intercal intop jmath ldotp leadsto leftarrowtail leftharpoondown leftharpoonup
leftleftarrows leftrightarrows leftrightharpoons leftrightsquigarrow leftthreetimes leqq
leqslant lessapprox lessdot lesseqgtr lesseqqgtr lessgtr lesssim lgroup lhd lhook llcorner lll
llless lmoustache lnapprox lneq lneqq lnsim looparrowleft looparrowright lozenge lrcorner
ltimes lvertneqq mapstochar mathdollar mathparagraph mathring mathsection measuredangle mho
multimap nLeftarrow nLeftrightarrow nRightarrow nVDash nVdash natural ncong nearrow nexists
ngeq ngeqq ngeqslant ngtr nleftarrow nleftrightarrow nleq nleqq nleqslant nless nmid nparallel
nprec npreceq nrightarrow nshortmid nshortparallel nsim nsubseteq nsubseteqq nsucc nsucceq
nsupseteq nsupseteqq ntriangleleft ntrianglelefteq ntriangleright ntrianglerighteq nvDash
nvdash nwarrow odot ointop ominus oslash owns pitchfork precapprox preccurlyeq precnapprox
precneqq precnsim precsim rgroup rhd rhook rightarrowtail rightharpoondown rightharpoonup
rightleftarrows rightleftharpoons rightrightarrows rightsquigarrow rightthreetimes risingdotseq
rmoustache rtimes searrow sharp shortmid shortparallel smallfrown smallint smallsmile smile
spadesuit sphericalangle sqcap sqcup sqsubset sqsubseteq sqsupset sqsupseteq square subseteqq
subsetneq subsetneqq succapprox succcurlyeq succnapprox succneqq succnsim succsim supseteqq
supsetneq supsetneqq swarrow thickapprox thicksim triangledown triangleleft trianglelefteq
triangleq triangleright trianglerighteq twoheadleftarrow twoheadrightarrow ulcorner unlhd unrhd
updownarrow upharpoonleft upharpoonright uplus upuparrows urcorner vDash varbigtriangledown
varbigtriangleup varkappa varpropto varsubsetneq varsubsetneqq varsupsetneq varsupsetneqq
vartriangle vartriangleleft vartriangleright veebar wp wr
Join bowtie coth dasharrow dashleftarrow dashrightarrow doteq dotsb dotsc dotsi dotsm dotso hom
hookleftarrow hookrightarrow injlim longmapsto mathbin mathclose mathinner mathop mathopen
mathord mathpunct mathrel overleftarrow overleftrightarrow overrightarrow projlim surd
underleftarrow underleftrightarrow underrightarrow varinjlim varliminf varlimsup varprojlim
xleftarrow xrightarrow
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


def _math_source_fallback(source: str) -> str:
    """Keep one rejected formula as readable source instead of failing the page."""
    return "{\\small " + escape_text(source) + "}"


def _math_payload(inner: str, display: bool, notes) -> str:
    """Sanitize one formula; when ``notes`` is a list a rejected formula degrades alone."""
    try:
        body = sanitize_math(inner)
    except RenderError as exc:
        if notes is None:
            raise
        notes.append(str(exc))
        return _math_source_fallback(inner)
    return ("\\[\n" + body + "\n\\]") if display else ("\\(" + body + "\\)")


def render_rich(text: str, display_fallback: bool = False, *, notes=None) -> str:
    text = _wrap_bare_latex(text or "")
    if not _MATH_TOKEN.search(text) and display_fallback and _looks_like_math(text):
        return _math_payload(text, True, notes)

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
                out.append(_math_payload(text[i + 2 : end], True, notes))
                i = end + 2
                continue
        if text.startswith(r"\[", i):
            end = text.find(r"\]", i + 2)
            if end >= 0:
                flush()
                out.append(_math_payload(text[i + 2 : end], True, notes))
                i = end + 2
                continue
        if text.startswith(r"\(", i):
            end = _find_paren_end(text, i)
            if end >= 0:
                inner = text[i + 2 : end].replace(r"\(", "").replace(r"\)", "")
                flush()
                out.append(_math_payload(inner, False, notes))
                i = end + 2
                continue
        if text[i] == "$":
            end = text.find("$", i + 1)
            if end >= 0:
                flush()
                out.append(_math_payload(text[i + 1 : end], False, notes))
                i = end + 1
                continue
        buffer.append(text[i])
        i += 1

    flush()
    return "".join(out)


def render_inline(text: str, *, notes=None) -> str:
    return render_rich(text, display_fallback=False, notes=notes)


def render_field(text: str, display: bool = False, *, notes=None) -> str:
    text = text or ""
    if not _MATH_TOKEN.search(text) and _looks_like_math(text):
        return _math_payload(text, display, notes)
    return render_rich(text, display_fallback=False, notes=notes)


def render_math(math: str, *, notes=None) -> str:
    try:
        body = sanitize_math(math)
    except RenderError as exc:
        if notes is None:
            raise
        notes.append(str(exc))
        return _math_source_fallback(math)
    if not body:
        return ""
    if "\\begin{" not in body and ("&" in body or "\\\\" in body):
        body = "\\begin{aligned}" + body + "\\end{aligned}"
    return "\\[\n" + body + "\n\\]"


def _header(title: str, fontsize: str = "11pt", margin: str = "1.7cm", *, plain=False,
            notes=None) -> str:
    title_tex = escape_text(title) if plain else render_inline(title, notes=notes)
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


def build_tex(doc: dict, *, plain=False, body_only=False, notes=None) -> str:
    """Build the document.

    ``notes`` is an optional list: when given, a formula rejected by the LaTeX
    whitelist degrades to its own source text and the reason is appended to
    ``notes`` instead of aborting the whole document.
    """
    title = str(doc.get("title") or "解题过程")
    steps = doc.get("steps", [])
    if not isinstance(steps, list):
        steps = []

    total_len = len(display_problem_text(doc.get("problem_latex", "")))
    total_len += len(str(doc.get("answer_latex") or ""))
    total_len += sum(
        len(str(step.get("text") or "")) + len(str(step.get("math") or ""))
        for step in steps
        if isinstance(step, dict)
    )
    compact = total_len > 6000 or len(steps) > 16
    fontsize = "10pt" if compact else "11pt"
    margin = "1.4cm" if compact else "1.7cm"

    def field(name, display=False):
        value = str(doc.get(name) or "")
        return escape_text(value) if plain else render_field(value, display=display, notes=notes)

    problem_text = display_problem_text(doc.get("problem_latex", ""))
    problem = escape_text(problem_text) if plain else render_field(problem_text, display=True, notes=notes)
    answer, check = field("answer_latex"), field("check_latex")
    lines = ([r"\section*{" + escape_text(title) + "}"] if body_only
             else [_header(title, fontsize, margin, plain=plain, notes=notes)])
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
        text_value, math_value = str(step.get("text") or ""), str(step.get("math") or "")
        text = escape_text(text_value) if plain else render_inline(text_value, notes=notes)
        math = escape_text(math_value) if plain else render_math(math_value, notes=notes)
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

    if not body_only:
        lines.append(r"\end{document}")
    return "\n".join(lines)


def build_favorites_tex(title, entries, *, plain=False, notes=None):
    bodies = [build_tex(entry["doc"], plain=plain or entry["plain"], body_only=True, notes=notes)
              for entry in entries]
    return "\n".join((_header(title, plain=True), "\n\\clearpage\n".join(bodies), r"\end{document}"))
