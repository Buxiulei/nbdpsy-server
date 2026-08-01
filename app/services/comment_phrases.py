"""发布后评论互动的话术池与分配规则(纯数据 + 纯函数,不碰库不碰浏览器)。

话术池与分配规则的唯一出处是《评论互动话术池-v5》(NBDpsy/文档/评论互动话术池-v5.md),
v1-v4 已作废。**硬编码在代码里**而不是放配置:这批句子是合规红线的载体(见下),
改一个字都该走 code review + 测试,不该是运维随手能改的运行时配置。

两类评论(v5 第二节)::

    ① 笔记所属账号本人  → 一条预约引导「想要预约 XXX 可以私信留言」
    ② 其他矩阵号        → 各补一条本号定位的专业视角,**零引流指向**

**矩阵号话术零引流指向,这是业务硬约束,不是保守兜底。**
每个账号背后是不同的运营,他们各有绩效,从各自账号来的客户算各自的 KPI。矩阵号在同事
的笔记底下写「我们那边写过这个」「可以对照着看」这类话,是把读者往自己号带 —— 不是
「矩阵互导」,是**从同事那里抢单**。v4 曾经这么写过,已全部删除。
故:**转化引导(预约、私信)只能由笔记所属账号本人发出**;矩阵号只做增加互动与曝光。
**别再把引流话术改回来。**(同一条约束在 ``counselor_quote`` 的跨账号引用上也成立。)

矩阵号还有一条:**只能从本号定位对应的池子里取,不得串池**。七个号全部 ``NBDpsy``
开头,读者一眼看出是同一家;创伤号说出日常落地的话就露馅了,不如不发。

分配规则(v5 第六节)由 ``assign_phrases`` 实现:同篇内每号一条且各不相同、跨笔记也不
重复(按历史用量轮换,**不随机抽** —— 池子只有五六句,随机必然撞车)。
"""

from typing import Optional, Sequence

# 定位码。账号名字就是它的定位(用户已确认),故按名字里的关键词判定。
POS_TRAUMA = "trauma"
POS_RELATIONSHIP = "relationship"
POS_DAILY = "daily"
POS_POPSCI = "popsci"
POS_BRAND = "brand"

# 账号名 → 定位码的关键词表(顺序敏感:先匹配到的胜出)。
# 「聊创伤」必须排在「聊心理」前面无所谓(两者不互相包含),但顺序固定便于推理。
# 表里没有的账号一律归**品牌综合** —— 那批话最泛、最不可能说错,是安全的兜底;
# 绝不因为认不出定位就去取别的池子。
_POSITION_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("聊创伤", POS_TRAUMA),
    ("亲密关系", POS_RELATIONSHIP),
    ("好好生活", POS_DAILY),
    ("聊心理", POS_POPSCI),
)

# 笔记所属账号的预约引导 —— 带姓名版(v5 第四节)。``XXX`` 是咨询师姓名占位符。
OWNER_WITH_NAME: tuple[str, ...] = (
    "想要预约 XXX 可以私信留言",
    "想约 XXX 老师的可以私信留言～",
    "需要预约 XXX 的话，私信留言就好",
    "想找 XXX 老师聊聊的，私信留言",
    "XXX 老师的预约可以私信留言哦",
    "想要预约 XXX，私信留言告诉我们就行",
    "有需要预约 XXX 的，私信留言～",
    "想跟 XXX 老师聊的话，私信留言给我们",
)

# 笔记所属账号的预约引导 —— 不带姓名版(咨询师姓名三级降级都没拿到时用)。
OWNER_NO_NAME: tuple[str, ...] = (
    "想要预约可以私信留言",
    "有想聊聊的可以私信留言～",
    "需要预约的话私信留言就好",
    "想找老师聊聊的，私信留言",
    "预约可以私信留言哦",
    "有需要的话，私信留言给我们～",
)

# 姓名占位符:模板里出现它才需要姓名(用于选池与渲染)。
NAME_PLACEHOLDER = "XXX"

# 矩阵号按定位分池(v5 第五节)。**每一句都不含任何引流指向** —— 没有「我们那边」
# 「可以去看看」「主页」这类把读者往外带的表述。改这里前先读模块 docstring。
MATRIX_POOLS: dict[str, tuple[str, ...]] = {
    POS_TRAUMA: (
        "从创伤的角度补一句：这种反应往往是当年学会的自我保护",
        "这类模式常常能追溯到更早的经历",
        "身体记住的东西，比我们以为的更久",
        "这种「过度警觉」不是性格问题",
        "创伤视角看，这不是脆弱，是适应",
        "很多人是在安全下来之后才开始难受的",
    ),
    POS_RELATIONSHIP: (
        "这个模式带进亲密关系里也很常见",
        "关系里最容易复现的就是这一套",
        "从依恋的角度看，这里还有一层",
        "这种状态会影响到怎么跟人靠近",
        "关系视角补一点：不只是一个人的事",
        "越亲近的关系越容易触发这些",
    ),
    POS_DAILY: (
        "日常层面可以先从最小的一件事开始",
        "落到生活里，先把节奏调下来会容易些",
        "不用一步到位，能做一点是一点",
        "先照顾好吃饭和睡觉，其他慢慢来",
        "生活里能改的地方，往往比想的多",
        "状态不好的时候，降低标准也是一种照顾",
    ),
    POS_POPSCI: (
        "这个现象在心理学里有专门的说法",
        "补充一点背景：这类反应其实很普遍",
        "这不是个例，是有共性的",
        "从机制上讲，这里面是有道理的",
        "心理学上对这个有过不少讨论",
    ),
    POS_BRAND: (
        "写得很扎实",
        "这个话题值得多讲讲",
        "这篇可以反复看",
        "说到点子上了",
        "把话说明白了",
        "这个角度很少有人写",
    ),
}


