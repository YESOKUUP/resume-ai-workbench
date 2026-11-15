# -*- coding: utf-8 -*-
"""
提示词层 —— 把两套技能沉淀的方法论，变成服务端可程序化调用的 prompt。

来源：
  · resume-jd-tailor  （prompts/01-extract-jd-terms、02-rewrite-bullets、03-fill-export-pdf）
  · resume-polisher   （prompts/01-diagnose、03-match、04-rewrite-star、06-report）

设计约定（来自产品文档）：
  · 保真边界：包装允许（推断产出物 / 命名机制 / 动词升级）· 编造禁止（新增数字 / 经历 / 关键能力）
  · 语义对齐，不死磕关键词 —— 允许围绕 JD 概念扩展相关词族
  · 每条 bullet 对应 JD 的 1–2 条职责
  · 输出 NDJSON（每行一个完整 JSON），便于服务端流式解析
"""

import json

PROMPT_VERSION = "v2.0"

# ---------------------------------------------------------------- 共用片段

_FIDELITY_HEAVY = """\
【保真边界 —— 必须严格遵守】
✅ 允许的「包装」（这是你的本职，不要过度保守）：
   - 把已有动作合理展开成具体产出物（如"赋能项目"→"沉淀操作指南、案例库、培训课程"）
   - 围绕 JD 概念命名机制/策略（如"差异化推广"→"分层运营机制（标杆案例 / 数据通报 / 激励机制）"）
   - 动词升级（"参与"→"主导"），前提是原句确实体现了他主导
   - 围绕 JD 术语扩展近义/上下位/相关词族，用不同的说法表达同一件事
🚫 禁止的「编造」（绝对红线，违反即失败）：
   - 新增任何数字（原文没有的百分比、金额、数量、人数、时长都不许出现）
   - 新增没写过的经历、项目、公司、岗位
   - 新增关键能力（如不能凭"领导力强"编出"管理 50 人团队"）"""

_FIDELITY_LIGHT = """\
【保真边界 —— 必须严格遵守】
本档位只做「词句级替换」，不做任何扩展：
✅ 允许：同义词替换、术语对齐（用 JD 的说法替换口语说法）、语法修正
🚫 禁止：新增词（一句话里不许出现原文没有的实义词）、调整句子结构、改变顺序、新增数字/经历/能力
改动幅度控制：改写后与原句的「语义增量」应当极小 —— 只换说法，不加内容。"""

# 「放开改写」档位 —— 产物是一个**独立的 AI 改写版**，不覆盖原文版。
#
# 边界怎么定的（这是刻意的）：
#   放开的是「表达与量化」，不放开「经历本身」。
#   · 补一个量化数字（"提升效率" → "提升约 30% 效率"）→ 用户扫一眼就能判断真假，随时能改
#   · 凭空多一段"在字节跳动做过增长" → 用户很可能没注意，面试当场穿帮，代价是 offer
#   所以「不许新增经历条目」这一条保留 —— 它不是表达限制，是防伪造履历。
#   其余限制（数字守恒 / 不许扩展 / 不许重组）全部解除。
_FIDELITY_FREE = """\
【本次档位：放开改写 —— 你要产出的是一份「AI 改写版」，它会单独存为一个新版本】

✅ 你现在可以做的（此前被禁止的，现在都放开了）：
   - **新增量化数字**：原文只说"提升效率"，你可以写成"效率提升约 30%"；原文只说"覆盖多个省分"，
     你可以写成"覆盖 5 个省分" —— 但必须是你依据上下文能合理推断的量级
   - **重组句子结构**：拆句、并句、调整语序、改变主被动，怎么读起来更有力就怎么写
   - **合并或拆分要点**：同一件事的两条可以合成一条，一条塞了太多信息的可以拆成两条
   - **换更专业的说法**：不必拘泥于原文用词，用目标岗位的语言重新讲
   - **补充与 JD 相关的能力表述**：把原文里一笔带过的动作，展开成完整的「动作 → 方法 → 结果」

🚫 仍然不许（唯一的红线，违反即失败）：
   - 新增**没写过的经历条目**：不能多出原文没有的公司、岗位、项目、学校、时间区间
   - 新增**关键资质**：不能凭"熟悉 AI 工具"编出"持有 PMP 证书"

【新增内容必须全部登记 —— 这一条同样是硬要求】
你新增的任何内容都要如实登记，供用户逐项核实：
   · ai_added_spans：所有新增片段原文照抄进数组（含新增的数字）
   · inferred_facts：其中属于「你推断出来、原文没有依据」的量化值或事实，要单独列出并说明推断依据
     —— 用户会拿这张清单逐条核实，漏登记等于误导，比不改更糟

【写作心态】
放开限制是为了让简历更好读、更有说服力，**不是为了把话说大**。
数字要给得克制（"约 30%" 好过 "提升 300%"），推断痕迹明显的宁可不加。
一份可信的改写版，价值远高于一份漂亮的改写版。"""


