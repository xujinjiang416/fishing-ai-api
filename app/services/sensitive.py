"""sensitive.py — 评论内容敏感词校验（前置拦截）

用于「评论发布」前置校验：命中敏感词时直接拦截，不落库。
词库策略：
- 内置一份演示词库（营销 / 违法 / 不文明常见词），保证开箱即用；
- 支持通过后台配置项 `sensitive_words`（英文逗号分隔）覆盖内置词库；
- 调用方传入额外词库时以「内置 + 额外」合并判断。

职责：
- 提供词库解析、合并与敏感词匹配的纯函数，供评论发布前置校验复用。

关键数据流：
  后台配置 sensitive_words → parse_words → build_wordlist（内置 + 自定义）
  → find_sensitive（大小写无关子串匹配）→ 命中词 / None

依赖：无外部服务，纯本地词库匹配。

失败降级策略：
- 文本为空返回 None（放行）；配置为空时使用内置词库；
- 匹配为简单子串包含，不抛异常。

注意事项：
- 当前为演示级词库与匹配，生产环境建议替换为专业词库 / 内容安全服务；
- 大小写不影响中文，主要覆盖英文词（QQ/qq）。
"""

# 内置演示敏感词库（示例用途，生产环境可替换为专业词库 / 第三方内容安全服务）
DEFAULT_SENSITIVE_WORDS: tuple[str, ...] = (
    # 营销 / 导流
    "加微信", "加微", "微信", "QQ", "代购", "微商", "刷单", "返现", "兼职",
    "私聊", "联系方式", "免费领", "点击链接", "扫码",
    # 违规 / 违法
    "赌博", "博彩", "彩票", "贷款", "套现", "诈骗", "传销", "走私", "枪支",
    "毒品", "发票代开",
    # 不文明 / 攻击
    "傻逼", "垃圾人", "去死", "滚蛋",
)


def parse_words(raw: str | None) -> list[str]:
    """把后台配置的字符串（英文逗号分隔）解析为词列表，去除空项。

    Args:
        raw: 英文逗号分隔的词库字符串，可为 None。

    Returns:
        list[str]: 去空白后的词列表；raw 为空时返回空列表。
    """
    if not raw:
        return []
    return [w.strip() for w in str(raw).split(",") if w.strip()]


def build_wordlist(extra: str | None = None) -> list[str]:
    """合并内置词库与后台自定义词库，去重后返回。

    Args:
        extra: 后台配置的英文逗号分隔词库，可为 None。

    Returns:
        list[str]: 内置词在前、自定义词在后的去重列表。
    """
    words = list(DEFAULT_SENSITIVE_WORDS)
    for w in parse_words(extra):
        if w not in words:
            words.append(w)
    return words


def find_sensitive(text: str | None, extra: str | None = None) -> str | None:
    """在文本中查找敏感词，命中返回命中的词，未命中返回 None。

    采用大小写无关匹配（中文无大小写影响，主要覆盖英文词如 QQ/qq）。

    Args:
        text: 待校验的评论内容，可为 None。
        extra: 后台自定义词库（英文逗号分隔）。

    Returns:
        str | None: 命中的原始敏感词；未命中或文本为空返回 None。
    """
    if not text:
        return None
    hay = str(text)
    low = hay.lower()
    for w in build_wordlist(extra):
        if w.lower() in low:
            return w
    return None
