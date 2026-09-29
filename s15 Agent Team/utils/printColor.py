"""通用打印工具"""


def print_color(template: str, hex_color: str = "", **kwargs) -> None:
    """用 ANSI 转义码打印带颜色的文本。

    Args:
        template:  模板字符串，支持 ``{name}`` 占位符。
        hex_color: 十六进制颜色，如 ``"FF0000"`` 或 ``"#FF0000"``。
                   留空则输出无颜色。
        **kwargs:  传给模板的占位符值。
    """
    if hex_color:
        hex_color = hex_color.lstrip("#")
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        code = f"\033[38;2;{r};{g};{b}m"
        reset = "\033[0m"
        print(code + template.format(**kwargs) + reset)
    else:
        print(template.format(**kwargs))