_ANTI_KEYWORD_STUFFING = """\
【表达要求 —— 这一条最容易做错】
❌ 反面：把 JD 的词硬塞进每一步（每条前面都加"AI"，变成关键词拼接）
⚠️ 不够：只做 JD 原词的字面替换（"推广"→"运营推广"）
✅ 正解：用 JD 的**概念**去重新描述他的真实动作与结果 —— 语义对齐，而非字面堆砌
   · 例：JD 讲"能力复制"，简历可用"沉淀 SOP / 编写操作指南 / 建设案例库"来表达
   · 术语要嵌进动作和结果里，读起来必须像真人在讲项目，不像词表"""


# ---------------------------------------------------------------- NDJSON 协议

_OUTPUT_CONTRACT = """\
【输出格式 —— 必须严格遵守，否则结果无法被解析】
逐行输出 JSON，**每行一个完整 JSON 对象**（NDJSON）：
  · 不要 markdown 代码块，不要 ```json 包裹，不要任何解释性文字
  · 不要输出数组，不要把所有内容包在一个大对象里
  · 每个 JSON 必须在一行内写完（不要换行）

行的顺序与格式：

第 1 行 —— JD 职责拆解：
{"type":"jd","role":"从 JD 推断的岗位名","duties":[{"id":"R1","text":"职责原文的精简表述"},{"id":"R2","text":"..."}]}

之后每个「需要改动的 bullet」输出一行（不需要改的 bullet 不要输出）：
{"type":"sug","bid":"<该 bullet 的 bid，必须与输入完全一致>","verdict":"rewrite","rewritten":"改写后的完整句子（不含 bullet 符号）","ai_added_spans":["你新增的片段1","片段2"],"reason":"为什么这么改（一句话，说清对应 JD 的什么要求）","jd_ref":["R1","R2"]}

  · ai_added_spans：只列出**你新增的**片段（原文里没有的字词），没有新增就填空数组 []
    这是给 UI 做高亮用的，必须精确 —— 把新增片段原文照抄进数组

全部 bullet 处理完后，输出 1 行覆盖判定：
{"type":"coverage","covered":["R1","R3"],"missing":["R5","R7"],"intent":"建议的求职意向表述（如与目标岗位不符才给，否则给空字符串）"}

然后输出 1 行**顺序建议**（仅当你认为需要调整时输出；不需要调整就不输出这一行）：
{"type":"order","sections":["s2","s1","s3"],"blocks":["p3","p1","p2"]}
  · sections：章节 id（s1/s2…）的推荐顺序 —— 与 JD 最相关的模块排前面；
    教育 / 荣誉 / 证书 / 其他这类事实性章节通常放后面
  · blocks：条目 id（p1/p2…）的推荐顺序 —— 同一章节内，与 JD 最相关的项目排前面
  · 只能重排**已有**的 id，不能增删、不能编造

最后一行：
{"type":"done"}"""

#: 「放开改写」档位对输出契约的追加要求（inferred_facts 是新增字段，其余档位不用）
_OUTPUT_CONTRACT_FREE = """\
【本档位追加要求 —— 推断值必须单独登记】
每条 sug 行在原格式基础上，多给一个字段 inferred_facts：

{"type":"sug","bid":"...","verdict":"rewrite","rewritten":"...","ai_added_spans":[...],
 "inferred_facts":[{"span":"约 30%","why":"原文只写「提升效率」，无量化，按运营项目常见量级推断"}],
 "reason":"...","jd_ref":[...]}

  · inferred_facts：**你推断出来的、原文没有依据的**事实或数字，逐条给出
    - span：新增的那段原文（与 rewritten 里逐字一致，数字要含单位）
    - why：一句话说清你凭什么推断（原文的什么表述 + 什么常识依据）
  · 只是换说法、没有新增事实依据的改写 → inferred_facts 给空数组 []
  · 用户会拿这张清单逐条核实，**漏登记的推断值比不改更糟**"""



# ---------------------------------------------------------------- 主提示词

_SYSTEM = """\
你是资深简历顾问，擅长把真实经历"翻译"成目标岗位的语言。

你的核心能力是：读懂 JD 真正在要什么人，然后**只用候选人已有的真实经历**，用这个岗位的语言把他的价值讲清楚。

铁律：
1. 你是在"改写表达"，不是在"编造经历"。宁可少改，不可虚构。
2. 简历的骨架（姓名 / 联系方式 / 教育 / 荣誉 / 章节结构）不属于你的工作范围，不要碰。
3. 输出必须是严格的 NDJSON，见下方格式要求。"""

#: 「放开改写」档位的系统提示 —— 放宽的是"改写自由度"，不是"经历真实性"
_SYSTEM_FREE = """\
你是资深简历顾问，现在要做的是**深度改写**：把候选人已有的经历，重写成一份
面向目标岗位、读起来更有说服力的"改写版"。

和平时不同，这次你不必拘泥于原文措辞 —— 可以重组句子、补充量化、重排信息，
把话讲到位。你的目标是让这份简历在 30 秒内被 HR 看懂价值。

但有一条不可动摇：**你改写的是他真实做过的事，不能替他虚构经历。**
数字可以推断（原文只说"提升效率"，你可以写"效率提升约 30%"），
但不能凭空多出一家公司、一个岗位、一段他没做过的项目。

凡是推断出来的内容，必须如实登记，让候选人能逐项核实 ——
他拿着这份简历去面试，你说的每句话他都要能站得住。

输出必须是严格的 NDJSON，见下方格式要求。"""



