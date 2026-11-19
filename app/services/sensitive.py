"""sensitive.py — 评论内容敏感词校验（前置拦截）

用于「评论发布」前置校验：命中敏感词时直接拦截，不落库。
词库策略：
- 内置一份演示词库（营销 / 违法 / 不文明常见词），保证开箱即用；
- 支持通过后台配置项 `sensitive_words`（英文逗号分隔）覆盖内置词库；
- 调用方传入额外词库时以「内置 + 额外」合并判断。
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
    """把后台配置的字符串（英文逗号分隔）解析为词列表，去除空项。"""
    if not raw:
        return []
    return [w.strip() for w in str(raw).split(",") if w.strip()]


def build_wordlist(extra: str | None = None) -> list[str]:
    """合并内置词库与后台自定义词库，去重后返回。"""
    words = list(DEFAULT_SENSITIVE_WORDS)
    for w in parse_words(extra):
        if w not in words:
            words.append(w)
    return words


def find_sensitive(text: str | None, extra: str | None = None) -> str | None:
    """在文本中查找敏感词，命中返回命中的词，未命中返回 None。

    采用大小写无关匹配（中文无大小写影响，主要覆盖英文词如 QQ/qq）。
    """
    if not text:
        return None
    hay = str(text)
    low = hay.lower()
    for w in build_wordlist(extra):
        if w.lower() in low:
            return w
    return None