def position_of(name: Optional[str], nickname: Optional[str] = None) -> str:
    """账号名 → 定位码。认不出一律归品牌综合(见 ``_POSITION_KEYWORDS`` 的说明)。

    先看内部展示名 ``name``,再看平台昵称 ``nickname`` —— 两者通常同步,但运营改过
    展示名时昵称还留着原文。
    """
    for text in (name or "", nickname or ""):
        for keyword, position in _POSITION_KEYWORDS:
            if keyword in text:
                return position
    return POS_BRAND


def owner_pool(counselor: Optional[str]) -> tuple[str, ...]:
    """笔记所属账号该用哪个池:拿到咨询师姓名用带姓名版,否则用通用版。"""
    return OWNER_WITH_NAME if (counselor or "").strip() else OWNER_NO_NAME


def render(template: str, counselor: Optional[str] = None) -> str:
    """模板 → 实际文案:把姓名占位符换成咨询师姓名。

    不带占位符的模板原样返回(矩阵号话术、不带姓名的预约引导都没有占位符)。
    姓名为空却传了带占位符的模板属调用方选错池,这里不猜、不静默吞 —— 直接返回原样,
    由 ``assign_phrases`` 的选池逻辑保证不会走到这一步。
    """
    name = (counselor or "").strip()
    if not name or NAME_PLACEHOLDER not in template:
        return template
    return template.replace(NAME_PLACEHOLDER, name)


def pick_phrase(
    pool: Sequence[str],
    used_counts: Optional[dict[str, int]] = None,
    taken: Optional[set[str]] = None,
) -> Optional[str]:
    """从 ``pool`` 里挑一句:本号历史上用得最少的那句;并列时按池内顺序取前面的。

    **不随机抽**:池子只有五六句,随机抽在小样本下必然撞车(v5 第六节点名了这一条)。
    按历史用量轮换是确定性的 —— 同样的输入永远同样的输出,也便于测试与事后复盘:
    第一次发 pool[0],第二次 pool[1] ……一轮用完再从 pool[0] 开始下一轮。

    参数:
        used_counts: 本号历史上每句用过几次(缺席视为 0 次)。
        taken: 本篇笔记里已经被别的号占掉的句子 —— 品牌综合定位有三个号共用一个池,
            不去重会出现同一篇底下两条一模一样的评论。
    参数都为空即"从没发过、也没人占"→ 取 ``pool[0]``。
    候选被占光返回 ``None``(该号这篇就不发,宁可少一条也不发重复的)。
    """
    counts = used_counts or {}
    occupied = taken or set()
    candidates = [(i, phrase) for i, phrase in enumerate(pool) if phrase not in occupied]
    if not candidates:
        return None
    return min(candidates, key=lambda item: (counts.get(item[1], 0), item[0]))[1]


def assign_phrases(
    owner: dict,
    matrix: Sequence[dict],
    counselor: Optional[str],
    history: Optional[dict[int, dict[str, int]]] = None,
) -> list[dict]:
    """给一篇笔记分配全部评论:所属账号一条预约引导 + 每个矩阵号一条本号定位的话。

    参数:
        owner: 笔记所属账号 ``{"account_id","name","nickname"?}``。
        matrix: 其余矩阵账号,同结构;顺序即分配顺序(调用方按 account_id 升序传)。
        counselor: 咨询师姓名(三级降级的结果,可为 None)。
        history: ``{account_id: {模板: 已用次数}}`` —— 跨笔记去重的依据。

    返回 ``[{"account_id","template","text","position"}, ...]``,**所属账号那条排第一**
    (v5 第六节:它要占住第一条评论位;排期时刻也由调用方据此拉开)。分不到句子的号
    直接不出现在结果里。
    """
    used = history or {}
    assigned: list[dict] = []
    # 所属账号:唯一允许发转化引导的角色(见模块 docstring 的绩效归属约束)
    pool = owner_pool(counselor)
    template = pick_phrase(pool, used.get(owner["account_id"]))
    if template is not None:
        assigned.append(
            {
                "account_id": owner["account_id"],
                "template": template,
                "text": render(template, counselor),
                "position": "owner",
            }
        )

    # 矩阵号:各取本号定位池里的一句,**不串池**;同篇内互不重复(品牌综合三个号共用一池)
    taken: set[str] = set()
    for account in matrix:
        position = position_of(account.get("name"), account.get("nickname"))
        template = pick_phrase(
            MATRIX_POOLS[position], used.get(account["account_id"]), taken
        )
        if template is None:
            continue
        taken.add(template)
        assigned.append(
            {
                "account_id": account["account_id"],
                "template": template,
                # 矩阵号话术不含姓名占位符,渲染是恒等的;走同一个出口是为了将来
                # 万一加了带变量的句子不会漏渲染
                "text": render(template),
                "position": position,
            }
        )
    return assigned