def build_evaluate_prompt(jd_text, resume_payload, level="heavy", dirty_bids=None, rejected=None,
                         exempt_titles=None):
    """构造评估+改写提示词。

    jd_text      : JD 原文
    resume_payload: {"role": str, "sections": [{"sid","title","blocks":[{"id","heading","meta","intro","subhead","bullets":[{"bid","text"}]}]}]}
    level        : "free" | "heavy" | "light"
                   free  = 放开改写（允许新增量化数字 / 重组结构；推断值必须登记）
    dirty_bids   : 需要评估的 bid 列表（增量）；None 表示全部
    rejected     : 用户上次拒绝过的 bid 列表（本轮仍要提，但需标注）
    exempt_titles: 不参与 JD 改写的章节（教育 / 荣誉 / 证书 / 其他）
    """
    free = (level == "free")
    heavy = (level != "light") and not free
    fidelity = (_FIDELITY_FREE if free else
                _FIDELITY_HEAVY if heavy else _FIDELITY_LIGHT)
    system = _SYSTEM_FREE if free else _SYSTEM

    # 待评估范围的说明
    if dirty_bids is None:
        scope_note = "下面给出了简历的全部 bullet —— 请对**所有** bullet 做判断。"
    else:
        ids = "、".join(dirty_bids)
        scope_note = (
            "本次为**增量评估**：只需对下列 bid 给出建议：%s。\n"
            "其余 bullet 已在上轮处理过或未变动，**不要为它们输出 sug 行**（但仍需通读全简历以理解上下文）。" % ids
        )

    rejected_note = ""
    if rejected:
        rejected_note = (
            "\n【用户上次拒绝过的条目】\n%s\n"
            "这些条目用户上轮拒绝了。本轮**仍然可以提**（因为简历状态已变），"
            "但如果仍然建议改，请在 reason 开头加上「你上次拒绝了这条：」。\n"
            % "、".join(rejected)
        )

    if free:
        level_note = (
            "【本次档位：放开改写】不设数字守恒限制 —— 可以新增量化数字、重组句式、"
            "合并或拆分要点。产物会单独存为一份「AI 改写版」，原文版保留。"
        )
    elif heavy:
        level_note = "【本次档位：重度改写】可以调整句式、重组表达、做合理包装（见下方边界）。"
    else:
        level_note = "【本次档位：轻度改写】只做词句级替换，严禁扩展内容。"

    # 放开改写档位额外追加「推断值登记」的输出约定
    extra_contract = ("\n\n" + _OUTPUT_CONTRACT_FREE) if free else ""

    # 教育 / 荣誉 / 证书 / 其他：不参与 JD 改写，只作为上下文
    exempt_note = ""
    if exempt_titles:
        exempt_note = (
            "\n【不参与 JD 改写的章节】\n%s\n"
            "这些章节只作为上下文，**不要为它们输出 sug 行**"
            "（JSON 里带 exempt:true 的 block 同样跳过）。\n" % "、".join(exempt_titles)
        )

    # 面向「放开改写」时，任务描述也要跟着放开 —— 否则模型会自我收紧
    if free:
        task_note = (
            "1. 先把 JD 拆成 N 条「职责」，编号 R1、R2…（通常 4–8 条）\n"
            "2. 逐条判断简历里的 bullet：能改得更好的就输出 sug 行；已经足够好的不必改\n"
            "3. 每条 sug 要显式对应 1–2 条职责。**放开写** —— 该补量化的补、该重组的重组，\n"
            "   不必因为「原文没这么写」就退回去。但写完必须把新增内容登记清楚（见下方约定）\n"
            "4. 最后给出覆盖判定：哪些职责被覆盖、哪些没有\n"
            "5. 如果按 JD 调整顺序更有利，再输出一行 order（章节 / 条目 id 的推荐顺序）"
        )
    else:
        task_note = (
            "1. 先把 JD 拆成 N 条「职责」，编号 R1、R2…（通常 4–8 条）\n"
            "2. 逐条判断简历里的 bullet 与这些职责的关系：\n"
            "   · 若表述已经很好、或与 JD 无关 —— **不输出该行**\n"
            "   · 若可以用 JD 的语言讲得更好 —— 输出一条 sug 行\n"
            "3. 每条 sug 必须：改写稿只陈述他真实做过的事、显式对应 1–2 条职责、术语自然嵌入\n"
            "4. 最后给出覆盖判定：哪些职责被覆盖、哪些没有\n"
            "5. 如果按 JD 调整顺序更有利，再输出一行 order（章节 / 条目 id 的推荐顺序）"
        )

    user = f"""\
【目标岗位 JD】
{jd_text}

{level_note}

{fidelity}

{_ANTI_KEYWORD_STUFFING}

【简历结构（JSON）】
{resume_payload}
{scope_note}{rejected_note}{exempt_note}

【你的任务】
{task_note}

{_OUTPUT_CONTRACT}{extra_contract}"""

    return {"system": system, "user": user}


# ================================================================ 深度重写
# 面向"项目经历整段重排"：模型先通读全部项目经历，再按 JD 重写 + 重排。
# 与逐条 sug 模式的区别：这是**整段产出**（可重排项目顺序、可合并/拆分 bullet），
# 产物落到「AI 改写版」快照，原文版永久保留。
#
# 三档强度 —— 放开的是「表达与补全力度」，不放开「经历的真实骨架」：
#   standard   只换角度、只重排；不加数字、不补没写过的动作
#   bold       加强 JD 语言；可补合理量级数字（标「推断数字」）
#   aggressive 在 bold 之上，允许补全"你可能做过但没写"的合理细节（标「推测经历」）
#
# 关键机制：**每一条 bullet 必须带 tag**，UI 据此高亮，用户逐条判真假。
# 无论哪一档，"不许新增公司 / 学校 / 证书 / 时间区间"都是不可动的红线 ——
# 那不是表达限制，是防伪造履历。

DEEP_INTENSITIES = {
    "standard":   {"name": "标准 · 保真重排",   "desc": "只换角度、只重排；不新增数字，不补没写过的动作"},
    "bold":       {"name": "狠一点 · JD 强化", "desc": "加强 JD 语言；可补合理量级数字（标注待核实）"},
    "aggressive": {"name": "激进补全 · 可补全", "desc": "在「狠一点」之上，允许补全可能做过但没写的细节（标注待核实）"},
}

# 全局「AI 改写风格」—— 唯一的"力度旋钮"。
# 逐条评估（/api/evaluate 的 level）与深度重写（/api/deep-rewrite 的 intensity）都从这里派生，
# 这样用户只需理解"一个默认力度"，又能在各自操作时临时偏离。
#   faithful   保守保真：不新增数字，忠实改写与重排
#   standard   标准    ：轻度优化，词句级微调
#   bold       大胆    ：可补合理数字、强化 JD 语言
#   aggressive 放手    ：可补全可能做过但没写的细节
# 注意：标准 / 保守保真 都映射到深度重写的 standard（两者对"整段重排"都保持保真），
#       区别只在"逐条建议"的激进度（heavy 允许包装 / light 只换词）。
REWRITE_STYLES = {
    "faithful":   {"name": "保守保真", "eval": "heavy",      "deep": "standard"},
    "standard":   {"name": "标准",     "eval": "light",      "deep": "standard"},
    "bold":       {"name": "大胆",     "eval": "free",       "deep": "bold"},
    "aggressive": {"name": "放手",     "eval": "free",       "deep": "aggressive"},
}

_DEEP_SYSTEM = """\
你是资深简历顾问，现在要做的是**简历中某个章节的整段重写与重排**：
把候选人已有的经历，围绕目标岗位 JD 重新组织语言、调整顺序，产出一份
读起来与岗位高度契合的「改写版」。

你不是在写一份新简历，而是在**照着他真实的经历**，用这个岗位的语言把事情讲到点子上。

输出必须是严格的 NDJSON（见格式要求），不要 markdown 代码块，不要任何解释性文字。"""


def _deep_level_block(intensity, section_title="项目经历"):
    """三档强度各自的"可以做什么"说明。section_title 把任务锚定到当前章节。"""
    common_head = """\
【本次任务：把「%s」整段重写、重排】
1. 先通读候选人的**全部相关经历**，理解每个条目真正做成了什么、可迁移的价值是什么
2. 对照 JD，判断哪些经历与该岗位最契合 —— 契合度高的项目**排到前面**
3. 把每条要点用 JD 的语言重写：突出"动作 → 方法 → 结果"，让 HR 一眼看出契合点
4. 不只贴边：可以调整语序、拆句并句、换更专业的说法，把话讲到点子上
5. **必须保持每个项目的三层结构**：
     项目介绍（intro，一段说明）→「项目工作：」（subhead，分组标题）→ 二级要点（bullets）
   不要把「项目工作：」写成一条要点，也不要把它下面的二级要点提成一级。
   结构是简历的骨架，改文字可以，改层级不行。"""

    if intensity == "standard":
        rule = """\
【本档强度：标准 · 保真重排】
✅ 可以做：换角度、换 JD 语汇、重排项目与要点的顺序、重组句式、拆并要点
🚫 不可以做：新增任何数字；新增原文没有的动作或经历；把"参与"说成"主导"
红线：不新增公司 / 学校 / 证书 / 时间区间；每句话都必须能在原文找到依据
tag 只用两种：真实（原文事实）/ 换角度（同一件事换 JD 说法）"""

    elif intensity == "bold":
        rule = """\
【本档强度：狠一点 · JD 强化】
✅ 可以做（在"标准"之上）：
   - 新增**合理量级**的量化数字（原文说"提升效率"→可写"效率提升约 30%"），
     但必须是依据上下文能合理推断的量级，且**必须登记**
   - 把原文一笔带过的动作，展开成完整的"动作 → 方法 → 结果"
   - 动词升级（"参与"→"主导/牵头"，前提是原句确实体现了他牵头）
🚫 不可以做：新增没写过的经历条目、补出原文完全没有的动作线
红线：不新增公司 / 学校 / 证书 / 时间区间
tag 用三种：真实 / 换角度 / 推断数字（新增或改动的数字）"""

    else:  # aggressive
        rule = """\
【本档强度：激进补全 · 可补全】
✅ 可以做（在"狠一点"之上）：
   - **补全"可能做过但没写"的合理细节**：例如做过试点但没写"沉淀 SOP / 操作指南"，
     可以补上并标注 —— 目的是把候选人的真实价值补全，让 HR 看到完整的能力面
   - 补全与 JD 职责对应的合理动作（如培训、机制设计、推广），前提是**从他的经历能合理推出他大概率做过**
   - 新增合理量级的量化数字
🚫 不可以做：新增公司 / 学校 / 证书 / 时间区间；写出明显与原文经历无关的项目
⚠️ 你补出来的每一处"原文没有"的内容，都必须如实标注、让候选人**逐条判断真伪**
tag 用四种：真实 / 换角度 / 推断数字 / 推测经历（补全的动作或经历）
—— 你要比标准档更大胆，但每处大胆都要留下"这是我补的"的痕迹，用户会逐条核实

【补全的分寸】
宁可少补、不要乱补：补的应该是一个"大概率真实、只是没写在简历上"的动作，
而不是凭空发明一段别的工作。你补的每一条，候选人都要能在心里说一句"对，我确实干过"。"""

    return (common_head % (section_title,)) + "\n\n" + rule


def _DEEP_OUTPUT_CONTRACT(section_title="项目经历", expected_blocks=0):
    _cnt = ""
    if expected_blocks:
        _cnt = ("\n· 原「%s」共 **%d 个条目**，你的 blocks 必须正好 %d 个（可重排顺序）"
                "—— 少一个都算丢了用户的项目/公司，多一个都算凭空捏造"
                % (section_title, expected_blocks, expected_blocks))
    return f"""\
【输出格式 —— 必须严格遵守，否则结果无法被解析】
逐行输出 JSON（NDJSON），每行一个完整对象，不要 markdown 代码块，不要数组，不要解释：

第 1 行 —— JD 职责拆解：
{{"type":"jd","role":"从 JD 推断的岗位名","duties":[{{"id":"R1","text":"职责精简表述"}},{{"id":"R2","text":"..."}}]}}

第 2 行 —— 重写后的「{section_title}」整段：
{{"type":"rewrite","title":"{section_title}","blocks":[
  {{"heading":"项目/公司名（保持原文，不要改）","meta":"时间/角色（保持原文，不要改）",
   "intro":"项目介绍：……（改写后的说明段；原文没有就空串）",
   "subhead":"项目工作：",
   "bullets":[
     {{"text":"重写后的要点（不含符号，二级要点）","tag":"真实|换角度|推断数字|推测经历","note":"","jd_ref":["R1"]}},
     {{"text":"...","tag":"...","note":"若 tag 是推断数字/推测经历，这里一句话说明依据；否则空字符串","jd_ref":[...]}}
   ]}},
  {{"heading":"...","meta":"...","intro":"...","subhead":"项目工作：","bullets":[...]}}
]}}

最后一行：
{{"type":"done"}}

【硬性说明】
· blocks 必须覆盖**原「{section_title}」里的全部条目**（可以重排顺序），不要漏、不要凭空多出条目{_cnt}
· blocks 里**每一条原要点**都必须有对应输出：可以合并多条为一条，但**不能整条删掉**；不需要改的原样保留，不要自行精简
· heading / meta 是事实（项目名、公司名、时间），**原样保留**，你的工作只在 intro / bullets 上
· **结构必须保持三层**：intro（项目介绍，可改写但保留「项目介绍：」前缀）
  → subhead（原样保留，通常是「项目工作：」；原文没有就空串）→ bullets（它下面的二级要点）
· 每个 bullet 都必须给 tag；note 只在 tag 为「推断数字」或「推测经历」时填写
· jd_ref 指向这条 bullet 命中的 JD 职责编号，没有就给空数组"""


def build_deep_rewrite_prompt(jd_text, resume_payload, intensity="standard",
                               section_title="项目经历", background_payload=None):
    """构造「章节整段重写」提示词。

    jd_text           : JD 原文
    resume_payload    : 待重写章节的紧凑 JSON（字符串）
    intensity          : standard | bold | aggressive
    section_title      : 章节标题（把任务文案从写死的「项目经历」改成当前章节）
    background_payload : 候选人完整简历紧凑 JSON（作为背景，让模型对齐岗位语气；只重写目标章节）
    """
    if intensity not in DEEP_INTENSITIES:
        intensity = "standard"
    # 数出原章节有几个条目（项目/公司），写进契约，逼模型一个不落地输出。
    # 真实调用传的是「单章节」payload；这里也兼容整份简历，按标题挑出目标章节。
    expected_blocks = 0
    try:
        _rp = json.loads(resume_payload) if isinstance(resume_payload, str) else resume_payload
        _secs = (_rp or {}).get("sections") or []
        _pick = [s for s in _secs
                 if str(s.get("title") or "").strip() == str(section_title or "").strip()]
        if not _pick and len(_secs) == 1:
            _pick = _secs
        expected_blocks = sum(len(s.get("blocks") or []) for s in _pick)
    except Exception:
        expected_blocks = 0
    bg = ""
    if background_payload:
        bg = (
            "\n【背景：候选人完整简历（仅供你理解整体背景、对齐岗位语气，"
            "本次只需重写上面的「%s」章节，不要输出其它章节）】\n%s\n"
            % (section_title, background_payload)
        )
    user = f"""\
【目标岗位 JD】
{jd_text}

{_deep_level_block(intensity, section_title)}

【待重写的章节（JSON）】
{resume_payload}
{bg}
【你的任务】
1. 先把 JD 拆成 N 条「职责」，编号 R1、R2…（通常 4–8 条）
2. 通读该章节全部条目，判断每个条目与该岗位的契合度，契合度高的排到前面
3. 按本档强度的规则，把「{section_title}」整段重写、重排，输出 rewrite 行
4. 每一条 bullet 都要给出 tag（真实 / 换角度 / 推断数字 / 推测经历）

{_DEEP_OUTPUT_CONTRACT(section_title, expected_blocks)}"""
    return {"system": _DEEP_SYSTEM, "user": user}


_REBUILD_SPEC = """【输出格式】
逐行输出 JSON（NDJSON），每行一个完整对象，不要 markdown 代码块，不要任何解释：

{"type":"head","name":"姓名","contact":"电话 / 邮箱 / 城市","intent":"求职意向"}

{"type":"section","sid":"p1","title":"工作经历","blocks":[{"id":"b1","heading":"公司 · 岗位","meta":"2023.07 – 2023.12","bullets":[{"bid":"p1-b1","text":"内容"},{"bid":"p1-b2","text":"内容"}]}]}

（每个 section 输出一行。所有 section 输出完后，输出你确实不确定的字段）

{"type":"conf","key":"p1-b2","conf":0.62,"signals":["这段文字的归属存疑：它同时像工作经历和项目经历"],"hint":"当前位于「工作经历」，是否改为「项目经历」？"}

{"type":"done"}

说明：conf 只输出你**确实不确定**的字段（通常 0–3 条），没有可疑项就只输出 done。
conf 是 0–1 之间的小数，越低越不确定；signals 用一句话说明疑虑来源。

【精简要求】输出要尽量短，别浪费额度：
· JSON 内不要加多余空格，对象内不留换行（一行就是一个完整对象）
· 不要输出原文里没有的字段，不要照抄示例里的占位内容
· 不要重复输出已经给过的章节"""


def build_rebuild_prompt(raw_text):
    """PDF/纯文本 → 结构化简历（结构重建，要求确定性、零改写）。"""
    system = (
        "你是简历结构解析器。把无结构的简历文本还原成标准 JSON 结构。"
        "只做还原，不做改写：文字内容必须与原文完全一致（可去掉多余空白与换行，但不得增删字词）。"
    )
    user = """【简历原始文本】
%s

【任务】
把它还原成下面的 JSON 结构。要求：
· 章节顺序、标题保持原文；缺失的章节不要凭空创造
· 每个条目（公司/学校/项目）拆成一个 block，含 heading（名称）与 meta（时间 / 地点 / 角色）
· 每条 bullet 单独成项，去掉行首的 · - • 等符号
· 每条 bullet 分配稳定 bid：章节序号 + "-b" + 该章节内 bullet 序号
  例如第 3 个章节的第 2 条 bullet = p3-b2
· 无法归类的短句放进对应章节的最后一条 bullet

%s""" % (str(raw_text or ""), _REBUILD_SPEC)
    return {"system": system, "user": user}


def build_rebuild_continue_prompt(raw_text, emitted):
    """截断后的续写提示词：只补上还没输出的章节。

    注意：chat() 每次都是全新的 messages（不做会话记忆），所以**必须重发原文**，
    否则模型看不到简历内容。这里不发"上一次的原始输出"——那比原文还长，
    发一批已完成的章节标题（sid+title）就够定位进度了。

    emitted: 已成功解析的章节列表 [{sid, title}, ...]（按出现顺序）
    """
    import re as _re
    done = "、".join("%s（%s）" % (s.get("sid"), s.get("title")) for s in emitted if s.get("sid"))
    next_idx = 1
    for s in emitted:
        m = _re.match(r"^p(\d+)$", str(s.get("sid") or ""))
        if m:
            next_idx = max(next_idx, int(m.group(1)) + 1)
    system = (
        "你是简历结构解析器。上一次输出因长度上限被截断了，现在要继续输出剩下的部分。"
        "只做还原，不做改写：文字内容必须与原文完全一致。"
    )
    user = """【简历原始文本】
%s

【已经输出过的章节】
%s

【任务】
接着输出**剩余的**章节，下一个章节的 sid 用 p%d。
要求：
· 不要重复输出上面列出的章节
· 上面列出的章节若内容不全，本轮也不要补，只往后写
· 如果原文已经没有更多章节，只输出 {"type":"done"}
· 格式要求与首次一致（NDJSON，一行一个对象，不要解释）

%s""" % (str(raw_text or ""), done or "（无）", next_idx, _REBUILD_SPEC)
    return {"system": system, "user": user}


STRUCT_SYSTEM = """\
你是简历版面结构分析器。用户会给你一份 PDF 简历里**按阅读顺序提取出来的每一行**，
行首 [n] 是行号。你要判断每一行扮演什么角色，然后**只输出行号**。

最重要的一条规则：**绝对不要输出任何原文文字**。
你只回答"第几行是什么"，文字由系统自己从原文取回来。
这样你就不可能写错字、不可能编造内容，也不必担心错别字 —— 那是取字环节的事，不是你的。

只输出一个 JSON 对象，不要 markdown 代码块，不要解释。
"""


def build_struct_prompt(worksheet, n_lines):
    """让模型只回答"第几行是什么"。

    为什么不让它直接输出内容：视觉/语言模型会认错字（实测把「–」读成「-」、
    把学校名多打一个字）。而字符从 PDF 文本层读出来时是 100% 准的。
    所以取字交给几何、判构交给模型，各干各擅长的事。
    """
    return """\
下面是这份简历的全部 %d 行（已按阅读顺序排好，空行表示视觉上的段落分隔）：

%s

请判断每一行的角色，输出**行号映射**。

行格式：[行号] 字号 粗细 缩进 颜色 「原文」

角色说明：
  name        姓名。通常在顶部，字号最大。
  intent      求职意向 / 职位标签。
  contacts    联系方式：电话、邮箱、城市、个人主页。
  section     章节标题：工作经历、项目经历、教育经历、科研课题、荣誉与技能…
              特征：字号大于正文，或颜色突出，或上下间距明显。
  entry.head  一条经历的名称（公司 / 项目 / 学校）。同一行里常连着日期和城市。
  entry.meta  该条经历单独占一行的角色 / 地点 / 日期。
  entry.intro 该条经历下不带项目符号的说明段。
  bullets     该条经历下的要点（缩进比条目名更深，或有项目符号）。

判断线索：
  · 字号明显大于正文 → 章节标题
  · 缩进比同组其他行更深 → 要点
  · 顶格、加粗、字号略大 → 条目标题
  · 空行 → 视觉上的段落分隔，通常意味着"新的一条经历"
  · 「照片」这类证件照占位文字 → 不属于简历内容，不要标

输出严格 JSON：
{
  "name": 行号,
  "intent": 行号或 null,
  "contacts": [行号, ...],
  "sections": [
    {"title": 行号,
     "entries": [
       {"head": [行号, ...], "meta": [行号, ...], "intro": [行号, ...], "bullets": [行号, ...]}
     ]}
  ]
}

要求：
  1. 所有行号必须在 1..%d 之间。
  2. 尽量覆盖每一行；确实不属于简历的（证件照占位、页码、页眉页脚）可以不标。
  3. 章节按简历上的实际顺序排列。
  4. **只输出行号，不要输出任何原文文字。**
""" % (n_lines, worksheet, n_lines)


def build_intent_prompt(jd_text, current_intent):
    """仅改写求职意向（简历头纳入评估，屏 10）。"""
    system = "你是资深简历顾问。只输出一行 JSON，不要任何其他文字。"
    user = f"""\
【目标岗位 JD】
{jd_text}

【当前求职意向】
{current_intent}

【任务】
给出一条「最贴合该 JD 的求职意向」表述。若当前表述已足够贴合，就原样返回。
要求：12–22 字，用 JD 的岗位语汇，不夸大（不要写"总监""专家""负责人"这类原文没支撑的层级）。

输出一行 JSON：
{{"type":"intent","intent":"……","reason":"为什么这么改"}}"""
    return {"system": system, "user": user}


# ================================================================ 指令式改写
# 面向「用户指定一段内容 + 一个修改要求，只让 AI 改这一段」（像 vibe coding 一样改简历）。
# 与另外两条 AI 路径的区别：
#   · 逐条评估  —— AI 主动、按固定逻辑改每条 bullet
#   · 深度重写  —— AI 主动、按固定档位重排整章
#   · 指令式改写 —— **用户主动**：改哪一段（scope）与改成什么样（instruction）都由用户给
#
# 复用同一条「数字守恒」红线：无论用户指令多自由，AI 都不能新增没写过的经历；
# 新增的量化数字必须逐条登记，由用户核实。

_SCOPE_SYSTEM = """\
你是资深简历顾问。现在用户会指定**一段内容**（一条要点 / 一段经历 / 一个章节），
并给出**具体的修改要求**，你要严格按要求只改写他指定的这一段，别的一个字都不要动。

铁律：
1. 范围严格：只改用户指定的这一范围，不要顺手动别的；输出结构与原结构一一对应
   （条目还是条目、要点还是要点，顺序不变；heading / meta 这类事实字段原样保留）。
2. 你是在"按他的要求改写表达"，不是"编造经历"。用户要你写得更量化，你可以补合理量级的数字，
   但不能凭空多出一家公司、一个岗位、一段他没做过的项目。
3. 输出必须是严格的 NDJSON，见下方格式要求，不要 markdown 代码块，不要任何解释性文字。"""

_SCOPE_OUTPUT_CONTRACT = """\
【输出格式 —— 必须严格遵守，否则结果无法被解析】
逐行输出 JSON（NDJSON），每行一个完整对象，不要代码块，不要数组，不要解释：

{"type":"scope","blocks":[{"heading":"条目名（原样保留，没有就空串）","meta":"时间/角色（原样保留，没有就空串）","intro":"该条目的简介（改写后；没有就空串）","subhead":"项目工作：（原样保留；没有就空串）","bullets":[{"ref":"该要点原来的 bid（新增或拆分出来的给空串）","text":"改写后的要点（不含符号）","ai_added_spans":["你新增的片段"],"inferred_facts":[{"span":"约 30%","why":"依据"}],"note":""}]}],"reason":"一句话说明你按指令做了什么"}

最后一行：
{"type":"done"}

【硬性说明】
· blocks 与**用户选中范围内的结构一一对应**（几个条目就几个 block，顺序不变）；
  heading / meta 是事实，原样保留，你的工作只在 bullets（以及 intro）上
· **保持三层结构**：intro（项目介绍）→ subhead（原样保留，通常是「项目工作：」）→ bullets（二级要点）。
  不要把「项目工作：」写成要点，也不要把二级要点提成一级
· 每条要点尽量沿用原来的 ref —— 前端据此知道"哪条对应哪条"；拆分或新增的要点 ref 给空串
· ai_added_spans：相对**这一段原文**新增的字词（原文照抄进数组），没有就空数组 []
· inferred_facts：你推断出来的、原文没有依据的量化值或事实，逐条给 span + why；没有就空数组 []
· 不要输出原文里根本没有的字段；不要输出范围之外的内容"""


def _scope_level_note(style):
    """按全局「AI 改写风格」折算这段改写的"力度边界"（指令仍是主驱动，这里只是兜底红线）。"""
    s = REWRITE_STYLES.get(style or "standard", REWRITE_STYLES["standard"])
    if s["eval"] == "free":          # 大胆 / 放手
        return ("【力度：可适度补全】允许补充合理量级的量化数字，"
                "但必须逐条登记到 inferred_facts；仍不许新增没写过的经历条目 / 关键资质。")
    if s["eval"] == "light":         # 标准
        return "【力度：轻度】只做词句级替换与结构调整，尽量不新增实义内容。"
    return ("【力度：保真】不新增原文没有的数字 / 动作 / 经历；可以换角度、可围绕 JD 术语重述，"
            "但每句话都要能在原文找到依据。")


def build_scope_rewrite_prompt(scope, instruction, jd_text="", style="standard", prev=None):
    """构造「按用户指令改写指定范围」的提示词。

    scope       : {"kind": "bullet"|"block"|"section", "content": {...原结构...}}
    instruction : 用户的自然语言修改要求
    jd_text     : 目标岗位 JD（可为空）
    style       : 全局 AI 改写风格（faithful|standard|bold|aggressive）
    prev        : 上一轮产物（迭代「再改改」时传入，让模型在上一版上继续改，而不是重头来）
    """
    jd_block = ("【目标岗位 JD】\n%s\n\n" % jd_text) if (jd_text or "").strip() else ""
    kind_name = {"bullet": "一条要点", "intro": "这段项目介绍", "block": "一段经历",
                 "section": "一个章节"}.get((scope or {}).get("kind"), "一段内容")
    intro_note = ("\n注意：这次范围只有 **intro（项目介绍）** —— 只重写 intro，"
                  "bullets 一律给空数组 []，不要顺手改要点。"
                  if (scope or {}).get("kind") == "intro" else "")
    prev_block = ""
    if prev:
        prev_block = ("\n【上一版（用户看了还不满意，请在这一版基础上继续改，不要推翻重来）】\n%s\n"
                      % json.dumps(prev, ensure_ascii=False))

    user = f"""\
{jd_block}{_scope_level_note(style)}

【用户要你改的内容 —— 范围：{kind_name}】
{json.dumps((scope or {}).get("content") or {}, ensure_ascii=False)}
{prev_block}
【用户的修改要求】
{instruction}

【你的任务】
严格按上面这条要求，只改写「用户要你改的内容」这一范围，输出一个 scope 行。
范围之外的内容一概不要输出、不要改。{intro_note}

{_SCOPE_OUTPUT_CONTRACT}"""
    return {"system": _SCOPE_SYSTEM, "user": user}
