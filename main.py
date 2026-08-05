# -*- coding: utf-8 -*-
"""
文件名称：main.py
功能描述：TUI 主入口（codex-cli 风格纯文本全屏布局）
         顶部横线自适应终端宽度，左侧菜单上下键切换，右侧显示对应内容，
         底部横线 + 版本号。零色块、零可视化组件，纯文本流。
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：Textual 侧边栏+内容区
    2026-08-03  简约化：去 emoji/富文本，纯文本风格
    2026-08-03  codex-cli 重构：扔掉 ListView，纯 Static 渲染全屏布局
"""

import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Optional

# 兼容打包（PyInstaller）时可能缺少 textual 的友好提示
try:
    from textual.app import App, ComposeResult
    from textual.widgets import Header, Footer, Static
    from textual.containers import Container, Vertical, VerticalScroll
    from textual.binding import Binding
    from textual.screen import ModalScreen
    from textual import work  # 异步任务装饰器,后台跑 Hashcat 不阻塞 UI
    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TEXTUAL_AVAILABLE = False
    work = None  # type: ignore


def _esc_markup(s: str) -> str:
    """转义字符串中的方括号以安全嵌入 rich markup
    rich markup 中 [ ] 是标签边界,路径等含方括号的内容需转义;
    反斜杠在 rich 15 中无需转义(转义会导致显示双反斜杠)。
    """
    return s.replace("[", "\\[").replace("]", "\\]")


# ======================================================================
# nushell 风格 box 渲染辅助函数
# 用于经典字典/社工字典生成子页面的右对齐 box 布局
# 复用 hardware_info 的显示宽度计算逻辑,本地定义避免跨模块耦合
# ======================================================================
def _disp_w(s: str) -> int:
    """计算字符串在终端中的显示宽度(全角字符占2列,半角占1列)"""
    width = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width


def _strip_markup(s: str) -> str:
    """去除 Textual markup 标签,返回纯文本(用于计算带 markup 的显示宽度)"""
    return re.sub(r'\[/?[^\]]*\]', '', s)


def _pad_w(text: str, target_width: int) -> str:
    """用空格将文本补齐到目标显示宽度(标签不计入显示宽度)"""
    current = _disp_w(_strip_markup(text))
    if current >= target_width:
        return text
    return text + ' ' * (target_width - current)


def _wrap_disp(text: str, max_width: int) -> list:
    """按显示宽度换行(中英文混排)，返回多行纯文本。

    全角字符按 2 列、半角按 1 列计算，避免长说明把 box 边框撑破。
    """
    lines: list = []
    current = ""
    width = 0
    for ch in text:
        ch_width = 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
        if width + ch_width > max_width and current:
            lines.append(current)
            current = ch
            width = ch_width
        else:
            current += ch
            width += ch_width
    if current:
        lines.append(current)
    return lines or [""]


def _fmt_bytes(num: int) -> str:
    """字节数格式化为人类可读字符串(自动选 KB/MB/GB/TB)
    例: 0 -> "0 B", 1536 -> "1.50 KB", 1073741824 -> "1.00 GB"
    """
    if num < 0:
        return "0 B"
    units = [("B", 1), ("KB", 1024), ("MB", 1024**2),
             ("GB", 1024**3), ("TB", 1024**4)]
    for unit, factor in units:
        if num < factor * 1024 or unit == "TB":
            if factor == 1:
                return f"{num} {unit}"
            return f"{num / factor:.2f} {unit}"
    return f"{num / 1024**4:.2f} TB"


def _disk_free_bytes(path: str) -> int:
    """获取指定路径所在盘符的剩余可用字节数
    :param path: 任意有效路径(目录或文件),自动取其所在盘符
    :return: 剩余字节数;路径无效返回 0
    """
    try:
        usage = shutil.disk_usage(path)
        return usage.free
    except Exception:  # noqa: BLE001
        return 0


def _nushell_box(lines_kv, title: str, width: int = 60,
                 color_border: str = "#00ff00", color_section: str = "#00ffff") -> list:
    """构建 nushell 风格 box(绿色边框 + 青色分区标题)
    :param lines_kv: 列表,每项为 (type, *args)
        ("section", "标题")           - 分区标题行
        ("kv", "key", "value_markup") - 键值行
        ("raw", "raw_markup")         - 原始行(不格式化)
        ("blank",)                    - 空行
    :param title: box 顶部标题
    :param width: box 宽度
    :param color_border: 边框颜色
    :param color_section: 分区标题颜色
    :return: 渲染后的行列表(每项为含 markup 的字符串)
    """
    box_width = max(50, width)
    inner = box_width - 4

    def _top(t: str) -> str:
        tw = _disp_w(f" {t} ")
        dashes = max(0, box_width - 2 - tw)
        return f"[{color_border}]╭ {t} " + "─" * dashes + "╮[/]"

    def _mid() -> str:
        return f"[{color_border}]├" + "─" * (box_width - 2) + "┤[/]"

    def _bot() -> str:
        return f"[{color_border}]╰" + "─" * (box_width - 2) + "╯[/]"

    def _row(content: str) -> str:
        padded = _pad_w(content, inner)
        return f"[{color_border}]│[/] {padded} [{color_border}]│[/]"

    out = [_top(title)]
    for item in lines_kv:
        kind = item[0]
        if kind == "section":
            out.append(_row(f"[{color_section}]{item[1]}[/]"))
        elif kind == "kv":
            key_padded = _pad_w(item[1], 12)
            out.append(_row(f"{key_padded} {item[2]}"))
        elif kind == "raw":
            out.append(_row(item[1]))
        elif kind == "blank":
            out.append(_row(""))
        elif kind == "mid":
            out.append(_mid())
    # 底部边框(必须有,否则 box 下面看起来空空的)
    out.append(_bot())
    return out


# 项目内核心模块
from core.path_manager import PathManager, ToolPaths
from core.archive_detector import ArchiveDetector, ArchiveType
from core.hash_extractor import HashExtractor, ExtractResult
from core.cracker import (
    HashcatExecutor, CrackConfig, CrackResult, CrackProgress,
    CrackStatus, AttackMode,
)
from core.dict_generator import (
    DictGenerator, GenConfig, GenResult, GenMode, SocialConfig,
)
from core.hardware_info import (
    collect_hardware_report, format_report_text,
    collect_realtime_stats,
)


def _app_base() -> Path:
    """返回应用资源根目录。

    打包运行时 PyInstaller 会把资源放进 _internal 并通过 sys._MEIPASS 指向它；
    开发运行时返回 main.py 所在的项目根目录。
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent


# ======================================================================
# TUI 应用主体（codex-cli 风格纯 Static 渲染，上下键切换）
# 配色参考 nushell 默认 dark theme
# ======================================================================
if _TEXTUAL_AVAILABLE:

    # nushell 配色常量（参考 nushell default_config.nu 的 color_config）
    # 表头/字符串：绿；数字/分隔线：蓝；强调/浮点：紫；路径/编号：青；布尔：黄；错误：红
    C_NS_GREEN  = "#00ff00"   # 表头、字符串、选中标记
    C_NS_BLUE   = "#82cfff"   # 分隔线、数字、表头(部分)
    C_NS_PURPLE = "#ff00ff"   # 强调、浮点、日期
    C_NS_CYAN   = "#00ffff"   # 路径、编号
    C_NS_YELLOW = "#ffff00"   # 布尔、警告
    C_NS_RED    = "#ff0000"   # 错误、失败
    C_NS_GRAY   = "#808080"   # 暗灰分隔
    C_NS_WHITE  = "#ffffff"   # 默认文本

    # 工具自检子页面操作项（模块级常量，供 CrackerApp 类内引用）
    # 进入工具自检后左侧显示这些操作项
    _TOOLS_SUB_ITEMS = [
        ("sub_recheck",  "1. 重新检测"),
        ("sub_download", "2. 下载工具（待命）"),
        ("sub_back",     "3. 返回上一层"),
    ]

    # 字典生成二级菜单项（进入"字典生成"后显示的列表）
    # 结构：(item_id, 显示文字) —— 与主菜单结构一致,便于复用渲染逻辑
    _DICT_MENU_ITEMS = [
        ("dict_classic", "1. 经典字典生成"),
        ("dict_social",  "2. 社工字典生成"),
        ("dict_mask",    "3. 掩码字典生成"),
        ("dict_other",   "4. 其他字典生成"),
        ("dict_help",    "5. 帮助使用说明"),
        ("dict_back",    "6. 返回上一层"),
    ]

    # 密码破解二级菜单项(进入"密码破解"后显示的列表)
    # 结构同字典二级菜单:(item_id, 显示文字)
    # 四种攻击模式 + 帮助 + 返回,编号与字典菜单对齐
    _CRACK_MENU_ITEMS = [
        ("crack_dict",   "1. 字典攻击"),
        ("crack_mask",   "2. 掩码攻击"),
        ("crack_rule",   "3. 字典加规则"),
        ("crack_brute",  "4. 暴力穷举"),
        ("crack_help",   "5. 帮助说明"),
        ("crack_back",   "6. 返回上一层"),
    ]

    # 经典字典生成子页面操作项（原字典生成功能,整体挪到经典字典生成下）
    # 类型：toggle=勾选项, action=执行项, input=输入项（回车进入输入模式）
    _DICT_SUB_ITEMS = [
        ("dict_lower",    "toggle", "1. 小写字母"),
        ("dict_upper",    "toggle", "2. 大写字母"),
        ("dict_digit",    "toggle", "3. 数字"),
        ("dict_special",  "toggle", "4. 特殊字符"),
        ("dict_single",   "toggle", "5. 单字符密码（1位）"),
        ("dict_min_len",  "input",  "6. 最小长度"),
        ("dict_max_len",  "input",  "7. 最大长度"),
        ("dict_out_dir",  "input",  "8. 输出目录"),
        ("dict_max_lines","input",  "9. 生成数量"),
        ("dict_gen",      "action", "10. 开始生成"),
        ("dict_help",     "action", "11. 帮助说明"),
        ("dict_back",     "action", "12. 返回上一层"),
    ]

    # 掩码字典生成子页面操作项
    # 结构同经典字典:toggle=勾选, action=执行, input=输入(回车进入输入模式)
    _DICT_MASK_ITEMS = [
        ("mask_input",    "input",  "1. 输入掩码"),
        ("mask_preset",   "action", "2. 快速模板"),
        ("mask_max_lines","input",  "3. 生成数量"),
        ("mask_out_dir",  "input",  "4. 输出目录"),
        ("mask_gen",      "action", "5. 开始生成"),
        ("mask_help",     "action", "6. 帮助说明"),
        ("mask_back",     "action", "7. 返回上一层"),
    ]

    # 字典攻击子页面操作项(密码破解 → 字典攻击)
    # 结构同掩码字典:input=输入(回车进入输入模式), action=执行
    # 字段说明:
    #   - crack_dict_archive: 加密压缩包路径(必填)
    #   - crack_dict_dict:    字典文件路径(必填,多个用逗号分隔)
    #   - crack_dict_workload:工作负载 1~4(默认3,影响GPU占用率)
    _CRACK_DICT_ITEMS = [
        ("crack_dict_drop",    "action", "0. 拖入文件(自动识别)"),
        ("crack_dict_archive", "input",  "1. 压缩包路径"),
        ("crack_dict_dict",    "input",  "2. 字典文件路径"),
        ("crack_dict_workload","input",  "3. 工作负载(1-4)"),
        ("crack_dict_device",  "input",  "4. 设备(auto/gpu/cpu)"),
        ("crack_dict_run",     "action", "5. 开始破解"),
        ("crack_dict_help",    "action", "6. 帮助说明"),
        ("crack_dict_back",    "action", "7. 返回上一层"),
    ]

    # 掩码快速模板列表(常用密码模式,一键填入)
    _MASK_PRESETS = [
        ("?d?d?d?d",          "纯数字4位(0000~9999)"),
        ("?d?d?d?d?d?d",      "纯数字6位(000000~999999)"),
        ("?d?d?d?d?d?d?d?d",  "纯数字8位(00000000~99999999)"),
        ("?l?l?l?l",          "小写字母4位"),
        ("?l?l?l?l?d?d?d?d",  "小写4位+数字4位"),
        ("?u?l?l?l?d?d",      "大写首字母+小写3位+数字2位"),
        ("pass?d?d?d",        "pass前缀+数字3位"),
        ("?d?d?d?d?d?d@?l?l", "数字6位+@+字母2位"),
    ]

    # 掩码攻击子页面操作项(密码破解 → 掩码攻击)
    _CRACK_MASK_ITEMS = [
        ("crack_mask_drop",    "action", "0. 拖入文件(自动识别)"),
        ("crack_mask_archive", "input",  "1. 压缩包路径"),
        ("crack_mask_expr",    "input",  "2. 掩码表达式"),
        ("crack_mask_preset",  "action", "3. 快速模板"),
        ("crack_mask_workload","input",  "4. 工作负载(1-4)"),
        ("crack_mask_device",  "input",  "5. 设备(auto/gpu/cpu)"),
        ("crack_mask_run",     "action", "6. 开始破解"),
        ("crack_mask_help",    "action", "7. 帮助说明"),
        ("crack_mask_back",    "action", "8. 返回上一层"),
    ]

    # 字典加规则子页面操作项(密码破解 → 字典加规则)
    _CRACK_RULE_ITEMS = [
        ("crack_rule_drop",    "action", "0. 拖入文件(自动识别)"),
        ("crack_rule_archive", "input",  "1. 压缩包路径"),
        ("crack_rule_dict",    "input",  "2. 字典文件路径"),
        ("crack_rule_pick",    "action", "3. 选择规则文件"),
        ("crack_rule_file",    "input",  "4. 规则文件路径(手动)"),
        ("crack_rule_workload","input",  "5. 工作负载(1-4)"),
        ("crack_rule_device",  "input",  "6. 设备(auto/gpu/cpu)"),
        ("crack_rule_run",     "action", "7. 开始破解"),
        ("crack_rule_help",    "action", "8. 帮助说明"),
        ("crack_rule_back",    "action", "9. 返回上一层"),
    ]

    # 暴力穷举子页面操作项(密码破解 → 暴力穷举)
    _CRACK_BRUTE_ITEMS = [
        ("crack_brute_drop",    "action", "0. 拖入文件(自动识别)"),
        ("crack_brute_archive", "input",  "1. 压缩包路径"),
        ("crack_brute_lower",   "toggle", "2. 小写字母"),
        ("crack_brute_upper",   "toggle", "3. 大写字母"),
        ("crack_brute_digit",   "toggle", "4. 数字"),
        ("crack_brute_special", "toggle", "5. 特殊字符"),
        ("crack_brute_custom",  "input",  "6. 自定义字符集(可选)"),
        ("crack_brute_min_len", "input",  "7. 最小长度"),
        ("crack_brute_max_len", "input",  "8. 最大长度"),
        ("crack_brute_preset",  "action", "9. 快速模板"),
        ("crack_brute_workload","input",  "10. 工作负载(1-4)"),
        ("crack_brute_device",  "input",  "11. 设备(auto/gpu/cpu)"),
        ("crack_brute_run",     "action", "12. 开始破解"),
        ("crack_brute_help",    "action", "13. 帮助说明"),
        ("crack_brute_back",    "action", "14. 返回上一层"),
    ]

    # 三个新攻击模式的页面标识，统一走通用引擎
    _CRACK_MODE_PAGES = ("crack_mask", "crack_rule", "crack_brute")

    # 内置规则文件预设(从 bin/windows/hashcat/rules 中挑选常用规则)
    _RULE_PRESETS = [
        ("best66.rule",         str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "best66.rule")),
        ("rockyou-30000.rule",  str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "rockyou-30000.rule")),
        ("dive.rule",           str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "dive.rule")),
        ("d3ad0ne.rule",        str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "d3ad0ne.rule")),
        ("toggles5.rule",       str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "toggles5.rule")),
        ("leetspeak.rule",      str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "leetspeak.rule")),
    ]

    # 规则文件中文说明(自动扫描 bin/windows/hashcat/rules 后展示给用户)
    _RULE_DESCRIPTIONS = {
        "best66.rule": "常用高频规则。覆盖大小写、数字后缀、首尾追加等最常见变形，速度快，适合日常快速破解。",
        "combinator.rule": "组合拼接规则。把字典词与常见字符/词缀拼接，覆盖“前后缀+单词”类密码。",
        "d3ad0ne.rule": "高强度综合规则。包含大量替换、大小写、数字组合，覆盖面广，速度较慢，适合时间充裕时使用。",
        "dive.rule": "深度变形规则。规则数量极大，覆盖非常广，适合字典不够用或追求高命中率时使用，速度最慢。",
        "generated.rule": "自动生成规则。由规则生成器产出，覆盖常见变形组合，通用性强，速度中等。",
        "generated2.rule": "自动生成规则扩展版。比 generated 覆盖更多组合，速度更慢，适合进一步扩大候选集。",
        "Incisive-leetspeak.rule": "Leetspeak 黑客文替换规则。把 a→4、e→3、o→0 等字符替换成数字/符号，并叠加常见变形。",
        "InsidePro-HashManager.rule": "InsidePro HashManager 通用规则。覆盖常规大小写、数字、替换组合，偏向通用口令测试。",
        "InsidePro-PasswordsPro.rule": "InsidePro PasswordsPro 规则。偏重数字和大小写混合变形，适合爆破常见业务密码。",
        "leetspeak.rule": "纯 Leetspeak 替换规则。a→4、e→3、o→0、s→5 等字符替换，命中“黑客文”风格密码。",
        "oscommerce.rule": "站点专项规则。针对 osCommerce 类站点常见密码习惯生成，适合特定站点场景。",
        "rockyou-30000.rule": "3 万条高频规则。基于真实泄露密码库整理，覆盖面极广，速度慢，适合最终大范围扩展。",
        "specific.rule": "精选少量通用规则。只做高频基础变形，速度快，适合快速试探。",
        "stacking58.rule": "堆叠 58 条高频规则。兼顾速度与覆盖率，适合中等规模扩展。",
        "T0XlC.rule": "T0XlC 基础规则集。包含常见大小写、数字、替换组合，通用型选择。",
        "T0XlCv2.rule": "T0XlC 规则集第 2 版。比基础版覆盖更多组合，速度较慢，适合深入扩展。",
        "T0XlC_3_rule.rule": "T0XlC 3 号规则。针对特定组合场景的补充规则，与 T0XlC 系列配合使用。",
        "T0XlC_insert_HTML_entities_0_Z.rule": "T0XlC HTML 实体规则。把常见字符替换为 HTML 实体编码变体，适合特殊编码密码。",
        "T0XlC-insert_00-99_1950-2050_toprules_0_F.rule": "T0XlC 年份/数字插入规则。插入 00-99 与 1950-2050 年份组合，命中带生日/年份的密码。",
        "T0XlC-insert_space_and_special_0_F.rule": "T0XlC 空格/特殊字符规则。在单词前后或中间插入空格和特殊符号，扩大候选集。",
        "T0XlC-insert_top_100_passwords_1_G.rule": "T0XlC Top100 弱密码规则。把常用弱密码作为片段插入，命中“单词+弱密码”组合。",
        "toggles1.rule": "大小写切换 1 级。最轻量的大小写变化，速度快，适合只做少量大写变体。",
        "toggles2.rule": "大小写切换 2 级。在 1 级基础上增加更多切换位置，速度较快。",
        "toggles3.rule": "大小写切换 3 级。覆盖较多种大小写组合，速度中等。",
        "toggles4.rule": "大小写切换 4 级。组合更多，适合爆破混合大小写密码，速度较慢。",
        "toggles5.rule": "大小写切换 5 级。最全的大小写切换组合，覆盖最多，速度最慢。",
        "top10_2025.rule": "2025 年 Top10 高频规则。基于最新常见密码变形整理，适合最新字典库快速扩展。",
        "unix-ninja-leetspeak.rule": "Unix-ninja Leetspeak 规则。专业 Leetspeak 替换集合，覆盖大量字符替换组合。",
    }

    # ==================================================================
    # 帮助内容定义(5 个场景:首页/字典菜单/经典字典/社工字典/掩码字典)
    # 每个场景一份 sections 列表,传入 HelpScreen 渲染
    # 格式:("section",标题) / ("kv",键,值) / ("raw",文本) / ("blank",)
    # ==================================================================
    _HELP_ABOUT = [
        ("section", "软件名称"),
        ("raw", "ArchiveCracker 压缩包密码爆破工具"),
        ("raw", "版本 V 0.1"),
        ("blank",),
        ("section", "软件简介"),
        ("raw", "面向压缩包密码恢复的批量验证工具"),
        ("raw", "支持 ZIP/RAR/7Z 等常见格式"),
        ("raw", "内置 Hashcat 引擎,可调用 GPU/CPU"),
        ("blank",),
        ("section", "核心功能"),
        ("kv", "密码破解", "字典/掩码/字典加规则/暴力穷举"),
        ("kv", "字典生成", "经典/社工/掩码字典生成"),
        ("kv", "工具自检", "检查 Hashcat/John 等依赖"),
        ("blank",),
        ("section", "技术说明"),
        ("raw", "TUI 界面基于 Textual 构建"),
        ("raw", "哈希提取由 John the Ripper 完成"),
        ("raw", "破解执行由 Hashcat 完成"),
        ("raw", "实时进度与历史记录在页面内展示"),
        ("blank",),
        ("section", "使用提示"),
        ("raw", "文件可直接拖入终端自动识别"),
        ("raw", "破解中按 ESC 可中断"),
        ("raw", "退出软件请选「退出软件」或按 Ctrl+Q"),
    ]

    _HELP_MAIN = [
        ("section", "工具简介"),
        ("raw", "压缩包密码爆破工具"),
        ("raw", "支持 ZIP/RAR/7Z 等压缩包格式"),
        ("raw", "内置 Hashcat 引擎,GPU/CPU 加速"),
        ("blank",),
        ("section", "功能说明"),
        ("kv", "密码破解", "字典/掩码/规则/暴力四种模式"),
        ("kv", "字典生成", "经典/社工/掩码三种生成方式"),
        ("kv", "工具自检", "检查 Hashcat/7-Zip 等依赖"),
        ("blank",),
        ("section", "密码破解"),
        ("kv", "字典攻击", "用字典逐行试,速度最快"),
        ("kv", "掩码攻击", "按位置规则精准穷举"),
        ("kv", "字典加规则", "字典变形扩面,命中率高"),
        ("kv", "暴力穷举", "字符集+长度全空间穷举"),
        ("blank",),
        ("section", "字典生成"),
        ("kv", "经典字典", "字符集笛卡尔积生成"),
        ("kv", "社工字典", "按个人信息组合生成"),
        ("kv", "掩码字典", "按占位符模式生成"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "W/S/上下键", "切换菜单项"),
        ("kv", "A/D", "返回上一层/进入确认"),
        ("kv", "J/K", "右侧内容下翻/上翻"),
        ("kv", "空格/回车", "进入/确认/执行"),
        ("kv", "ESC", "返回上一层/取消输入"),
        ("kv", "Ctrl+1/2/3/4", "快速跳转破解/字典/自检/帮助"),
        ("kv", "Ctrl+Q", "退出程序"),
        ("blank",),
        ("section", "破解提示"),
        ("raw", "文件可直接拖入终端自动识别"),
        ("raw", "掩码 ?d/?l/?u/?s/?a 含义见掩码攻击页"),
        ("raw", "规则文件可在字典加规则页选择,带中文说明"),
        ("raw", "破解历史很多时按 J 下翻查看"),
        ("raw", "破解中按 ESC 可中断任务"),
    ]

    _HELP_DICT = [
        ("section", "字典模式说明"),
        ("kv", "经典字典", "字符集组合(笛卡尔积)"),
        ("kv", "社工字典", "基于个人信息组合生成"),
        ("kv", "掩码字典", "按占位符模式精准生成"),
        ("kv", "其他字典", "功能开发中"),
        ("blank",),
        ("section", "适用场景"),
        ("raw", "经典:全空间爆破,覆盖面广"),
        ("raw", "社工:针对性爆破,命中率高"),
        ("raw", "掩码:已知密码模式,精准高效"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "上下键", "选择字典模式"),
        ("kv", "回车", "进入对应模式子页面"),
        ("kv", "ESC", "返回主菜单"),
    ]

    # 密码破解帮助内容:对应 _CRACK_MENU_ITEMS 的 5. 帮助说明
    # 覆盖四种攻击模式原理、速度对比、操作方式
    _HELP_CRACK = [
        ("section", "攻击模式说明"),
        ("kv", "字典攻击", "用字典文件逐行试"),
        ("kv", "掩码攻击", "按位置规则穷举"),
        ("kv", "字典加规则", "字典变体(大小写/加数字)"),
        ("kv", "暴力穷举", "无脑全试,最慢"),
        ("blank",),
        ("section", "速度对比"),
        ("raw", "字典 > 掩码 > 字典+规则 > 暴力"),
        ("raw", "字典最快,暴力最慢"),
        ("blank",),
        ("section", "适用场景"),
        ("raw", "字典:有社工字典或常见弱口令"),
        ("raw", "掩码:已知密码结构(如4位数字)"),
        ("raw", "字典+规则:字典不够用时扩面"),
        ("raw", "暴力:无任何线索,兜底方案"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "上下键", "选择攻击模式"),
        ("kv", "回车", "进入对应模式子页面"),
        ("kv", "ESC", "返回主菜单"),
    ]

    # 字典攻击帮助内容:对应 _CRACK_DICT_ITEMS 的 6. 帮助说明
    # 覆盖字典攻击原理、配置项说明、工作负载建议、操作方式
    _HELP_CRACK_DICT = [
        ("section", "字典攻击说明"),
        ("raw", "用字典文件逐行试密码,GPU加速"),
        ("raw", "字典质量决定成败,速度最快"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "0", "拖入文件(自动识别)"),
        ("kv", "1", "压缩包路径(必填)"),
        ("kv", "2", "字典文件路径(必填,可多个)"),
        ("kv", "3", "工作负载1-4(默认3)"),
        ("kv", "4", "开始破解"),
        ("blank",),
        ("section", "工作负载说明"),
        ("raw", "1=低(后台任务,不卡顿)"),
        ("raw", "2=中低(轻度影响)"),
        ("raw", "3=高(默认,显卡满载)"),
        ("raw", "4=极致(系统可能卡顿)"),
        ("blank",),
        ("section", "多字典文件"),
        ("raw", "多个字典用英文逗号分隔"),
        ("raw", "例: a.txt,b.txt,c.txt"),
        ("raw", "hashcat会依次使用每个字典"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "W/S/上下键", "选择配置项"),
        ("kv", "A/D", "返回/确认"),
        ("kv", "空格/回车", "输入/执行对应项"),
        ("kv", "ESC", "返回密码破解菜单"),
        ("blank",),
        ("section", "拖入文件(推荐)"),
        ("raw", "选「0.拖入文件」回车进入等待"),
        ("raw", "将文件直接拖入终端窗口"),
        ("raw", "系统按扩展名自动分类:"),
        ("raw", "  zip/rar/7z → 压缩包字段"),
        ("raw", "  txt/dic/lst → 字典字段"),
        ("raw", "可一次拖多个文件,字典自动累加"),
        ("raw", "ESC 取消拖入等待"),
    ]

    _HELP_CRACK_MASK = [
        ("section", "掩码攻击说明"),
        ("raw", "按位置规则精准穷举,速度最快"),
        ("blank",),
        ("section", "掩码占位符"),
        ("kv", "?d", "数字 0-9"),
        ("kv", "?l", "小写字母 a-z"),
        ("kv", "?u", "大写字母 A-Z"),
        ("kv", "?s", "常见特殊字符"),
        ("kv", "?a", "所有可打印字符"),
        ("kv", "?1", "自定义字符集"),
        ("raw", "输入 ? 后按 Tab 可快速补全 ?d"),
        ("blank",),
        ("section", "示例"),
        ("raw", "?d?d?d?d = 0000-9999"),
        ("raw", "pass?d?d?d = pass000-999"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "1", "压缩包路径(必填)"),
        ("kv", "2", "掩码表达式(必填)"),
        ("kv", "3", "快速模板循环选择"),
        ("kv", "6", "开始破解"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "W/S/上下键", "选择配置项"),
        ("kv", "A/D", "返回/确认"),
        ("kv", "空格/回车", "输入/执行对应项"),
        ("kv", "ESC", "返回密码破解菜单"),
    ]

    _HELP_CRACK_RULE = [
        ("section", "字典加规则说明"),
        ("raw", "字典逐行变形(加数字/大小写/字符替换)"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "1", "压缩包路径(必填)"),
        ("kv", "2", "字典文件路径(必填,可多个)"),
        ("kv", "3", "选择规则文件(自动读取+中文说明)"),
        ("kv", "4", "规则文件路径(手动填写)"),
        ("kv", "7", "开始破解"),
        ("blank",),
        ("section", "常用规则"),
        ("raw", "best66: 高频变形,快速"),
        ("raw", "rockyou-30000: 全量变形,最全"),
        ("raw", "dive: 深度变形,耗时长"),
        ("raw", "规则选择弹窗中每个规则都有中文说明"),
        ("blank",),
        ("section", "拖入文件"),
        ("raw", "zip/rar/7z → 压缩包字段"),
        ("raw", "txt/dic/lst → 字典字段"),
        ("raw", "rule → 规则文件字段"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "W/S/上下键", "选择配置项"),
        ("kv", "A/D", "返回/确认"),
        ("kv", "空格/回车", "输入/执行对应项"),
        ("kv", "ESC", "返回密码破解菜单"),
    ]

    _HELP_CRACK_BRUTE = [
        ("section", "暴力穷举说明"),
        ("raw", "按字符集组合穷举,覆盖最广"),
        ("raw", "最小/最大长度之间自动增量"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "2-5", "勾选字符集(可多选)"),
        ("kv", "6", "自定义字符集(可选)"),
        ("kv", "7", "最小长度"),
        ("kv", "8", "最大长度"),
        ("kv", "9", "快速模板循环选择"),
        ("kv", "12", "开始破解"),
        ("blank",),
        ("section", "注意"),
        ("raw", "长度越大组合数爆炸式增长"),
        ("raw", "建议先小长度/小字符集测试"),
        ("blank",),
        ("section", "操作方式"),
        ("kv", "W/S/上下键", "选择配置项"),
        ("kv", "A/D", "返回/确认"),
        ("kv", "空格/回车", "输入/执行对应项"),
        ("kv", "ESC", "返回密码破解菜单"),
    ]

    _HELP_CLASSIC = [
        ("section", "经典字典说明"),
        ("raw", "基于字符集笛卡尔积生成密码"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "1-5", "勾选字符集(可多选)"),
        ("kv", "6-7", "设置密码长度范围"),
        ("kv", "8", "设置输出目录路径"),
        ("kv", "9", "生成数量(0=全部)"),
        ("kv", "10", "开始生成字典"),
        ("blank",),
        ("section", "右侧信息"),
        ("kv", "预估数量", "候选密码总数"),
        ("kv", "预估大小", "输出文件预估大小"),
        ("kv", "磁盘剩余", "输出盘符剩余空间"),
        ("blank",),
        ("section", "安全提示"),
        ("raw", "数量超1千万会二次确认"),
        ("raw", "磁盘不足会阻断生成"),
        ("raw", "生成数量限制可截断输出"),
    ]

    _HELP_SOCIAL = [
        ("section", "社工字典说明"),
        ("raw", "基于目标个人信息组合生成密码"),
        ("blank",),
        ("section", "信息字段"),
        ("kv", "1-13", "基础信息(姓名/生日/手机等)"),
        ("kv", "14-18", "工作/学校信息"),
        ("kv", "19-23", "家庭/其他信息"),
        ("kv", "24-27", "习惯/自定义后缀"),
        ("kv", "28", "输出目录路径"),
        ("kv", "29", "开始生成字典"),
        ("blank",),
        ("section", "使用建议"),
        ("raw", "信息越全,命中率越高"),
        ("raw", "字段可留空,不影响生成"),
        ("raw", "自动去重,不会重复输出"),
    ]

    _HELP_MASK = [
        ("section", "掩码字典说明"),
        ("raw", "按 Hashcat 掩码语法生成密码"),
        ("blank",),
        ("section", "占位符"),
        ("kv", "?l", "小写字母 a-z (26个)"),
        ("kv", "?u", "大写字母 A-Z (26个)"),
        ("kv", "?d", "数字 0-9 (10个)"),
        ("kv", "?s", "特殊字符 (33个)"),
        ("kv", "?a", "全部可打印字符 (95个)"),
        ("blank",),
        ("section", "配置项"),
        ("kv", "1", "输入掩码表达式"),
        ("kv", "2", "快速模板(回车循环切换)"),
        ("kv", "3", "生成数量(0=全部)"),
        ("kv", "4", "输出目录路径"),
        ("kv", "5", "开始生成字典"),
        ("blank",),
        ("section", "示例"),
        ("raw", "?d?d?d?d → 0000~9999"),
        ("raw", "pass?d?d → pass00~pass99"),
        ("raw", "?l?l?d?d → aa00~zz99"),
    ]

    # 输出目录值换行阈值:超过该长度则单独换行缩进显示,避免在窄菜单里折行错乱
    _DICT_LONG_VAL_THRESHOLD = 20

    # 字符集常量（供字典生成勾选时拼接使用）
    _CHARSET_LOWER   = "abcdefghijklmnopqrstuvwxyz"
    _CHARSET_UPPER   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _CHARSET_DIGIT   = "0123456789"
    _CHARSET_SPECIAL = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"

    # 生成数量过多确认阈值:预估行数超过此值时弹窗二次确认
    # 1千万行是一个平衡点:纯数字4位1万行、6位100万行、8位1亿行
    _DICT_LARGE_COUNT_THRESHOLD: int = 10_000_000

    # 社工字典生成子页面操作项
    # 所有字段均为 input 类型(回车进入输入模式),最后两项为执行项
    # 字段顺序按:基础信息 → 工作/学校 → 家庭/其他 → 习惯/其他 → 执行
    _DICT_SOCIAL_ITEMS = [
        # 基础信息
        ("soc_name_cn",      "input",  "1.  中文姓名"),
        ("soc_name_pinyin",  "input",  "2.  拼音全拼"),
        ("soc_name_en",      "input",  "3.  英文名"),
        ("soc_nickname",     "input",  "4.  昵称/网名"),
        ("soc_birth_year",   "input",  "5.  生日年份"),
        ("soc_birth_month",  "input",  "6.  生日月份"),
        ("soc_birth_day",    "input",  "7.  生日日期"),
        ("soc_birth_full",   "input",  "8.  完整生日"),
        ("soc_phone",        "input",  "9.  手机号"),
        ("soc_qq",           "input",  "10. QQ号"),
        ("soc_wechat",       "input",  "11. 微信号"),
        ("soc_email",        "input",  "12. 邮箱"),
        ("soc_id_card",      "input",  "13. 身份证号"),
        # 工作/学校
        ("soc_company",      "input",  "14. 公司名"),
        ("soc_position",     "input",  "15. 职位"),
        ("soc_employee_id",  "input",  "16. 工号"),
        ("soc_school",       "input",  "17. 学校名"),
        ("soc_school_year",  "input",  "18. 入学年份"),
        # 家庭/其他
        ("soc_spouse_name",  "input",  "19. 配偶姓名"),
        ("soc_child_name",   "input",  "20. 子女姓名"),
        ("soc_pet_name",     "input",  "21. 宠物名"),
        ("soc_anniversary",  "input",  "22. 纪念日"),
        ("soc_car_plate",    "input",  "23. 车牌号"),
        # 习惯/其他
        ("soc_favorite_words", "input", "24. 喜好词汇(逗号分隔)"),
        ("soc_lucky_numbers",  "input", "25. 幸运数字(逗号分隔)"),
        ("soc_area_code",      "input", "26. 地区区号"),
        ("soc_common_suffixes","input", "27. 自定义后缀(逗号分隔)"),
        # 通用
        ("soc_out_dir",      "input",  "28. 输出目录"),
        # 执行
        ("soc_gen",          "action", "29. 开始生成"),
        ("soc_help",         "action", "30. 帮助说明"),
        ("soc_back",         "action", "31. 返回上一层"),
    ]

    class ToolCheckResultScreen(ModalScreen):
        """工具检测完成弹窗（ModalScreen）
        显示检测结果摘要 + 失败项列表，按 回车 或 ESC 关闭
        """

        # 绑定回车和 ESC 都关闭弹窗（priority 确保优先于子组件拦截）
        BINDINGS = [
            Binding("enter",   "close_modal", "关闭", show=False, priority=True),
            Binding("escape",  "close_modal", "关闭", show=False, priority=True),
            Binding("space",   "close_modal", "关闭", show=False, priority=True),
        ]

        def __init__(self, passed: list, failed: list):
            """
            :param passed: 通过项名称列表
            :param failed: 失败项名称列表
            """
            super().__init__()
            self._passed = passed
            self._failed = failed

        def compose(self) -> ComposeResult:
            """构建弹窗内容（nushell 风格：蓝色边框 + 绿色表头 + 红色失败项）"""
            total = len(self._passed) + len(self._failed)
            # 顶部边框（nushell 表格顶边框，蓝色）
            lines = [f"[{C_NS_BLUE}]╭──────────────────────────────╮[/]"]
            # 标题行（绿色加粗）
            title = f"检测完成（{len(self._passed)}/{total} 通过）"
            lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN} bold]{title:<24}[/] [{C_NS_BLUE}]│[/]")
            lines.append(f"[{C_NS_BLUE}]├──────────────────────────────┤[/]")

            # 失败项列表（红色）
            if self._failed:
                lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_RED}]以下工具未检测到：[/]     [{C_NS_BLUE}]│[/]")
                for name in self._failed:
                    lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_RED}]FAIL[/]  {name:<19} [{C_NS_BLUE}]│[/]")
                lines.append(f"[{C_NS_BLUE}]│[/]                              [{C_NS_BLUE}]│[/]")
                lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_YELLOW}]请使用「下载工具」补齐。[/]   [{C_NS_BLUE}]│[/]")
            else:
                lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN}]所有工具检测通过。[/]        [{C_NS_BLUE}]│[/]")

            lines.append(f"[{C_NS_BLUE}]│[/]                              [{C_NS_BLUE}]│[/]")
            lines.append(f"[{C_NS_BLUE}]│[/] [{C_NS_GRAY}]按 回车 或 ESC 关闭[/]      [{C_NS_BLUE}]│[/]")
            lines.append(f"[{C_NS_BLUE}]╰──────────────────────────────╯[/]")
            yield Static("\n".join(lines))

        def action_close_modal(self) -> None:
            """关闭弹窗（回车/ESC/空格触发）"""
            self.dismiss(None)


    class ConfirmScreen(ModalScreen):
        """通用确认弹窗(ModalScreen)
        显示提示信息,按 Y/回车 确认, N/ESC 取消
        dismiss(True)=确认继续, dismiss(False)=取消
        用于:字典生成数量过多时的二次确认
        """

        # Y 确认 / N 或 ESC 取消 / 回车 确认
        BINDINGS = [
            Binding("y",      "confirm", "确认", show=False, priority=True),
            Binding("n",      "cancel",  "取消", show=False, priority=True),
            Binding("enter",  "confirm", "确认", show=False, priority=True),
            Binding("escape", "cancel",  "取消", show=False, priority=True),
        ]

        def __init__(self, message: str, title: str = "确认操作"):
            """
            :param message: 提示信息(支持 rich markup)
            :param title: 弹窗标题
            """
            super().__init__()
            self._message = message
            self._title = title

        def compose(self) -> ComposeResult:
            """构建弹窗内容(nushell 风格:黄色边框 + 黄色标题 + 提示文本)"""
            lines = [f"[{C_NS_YELLOW}]╭──────────────────────────────────╮[/]"]
            # 标题行
            lines.append(f"[{C_NS_YELLOW}]│[/] [{C_NS_YELLOW} bold]{self._title:<32}[/] [{C_NS_YELLOW}]│[/]")
            lines.append(f"[{C_NS_YELLOW}]├──────────────────────────────────┤[/]")
            # 提示信息(可能多行,按实际换行)
            for msg_line in self._message.split("\n"):
                lines.append(f"[{C_NS_YELLOW}]│[/] {msg_line:<33} [{C_NS_YELLOW}]│[/]")
            lines.append(f"[{C_NS_YELLOW}]│[/]                                  [{C_NS_YELLOW}]│[/]")
            lines.append(f"[{C_NS_YELLOW}]│[/] [{C_NS_GREEN}]Y[/] 确认  [{C_NS_RED}]N[/] 取消            [{C_NS_YELLOW}]│[/]")
            lines.append(f"[{C_NS_YELLOW}]╰──────────────────────────────────╯[/]")
            yield Static("\n".join(lines))

        def action_confirm(self) -> None:
            """确认:dismiss(True)"""
            self.dismiss(True)

        def action_cancel(self) -> None:
            """取消:dismiss(False)"""
            self.dismiss(False)


    class InfoScreen(ModalScreen):
        """通用提示弹窗(ModalScreen)
        仅显示信息,按 回车/ESC/空格 关闭
        用于:磁盘空间不足等阻断性提示
        """

        BINDINGS = [
            Binding("enter",  "close_modal", "关闭", show=False, priority=True),
            Binding("escape", "close_modal", "关闭", show=False, priority=True),
            Binding("space",  "close_modal", "关闭", show=False, priority=True),
        ]

        def __init__(self, message: str, title: str = "提示"):
            """
            :param message: 提示信息(支持 rich markup)
            :param title: 弹窗标题
            """
            super().__init__()
            self._message = message
            self._title = title

        def compose(self) -> ComposeResult:
            """构建弹窗内容(nushell 风格:红色边框 + 红色标题 + 提示文本)"""
            lines = [f"[{C_NS_RED}]╭──────────────────────────────────╮[/]"]
            lines.append(f"[{C_NS_RED}]│[/] [{C_NS_RED} bold]{self._title:<32}[/] [{C_NS_RED}]│[/]")
            lines.append(f"[{C_NS_RED}]├──────────────────────────────────┤[/]")
            for msg_line in self._message.split("\n"):
                lines.append(f"[{C_NS_RED}]│[/] {msg_line:<33} [{C_NS_RED}]│[/]")
            lines.append(f"[{C_NS_RED}]│[/]                                  [{C_NS_RED}]│[/]")
            lines.append(f"[{C_NS_RED}]│[/] [{C_NS_GRAY}]按 回车 或 ESC 关闭[/]        [{C_NS_RED}]│[/]")
            lines.append(f"[{C_NS_RED}]╰──────────────────────────────────╯[/]")
            yield Static("\n".join(lines))

        def action_close_modal(self) -> None:
            """关闭弹窗"""
            self.dismiss(None)


    class HelpScreen(ModalScreen):
        """通用帮助弹窗(ModalScreen)
        以 nushell box 风格显示多行帮助内容,支持分区标题和 kv 格式
        按回车/ESC/空格关闭
        用于:首页、字典生成、各子页面的使用帮助
        布局策略:背景透明 + 靠左上对齐 + 固定宽度
                下层 App 的右侧 content_panel(设备信息/字典模式)不被遮挡,照常显示
                仅左侧菜单区域被帮助 box 覆盖
        """

        # 覆盖 ModalScreen 默认样式(居中 + 半透明遮罩)
        # 改为:背景透明(下层界面可见) + 靠左上对齐(box 贴左侧菜单区域)
        CSS = """
        HelpScreen {
            align: left top;
            background: transparent;
            padding: 1 1;
        }
        #help_scroll {
            width: 68;
            height: 90%;
        }
        .help_box {
            width: 1fr;
            height: auto;
        }
        """

        BINDINGS = [
            Binding("enter",  "close_modal", "关闭", show=False, priority=True),
            Binding("escape", "close_modal", "关闭", show=False, priority=True),
            Binding("space",  "close_modal", "关闭", show=False, priority=True),
        ]

        def __init__(self, sections: list, title: str = "使用帮助"):
            """
            :param sections: 帮助内容列表,每项为 tuple:
                ("section", "标题")     — 青色分区标题
                ("kv", "键", "值")       — 键值对(键灰色,值白色)
                ("raw", "文本")          — 原始文本行
                ("blank",)               — 空行
            :param title: 弹窗标题
            """
            super().__init__()
            self._sections = sections
            self._title = title

        def compose(self) -> ComposeResult:
            """构建弹窗内容(nushell 风格:青色边框 + 分区,自动换行可滚动)"""
            box_w = 66
            inner = box_w - 4

            def _top(t: str) -> str:
                tw = _disp_w(f" {t} ")
                dashes = max(0, box_w - 2 - tw)
                return f"[{C_NS_CYAN}]╭ {t} " + "─" * dashes + "╮[/]"

            def _mid() -> str:
                return f"[{C_NS_CYAN}]├" + "─" * (box_w - 2) + "┤[/]"

            def _bot() -> str:
                return f"[{C_NS_CYAN}]╰" + "─" * (box_w - 2) + "╯[/]"

            def _row(content: str) -> str:
                padded = _pad_w(content, inner)
                return f"[{C_NS_CYAN}]│[/] {padded} [{C_NS_CYAN}]│[/]"

            lines = [_top(self._title)]
            for item in self._sections:
                kind = item[0]
                if kind == "section":
                    lines.append(_mid())
                    for ln in _wrap_disp(item[1], inner):
                        lines.append(_row(f"[{C_NS_CYAN} bold]{ln}[/]"))
                elif kind == "kv":
                    content = f"{item[1]}: {item[2]}"
                    for idx, ln in enumerate(_wrap_disp(content, inner)):
                        prefix = "  " if idx else ""
                        lines.append(_row(f"[{C_NS_GRAY}]{prefix}{ln}[/]"))
                elif kind == "raw":
                    for ln in _wrap_disp(item[1], inner):
                        lines.append(_row(ln))
                elif kind == "blank":
                    lines.append(_row(""))
            lines.append(_mid())
            lines.append(_row(f"[{C_NS_GRAY}]J/K 滚动  回车/ESC/空格 关闭[/]"))
            lines.append(_bot())
            yield VerticalScroll(
                Static("\n".join(lines), classes="help_box"),
                id="help_scroll",
                can_focus=False,
            )

        def on_key(self, event) -> None:
            """帮助弹窗内 J/K/上下键滚动内容。"""
            key = event.key
            if key in ("j", "J", "down"):
                try:
                    self.query_one("#help_scroll", VerticalScroll).scroll_down(
                        animate=False, immediate=True
                    )
                except Exception:  # noqa: BLE001
                    pass
                event.stop()
            elif key in ("k", "K", "up"):
                try:
                    self.query_one("#help_scroll", VerticalScroll).scroll_up(
                        animate=False, immediate=True
                    )
                except Exception:  # noqa: BLE001
                    pass
                event.stop()

        def action_close_modal(self) -> None:
            """关闭弹窗"""
            self.dismiss(None)


    class RuleSelectScreen(ModalScreen):
        """规则选择弹窗：自动读取 .rule 文件，W/S 选择，D/回车/空格 确认，A/ESC 返回。"""

        CSS = """
        RuleSelectScreen {
            align: left top;
            background: transparent;
            padding: 1 2;
        }
        #rule_scroll {
            width: 84;
            height: 90%;
            border: round #00ffff;
        }
        #rule_list {
            width: 1fr;
            height: auto;
        }
        """

        def __init__(self, rules: list):
            super().__init__()
            self._rules = rules
            self._selected = 0
            self._scroll: Optional[VerticalScroll] = None

        def compose(self) -> ComposeResult:
            yield VerticalScroll(
                Static("", id="rule_list"),
                id="rule_scroll",
                can_focus=False,
            )

        def on_mount(self) -> None:
            self._scroll = self.query_one("#rule_scroll", VerticalScroll)
            self._refresh()

        def _refresh(self) -> None:
            lines = [
                f"[{C_NS_BLUE}]──────────────────────────────────────────────────────────────────[/]",
                f"[{C_NS_GREEN} bold]选择规则文件[/]",
                f"[{C_NS_BLUE}]──────────────────────────────────────────────────────────────────[/]",
            ]
            for i, (name, _path, desc) in enumerate(self._rules):
                marker = f"[{C_NS_GREEN}]❯[/]" if i == self._selected else " "
                name_color = C_NS_GREEN if i == self._selected else C_NS_WHITE
                lines.append(
                    f"{marker} [{C_NS_CYAN}]{i + 1:>2}.[/] "
                    f"[{name_color} bold]{_esc_markup(name)}[/]"
                )
                lines.append(f"      [{C_NS_GRAY}]{_esc_markup(desc)}[/]")
            lines.append(f"[{C_NS_BLUE}]──────────────────────────────────────────────────────────────────[/]")
            lines.append(f"[{C_NS_GRAY}]W/S 选择  D/回车/空格 确认  A/ESC 返回[/]")
            self.query_one("#rule_list", Static).update("\n".join(lines))
            if self._scroll is not None:
                # 每项占两行(名称+说明)，让当前选中项尽量保持在可视区
                target = max(0, self._selected * 2 - 4)
                self._scroll.scroll_to(y=target, animate=False, immediate=True)

        def on_key(self, event) -> None:
            key = event.key
            if key in ("w", "W", "up"):
                self._selected = (self._selected - 1) % len(self._rules)
                self._refresh()
                event.stop()
            elif key in ("s", "S", "down"):
                self._selected = (self._selected + 1) % len(self._rules)
                self._refresh()
                event.stop()
            elif key in ("a", "A", "escape"):
                self.dismiss(None)
                event.stop()
            elif key in ("d", "D", "enter", "space"):
                self.dismiss(self._rules[self._selected])
                event.stop()


    class CrackerApp(App):
        """
        主应用：codex-cli 风格纯文本全屏布局
        布局结构（自适应终端宽度）：
            [====================== ArchiveCracker ==========================]
            > 1. 密码破解          |  右侧内容区（设备信息/使用率等）
              2. 字典生成          |
              3. 设备信息          |
              4. 工具自检          |
            [--------------------------------------------------------------]
            当前版本：V 0.1    CPU:8.7%  GPU:16%  内存:14.7/31G(46%)
        """

        TITLE = "ArchiveCracker"
        SUB_TITLE = "压缩包密码爆破工具"

        # CSS：纯文本流布局，左右两栏并排，无色块无边框
        CSS = """
        #root {
            layout: vertical;
            height: 100%;
        }
        #top_bar {
            height: 1;
        }
        #body_container {
            layout: horizontal;
            height: 1fr;
        }
        #menu_panel {
            width: 30;
            height: 1fr;
            padding: 0 1;
        }
        #content_scroll {
            width: 1fr;
            height: 1fr;
            padding: 0 1;
        }
        #content_panel {
            width: 1fr;
            height: auto;
        }
        #bottom_bar {
            height: 3;
        }
        """

        # 菜单项定义：(id, 显示文字)
        # 注：设备信息不作为菜单项，固定显示在右侧（未回车时）
        _MENU_ITEMS = [
            ("menu_crack", "1. 密码破解"),
            ("menu_dict",  "2. 字典生成"),
            ("menu_tools", "3. 工具自检"),
            ("menu_help",  "4. 帮助说明"),
            ("menu_about", "5. 软件说明"),
            ("menu_quit",  "6. 退出软件"),
        ]

        # 快捷键绑定
        # 注意：up/down/enter/escape 不在此绑定，统一由 on_key 第1优先级处理
        # 否则 BINDINGS 和 on_key 会各调一次 action，导致上下键跳两格
        BINDINGS = [
            Binding("ctrl+q",    "do_quit",     "退出",     show=True),
            Binding("ctrl+1",    "go_crack",    "破解",     show=True),
            Binding("ctrl+2",    "go_dict",     "字典",     show=True),
            Binding("ctrl+3",    "go_tools",    "自检",     show=True),
            Binding("ctrl+4",    "go_help",     "帮助",     show=True),
            Binding("ctrl+p",    "show_cmd_cn", "命令面板", show=True, priority=True),
        ]

        def __init__(self):
            super().__init__()
            # core 层管理器（后续面板功能对接时使用）
            self._pm = PathManager()
            self._extractor = HashExtractor(self._pm)
            self._cracker = HashcatExecutor(self._pm)
            self._dict_gen = DictGenerator(self._pm)
            # 当前选中菜单项索引
            self._menu_index: int = 0
            # 是否已回车进入当前菜单项（False 时右侧固定显示设备信息）
            self._menu_entered: bool = False
            # 终端宽度（用于自适应横线）
            self._term_width: int = 80
            # 实时进度渲染节流时间戳(避免每次 hashcat 状态行都全量重绘)
            self._last_live_render: float = 0.0
            # 硬件报告缓存（避免每次切换菜单都重新跑 wmic 子进程导致卡顿）
            self._hw_report_cache = None
            # 当前所在子页面（None=主菜单, "tools"=工具自检子页面）
            self._sub_page: Optional[str] = None
            # 子页面内当前选中项索引
            self._sub_index: int = 0
            # 工具自检结果缓存 [(名称, 路径), ...]
            self._tools_check_cache: Optional[list] = None
            # 字典生成子页面状态：勾选项 + 输入项 + 生成结果
            # toggle 勾选状态（id -> bool）
            self._dict_toggles: dict = {
                "dict_lower":   True,   # 小写字母默认勾选
                "dict_upper":   False,
                "dict_digit":   True,   # 数字默认勾选
                "dict_special": False,
                "dict_single":  False,
            }
            # input 输入项当前值（id -> str）
            self._dict_inputs: dict = {
                "dict_min_len": "4",
                "dict_max_len": "6",
                "dict_out_dir": str(_app_base() / "data" / "output"),
                # 生成数量:0 或空=全部生成;>0=只生成指定行数
                "dict_max_lines": "0",
            }
            # 输入模式：None=非输入模式, id=正在输入的项 id
            self._dict_input_mode: Optional[str] = None
            # 输入缓冲
            self._dict_input_buf: str = ""
            # 生成历史记录列表(支持多次生成,每项为 (结果文本, 输出文件路径))
            # 越新的记录越靠前(index 0 为最近一次)
            self._dict_history: list = []
            # 历史记录上限,超过后自动删除最早的(避免内存膨胀)
            self._dict_history_limit: int = 10
            # 待执行的经典字典生成配置(数量过多确认弹窗通过后使用)
            self._pending_dict_cfg: Optional[GenConfig] = None
            self._pending_dict_ts: str = ""
            # 掩码字典生成输入项状态(id -> str)
            self._dict_mask_inputs: dict = {
                "mask_input":     "?d?d?d?d",  # 掩码表达式,默认纯数字4位
                "mask_max_lines": "0",          # 生成数量:0=全部
                "mask_out_dir":   str(_app_base() / "data" / "output"),
            }
            # 掩码字典当前输入模式(None=未输入, item_id=正在输入该项)
            self._dict_mask_input_mode: Optional[str] = None
            # 掩码字典输入缓冲区(实时编辑用)
            self._dict_mask_input_buf: str = ""
            # 掩码字典生成历史记录(同经典字典结构)
            self._dict_mask_history: list = []
            self._dict_mask_history_limit: int = 10
            # 掩码字典待执行配置(数量过多确认弹窗通过后使用)
            self._pending_mask_cfg: Optional[GenConfig] = None
            self._pending_mask_ts: str = ""
            # 掩码模板选择索引(快速模板用)
            self._mask_preset_index: int = 0

            # ===== 字典攻击子页面状态(密码破解 → 字典攻击) =====
            # 输入项当前值(id -> str)
            # crack_dict_workload: 工作负载 1~4,默认3
            self._crack_dict_inputs: dict = {
                "crack_dict_archive":  "",                                      # 压缩包路径
                "crack_dict_dict":     "",                                      # 字典文件路径(逗号分隔多个)
                "crack_dict_workload": "3",                                     # 工作负载 1~4
                "crack_dict_device":   "auto",                                  # 设备类型 auto/gpu/cpu
            }
            # 破解任务运行状态(True=后台正在跑,防止重复启动)
            self._crack_dict_running: bool = False
            # 破解任务实时进度缓存(供 progress_callback 写入,渲染时读取)
            # 字段:status_text / speed / percent / recovered_pwd / elapsed
            self._crack_dict_live: dict = {}
            # 当前输入模式(None=未输入, item_id=正在输入该项)
            self._crack_dict_input_mode: Optional[str] = None
            # 输入缓冲区(实时编辑用)
            self._crack_dict_input_buf: str = ""
            # 拖入文件等待模式(True=正在等待用户拖入文件)
            # 此模式下 on_key 拦截所有按键,ESC 取消
            self._crack_dict_drop_mode: bool = False
            # 拖入路径累积缓冲区
            # 终端不支持 bracketed paste,拖入文件时路径被拆成字符逐个发送
            # 经实测诊断:Windows Terminal 会以 ctrl+@(\x00) 作为路径段的转义定界符,
            # 把完整路径(如 H:\桌面\杨CC性格技能_更新版.rar)拆成多段:
            #   ctrl+@ + "H" + ctrl+@ + ":\桌面\杨" + ctrl+@ + "C" + ctrl+@ + ...
            # 因此 ctrl+@ 不能作为结束符,只能作为分隔符忽略;
            # 结束判断改为"空闲超时":每次收到新字符重置定时器,静默若干毫秒后触发处理
            # 状态机:None=未在累积, str=正在累积路径
            self._crack_dict_drop_buffer: Optional[str] = None
            # 拖入路径空闲超时定时器
            # 每收到一个路径字符就 stop+重建,定时器触发即认为路径接收完毕
            # 静默阈值:拖入字符间隔极快(微秒级),完成后静默明显(数百毫秒以上)
            self._crack_dict_drop_timer = None
            # 拖入路径空闲超时阈值(毫秒)
            # 取 800ms:拖入字符在数毫秒内全部送达,800ms 足够覆盖终端抖动;
            # 且累积阶段不再每字符重置定时器(改为开启时一次性设置),
            # 故阈值需略大于拖入总耗时,避免提前触发截断路径
            self._crack_dict_drop_idle_ms: int = 800
            # 破解历史记录列表(每次破解一条,结构同字典生成历史)
            # 每项为 tuple: (timestamp, result_file, result_text, extra_dict)
            self._crack_dict_history: list = []
            self._crack_dict_history_limit: int = 10

            # ===== 掩码/字典加规则/暴力穷举 通用状态(字典攻击仍用上面的独立状态) =====
            self._crack_mode_states: dict = {
                "crack_mask": {
                    "inputs": {
                        "crack_mask_archive": "",
                        "crack_mask_expr": "?d?d?d?d",
                        "crack_mask_workload": "3",
                        "crack_mask_device": "auto",
                    },
                    "running": False,
                    "live": {},
                    "history": [],
                    "history_limit": 10,
                    "input_mode": None,
                    "input_buf": "",
                    "drop_mode": False,
                    "drop_buffer": None,
                    "drop_timer": None,
                    "mask_index": 0,
                },
                "crack_rule": {
                    "inputs": {
                        "crack_rule_archive": "",
                        "crack_rule_dict": "",
                        "crack_rule_file": _RULE_PRESETS[0][1] if _RULE_PRESETS else "",
                        "crack_rule_workload": "3",
                        "crack_rule_device": "auto",
                    },
                    "running": False,
                    "live": {},
                    "history": [],
                    "history_limit": 10,
                    "input_mode": None,
                    "input_buf": "",
                    "drop_mode": False,
                    "drop_buffer": None,
                    "drop_timer": None,
                    "rule_index": 0,
                },
                "crack_brute": {
                    "inputs": {
                        "crack_brute_archive": "",
                        "crack_brute_custom": "",
                        "crack_brute_min_len": "1",
                        "crack_brute_max_len": "8",
                        "crack_brute_workload": "3",
                        "crack_brute_device": "auto",
                    },
                    "toggles": {
                        "crack_brute_lower": True,
                        "crack_brute_upper": False,
                        "crack_brute_digit": True,
                        "crack_brute_special": False,
                    },
                    "running": False,
                    "live": {},
                    "history": [],
                    "history_limit": 10,
                    "input_mode": None,
                    "input_buf": "",
                    "drop_mode": False,
                    "drop_buffer": None,
                    "drop_timer": None,
                    "brute_index": 0,
                },
            }
            # 社工字典生成输入项状态(id -> str)
            # 所有字段默认空,用户填哪些用哪些;输出目录默认 data/output
            self._dict_social_inputs: dict = {
                "soc_name_cn":        "",                          # 中文姓名
                "soc_name_pinyin":    "",                          # 拼音全拼
                "soc_name_en":        "",                          # 英文名
                "soc_nickname":       "",                          # 昵称/网名
                "soc_birth_year":     "",                          # 生日年份
                "soc_birth_month":    "",                          # 生日月份
                "soc_birth_day":      "",                          # 生日日期
                "soc_birth_full":     "",                          # 完整生日
                "soc_phone":          "",                          # 手机号
                "soc_qq":             "",                          # QQ号
                "soc_wechat":         "",                          # 微信号
                "soc_email":          "",                          # 邮箱
                "soc_id_card":        "",                          # 身份证号(完整)
                "soc_company":        "",                          # 公司名
                "soc_position":       "",                          # 职位
                "soc_employee_id":    "",                          # 工号
                "soc_school":         "",                          # 学校名
                "soc_school_year":    "",                          # 入学年份
                "soc_spouse_name":    "",                          # 配偶姓名
                "soc_child_name":     "",                          # 子女姓名
                "soc_pet_name":       "",                          # 宠物名
                "soc_anniversary":    "",                          # 纪念日
                "soc_car_plate":      "",                          # 车牌号
                "soc_favorite_words": "",                          # 喜好词汇(逗号分隔)
                "soc_lucky_numbers":  "",                          # 幸运数字(逗号分隔)
                "soc_area_code":      "",                          # 地区区号
                "soc_common_suffixes":"",                          # 自定义后缀(逗号分隔)
                "soc_out_dir":        str(_app_base() / "data" / "output"),  # 输出目录
            }
            # 社工字典生成输入模式:None=非输入模式, id=正在输入的项 id
            self._dict_social_input_mode: Optional[str] = None
            # 输入缓冲
            self._dict_social_input_buf: str = ""
            # 社工字典生成历史记录列表(支持多次生成,每项为 (结果文本, 输出文件路径))
            # 越新的记录越靠前(index 0 为最近一次)
            self._dict_social_history: list = []
            # 历史记录上限,超过后自动删除最早的(避免内存膨胀)
            self._dict_social_history_limit: int = 10
            # 内容区路径点击映射：{行号: 原始路径}
            # _render_content 时重建,Ctrl+点击 content_panel 查此表跳转
            self._content_path_map: dict = {}
            # 路径标记临时列表(渲染 box 时暂存,渲染后回填到 _content_path_map)
            self._pending_path_markers: list = []

        def compose(self) -> ComposeResult:
            """构建纯文本全屏布局：顶横线 + 左菜单/右内容 + 底横线"""
            yield Container(
                Static("", id="top_bar"),       # 顶部横线
                Container(
                    Static("", id="menu_panel"),  # 左侧菜单
                    VerticalScroll(
                        Static("", id="content_panel"),  # 右侧内容(可滚动)
                        id="content_scroll",
                        can_focus=False,
                    ),
                    id="body_container",
                ),
                Static("", id="bottom_bar"),     # 底部横线+版本+监控
                id="root",
            )

        def on_mount(self) -> None:
            """挂载后：采集终端宽度、渲染初始界面、启动监控定时器"""
            # 获取终端宽度（Textual 返回终端总宽度）
            try:
                self._term_width = self.console.width
            except Exception:  # noqa: BLE001
                self._term_width = 80
            # psutil 首次 CPU 采样基线
            try:
                import psutil
                psutil.cpu_percent(interval=None)
            except Exception:  # noqa: BLE001
                pass
            # 首次渲染
            self._render_all()
            # 每 2 秒刷新监控数据
            self.set_interval(2.0, self._refresh_stats)

        def on_resize(self, event) -> None:
            """终端尺寸变化时重新渲染（自适应宽度）"""
            try:
                self._term_width = event.size.width
            except Exception:  # noqa: BLE001
                pass
            # 同步菜单面板宽度(字典子页面时随终端宽度变化)
            self._update_menu_width()
            self._render_all()

        # ================================================================
        # 渲染逻辑（纯 Static.update，零组件）
        # ================================================================

        def _render_all(self) -> None:
            """全量渲染：顶横线 + 菜单 + 内容 + 底横线"""
            self._render_top_bar()
            self._render_menu()
            self._render_content()
            self._render_bottom_bar()

        def _render_top_bar(self) -> None:
            """渲染顶部横线（nushell 风格：蓝色 ─ 横线 + 绿色标题）
            注意：中文显示占2列，必须用显示宽度计算，否则右侧横线补不全
            """
            import unicodedata

            def _disp_width(s: str) -> int:
                """计算字符串显示宽度（全角2列，半角1列）"""
                w = 0
                for ch in s:
                    w += 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
                return w

            title = f" {self.TITLE} — {self.SUB_TITLE} "
            w = self._term_width
            # 标题居中嵌入蓝色横线
            title_w = _disp_width(title)
            dash_count = max(0, w - title_w - 2)
            left = dash_count // 2
            right = dash_count - left
            # nushell 风格：标题绿色加粗，横线蓝色
            bar = (
                f"[{C_NS_BLUE}]{'─' * left}[/]"
                f"[{C_NS_GREEN} bold]{title}[/]"
                f"[{C_NS_BLUE}]{'─' * right}[/]"
            )
            self.query_one("#top_bar", Static).update(bar)

        def _render_menu(self) -> None:
            """渲染左侧菜单（nushell 风格）
            - 选中项：❯ 标记 + 绿色文字 + 青色编号
            - 未选中：空格标记 + 白色文字 + 青色编号
            - 分隔线：蓝色 ─
            """
            lines = []
            if self._sub_page == "tools":
                # 工具自检子页面：渲染子页面操作项
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]工具自检[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (_, label) in enumerate(_TOOLS_SUB_ITEMS):
                    # 拆分编号和文字：label 形如 "1. 重新检测"
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    if i == self._sub_index:
                        # 选中：❯ + 绿色文字 + 青色编号
                        lines.append(
                            f"[{C_NS_GREEN}]❯[/] [{C_NS_CYAN}]{num}.[/] [{C_NS_GREEN} bold]{text}[/]"
                        )
                    else:
                        # 未选中：空格 + 青色编号 + 白色文字
                        lines.append(
                            f"  [{C_NS_CYAN}]{num}.[/] [{C_NS_WHITE}]{text}[/]"
                        )
            elif self._sub_page == "dict":
                # 字典生成二级菜单：显示子功能列表（经典/社工/随机/其他/返回）
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]字典生成[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (_, label) in enumerate(_DICT_MENU_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    if i == self._sub_index:
                        # 选中：❯ + 绿色文字 + 青色编号
                        lines.append(
                            f"[{C_NS_GREEN}]❯[/] [{C_NS_CYAN}]{num}.[/] [{C_NS_GREEN} bold]{text}[/]"
                        )
                    else:
                        # 未选中：空格 + 青色编号 + 白色文字
                        lines.append(
                            f"  [{C_NS_CYAN}]{num}.[/] [{C_NS_WHITE}]{text}[/]"
                        )
            elif self._sub_page == "crack":
                # 密码破解二级菜单:显示4种攻击模式 + 帮助 + 返回
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]密码破解[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (_, label) in enumerate(_CRACK_MENU_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    if i == self._sub_index:
                        # 选中:❯ + 绿色文字 + 青色编号
                        lines.append(
                            f"[{C_NS_GREEN}]❯[/] [{C_NS_CYAN}]{num}.[/] [{C_NS_GREEN} bold]{text}[/]"
                        )
                    else:
                        # 未选中:空格 + 青色编号 + 白色文字
                        lines.append(
                            f"  [{C_NS_CYAN}]{num}.[/] [{C_NS_WHITE}]{text}[/]"
                        )
            elif self._sub_page == "dict_classic":
                # 经典字典生成子页面：渲染勾选项 + 输入项 + 执行项（原字典生成功能）
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]经典字典生成[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (item_id, item_type, label) in enumerate(_DICT_SUB_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    # 选中项前加 ❯，否则加空格
                    marker = f"[{C_NS_GREEN}]❯[/]" if i == self._sub_index else " "
                    # 文字颜色：选中绿色，未选中白色
                    text_color = C_NS_GREEN if i == self._sub_index else C_NS_WHITE
                    bold_tag = " bold" if i == self._sub_index else ""
                    # 根据类型生成行尾内容
                    if item_type == "toggle":
                        # toggle 项：[✓]/[ ] 勾选标记
                        # 注意:外层左方括号是字面量显示,必须用 \[ 转义,
                        # 否则 [[#00ff00]✓[/]] 会被解析为嵌套标签导致 MarkupError;
                        # 右方括号 ] 在 rich 中单独出现不触发标签解析,无需转义
                        checked = self._dict_toggles.get(item_id, False)
                        mark = f"[{C_NS_GREEN}]✓[/]" if checked else f"[{C_NS_GRAY}] [/]"
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] \\[{mark}] "
                            f"[{text_color}{bold_tag}]{text}[/]"
                        )
                    elif item_type == "input":
                        # input 项：显示当前值（黄色），输入模式时显示光标
                        # 路径含反斜杠等特殊字符，必须用 escape 转义避免 MarkupError
                        cur_val = self._dict_inputs.get(item_id, "")
                        if self._dict_input_mode == item_id:
                            # 输入模式：显示缓冲 + 光标（缓冲也要转义）
                            buf_escaped = _esc_markup(self._dict_input_buf)
                            val_display = f"[{C_NS_YELLOW}]{buf_escaped}_[/]"
                            # 输入模式用缓冲值计算行宽
                            actual_val = self._dict_input_buf
                        else:
                            val_escaped = _esc_markup(cur_val)
                            val_display = f"[{C_NS_YELLOW}]{val_escaped}[/]"
                            actual_val = cur_val
                        # 动态换行阈值:终端宽度的一半
                        # 整行显示宽度超过阈值才换行,不超过则一行显示
                        half_width = max(30, self._term_width // 2)
                        line_text = f"{marker} {num}. {text} : {actual_val}"
                        if _disp_w(line_text) > half_width:
                            # 超过阈值:值单独换行缩进显示
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/]"
                            )
                            lines.append(f"      {val_display}")
                        else:
                            # 未超过阈值:一行显示
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/] {val_display}"
                            )
                    else:  # action
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/]"
                        )
            elif self._sub_page == "dict_social":
                # 社工字典生成子页面:渲染多输入项 + 执行项
                # 复用经典字典生成的渲染逻辑(input/action),数据源换为 _DICT_SOCIAL_ITEMS
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]社工字典生成[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (item_id, item_type, label) in enumerate(_DICT_SOCIAL_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    marker = f"[{C_NS_GREEN}]❯[/]" if i == self._sub_index else " "
                    text_color = C_NS_GREEN if i == self._sub_index else C_NS_WHITE
                    bold_tag = " bold" if i == self._sub_index else ""
                    if item_type == "input":
                        cur_val = self._dict_social_inputs.get(item_id, "")
                        if self._dict_social_input_mode == item_id:
                            buf_escaped = _esc_markup(self._dict_social_input_buf)
                            val_display = f"[{C_NS_YELLOW}]{buf_escaped}_[/]"
                            actual_val = self._dict_social_input_buf
                        else:
                            val_escaped = _esc_markup(cur_val)
                            val_display = f"[{C_NS_YELLOW}]{val_escaped}[/]"
                            actual_val = cur_val
                        # 动态换行阈值:终端宽度的一半
                        half_width = max(30, self._term_width // 2)
                        line_text = f"{marker} {num}. {text} : {actual_val}"
                        if _disp_w(line_text) > half_width:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/]"
                            )
                            lines.append(f"      {val_display}")
                        else:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/] {val_display}"
                            )
                    else:  # action
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/]"
                        )
            elif self._sub_page == "dict_mask":
                # 掩码字典生成子页面:渲染输入项 + 执行项
                # 复用社工字典的渲染逻辑(input/action),数据源换为 _DICT_MASK_ITEMS
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]掩码字典生成[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (item_id, item_type, label) in enumerate(_DICT_MASK_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    marker = f"[{C_NS_GREEN}]❯[/]" if i == self._sub_index else " "
                    text_color = C_NS_GREEN if i == self._sub_index else C_NS_WHITE
                    bold_tag = " bold" if i == self._sub_index else ""
                    if item_type == "input":
                        cur_val = self._dict_mask_inputs.get(item_id, "")
                        if self._dict_mask_input_mode == item_id:
                            buf_escaped = _esc_markup(self._dict_mask_input_buf)
                            val_display = f"[{C_NS_YELLOW}]{buf_escaped}_[/]"
                            actual_val = self._dict_mask_input_buf
                        else:
                            val_escaped = _esc_markup(cur_val)
                            val_display = f"[{C_NS_YELLOW}]{val_escaped}[/]"
                            actual_val = cur_val
                        half_width = max(30, self._term_width // 2)
                        line_text = f"{marker} {num}. {text} : {actual_val}"
                        if _disp_w(line_text) > half_width:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/]"
                            )
                            lines.append(f"      {val_display}")
                        else:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/] {val_display}"
                            )
                    else:  # action
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/]"
                        )
            elif self._sub_page == "crack_dict":
                # 字典攻击子页面:渲染输入项 + 执行项
                # 复用掩码字典的渲染逻辑(input/action),数据源换为 _CRACK_DICT_ITEMS
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]字典攻击[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (item_id, item_type, label) in enumerate(_CRACK_DICT_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    marker = f"[{C_NS_GREEN}]❯[/]" if i == self._sub_index else " "
                    text_color = C_NS_GREEN if i == self._sub_index else C_NS_WHITE
                    bold_tag = " bold" if i == self._sub_index else ""
                    if item_type == "input":
                        cur_val = self._crack_dict_inputs.get(item_id, "")
                        if self._crack_dict_input_mode == item_id:
                            # 输入模式:显示缓冲 + 光标
                            buf_escaped = _esc_markup(self._crack_dict_input_buf)
                            val_display = f"[{C_NS_YELLOW}]{buf_escaped}_[/]"
                            actual_val = self._crack_dict_input_buf
                        else:
                            val_escaped = _esc_markup(cur_val)
                            val_display = f"[{C_NS_YELLOW}]{val_escaped}[/]"
                            actual_val = cur_val
                        # 动态换行阈值:终端宽度的一半
                        half_width = max(30, self._term_width // 2)
                        line_text = f"{marker} {num}. {text} : {actual_val}"
                        if _disp_w(line_text) > half_width:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/]"
                            )
                            lines.append(f"      {val_display}")
                        else:
                            lines.append(
                                f"{marker} [{C_NS_CYAN}]{num}.[/] "
                                f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/] {val_display}"
                            )
                    else:  # action
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/]"
                        )
                # 拖入等待模式:在菜单底部显示提示
                if self._crack_dict_drop_mode:
                    lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                    lines.append(f"[{C_NS_YELLOW} bold]等待拖入文件...[/]")
                    lines.append(f"[{C_NS_GRAY}]将文件拖入终端窗口[/]")
                    lines.append(f"[{C_NS_GRAY}]按 ESC 取消[/]")
            elif self._sub_page in _CRACK_MODE_PAGES:
                self._render_crack_mode_menu(self._sub_page)
                return
            else:
                # 主菜单
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_GREEN} bold]ArchiveCracker[/]")
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                for i, (_, label) in enumerate(self._MENU_ITEMS):
                    num = label.split(".")[0]
                    text = label.split(".", 1)[1].strip()
                    if i == self._menu_index:
                        # 选中：❯ + 绿色文字 + 青色编号；已进入则追加黄色标记
                        if self._menu_entered:
                            suffix = f" [{C_NS_YELLOW}]已进入[/]"
                        else:
                            suffix = ""
                        lines.append(
                            f"[{C_NS_GREEN}]❯[/] [{C_NS_CYAN}]{num}.[/] [{C_NS_GREEN} bold]{text}[/]{suffix}"
                        )
                    else:
                        # 未选中：空格 + 青色编号 + 白色文字
                        lines.append(
                            f"  [{C_NS_CYAN}]{num}.[/] [{C_NS_WHITE}]{text}[/]"
                        )
            self.query_one("#menu_panel", Static).update("\n".join(lines))

        def _get_hw_report(self):
            """获取硬件报告（带缓存）
            首次调用时采集，之后复用缓存，避免每次切换菜单都重新跑 wmic 子进程
            :return: HardwareReport 对象
            """
            if self._hw_report_cache is None:
                self._hw_report_cache = collect_hardware_report()
            return self._hw_report_cache

        def _render_content(self) -> None:
            """渲染右侧内容
            规则：
                - 未回车进入：右侧靠右显示「绿色虚线盒子」包裹的设备信息
                  盒子宽度约为终端宽度的一半（从中间到右侧），随终端宽度自适应
                  使用缓存的硬件报告，不重新采集（避免卡顿）
                - 已回车进入：靠左显示当前菜单项对应的功能内容
                - 子页面模式（工具自检）：靠左显示工具检测结果（用缓存，不每次重跑）
            """
            panel = self.query_one("#content_panel", Static)

            # 清空路径点击映射(每次重渲染时重建,避免行号错位)
            self._content_path_map = {}

            # 子页面模式：工具自检
            if self._sub_page == "tools":
                panel.styles.text_align = "left"
                content = self._render_tools_check_content()
                panel.update(content)
                return

            # 子页面模式：字典生成二级菜单（nushell box 右对齐,与设备信息一致）
            if self._sub_page == "dict":
                panel.styles.text_align = "right"
                content = self._render_dict_menu_content()
                panel.update(content)
                return

            # 子页面模式:密码破解二级菜单(nushell box 右对齐,与字典模式选择页一致)
            if self._sub_page == "crack":
                panel.styles.text_align = "right"
                content = self._render_crack_menu_content()
                panel.update(content)
                return

            # 子页面模式：经典字典生成(nushell box 右对齐)
            if self._sub_page == "dict_classic":
                panel.styles.text_align = "right"
                content = self._render_dict_content()
                panel.update(content)
                return

            # 子页面模式:社工字典生成(nushell box 右对齐)
            if self._sub_page == "dict_social":
                panel.styles.text_align = "right"
                content = self._render_dict_social_content()
                panel.update(content)
                return

            # 子页面模式:掩码字典生成(nushell box 右对齐)
            if self._sub_page == "dict_mask":
                panel.styles.text_align = "right"
                content = self._render_dict_mask_content()
                panel.update(content)
                return

            # 子页面模式:字典攻击(nushell box 右对齐,与掩码字典生成页一致)
            if self._sub_page == "crack_dict":
                panel.styles.text_align = "right"
                content = self._render_crack_dict_content()
                panel.update(content)
                return

            # 子页面模式:掩码/字典加规则/暴力穷举(nushell box 右对齐)
            if self._sub_page in _CRACK_MODE_PAGES:
                panel.styles.text_align = "right"
                content = self._render_crack_mode_content(self._sub_page)
                panel.update(content)
                return

            if not self._menu_entered:
                # 未回车：设备信息靠右对齐
                panel.styles.text_align = "right"
                # 盒子宽度：终端宽度的一半左右，最小 40 保证内容可读
                avail = max(40, self._term_width - 24)
                box_width = max(40, avail // 2)
                try:
                    report = self._get_hw_report()
                    content = format_report_text(report, box_width)
                except Exception as exc:  # noqa: BLE001
                    content = f"设备信息采集失败: {type(exc).__name__}: {exc}"
            else:
                # 回车后：菜单功能内容靠左对齐
                panel.styles.text_align = "left"
                menu_id = self._MENU_ITEMS[self._menu_index][0]
                if menu_id == "menu_tools":
                    # 工具自检（主菜单回车预览，用缓存；若无缓存则跑一次）
                    checks = self._run_tool_check()
                    content = self._format_tools_check(checks)
                elif menu_id == "menu_crack":
                    # 密码破解(nushell 风格预览)
                    content = (
                        f"[{C_NS_GREEN} bold]密码破解[/]\n"
                        f"[{C_NS_BLUE}]{'─' * 50}[/]\n"
                        f"[{C_NS_GRAY}]回车进入攻击模式选择(字典/掩码/规则/暴力)[/]"
                    )
                elif menu_id == "menu_dict":
                    # 字典生成（nushell 风格）
                    content = (
                        f"[{C_NS_GREEN} bold]字典生成[/]\n"
                        f"[{C_NS_BLUE}]{'─' * 50}[/]\n"
                        f"[{C_NS_YELLOW}]待实现[/]：字符集/掩码/规则 + 字典生成"
                    )
                elif menu_id == "menu_help":
                    # 帮助说明(nushell 风格预览)
                    content = (
                        f"[{C_NS_GREEN} bold]帮助说明[/]\n"
                        f"[{C_NS_BLUE}]{'─' * 50}[/]\n"
                        f"[{C_NS_GRAY}]回车查看完整使用帮助[/]"
                    )
                else:
                    content = ""
            panel.update(content)

        def _run_tool_check(self) -> list:
            """执行工具路径检测（带缓存）
            :return: [(名称, 路径), ...]，路径为空表示未找到
            """
            if self._tools_check_cache is None:
                paths = self._pm.discover()
                self._tools_check_cache = [
                    ("Hashcat 主程序",   paths.hashcat),
                    ("zip2john (ZIP)",   paths.zip2john),
                    ("rar2john (RAR)",   paths.rar2john),
                    ("7z2john (pl/exe)", paths.seven2john_perl),
                    ("John run 目录",    paths.john_root),
                ]
            return self._tools_check_cache

        def _format_tools_check(self, checks: list) -> str:
            """格式化工具检测结果为 nushell 风格表格
            表头4列：# | 工具名称 | 状态 | 路径（路径列按最长路径自适应宽度）
            :param checks: [(名称, 路径), ...]
            :return: nushell 表格风格多行文本
            """
            # 固定列宽：编号列 2 字符，名称列 16 字符，状态列 6 字符
            # 路径列按所有路径（含"未找到"）的最大长度自适应，至少 20
            COL_NUM = 2
            COL_NAME = 16
            COL_STATUS = 6
            # 计算路径列最大宽度（中文路径按2列算，用unicodedata.east_asian_width）
            def _disp_len(s: str) -> int:
                """计算字符串显示宽度（中文占2，英文占1）"""
                import unicodedata
                return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1
                           for c in s)
            # 取所有路径（未找到的用"未找到"）中的最大显示宽度
            max_path_w = 0
            for _, path in checks:
                path_str = str(path) if path else "未找到"
                w = _disp_len(path_str)
                if w > max_path_w:
                    max_path_w = w
            col_path_w = max(max_path_w, 20)
            # 左侧固定列宽合计（含分隔符）：2 + 1 + 16 + 1 + 6 + 1 = 27
            # 加上路径列宽度 = 总内容宽
            # 边框横线长度 = 2 + 16 + 6 + col_path_w + 5（4个分隔符+1左边距）
            # 顶/底边框需要3个┬/┴，把4列隔开
            # 左边框 ╭ + ─*2 + ┬ + ─*16 + ┬ + ─*6 + ┬ + ─*col_path_w + ╮
            lines = []
            # 顶边框（按列宽生成 ─）
            top = (
                "╭" + "─" * COL_NUM
                + "┬" + "─" * COL_NAME
                + "┬" + "─" * COL_STATUS
                + "┬" + "─" * col_path_w
                + "╮"
            )
            lines.append(f"[{C_NS_BLUE}]{top}[/]")
            # 表头（绿色加粗）：# | 工具名称 | 状态 | 路径
            # 各列内容左对齐到列宽，中文按2列算
            def _pad(s: str, w: int) -> str:
                """按显示宽度左对齐补空格到 w 列"""
                dl = _disp_len(s)
                return s + " " * max(0, w - dl)
            hdr_num = _pad("#", COL_NUM)
            hdr_name = _pad("工具名称", COL_NAME)
            hdr_status = _pad("状态", COL_STATUS)
            hdr_path = _pad("路径", col_path_w)
            lines.append(
                f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN} bold]{hdr_num}[/]"
                f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN} bold]{hdr_name}[/]"
                f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN} bold]{hdr_status}[/]"
                f"[{C_NS_BLUE}]│[/] [{C_NS_GREEN} bold]{hdr_path}[/]"
                f"[{C_NS_BLUE}]│[/]"
            )
            # 表头分隔行
            mid = (
                "├" + "─" * COL_NUM
                + "┼" + "─" * COL_NAME
                + "┼" + "─" * COL_STATUS
                + "┼" + "─" * col_path_w
                + "┤"
            )
            lines.append(f"[{C_NS_BLUE}]{mid}[/]")
            # 数据行（4 列：编号 | 名称 | 状态 | 路径）
            for idx, (name, path) in enumerate(checks, 1):
                # 编号列（紫色，右对齐到 COL_NUM）
                num_str = str(idx)
                num = f"[{C_NS_PURPLE}]{num_str.rjust(COL_NUM)}[/]"
                # 名称列（白色，左对齐到 COL_NAME）
                name_col = f"[{C_NS_WHITE}]{_pad(name, COL_NAME)}[/]"
                # 状态列（OK绿色 / FAIL红色，左对齐到 COL_STATUS）
                if path:
                    status_txt = "OK"
                    status_col = f"[{C_NS_GREEN}]{_pad(status_txt, COL_STATUS)}[/]"
                else:
                    status_txt = "FAIL"
                    status_col = f"[{C_NS_RED}]{_pad(status_txt, COL_STATUS)}[/]"
                # 路径列（青色找到 / 红色未找到，左对齐到 col_path_w，不截断）
                if path:
                    path_str = str(path)
                    path_col = f"[{C_NS_CYAN}]{_pad(path_str, col_path_w)}[/]"
                else:
                    path_col = f"[{C_NS_RED}]{_pad('未找到', col_path_w)}[/]"
                lines.append(
                    f"[{C_NS_BLUE}]│[/] {num}"
                    f"[{C_NS_BLUE}]│[/] {name_col}"
                    f"[{C_NS_BLUE}]│[/] {status_col}"
                    f"[{C_NS_BLUE}]│[/] {path_col}"
                    f"[{C_NS_BLUE}]│[/]"
                )
            # 底边框
            bot = (
                "╰" + "─" * COL_NUM
                + "┴" + "─" * COL_NAME
                + "┴" + "─" * COL_STATUS
                + "┴" + "─" * col_path_w
                + "╯"
            )
            lines.append(f"[{C_NS_BLUE}]{bot}[/]")
            return "\n".join(lines)

        def _render_tools_check_content(self) -> str:
            """渲染工具自检子页面右侧内容（nushell 风格）
            包含：标题 + 操作提示 + 工具检测表格
            """
            checks = self._run_tool_check()
            lines = [
                f"[{C_NS_GREEN} bold]工具自检[/]",
                f"[{C_NS_BLUE}]{'─' * 50}[/]",
                f"[{C_NS_GRAY}]左侧选择操作：重新检测 / 下载工具 / 返回上一层[/]",
                "",
            ]
            lines.append(self._format_tools_check(checks))
            return "\n".join(lines)

        def _render_dict_menu_content(self) -> str:
            """渲染字典生成二级菜单右侧说明页(nushell box 格式 + 右对齐)
            展示4种字典生成模式的简介,供用户选择进入对应子页面
            样式与首页「设备信息」一致:绿色 box 边框 + 青色分区标题
            """
            # box 宽度:与设备信息保持一致(终端宽度一半左右,最小 50)
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # 构建 box 内容:每种模式一个分区(section),含说明和状态
            kv_lines = [
                ("section", "1. 经典字典生成"),
                ("kv", "原理", f"[{C_NS_WHITE}]基于字符集组合生成密码字典[/]"),
                ("kv", "支持", f"[{C_NS_GRAY}]小写/大写/数字/特殊字符勾选[/]"),
                ("kv", "特性", f"[{C_NS_GRAY}]可设长度范围、生成数量、单字符密码[/]"),
                ("kv", "状态", f"[{C_NS_GREEN}]可用[/]"),
                ("mid",),
                ("section", "2. 社工字典生成"),
                ("kv", "原理", f"[{C_NS_WHITE}]基于目标个人信息组合生成密码[/]"),
                ("kv", "支持", f"[{C_NS_GRAY}]姓名/生日/手机号/QQ/微信号等组合[/]"),
                ("kv", "状态", f"[{C_NS_GREEN}]可用[/]"),
                ("mid",),
                ("section", "3. 掩码字典生成"),
                ("kv", "原理", f"[{C_NS_WHITE}]按掩码占位符生成密码[/]"),
                ("kv", "支持", f"[{C_NS_GRAY}]?l ?u ?d ?s ?a 占位符+字面混合[/]"),
                ("kv", "示例", f"[{C_NS_GRAY}]?d?d?d?d / pass?d?d / ?l?l?d?d[/]"),
                ("kv", "状态", f"[{C_NS_GREEN}]可用[/]"),
                ("mid",),
                ("section", "4. 其他字典生成"),
                ("kv", "原理", f"[{C_NS_WHITE}]其他生成策略[/]"),
                ("kv", "状态", f"[{C_NS_YELLOW}]开发中[/]"),
                ("mid",),
                ("raw", f"[{C_NS_GRAY}]上下键选择,回车进入对应模式[/]"),
            ]

            box_lines = _nushell_box(kv_lines, "字典模式选择", box_width)
            return "\n".join(box_lines)

        def _render_crack_menu_content(self) -> str:
            """渲染密码破解二级菜单右侧说明页(nushell box 格式 + 右对齐)
            展示4种攻击模式的原理/速度/适用场景,供用户选择进入对应子页面
            样式与字典模式选择页一致:绿色 box 边框 + 青色分区标题
            """
            # box 宽度:与字典模式选择页保持一致
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # 构建 box 内容:每种攻击模式一个分区,含原理/速度/状态
            kv_lines = [
                ("section", "1. 字典攻击"),
                ("kv", "原理", f"[{C_NS_WHITE}]用字典文件逐行试密码[/]"),
                ("kv", "速度", f"[{C_NS_GREEN}]最快(字典质量决定成败)[/]"),
                ("kv", "适用", f"[{C_NS_GRAY}]有社工字典或常见弱口令[/]"),
                ("mid",),
                ("section", "2. 掩码攻击"),
                ("kv", "原理", f"[{C_NS_WHITE}]按位置规则穷举(?d?l?u)[/]"),
                ("kv", "速度", f"[{C_NS_GREEN}]GPU飞速,位数长则爆炸[/]"),
                ("kv", "适用", f"[{C_NS_GRAY}]已知密码结构(如4位数字)[/]"),
                ("mid",),
                ("section", "3. 字典加规则"),
                ("kv", "原理", f"[{C_NS_WHITE}]字典基础上做变体(大小写/加数字)[/]"),
                ("kv", "速度", f"[{C_NS_YELLOW}]比纯字典慢,覆盖面广[/]"),
                ("kv", "适用", f"[{C_NS_GRAY}]字典不够用时榨干每个词[/]"),
                ("mid",),
                ("section", "4. 暴力穷举"),
                ("kv", "原理", f"[{C_NS_WHITE}]无脑全试,所有字符所有长度[/]"),
                ("kv", "速度", f"[{C_NS_RED}]最慢,6位以上基本绝望[/]"),
                ("kv", "适用", f"[{C_NS_GRAY}]无任何线索,兜底方案[/]"),
                ("mid",),
                ("raw", f"[{C_NS_GRAY}]上下键选择,回车进入对应攻击模式[/]"),
            ]

            box_lines = _nushell_box(kv_lines, "攻击模式选择", box_width)
            return "\n".join(box_lines)

        def _render_dict_content(self) -> str:
            """渲染经典字典生成子页面右侧内容(nushell box 格式 + 右对齐)
            结构:绿色 box 包裹当前配置 + 生成历史(支持多次生成记录)
            """
            # box 宽度:终端宽度的一半左右,最小 50
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # ===== 收集当前配置 kv =====
            enabled_sets = []
            if self._dict_toggles.get("dict_lower"):
                enabled_sets.append("小写字母")
            if self._dict_toggles.get("dict_upper"):
                enabled_sets.append("大写字母")
            if self._dict_toggles.get("dict_digit"):
                enabled_sets.append("数字")
            if self._dict_toggles.get("dict_special"):
                enabled_sets.append("特殊字符")
            if self._dict_toggles.get("dict_single"):
                enabled_sets.append("单字符密码(1位)")

            sets_str = "、".join(enabled_sets) if enabled_sets else f"[{C_NS_RED}]未勾选[/]"

            # 长度范围
            try:
                min_l = int(self._dict_inputs.get("dict_min_len", "4"))
            except ValueError:
                min_l = 0
            try:
                max_l = int(self._dict_inputs.get("dict_max_len", "6"))
            except ValueError:
                max_l = 0
            if self._dict_toggles.get("dict_single"):
                length_str = f"[{C_NS_CYAN}]1[/] [{C_NS_GRAY}](单字符模式)[/]"
            else:
                length_str = f"[{C_NS_CYAN}]{min_l} ~ {max_l}[/]"

            # 输出目录
            out_dir = self._dict_inputs.get("dict_out_dir", "")
            out_dir_escaped = _esc_markup(out_dir)

            # 预估数量
            count_str = ""
            # 预估大小 + 磁盘剩余(生成数量限制下的预估值)
            size_str = ""
            disk_str = ""
            if enabled_sets:
                charset_len = 0
                est_charset_parts = []
                if self._dict_toggles.get("dict_lower"):
                    charset_len += len(_CHARSET_LOWER)
                    est_charset_parts.append(_CHARSET_LOWER)
                if self._dict_toggles.get("dict_upper"):
                    charset_len += len(_CHARSET_UPPER)
                    est_charset_parts.append(_CHARSET_UPPER)
                if self._dict_toggles.get("dict_digit"):
                    charset_len += len(_CHARSET_DIGIT)
                    est_charset_parts.append(_CHARSET_DIGIT)
                if self._dict_toggles.get("dict_special"):
                    charset_len += len(_CHARSET_SPECIAL)
                    est_charset_parts.append(_CHARSET_SPECIAL)
                # 解析生成数量(0 或空=全部生成)
                try:
                    max_lines_val = int(self._dict_inputs.get("dict_max_lines", "0") or "0")
                    if max_lines_val < 0:
                        max_lines_val = 0
                except ValueError:
                    max_lines_val = 0
                # 单字符模式:长度强制为1
                est_min = 1 if self._dict_toggles.get("dict_single") else min_l
                est_max = 1 if self._dict_toggles.get("dict_single") else max_l
                if self._dict_toggles.get("dict_single"):
                    count_str = f"[{C_NS_YELLOW}]{charset_len}[/]"
                elif charset_len > 0 and min_l > 0 and max_l >= min_l:
                    total = sum(charset_len ** L for L in range(min_l, max_l + 1))
                    if total > 1_000_000_000:
                        count_str = f"[{C_NS_YELLOW}]{total:.2e}[/] [{C_NS_GRAY}](过大,建议掩码模式)[/]"
                    else:
                        count_str = f"[{C_NS_YELLOW}]{total:,}[/]"
                # 预估大小 + 磁盘剩余(仅在有有效字符集和长度时计算)
                if charset_len > 0 and est_min > 0 and est_max >= est_min:
                    est_cfg = GenConfig(
                        output_file="",
                        mode=GenMode.CHARSET_COMB,
                        charset="".join(est_charset_parts),
                        min_length=est_min,
                        max_length=est_max,
                        max_lines=max_lines_val,
                    )
                    _, est_bytes = self._dict_gen.estimate(est_cfg)
                    size_str = f"[{C_NS_YELLOW}]{_fmt_bytes(est_bytes)}[/]"
                    # 磁盘剩余空间(取输出目录所在盘符)
                    free = _disk_free_bytes(out_dir) if out_dir else 0
                    if free > 0:
                        if est_bytes > free:
                            # 空间不足:红色警示
                            disk_str = f"[{C_NS_RED}]{_fmt_bytes(free)} (空间不足!)[/]"
                        else:
                            disk_str = f"[{C_NS_CYAN}]{_fmt_bytes(free)}[/]"
                    else:
                        disk_str = f"[{C_NS_GRAY}]无法获取[/]"

            # 生成数量展示文本(0=全部)
            max_lines_disp_val = self._dict_inputs.get("dict_max_lines", "0") or "0"
            try:
                mlv = int(max_lines_disp_val)
            except ValueError:
                mlv = 0
            max_lines_str = f"[{C_NS_CYAN}]全部[/]" if mlv == 0 else f"[{C_NS_YELLOW}]{mlv:,}[/]"

            # ===== 构建 box 内容 =====
            kv_lines = [
                ("section", "当前配置"),
                ("kv", "字符集", sets_str),
                ("kv", "长度范围", length_str),
                ("kv", "生成数量", max_lines_str),
            ]
            if count_str:
                kv_lines.append(("kv", "预估数量", count_str))
            if size_str:
                kv_lines.append(("kv", "预估大小", size_str))
            if disk_str:
                kv_lines.append(("kv", "磁盘剩余", disk_str))

            # 输出目录行:记录行号供 Ctrl+点击跳转
            # 行号 = box 渲染后该行的索引(顶部边框1 + section1 + kv若干)
            # 在 box 渲染后统一回填路径映射,这里先标记
            out_dir_line_marker = len(kv_lines)
            kv_lines.append(("kv", "输出目录", f"[{C_NS_CYAN}]{out_dir_escaped}[/]"))

            # 生成历史
            if self._dict_history:
                kv_lines.append(("mid",))
                kv_lines.append(("section", f"生成历史(共 {len(self._dict_history)} 次)"))
                for idx, record in enumerate(self._dict_history):
                    # 记录格式: (timestamp, output_file, result_text, extra_dict?)
                    ts_str = record[0]
                    output_file = record[1]
                    result_text = record[2]
                    extra = record[3] if len(record) > 3 else {}

                    kv_lines.append(("raw", f"[{C_NS_PURPLE}]#{idx + 1}[/] [{C_NS_GRAY}]{ts_str}[/]"))
                    kv_lines.append(("kv", "状态", result_text))
                    # extra 中的 kv 行
                    for k, (v_markup, orig_path) in extra.items():
                        # 标记输出文件行,供 Ctrl+点击跳转
                        if k == "输出文件" and orig_path:
                            marker_idx = len(kv_lines)
                            kv_lines.append(("kv", k, v_markup))
                            # 渲染后回填:在 box 渲染结果中找到该行
                            self._pending_path_markers.append(("classic", marker_idx, orig_path))
                        else:
                            kv_lines.append(("kv", k, v_markup))
                    if idx < len(self._dict_history) - 1:
                        kv_lines.append(("blank",))
            else:
                kv_lines.append(("mid",))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]配置完成后,左侧选择「9. 开始生成」并回车[/]"))

            # 渲染 box
            box_lines = _nushell_box(kv_lines, "经典字典生成", box_width)

            # 回填路径映射(输出目录行 + 输出文件行)
            # box_lines[0] = 顶部边框, [1] = section, [2..] = kv 行
            # 输出目录行在 box_lines 中的索引 = 1(section) + 字符集/长度/预估数量 + 1
            # 由于 kv_lines 顺序固定,可按 marker 索引计算
            # 顶部边框占1行,后续每个 kv_lines 项占1行
            # out_dir_line_marker = kv_lines 中"输出目录"项的索引
            out_dir_box_line_idx = out_dir_line_marker + 1  # +1 顶部边框
            if out_dir:
                self._content_path_map[out_dir_box_line_idx] = out_dir

            # 回填 pending markers
            for kind, marker_idx, orig_path in self._pending_path_markers:
                if kind == "classic":
                    self._content_path_map[marker_idx + 1] = orig_path
            self._pending_path_markers = []

            return "\n".join(box_lines)

        def _render_dict_social_content(self) -> str:
            """渲染社工字典生成子页面右侧内容(nushell box 格式 + 右对齐)
            结构:绿色 box 包裹已填信息摘要 + 生成历史(支持多次生成记录)
            """
            # box 宽度:终端宽度的一半左右,最小 50
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # ===== 收集已填信息 kv =====
            kv_lines = [("section", "已填信息摘要")]

            # 字段定义:(label, key, 分类) 按分类顺序排列
            fields = [
                # 基础信息
                ("中文姓名", "soc_name_cn"),
                ("拼音全拼", "soc_name_pinyin"),
                ("英文名",   "soc_name_en"),
                ("昵称",     "soc_nickname"),
                ("生日年份", "soc_birth_year"),
                ("生日月份", "soc_birth_month"),
                ("生日日期", "soc_birth_day"),
                ("完整生日", "soc_birth_full"),
                ("手机号",   "soc_phone"),
                ("QQ号",     "soc_qq"),
                ("微信号",   "soc_wechat"),
                ("邮箱",     "soc_email"),
                ("身份证号", "soc_id_card"),
                # 工作/学校
                ("公司名",   "soc_company"),
                ("职位",     "soc_position"),
                ("工号",     "soc_employee_id"),
                ("学校名",   "soc_school"),
                ("入学年份", "soc_school_year"),
                # 家庭/其他
                ("配偶姓名", "soc_spouse_name"),
                ("子女姓名", "soc_child_name"),
                ("宠物名",   "soc_pet_name"),
                ("纪念日",   "soc_anniversary"),
                ("车牌号",   "soc_car_plate"),
                # 习惯/其他
                ("喜好词汇", "soc_favorite_words"),
                ("幸运数字", "soc_lucky_numbers"),
                ("地区区号", "soc_area_code"),
                ("自定义后缀", "soc_common_suffixes"),
            ]

            filled_count = 0
            for label, key in fields:
                val = self._dict_social_inputs.get(key, "").strip()
                if val:
                    val_escaped = _esc_markup(val)
                    kv_lines.append(("kv", label, f"[{C_NS_CYAN}]{val_escaped}[/]"))
                    filled_count += 1

            if filled_count == 0:
                kv_lines.append(("raw", f"[{C_NS_YELLOW}]尚未填写任何信息字段[/]"))
            else:
                kv_lines.append(("raw", f"[{C_NS_GRAY}]已填 {filled_count} 个信息字段[/]"))

            # 输出目录
            out_dir = self._dict_social_inputs.get("soc_out_dir", "")
            out_dir_escaped = _esc_markup(out_dir)
            out_dir_line_marker = len(kv_lines)
            kv_lines.append(("kv", "输出目录", f"[{C_NS_CYAN}]{out_dir_escaped}[/]"))

            # 生成历史
            if self._dict_social_history:
                kv_lines.append(("mid",))
                kv_lines.append(("section", f"生成历史(共 {len(self._dict_social_history)} 次)"))
                for idx, record in enumerate(self._dict_social_history):
                    ts_str = record[0]
                    output_file = record[1]
                    result_text = record[2]
                    extra = record[3] if len(record) > 3 else {}

                    kv_lines.append(("raw", f"[{C_NS_PURPLE}]#{idx + 1}[/] [{C_NS_GRAY}]{ts_str}[/]"))
                    kv_lines.append(("kv", "状态", result_text))
                    for k, (v_markup, orig_path) in extra.items():
                        if k == "输出文件" and orig_path:
                            marker_idx = len(kv_lines)
                            kv_lines.append(("kv", k, v_markup))
                            self._pending_path_markers.append(("social", marker_idx, orig_path))
                        else:
                            kv_lines.append(("kv", k, v_markup))
                    if idx < len(self._dict_social_history) - 1:
                        kv_lines.append(("blank",))
            else:
                kv_lines.append(("mid",))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]填写目标信息后,左侧选择「29. 开始生成」并回车[/]"))

            # 渲染 box
            box_lines = _nushell_box(kv_lines, "社工字典生成", box_width)

            # 回填输出目录路径映射
            out_dir_box_line_idx = out_dir_line_marker + 1  # +1 顶部边框
            if out_dir:
                self._content_path_map[out_dir_box_line_idx] = out_dir

            # 回填 pending markers
            for kind, marker_idx, orig_path in self._pending_path_markers:
                if kind == "social":
                    self._content_path_map[marker_idx + 1] = orig_path
            self._pending_path_markers = []

            return "\n".join(box_lines)

        def _render_dict_mask_content(self) -> str:
            """渲染掩码字典生成子页面右侧内容(nushell box 格式 + 右对齐)
            结构:绿色 box 包裹当前配置(掩码/数量/预估值/磁盘剩余) + 生成历史
            """
            # box 宽度:与经典字典/社工字典一致
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # ===== 收集当前配置 =====
            mask_val = self._dict_mask_inputs.get("mask_input", "?d?d?d?d")
            mask_escaped = _esc_markup(mask_val)
            out_dir = self._dict_mask_inputs.get("mask_out_dir", "")
            out_dir_escaped = _esc_markup(out_dir)

            # 生成数量展示(0=全部)
            try:
                mlv = int(self._dict_mask_inputs.get("mask_max_lines", "0") or "0")
            except ValueError:
                mlv = 0
            max_lines_str = f"[{C_NS_CYAN}]全部[/]" if mlv == 0 else f"[{C_NS_YELLOW}]{mlv:,}[/]"

            # 预估行数和字节数
            count_str = ""
            size_str = ""
            disk_str = ""
            try:
                est_cfg = GenConfig(
                    output_file="",
                    mode=GenMode.MASK,
                    mask=mask_val,
                    max_lines=mlv,
                )
                est_lines, est_bytes = self._dict_gen.estimate(est_cfg)
                if est_lines > 0:
                    count_str = f"[{C_NS_YELLOW}]{est_lines:,}[/]"
                    size_str = f"[{C_NS_YELLOW}]{_fmt_bytes(est_bytes)}[/]"
                    free = _disk_free_bytes(out_dir) if out_dir else 0
                    if free > 0:
                        if est_bytes > free:
                            disk_str = f"[{C_NS_RED}]{_fmt_bytes(free)} (空间不足!)[/]"
                        else:
                            disk_str = f"[{C_NS_CYAN}]{_fmt_bytes(free)}[/]"
                    else:
                        disk_str = f"[{C_NS_GRAY}]无法获取[/]"
                else:
                    count_str = f"[{C_NS_RED}]掩码无效[/]"
            except Exception:  # noqa: BLE001
                count_str = f"[{C_NS_RED}]掩码解析失败[/]"

            # ===== 构建 box 内容 =====
            kv_lines = [
                ("section", "当前配置"),
                ("kv", "掩码", f"[{C_NS_CYAN}]{mask_escaped}[/]"),
                ("kv", "生成数量", max_lines_str),
            ]
            if count_str:
                kv_lines.append(("kv", "预估数量", count_str))
            if size_str:
                kv_lines.append(("kv", "预估大小", size_str))
            if disk_str:
                kv_lines.append(("kv", "磁盘剩余", disk_str))

            # 输出目录行:记录行号供 Ctrl+点击跳转
            out_dir_line_marker = len(kv_lines)
            kv_lines.append(("kv", "输出目录", f"[{C_NS_CYAN}]{out_dir_escaped}[/]"))

            # 掩码占位符说明(帮助用户理解)
            kv_lines.append(("mid",))
            kv_lines.append(("section", "占位符说明"))
            kv_lines.append(("kv", "?l", f"[{C_NS_GRAY}]小写字母 a-z (26)[/]"))
            kv_lines.append(("kv", "?u", f"[{C_NS_GRAY}]大写字母 A-Z (26)[/]"))
            kv_lines.append(("kv", "?d", f"[{C_NS_GRAY}]数字 0-9 (10)[/]"))
            kv_lines.append(("kv", "?s", f"[{C_NS_GRAY}]特殊字符 (33)[/]"))
            kv_lines.append(("kv", "?a", f"[{C_NS_GRAY}]全部可打印字符 (95)[/]"))
            kv_lines.append(("raw", f"[{C_NS_GRAY}]字面字符可直接混入,如 pass?d?d[/]"))

            # 生成历史
            if self._dict_mask_history:
                kv_lines.append(("mid",))
                kv_lines.append(("section", f"生成历史(共 {len(self._dict_mask_history)} 次)"))
                for idx, record in enumerate(self._dict_mask_history):
                    # record 结构:(ts, output_file, result_text, extra_dict)
                    ts_r, out_file_r, result_r = record[0], record[1], record[2]
                    extra_r = record[3] if len(record) > 3 else {}
                    ts_esc = _esc_markup(ts_r)
                    kv_lines.append(("raw", f"[{C_NS_GRAY}]{ts_esc}[/] [{C_NS_GREEN}]✓[/] {result_r}"))
                    for k, (v_markup, orig_path) in extra_r.items():
                        kv_lines.append(("kv", k, v_markup))
                        # 记录路径映射(输出文件行可 Ctrl+点击打开)
                        if orig_path:
                            self._pending_path_markers.append(("mask", len(kv_lines) - 1, orig_path))
                    if idx < len(self._dict_mask_history) - 1:
                        kv_lines.append(("blank",))

            box_lines = _nushell_box(kv_lines, "掩码字典生成", box_width)

            # 回填路径映射:行号 = 顶部边框(1) + kv_lines 索引
            for kind, marker_idx, orig_path in self._pending_path_markers:
                if kind == "mask":
                    self._content_path_map[marker_idx + 1] = orig_path
            self._pending_path_markers = []

            return "\n".join(box_lines)

        def _render_crack_dict_content(self) -> str:
            """渲染字典攻击子页面右侧内容(nushell box 格式 + 右对齐)
            结构:绿色 box 包裹当前配置(压缩包/字典/工作负载/Hashcat状态) + 破解历史
            样式与掩码字典生成页一致
            """
            # box 宽度:与字典生成子页面一致
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)

            # ===== 收集当前配置 =====
            archive_val = self._crack_dict_inputs.get("crack_dict_archive", "")
            archive_escaped = _esc_markup(archive_val)
            dict_val = self._crack_dict_inputs.get("crack_dict_dict", "")
            dict_escaped = _esc_markup(dict_val)
            workload_val = self._crack_dict_inputs.get("crack_dict_workload", "3")

            # 工作负载展示(带说明)
            workload_map = {
                "1": "1=低(后台)",
                "2": "2=中低",
                "3": "3=高(默认)",
                "4": "4=极致",
            }
            workload_str = workload_map.get(workload_val, f"{workload_val}(自定义)")

            # Hashcat 可用性检查
            hashcat_ok = self._cracker.is_available()

            # 字典文件数量统计
            dict_count = len([d for d in dict_val.split(",") if d.strip()]) if dict_val.strip() else 0

            # ===== 构建 box 内容 =====
            kv_lines = []

            # 拖入等待模式:顶部显示醒目提示
            if self._crack_dict_drop_mode:
                kv_lines.append(("section", "等待拖入文件"))
                kv_lines.append(("raw", f"[{C_NS_YELLOW} bold]请将文件拖入终端窗口[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]zip/rar/7z → 压缩包[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]txt/dic/lst → 字典[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]ESC 取消[/]"))
                kv_lines.append(("mid",))

            kv_lines.append(("section", "当前配置"))
            kv_lines.append(("kv", "压缩包", archive_escaped or f"[{C_NS_GRAY}]未选择[/]"))
            kv_lines.append(("kv", "字典文件", dict_escaped or f"[{C_NS_GRAY}]未选择[/]"))
            if dict_count > 1:
                kv_lines.append(("kv", "字典数量", f"[{C_NS_YELLOW}]{dict_count} 个[/]"))
            kv_lines.append(("kv", "工作负载", f"[{C_NS_CYAN}]{workload_str}[/]"))
            # 设备类型显示
            device_val = self._crack_dict_inputs.get("crack_dict_device", "auto")
            if device_val == "gpu":
                kv_lines.append(("kv", "设备", f"[{C_NS_GREEN}]强制GPU[/]"))
            elif device_val == "cpu":
                kv_lines.append(("kv", "设备", f"[{C_NS_GRAY}]强制CPU[/]"))
            else:
                kv_lines.append(("kv", "设备", f"[{C_NS_GREEN}]自动[/]"))

            # 实时进度(仅破解运行中显示)
            if self._crack_dict_running and self._crack_dict_live:
                kv_lines.append(("mid",))
                kv_lines.append(("section", "实时进度"))
                live = self._crack_dict_live
                status_text = live.get("status_text", "")
                if status_text:
                    kv_lines.append(("kv", "状态", f"[{C_NS_YELLOW}]{_esc_markup(status_text)}[/]"))
                speed = live.get("speed")
                if speed:
                    kv_lines.append(("kv", "速度", f"[{C_NS_GREEN}]{_esc_markup(speed)}[/]"))
                # 绝对进度数(已试/总数) + 百分比
                progress_abs = live.get("progress_abs")
                pct = live.get("percent")
                if progress_abs:
                    # 形如 "18/36"
                    kv_lines.append(("kv", "已试/总数", f"[{C_NS_CYAN}]{progress_abs}[/]"))
                if pct is not None:
                    kv_lines.append(("kv", "百分比", f"[{C_NS_CYAN}]{pct:.1f}%[/]"))
                # 候选密码顺序(当前正在尝试的密码区间)
                candidates = live.get("candidates")
                if candidates:
                    kv_lines.append(("kv", "当前候选", f"[{C_NS_GRAY}]{_esc_markup(candidates)}[/]"))
                # 已破解密码(仅真实破解成功时显示,过滤假密码)
                pwd = live.get("recovered_pwd")
                if pwd:
                    kv_lines.append(("kv", "已破解密码", f"[{C_NS_GREEN} bold]{_esc_markup(pwd)}[/]"))
                elapsed = live.get("elapsed")
                if elapsed is not None:
                    kv_lines.append(("kv", "耗时", f"[{C_NS_GRAY}]{elapsed:.1f} 秒[/]"))

            # Hashcat 状态
            kv_lines.append(("mid",))
            kv_lines.append(("section", "环境状态"))
            if hashcat_ok:
                kv_lines.append(("kv", "Hashcat", f"[{C_NS_GREEN}]可用[/]"))
            else:
                kv_lines.append(("kv", "Hashcat", f"[{C_NS_RED}]未找到[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]请检查 bin/ 目录[/]"))

            # 配置完整性检查
            kv_lines.append(("mid",))
            kv_lines.append(("section", "配置检查"))
            checks = []
            if not archive_val.strip():
                checks.append(("raw", f"[{C_NS_RED}]✗ 未选择压缩包[/]"))
            elif not Path(archive_val.strip()).exists():
                checks.append(("raw", f"[{C_NS_RED}]✗ 压缩包不存在[/]"))
            else:
                checks.append(("raw", f"[{C_NS_GREEN}]✓ 压缩包已选择[/]"))
            if not dict_val.strip():
                checks.append(("raw", f"[{C_NS_RED}]✗ 未选择字典[/]"))
            else:
                # 检查每个字典文件是否存在
                missing = [d.strip() for d in dict_val.split(",")
                           if d.strip() and not Path(d.strip()).exists()]
                if missing:
                    checks.append(("raw", f"[{C_NS_RED}]✗ {len(missing)}个字典不存在[/]"))
                else:
                    checks.append(("raw", f"[{C_NS_GREEN}]✓ 字典已选择({dict_count}个)[/]"))
            if not hashcat_ok:
                checks.append(("raw", f"[{C_NS_RED}]✗ Hashcat不可用[/]"))
            kv_lines.extend(checks)

            # 破解历史
            if self._crack_dict_history:
                kv_lines.append(("mid",))
                kv_lines.append(("section", f"破解历史(共 {len(self._crack_dict_history)} 次)"))
                for idx, record in enumerate(self._crack_dict_history):
                    # record 结构:(ts, result_file, result_text, extra_dict)
                    ts_r, out_file_r, result_r = record[0], record[1], record[2]
                    extra_r = record[3] if len(record) > 3 else {}
                    ts_esc = _esc_markup(ts_r)
                    kv_lines.append(("raw", f"[{C_NS_GRAY}]{ts_esc}[/] {result_r}"))
                    for k, (v_markup, orig_path) in extra_r.items():
                        kv_lines.append(("kv", k, v_markup))
                        # 记录路径映射(结果文件行可 Ctrl+点击打开)
                        if orig_path:
                            self._pending_path_markers.append(("crack_dict", len(kv_lines) - 1, orig_path))
                    if idx < len(self._crack_dict_history) - 1:
                        kv_lines.append(("blank",))
            else:
                kv_lines.append(("mid",))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]配置完成后,左侧选择「5. 开始破解」并回车[/]"))

            box_lines = _nushell_box(kv_lines, "字典攻击", box_width)

            # 回填路径映射:行号 = 顶部边框(1) + kv_lines 索引
            for kind, marker_idx, orig_path in self._pending_path_markers:
                if kind == "crack_dict":
                    self._content_path_map[marker_idx + 1] = orig_path
            self._pending_path_markers = []

            return "\n".join(box_lines)

        def _do_dict_mask_generate(self) -> None:
            """执行掩码字典生成(调用 core 层 DictGenerator.generate)
            流程:
                1. 校验掩码合法性(通过 estimate 探测)
                2. 磁盘空间检查:预估大小 > 剩余空间 → 弹 InfoScreen 阻断
                3. 数量过多:预估行数 > 阈值 → 弹 ConfirmScreen 二次确认
                4. 确认后调用 _execute_mask_generate 执行生成
            """
            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 1. 校验掩码
            mask_val = self._dict_mask_inputs.get("mask_input", "").strip()
            if not mask_val:
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：掩码不能为空[/]",
                ))
                self._trim_history("mask")
                return

            # 2. 解析生成数量
            try:
                max_lines = int(self._dict_mask_inputs.get("mask_max_lines", "0") or "0")
                if max_lines < 0:
                    max_lines = 0
            except ValueError:
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：生成数量必须是数字[/]",
                ))
                self._trim_history("mask")
                return

            # 3. 校验输出目录
            out_dir = self._dict_mask_inputs.get("mask_out_dir", "")
            if not out_dir:
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：输出目录不能为空[/]",
                ))
                self._trim_history("mask")
                return
            out_dir_path = Path(out_dir)
            try:
                out_dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                err_escaped = _esc_markup(str(exc))
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：创建输出目录失败: {err_escaped}[/]",
                ))
                self._trim_history("mask")
                return

            # 4. 构建输出文件名
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = out_dir_path / f"mask_{ts}.txt"

            # 5. 构建配置
            cfg = GenConfig(
                output_file=str(out_file),
                mode=GenMode.MASK,
                mask=mask_val,
                max_lines=max_lines,
            )

            # 6. 预估(同时验证掩码合法性)
            try:
                est_lines, est_bytes = self._dict_gen.estimate(cfg)
            except Exception as exc:  # noqa: BLE001
                err_escaped = _esc_markup(str(exc))
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：掩码解析失败: {err_escaped}[/]",
                ))
                self._trim_history("mask")
                return
            if est_lines == 0:
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：掩码无效,请检查占位符[/]",
                ))
                self._trim_history("mask")
                return

            # 7. 磁盘空间检查
            free = _disk_free_bytes(out_dir)
            if free > 0 and est_bytes > free:
                msg = (
                    f"预估字典大小: {_fmt_bytes(est_bytes)}\n"
                    f"磁盘剩余空间: {_fmt_bytes(free)}\n"
                    f"存储空间不足,请减少字典生成数量\n"
                    f"或更换余量更充足的盘符"
                )
                self.push_screen(
                    InfoScreen(msg, "空间不足"),
                    lambda _: self._render_content(),
                )
                return

            # 8. 数量过多确认
            if est_lines > _DICT_LARGE_COUNT_THRESHOLD:
                msg = (
                    f"预估生成数量: {est_lines:,} 行\n"
                    f"预估大小: {_fmt_bytes(est_bytes)}\n"
                    f"数量可能过多,确定要生成吗?"
                )
                self._pending_mask_cfg = cfg
                self._pending_mask_ts = ts_now
                self.push_screen(
                    ConfirmScreen(msg, "数量确认"),
                    lambda confirmed: self._on_mask_confirm(confirmed),
                )
                return

            # 9. 正常执行
            self._execute_mask_generate(cfg, ts_now)

        def _on_mask_confirm(self, confirmed: bool) -> None:
            """掩码字典数量过多确认弹窗的回调
            :param confirmed: True=确认生成, False=取消
            """
            if confirmed:
                cfg = self._pending_mask_cfg
                ts = self._pending_mask_ts
                if cfg is not None:
                    self._execute_mask_generate(cfg, ts)
            self._pending_mask_cfg = None
            self._pending_mask_ts = ""
            self._render_content()

        def _execute_mask_generate(self, cfg: GenConfig, ts_now: str) -> None:
            """实际执行掩码字典生成并写入历史记录
            :param cfg: 已构建好的生成配置(含 mask, max_lines)
            :param ts_now: 时间戳字符串(用于历史记录)
            """
            result = self._dict_gen.generate(cfg)
            if result.success:
                out_file_escaped = _esc_markup(result.output_file or "")
                result_text = f"[{C_NS_GREEN}]成功[/]"
                extra = {
                    "输出文件": (f"[{C_NS_CYAN}]{out_file_escaped}[/]", result.output_file),
                    "掩码": (f"[{C_NS_CYAN}]{_esc_markup(cfg.mask or '')}[/]", None),
                    "总行数": (f"[{C_NS_YELLOW}]{result.total_lines:,}[/]", None),
                    "文件大小": (f"[{C_NS_YELLOW}]{_fmt_bytes(result.size_bytes)}[/]", None),
                    "耗时": (f"[{C_NS_GRAY}]{result.duration_seconds:.3f} 秒[/]", None),
                }
                self._dict_mask_history.insert(0, (
                    ts_now, result.output_file, result_text, extra,
                ))
            else:
                err_escaped = _esc_markup(result.error_message or "")
                self._dict_mask_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]失败[/]",
                    {"错误信息": (f"[{C_NS_RED}]{err_escaped}[/]", None)},
                ))
            self._trim_history("mask")

        # 压缩包扩展名集合(自动识别用)
        _ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz"}
        # 字典文件扩展名集合(自动识别用)
        _DICT_EXTS = {".txt", ".dic", ".lst", ".dict"}

        def _classify_dropped_file(self, file_path: str) -> str:
            """根据扩展名自动识别文件类型
            :param file_path: 文件路径
            :return: "archive"=压缩包, "dict"=字典, "unknown"=未知
            """
            ext = Path(file_path).suffix.lower()
            if ext in self._ARCHIVE_EXTS:
                return "archive"
            if ext in self._DICT_EXTS:
                return "dict"
            return "unknown"

        def _reset_crack_dict_drop_timer(self) -> None:
            """重置拖入路径的空闲超时定时器
            - 取消已有定时器(若存在)
            - 按空闲阈值(_crack_dict_drop_idle_ms)新建定时器
            - 定时器触发回调 _on_crack_dict_drop_idle 处理累积完成的路径
            设计意图:拖入字符间隔极快(微秒级),完成后静默明显,
            用空闲超时作为路径接收完毕的信号,避免误判 ctrl+@ 为结束符
            """
            # 取消已有定时器
            if self._crack_dict_drop_timer is not None:
                try:
                    self._crack_dict_drop_timer.stop()
                except Exception:
                    pass
                self._crack_dict_drop_timer = None
            # 新建定时器:Textual 的 set_timer 接受秒
            idle_seconds = self._crack_dict_drop_idle_ms / 1000.0
            self._crack_dict_drop_timer = self.set_timer(
                idle_seconds, self._on_crack_dict_drop_idle
            )

        def _on_crack_dict_drop_idle(self) -> None:
            """拖入路径空闲超时回调
            - 触发条件:累积路径后超过 _crack_dict_drop_idle_ms 无新按键
            - 动作:取出累积路径,清空缓冲区,调用 _handle_crack_dict_paste 分类填入
            - 边界:空路径或仅含空白的路径直接丢弃
            """
            path = self._crack_dict_drop_buffer
            self._crack_dict_drop_buffer = None
            self._crack_dict_drop_timer = None
            if path and path.strip():
                self._handle_crack_dict_paste(path)

        def _handle_crack_dict_paste(self, paste_text: str) -> bool:
            """处理字典攻击页面的文件拖入(Paste 事件)
            :param paste_text: 终端粘贴的文本(通常为文件路径,可能多个)
            :return: True=已处理, False=空内容
            流程:
                1. 按换行拆分出多个路径
                2. 清理路径两端的引号和空白
                3. 按扩展名自动分类(压缩包/字典)
                4. 压缩包:覆盖写入 archive 字段(只保留最后一个)
                5. 字典:追加到 dict 字段(逗号分隔)
                6. 未知文件:忽略
            注:调用方(on_paste)已负责退出输入模式和拖入模式,本方法仅处理内容
            """
            # 拆分路径:Windows Terminal 拖入多文件用换行分隔
            # 路径可能含空格(被引号包围),需先按引号分割
            raw = paste_text.strip()
            if not raw:
                return False

            # 解析路径列表:支持 "C:\\path with space\\a.txt" C:\\b.txt 形式
            paths: list = []
            # 先按引号配对提取带空格的路径
            parts = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                # 去除首尾引号
                if (part.startswith('"') and part.endswith('"')) or \
                   (part.startswith("'") and part.endswith("'")):
                    part = part[1:-1]
                paths.append(part)

            if not paths:
                return True

            # 分类统计
            archives: list = []
            dicts: list = []
            unknown: list = []
            for p in paths:
                ftype = self._classify_dropped_file(p)
                if ftype == "archive":
                    archives.append(p)
                elif ftype == "dict":
                    dicts.append(p)
                else:
                    unknown.append(p)

            # 写入字段
            # 压缩包:覆盖写入最后一个(通常只拖一个)
            if archives:
                self._crack_dict_inputs["crack_dict_archive"] = archives[-1]

            # 字典:追加到已有列表(逗号分隔,去重)
            if dicts:
                existing = self._crack_dict_inputs.get("crack_dict_dict", "")
                existing_list = [d.strip() for d in existing.split(",") if d.strip()] if existing else []
                # 合并去重(保持顺序)
                merged: list = []
                seen: set = set()
                for d in existing_list + dicts:
                    if d not in seen:
                        merged.append(d)
                        seen.add(d)
                self._crack_dict_inputs["crack_dict_dict"] = ",".join(merged)

            # 写入历史记录(提示用户识别结果)
            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            summary_parts: list = []
            if archives:
                summary_parts.append(f"[{C_NS_GREEN}]压缩包×{len(archives)}[/]")
            if dicts:
                summary_parts.append(f"[{C_NS_CYAN}]字典×{len(dicts)}[/]")
            if unknown:
                summary_parts.append(f"[{C_NS_GRAY}]未知×{len(unknown)}[/]")
            summary = " ".join(summary_parts) if summary_parts else f"[{C_NS_GRAY}]无有效文件[/]"

            extra: dict = {}
            if archives:
                last_arc = _esc_markup(Path(archives[-1]).name)
                extra["压缩包"] = (f"[{C_NS_CYAN}]{last_arc}[/]", None)
            if dicts:
                dict_names = [_esc_markup(Path(d).name) for d in dicts[:3]]
                if len(dicts) > 3:
                    dict_names.append(f"[{C_NS_GRAY}]等{len(dicts)}个[/]")
                extra["字典"] = (f"[{C_NS_CYAN}]{', '.join(dict_names)}[/]", None)
            if unknown:
                unk_names = [_esc_markup(Path(u).name) for u in unknown[:2]]
                if len(unknown) > 2:
                    unk_names.append(f"[{C_NS_GRAY}]等{len(unknown)}个[/]")
                extra["未识别"] = (f"[{C_NS_YELLOW}]{', '.join(unk_names)}[/]", None)

            self._crack_dict_history.insert(0, (
                ts_now, None,
                f"[{C_NS_GREEN}]拖入识别[/] {summary}",
                extra,
            ))
            self._trim_history("crack_dict")

            # 重渲染
            self._render_menu()
            self._render_content()
            return True

        # ================================================================
        # 掩码/字典加规则/暴力穷举 通用引擎
        # ================================================================

        def _crack_state(self, page: str) -> dict:
            """获取新模式页面的运行时状态字典。"""
            return self._crack_mode_states[page]

        def _crack_mode_items(self, page: str) -> list:
            if page == "crack_mask":
                return _CRACK_MASK_ITEMS
            if page == "crack_rule":
                return _CRACK_RULE_ITEMS
            return _CRACK_BRUTE_ITEMS

        def _crack_mode_title(self, page: str) -> str:
            return {
                "crack_mask": "掩码攻击",
                "crack_rule": "字典加规则",
                "crack_brute": "暴力穷举",
            }[page]

        def _crack_mode_help(self, page: str) -> list:
            if page == "crack_mask":
                return _HELP_CRACK_MASK
            if page == "crack_rule":
                return _HELP_CRACK_RULE
            return _HELP_CRACK_BRUTE

        def _crack_mode_in_input(self) -> bool:
            """是否处于任一新模式页面的输入模式。"""
            for page in _CRACK_MODE_PAGES:
                if self._crack_state(page)["input_mode"] is not None:
                    return True
            return False

        def _reset_crack_mode_states(self, page: str, clear_inputs: bool = False) -> None:
            """统一清理新模式页面的运行态。"""
            state = self._crack_state(page)
            try:
                if state.get("drop_timer") is not None:
                    try:
                        state["drop_timer"].stop()
                    except Exception:  # noqa: BLE001
                        pass
                    state["drop_timer"] = None
            except Exception:  # noqa: BLE001
                pass
            try:
                state["drop_buffer"] = None
            except Exception:  # noqa: BLE001
                pass
            try:
                state["drop_mode"] = False
            except Exception:  # noqa: BLE001
                pass
            try:
                state["running"] = False
            except Exception:  # noqa: BLE001
                pass
            try:
                state["live"] = {}
            except Exception:  # noqa: BLE001
                pass
            if clear_inputs:
                try:
                    state["input_mode"] = None
                except Exception:  # noqa: BLE001
                    pass
                try:
                    state["input_buf"] = ""
                except Exception:  # noqa: BLE001
                    pass

        def _reset_crack_mode_drop_timer(self, page: str) -> None:
            """重置新模式页面的拖入空闲定时器。"""
            state = self._crack_state(page)
            if state.get("drop_timer") is not None:
                try:
                    state["drop_timer"].stop()
                except Exception:  # noqa: BLE001
                    pass
                state["drop_timer"] = None
            idle_seconds = self._crack_dict_drop_idle_ms / 1000.0
            state["drop_timer"] = self.set_timer(
                idle_seconds, lambda: self._on_crack_mode_drop_idle(page)
            )

        def _on_crack_mode_drop_idle(self, page: str) -> None:
            """新模式页面拖入空闲超时回调。"""
            state = self._crack_state(page)
            path = state.get("drop_buffer")
            state["drop_buffer"] = None
            state["drop_timer"] = None
            if path and path.strip():
                self._handle_crack_mode_paste(page, path)

        def _handle_crack_mode_paste(self, page: str, paste_text: str) -> bool:
            """处理新模式页面的文件拖入(自动识别压缩包/字典/规则)。"""
            raw = paste_text.strip()
            if not raw:
                return False
            paths: list = []
            parts = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if (part.startswith('"') and part.endswith('"')) or \
                   (part.startswith("'") and part.endswith("'")):
                    part = part[1:-1]
                paths.append(part)
            if not paths:
                return True

            archives: list = []
            dicts: list = []
            rules: list = []
            unknown: list = []
            for p in paths:
                ext = Path(p).suffix.lower()
                if ext in self._ARCHIVE_EXTS:
                    archives.append(p)
                elif ext in self._DICT_EXTS:
                    dicts.append(p)
                elif ext in (".rule", ".rules"):
                    rules.append(p)
                else:
                    unknown.append(p)

            state = self._crack_state(page)
            inputs = state["inputs"]
            if archives:
                inputs[f"{page}_archive"] = archives[-1]
            if dicts and page == "crack_rule":
                existing = inputs.get(f"{page}_dict", "")
                existing_list = [d.strip() for d in existing.split(",") if d.strip()] if existing else []
                merged: list = []
                seen: set = set()
                for d in existing_list + dicts:
                    if d not in seen:
                        merged.append(d)
                        seen.add(d)
                inputs[f"{page}_dict"] = ",".join(merged)
            if rules and page == "crack_rule":
                inputs[f"{page}_file"] = rules[-1]

            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            summary_parts: list = []
            if archives:
                summary_parts.append(f"[{C_NS_GREEN}]压缩包×{len(archives)}[/]")
            if dicts:
                summary_parts.append(f"[{C_NS_CYAN}]字典×{len(dicts)}[/]")
            if rules:
                summary_parts.append(f"[{C_NS_YELLOW}]规则×{len(rules)}[/]")
            if unknown:
                summary_parts.append(f"[{C_NS_GRAY}]未知×{len(unknown)}[/]")
            summary = " ".join(summary_parts) if summary_parts else f"[{C_NS_GRAY}]无有效文件[/]"

            state["history"].insert(0, (
                ts_now, None,
                f"[{C_NS_GREEN}]拖入识别[/] {summary}",
                {"识别": (f"[{C_NS_GRAY}]{summary}[/]", None)},
            ))
            self._trim_history("crack_mode_" + page)
            self._render_menu()
            self._render_content()
            return True

        def _build_crack_mode_config(self, page: str):
            """按模式构造 CrackConfig(哈希字段由 worker 提取后回填)。"""
            state = self._crack_state(page)
            inputs = state["inputs"]
            prefix = f"{page}_"

            try:
                workload = int(inputs.get(prefix + "workload", "3"))
                if workload < 1 or workload > 4:
                    workload = 3
            except ValueError:
                workload = 3

            device_val = inputs.get(prefix + "device", "auto").strip().lower()
            if device_val in ("gpu", "2", "force_gpu"):
                device_type = "force_gpu"
            elif device_val in ("cpu", "1", "force_cpu"):
                device_type = "force_cpu"
            else:
                device_type = "auto"

            if page == "crack_mask":
                mask = inputs.get("crack_mask_expr", "").strip()
                if not mask:
                    raise ValueError("掩码表达式不能为空")
                return CrackConfig(
                    hash_file_path="", hashcat_mode=0,
                    attack_mode=AttackMode.MASK, mask=mask,
                    work_load_profile=workload, device_type=device_type,
                )

            if page == "crack_rule":
                dict_val = inputs.get("crack_rule_dict", "").strip()
                dict_paths = [d.strip() for d in dict_val.split(",") if d.strip()]
                if not dict_paths:
                    raise ValueError("未选择字典文件")
                missing = [d for d in dict_paths if not Path(d).exists()]
                if missing:
                    raise ValueError(f"字典不存在: {missing[0]}")
                rule_file = inputs.get("crack_rule_file", "").strip()
                if not rule_file:
                    raise ValueError("未选择规则文件")
                if not Path(rule_file).exists():
                    raise ValueError("规则文件不存在")
                return CrackConfig(
                    hash_file_path="", hashcat_mode=0,
                    attack_mode=AttackMode.DICT,
                    dictionary_paths=dict_paths, rules_file=rule_file,
                    work_load_profile=workload, device_type=device_type,
                )

            # 暴力穷举：字符集 + 最小/最大长度，走 hashcat 增量掩码
            custom = inputs.get("crack_brute_custom", "").strip()
            toggles = state["toggles"]
            if custom:
                charset = custom
            else:
                charset_parts: list = []
                if toggles.get("crack_brute_lower"):
                    charset_parts.append("abcdefghijklmnopqrstuvwxyz")
                if toggles.get("crack_brute_upper"):
                    charset_parts.append("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                if toggles.get("crack_brute_digit"):
                    charset_parts.append("0123456789")
                if toggles.get("crack_brute_special"):
                    charset_parts.append("!@#$%^&*()-_=+[]{};:,.<>?/")
                charset = "".join(charset_parts)
            if not charset:
                raise ValueError("至少勾选一个字符集或填写自定义字符集")

            try:
                min_len = int(inputs.get("crack_brute_min_len", "1"))
            except ValueError:
                raise ValueError("最小长度必须是数字")
            try:
                max_len = int(inputs.get("crack_brute_max_len", "8"))
            except ValueError:
                raise ValueError("最大长度必须是数字")
            if min_len < 1 or max_len < min_len:
                raise ValueError("长度范围不合法")

            extra_args = [
                "--increment",
                "--increment-min", str(min_len),
                "--increment-max", str(max_len),
                f"--custom-charset1={charset}",
            ]
            return CrackConfig(
                hash_file_path="", hashcat_mode=0,
                attack_mode=AttackMode.MASK, mask="?1" * max_len,
                work_load_profile=workload, device_type=device_type,
                extra_args=extra_args,
            )

        def _do_crack_mode_run(self, page: str) -> None:
            """执行新模式破解(掩码/规则/暴力)。"""
            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state = self._crack_state(page)
            inputs = state["inputs"]

            self._reset_crack_mode_states(page, clear_inputs=True)

            archive_val = inputs.get(f"{page}_archive", "").strip()
            if not archive_val:
                state["history"].insert(0, (
                    ts_now, None, f"[{C_NS_RED}]错误:未选择压缩包[/]",
                ))
                self._trim_history("crack_mode_" + page)
                self._render_content()
                return
            if not Path(archive_val).exists():
                state["history"].insert(0, (
                    ts_now, None, f"[{C_NS_RED}]错误:压缩包不存在[/]",
                ))
                self._trim_history("crack_mode_" + page)
                self._render_content()
                return

            try:
                cfg = self._build_crack_mode_config(page)
            except ValueError as exc:
                state["history"].insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:{_esc_markup(str(exc))}[/]",
                ))
                self._trim_history("crack_mode_" + page)
                self._render_content()
                return

            if not self._cracker.is_available():
                state["history"].insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:Hashcat不可用,请检查bin目录[/]",
                ))
                self._trim_history("crack_mode_" + page)
                self._render_content()
                return

            if state["running"]:
                state["history"].insert(0, (
                    ts_now, None,
                    f"[{C_NS_YELLOW}]已有破解任务正在运行,按 ESC 中断[/]",
                ))
                self._trim_history("crack_mode_" + page)
                self._render_content()
                return

            state["history"].insert(0, (
                ts_now, None,
                f"[{C_NS_YELLOW}]正在提取哈希...[/]",
            ))
            self._trim_history("crack_mode_" + page)
            state["running"] = True
            state["live"] = {"status_text": "初始化", "elapsed": 0.0}
            self._render_content()
            self._crack_mode_worker(page, archive_val, cfg, ts_now)

        def _crack_mode_progress_callback(self, start_ts: float, state: dict):
            """构造新模式页面的实时进度回调。"""
            import time as _time
            _STATUS_CN = {
                "running": "运行中",
                "cracked": "已破解",
                "exhausted": "字典试完",
                "stopped": "已停止",
                "error": "错误",
                "init": "初始化",
                "autotune": "调优中",
            }
            _FAKE = ("hashcat.net/faq", "No device", "Invalid argument")
            _STATUS_PREFIXES = (
                "Recovered", "Progress", "Speed", "Status", "Candidates",
                "Session", "Hash.Mode", "Hash.Target", "Time.", "Kernel",
                "Guess.", "Restore.", "Rejected", "Hardware", "Started",
                "Stopped", "Candidate.Engine", "Bitmaps", "Rules",
                "Watchdog", "Initializing", "Host memory", "Dictionary",
                "Approaching", "[s]tatus",
            )

            def _on_progress(progress) -> None:
                live: dict = {"elapsed": _time.time() - start_ts}
                if progress.status:
                    live["status_text"] = _STATUS_CN.get(
                        progress.status.value, progress.status.value
                    )
                if progress.speed_hs and progress.speed_hs > 0:
                    if progress.speed_hs >= 1e6:
                        live["speed"] = f"{progress.speed_hs/1e6:.2f} MH/s"
                    elif progress.speed_hs >= 1e3:
                        live["speed"] = f"{progress.speed_hs/1e3:.2f} KH/s"
                    else:
                        live["speed"] = f"{progress.speed_hs:.0f} H/s"
                if progress.progress_percent and progress.progress_percent > 0:
                    live["percent"] = progress.progress_percent
                if progress.progress_abs:
                    live["progress_abs"] = progress.progress_abs
                if progress.candidates:
                    live["candidates"] = progress.candidates
                if progress.recovered > 0 and progress.raw_line:
                    raw = progress.raw_line
                    if ":" in raw and not raw.startswith(_STATUS_PREFIXES):
                        parts = raw.split(":", 1)
                        lhs = parts[0]
                        rhs = parts[1] if len(parts) > 1 else ""
                        if ("$" in lhs or len(lhs) > 32) and rhs:
                            if not any(m in rhs for m in _FAKE):
                                live["recovered_pwd"] = rhs
                self.call_from_thread(self._refresh_crack_mode_live, state, live)

            return _on_progress

        def _refresh_crack_mode_live(self, state: dict, live: dict) -> None:
            """刷新新模式页面实时进度(主线程执行)。"""
            state["live"].update(live)
            import time as _time
            now = _time.time()
            # 节流:hashcat 状态行较密集,避免每次都全量重绘右侧导致卡顿
            if now - self._last_live_render >= 0.3:
                self._last_live_render = now
                self._render_content()

        @work(exclusive=True, thread=True, name="crack_mode_worker")
        def _crack_mode_worker(self, page: str, archive_val: str, cfg, ts_now: str) -> None:
            """新模式页面异步破解 worker。"""
            import time as _time
            start_ts = _time.time()
            state = self._crack_state(page)

            extract_result = self._extractor.extract(archive_val)
            if not extract_result.success:
                err_escaped = _esc_markup(extract_result.error_message or "未知错误")
                self.call_from_thread(
                    self._crack_mode_final, page, ts_now, False, None,
                    extract_result, archive_val, err_escaped,
                    _time.time() - start_ts,
                )
                try:
                    self.call_from_thread(self._finalize_crack_mode_cleanup, page)
                except Exception:  # noqa: BLE001
                    pass
                return

            cfg.hash_file_path = extract_result.hash_file_path or ""
            cfg.hashcat_mode = extract_result.hashcat_mode or 0
            progress_cb = self._crack_mode_progress_callback(start_ts, state)
            crack_result = None
            try:
                crack_result = self._cracker.run(cfg, progress_callback=progress_cb)
            except Exception as exc:  # noqa: BLE001
                crack_result = type("ErrResult", (), {
                    "success": False,
                    "status": None,
                    "recovered_passwords": {},
                    "error_message": f"worker异常: {type(exc).__name__}: {exc}",
                })()
            finally:
                elapsed = _time.time() - start_ts
                success = crack_result.success if crack_result is not None else False
                error_msg = (
                    crack_result.error_message if crack_result is not None
                    else "破解任务未返回结果"
                )
                try:
                    self._crack_mode_final(
                        page, ts_now, success, crack_result, extract_result,
                        archive_val, error_msg, elapsed,
                    )
                except Exception as exc:  # noqa: BLE001
                    try:
                        state["history"][0] = (
                            ts_now, None,
                            f"[{C_NS_RED}]内部错误: {type(exc).__name__}[/]",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self.call_from_thread(self._finalize_crack_mode_cleanup, page)
                except Exception:  # noqa: BLE001
                    pass

        def _crack_mode_final(self, page: str, ts_now: str, success: bool,
                              crack_result, extract_result, archive_val: str,
                              error_msg: str, elapsed: float) -> None:
            """新模式页面破解结束,写入历史记录。"""
            state = self._crack_state(page)
            state["running"] = False
            state["live"] = {}
            archive_escaped = _esc_markup(Path(archive_val).name)

            try:
                out_dir = _app_base() / "data" / "output"
                if out_dir.exists():
                    for hash_file in out_dir.glob("*.hash"):
                        try:
                            hash_file.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                    potfile = out_dir / "hashcat.potfile"
                    if potfile.exists():
                        try:
                            potfile.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                    for pattern in ("hashcat.indb", "hashcat.log", "*.restore", "cracked_*.txt"):
                        for residue in out_dir.glob(pattern):
                            try:
                                residue.unlink()
                            except Exception:  # noqa: BLE001
                                pass
            except Exception:  # noqa: BLE001
                pass

            inputs = state["inputs"]
            extra: dict = {
                "压缩包": (f"[{C_NS_CYAN}]{archive_escaped}[/]", None),
                "耗时": (f"[{C_NS_GRAY}]{elapsed:.2f} 秒[/]", None),
            }
            if page == "crack_mask":
                extra["掩码"] = (f"[{C_NS_CYAN}]{_esc_markup(inputs.get('crack_mask_expr',''))}[/]", None)
            elif page == "crack_rule":
                dict_count = len([d for d in inputs.get("crack_rule_dict","").split(",") if d.strip()])
                extra["字典数"] = (f"[{C_NS_YELLOW}]{dict_count}[/]", None)
                extra["规则"] = (f"[{C_NS_GRAY}]{_esc_markup(Path(inputs.get('crack_rule_file','')).name)}[/]", None)
            else:
                extra["字符集"] = (
                    f"[{C_NS_GRAY}]{_esc_markup(inputs.get('crack_brute_custom','') or '勾选字符集')}[/]", None)
                extra["长度"] = (
                    f"[{C_NS_GRAY}]{inputs.get('crack_brute_min_len','')}-{inputs.get('crack_brute_max_len','')}[/]", None)

            if success and crack_result and crack_result.recovered_passwords:
                passwords = crack_result.recovered_passwords
                _real_pwds = [
                    v for k, v in passwords.items()
                    if k.startswith("$") and v and not v.strip().startswith(" ")
                ]
                pwd = _real_pwds[0] if _real_pwds else ""
                extra["密码"] = (f"[{C_NS_GREEN} bold]{_esc_markup(pwd)}[/]", None)
                state["history"][0] = (
                    ts_now, None, f"[{C_NS_GREEN}]破解成功[/]", extra,
                )
            else:
                status = crack_result.status if crack_result else None
                if status and status.value == "exhausted":
                    status_text = f"[{C_NS_YELLOW}]字典试完未命中[/]"
                elif status and status.value == "stopped":
                    status_text = f"[{C_NS_GRAY}]已停止[/]"
                elif status and status.value == "error":
                    status_text = f"[{C_NS_RED}]执行异常[/]"
                elif not extract_result.success:
                    status_text = f"[{C_NS_RED}]哈希提取失败[/]"
                else:
                    status_text = f"[{C_NS_RED}]失败[/]"
                if error_msg:
                    extra["错误信息"] = (f"[{C_NS_RED}]{_esc_markup(error_msg)}[/]", None)
                state["history"][0] = (
                    ts_now, None, status_text, extra,
                )
            self._trim_history("crack_mode_" + page)

        def _finalize_crack_mode_cleanup(self, page: str) -> None:
            """新模式页面破解收尾(主线程执行)。"""
            self._reset_crack_mode_states(page, clear_inputs=True)
            try:
                self._render_content()
            except Exception:  # noqa: BLE001
                pass

        def _apply_brute_preset(self, state: dict, mask: str) -> None:
            """把掩码模板应用到暴力穷举页(仅处理纯 ?x 模板)。"""
            length = max(1, len(mask) // 2)
            state["inputs"]["crack_brute_min_len"] = str(length)
            state["inputs"]["crack_brute_max_len"] = str(length)
            state["inputs"]["crack_brute_custom"] = ""
            state["toggles"]["crack_brute_lower"] = "?l" in mask
            state["toggles"]["crack_brute_upper"] = "?u" in mask
            state["toggles"]["crack_brute_digit"] = "?d" in mask
            state["toggles"]["crack_brute_special"] = "?s" in mask

        def _cycle_crack_mode_preset(self, page: str) -> None:
            """循环切换模式预设(掩码模板/规则预设)。"""
            state = self._crack_state(page)
            if page == "crack_mask":
                state["mask_index"] = (state["mask_index"] + 1) % len(_MASK_PRESETS)
                preset = _MASK_PRESETS[state["mask_index"]]
                state["inputs"]["crack_mask_expr"] = preset[0]
            elif page == "crack_rule":
                state["rule_index"] = (state["rule_index"] + 1) % len(_RULE_PRESETS)
                _, rule_path = _RULE_PRESETS[state["rule_index"]]
                state["inputs"]["crack_rule_file"] = rule_path
            elif page == "crack_brute":
                state["brute_index"] = (state["brute_index"] + 1) % len(_MASK_PRESETS)
                self._apply_brute_preset(state, _MASK_PRESETS[state["brute_index"]][0])

        def _rule_file_list(self) -> list:
            """自动扫描规则目录，返回 [(文件名, 路径, 中文说明), ...]。"""
            rules_dir = _app_base() / "bin" / "windows" / "hashcat" / "rules"
            out: list = []
            try:
                if rules_dir.exists():
                    for p in sorted(rules_dir.glob("*.rule")):
                        out.append((
                            p.name,
                            str(p.resolve()),
                            _RULE_DESCRIPTIONS.get(p.name, "hashcat 内置规则，用于字典变形扩展。"),
                        ))
            except Exception:  # noqa: BLE001
                pass
            if not out:
                # 目录缺失时退回内置预设，保证选择界面不空白
                for name, path in _RULE_PRESETS:
                    out.append((
                        name,
                        path,
                        _RULE_DESCRIPTIONS.get(name, "hashcat 内置规则，用于字典变形扩展。"),
                    ))
            return out

        def _on_rule_selected(self, selected) -> None:
            """规则选择弹窗回调：把选中的规则写入规则文件字段。"""
            if selected:
                state = self._crack_state("crack_rule")
                state["inputs"]["crack_rule_file"] = selected[1]
                self._render_menu()
                self._render_content()

        def _action_crack_mode_enter(self, page: str) -> None:
            """新模式页面的回车/空格/D 键动作分发。"""
            from datetime import datetime
            items = self._crack_mode_items(page)
            item_id, item_type, _ = items[self._sub_index]
            state = self._crack_state(page)

            if item_type == "toggle":
                state["toggles"][item_id] = not state["toggles"].get(item_id, False)
                self._render_menu()
                self._render_content()
                return

            if item_type == "input":
                state["input_mode"] = item_id
                state["input_buf"] = state["inputs"].get(item_id, "")
                self._render_menu()
                return

            if item_id == f"{page}_drop":
                state["drop_mode"] = True
                self._render_menu()
                self._render_content()
                return
            if item_id == f"{page}_pick":
                # 自动读取规则目录，弹窗让用户选择并显示中文说明
                self.push_screen(
                    RuleSelectScreen(self._rule_file_list()),
                    lambda selected: self._on_rule_selected(selected),
                )
                return
            if item_id == f"{page}_preset":
                self._cycle_crack_mode_preset(page)
                self._render_menu()
                self._render_content()
                return
            if item_id == f"{page}_run":
                if state["running"]:
                    state["history"].insert(0, (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        None,
                        f"[{C_NS_YELLOW}]已有破解任务正在运行,按 ESC 中断[/]",
                    ))
                    self._trim_history("crack_mode_" + page)
                    self._render_content()
                    return
                self._do_crack_mode_run(page)
                self._render_content()
                return
            if item_id == f"{page}_help":
                self.push_screen(
                    HelpScreen(self._crack_mode_help(page), self._crack_mode_title(page) + "帮助")
                )
                return
            if item_id == f"{page}_back":
                self._exit_to_parent()

        def _render_crack_mode_menu(self, page: str) -> None:
            """渲染新模式页面左侧菜单。"""
            lines = []
            lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
            lines.append(f"[{C_NS_GREEN} bold]{self._crack_mode_title(page)}[/]")
            lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
            state = self._crack_state(page)
            for i, (item_id, item_type, label) in enumerate(self._crack_mode_items(page)):
                num = label.split(".")[0]
                text = label.split(".", 1)[1].strip()
                marker = f"[{C_NS_GREEN}]❯[/]" if i == self._sub_index else " "
                text_color = C_NS_GREEN if i == self._sub_index else C_NS_WHITE
                bold_tag = " bold" if i == self._sub_index else ""
                if item_type == "toggle":
                    checked = state["toggles"].get(item_id, False)
                    mark = f"[{C_NS_GREEN}]✓[/]" if checked else f"[{C_NS_GRAY}] [/]"
                    lines.append(
                        f"{marker} [{C_NS_CYAN}]{num}.[/] \\[{mark}] "
                        f"[{text_color}{bold_tag}]{text}[/]"
                    )
                elif item_type == "input":
                    cur_val = state["inputs"].get(item_id, "")
                    if state["input_mode"] == item_id:
                        buf_escaped = _esc_markup(state["input_buf"])
                        val_display = f"[{C_NS_YELLOW}]{buf_escaped}_[/]"
                        actual_val = state["input_buf"]
                    else:
                        val_escaped = _esc_markup(cur_val)
                        val_display = f"[{C_NS_YELLOW}]{val_escaped}[/]"
                        actual_val = cur_val
                    half_width = max(30, self._term_width // 2)
                    line_text = f"{marker} {num}. {text} : {actual_val}"
                    if _disp_w(line_text) > half_width:
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/]"
                        )
                        lines.append(f"      {val_display}")
                    else:
                        lines.append(
                            f"{marker} [{C_NS_CYAN}]{num}.[/] "
                            f"[{text_color}{bold_tag}]{text}[/] [{C_NS_GRAY}]:[/] {val_display}"
                        )
                else:
                    lines.append(
                        f"{marker} [{C_NS_CYAN}]{num}.[/] "
                        f"[{text_color}{bold_tag}]{text}[/]"
                    )
            # 掩码表达式输入提示：解释 ?x 标记并给出快速补全方式
            if state["input_mode"] == "crack_mask_expr":
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_CYAN} bold]掩码提示[/]")
                lines.append(f"[{C_NS_GRAY}]?d 数字  ?l 小写  ?u 大写[/]")
                lines.append(f"[{C_NS_GRAY}]?s 特殊  ?a 全部  ?1-?4 自定义[/]")
                if state["input_buf"].endswith("?"):
                    lines.append(f"[{C_NS_YELLOW}]已输入?，按 Tab 补全 ?d，或输入 d/l/u/s/a/1-4[/]")
                else:
                    lines.append(f"[{C_NS_GRAY}]输入 ? 后按 Tab 可快速补全 ?d[/]")
            if state["drop_mode"]:
                lines.append(f"[{C_NS_BLUE}]────────────────────[/]")
                lines.append(f"[{C_NS_YELLOW} bold]等待拖入文件...[/]")
                lines.append(f"[{C_NS_GRAY}]将文件拖入终端窗口[/]")
                lines.append(f"[{C_NS_GRAY}]按 ESC 取消[/]")
            self.query_one("#menu_panel", Static).update("\n".join(lines))

        def _render_crack_mode_content(self, page: str) -> str:
            """渲染新模式页面右侧内容。"""
            avail = max(50, self._term_width - 24)
            box_width = max(50, avail // 2)
            state = self._crack_state(page)
            inputs = state["inputs"]
            prefix = f"{page}_"
            title = self._crack_mode_title(page)

            archive_val = inputs.get(prefix + "archive", "")
            workload_val = inputs.get(prefix + "workload", "3")
            workload_map = {
                "1": "1=低(后台)",
                "2": "2=中低",
                "3": "3=高(默认)",
                "4": "4=极致",
            }
            workload_str = workload_map.get(workload_val, f"{workload_val}(自定义)")
            hashcat_ok = self._cracker.is_available()

            kv_lines = []
            if state["drop_mode"]:
                kv_lines.append(("section", "等待拖入文件"))
                kv_lines.append(("raw", f"[{C_NS_YELLOW} bold]请将文件拖入终端窗口[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]zip/rar/7z → 压缩包[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]txt/dic/lst → 字典[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]rule → 规则文件[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]ESC 取消[/]"))
                kv_lines.append(("mid",))

            kv_lines.append(("section", "当前配置"))
            kv_lines.append(("kv", "压缩包", _esc_markup(archive_val) or f"[{C_NS_GRAY}]未选择[/]"))
            if page == "crack_mask":
                kv_lines.append(("kv", "掩码", _esc_markup(inputs.get("crack_mask_expr", "")) or f"[{C_NS_GRAY}]未设置[/]"))
            elif page == "crack_rule":
                kv_lines.append(("kv", "字典", _esc_markup(inputs.get("crack_rule_dict", "")) or f"[{C_NS_GRAY}]未选择[/]"))
                kv_lines.append(("kv", "规则", _esc_markup(Path(inputs.get("crack_rule_file", "")).name) or f"[{C_NS_GRAY}]未选择[/]"))
            else:
                toggles = state["toggles"]
                charset_names = []
                if toggles.get("crack_brute_lower"):
                    charset_names.append("小写")
                if toggles.get("crack_brute_upper"):
                    charset_names.append("大写")
                if toggles.get("crack_brute_digit"):
                    charset_names.append("数字")
                if toggles.get("crack_brute_special"):
                    charset_names.append("特殊")
                if inputs.get("crack_brute_custom", "").strip():
                    charset_names.append("自定义")
                kv_lines.append(("kv", "字符集", " ".join(charset_names) or f"[{C_NS_GRAY}]未选择[/]"))
                kv_lines.append(("kv", "长度", f"{inputs.get('crack_brute_min_len','')}-{inputs.get('crack_brute_max_len','')}"))
            kv_lines.append(("kv", "工作负载", f"[{C_NS_CYAN}]{workload_str}[/]"))
            device_val = inputs.get(prefix + "device", "auto")
            if device_val == "gpu":
                kv_lines.append(("kv", "设备", f"[{C_NS_GREEN}]强制GPU[/]"))
            elif device_val == "cpu":
                kv_lines.append(("kv", "设备", f"[{C_NS_GRAY}]强制CPU[/]"))
            else:
                kv_lines.append(("kv", "设备", f"[{C_NS_GREEN}]自动[/]"))

            # 掩码说明放在“当前配置”和“环境状态”之间，方便用户理解表达式
            if page == "crack_mask":
                kv_lines.append(("mid",))
                kv_lines.append(("section", "掩码说明"))
                kv_lines.append(("kv", "?d", "数字 0-9"))
                kv_lines.append(("kv", "?l", "小写字母 a-z"))
                kv_lines.append(("kv", "?u", "大写字母 A-Z"))
                kv_lines.append(("kv", "?s", "常见特殊字符"))
                kv_lines.append(("kv", "?a", "所有可打印字符"))
                kv_lines.append(("kv", "?1-?4", "自定义字符集(高级参数)"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]普通字符直接写,如 pass?d?d?d[/]"))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]示例: ?d?d?d?d = 4位数字[/]"))

            # 规则说明放在“当前配置”和“环境状态”之间，展示当前规则的中文介绍
            if page == "crack_rule":
                rule_path = inputs.get("crack_rule_file", "")
                rule_name = Path(rule_path).name if rule_path else ""
                rule_desc = _RULE_DESCRIPTIONS.get(
                    rule_name, "hashcat 内置规则，用于字典变形扩展。"
                )
                kv_lines.append(("mid",))
                kv_lines.append(("section", "规则说明"))
                kv_lines.append((
                    "kv", "当前规则",
                    f"[{C_NS_CYAN}]{_esc_markup(rule_name) or '未选择'}[/]",
                ))
                for _line in _wrap_disp(rule_desc, max(20, box_width - 6)):
                    kv_lines.append(("raw", f"[{C_NS_GRAY}]{_esc_markup(_line)}[/]"))
                for _line in _wrap_disp("可在左侧「选择规则文件」查看全部规则说明", max(20, box_width - 6)):
                    kv_lines.append(("raw", f"[{C_NS_GRAY}]{_esc_markup(_line)}[/]"))

            if state["running"] and state["live"]:
                kv_lines.append(("mid",))
                kv_lines.append(("section", "实时进度"))
                live = state["live"]
                status_text = live.get("status_text", "")
                if status_text:
                    kv_lines.append(("kv", "状态", f"[{C_NS_YELLOW}]{_esc_markup(status_text)}[/]"))
                speed = live.get("speed")
                if speed:
                    kv_lines.append(("kv", "速度", f"[{C_NS_GREEN}]{_esc_markup(speed)}[/]"))
                progress_abs = live.get("progress_abs")
                pct = live.get("percent")
                if progress_abs:
                    kv_lines.append(("kv", "已试/总数", f"[{C_NS_CYAN}]{progress_abs}[/]"))
                if pct is not None:
                    kv_lines.append(("kv", "百分比", f"[{C_NS_CYAN}]{pct:.1f}%[/]"))
                candidates = live.get("candidates")
                if candidates:
                    kv_lines.append(("kv", "当前候选", f"[{C_NS_GRAY}]{_esc_markup(candidates)}[/]"))
                pwd = live.get("recovered_pwd")
                if pwd:
                    kv_lines.append(("kv", "已破解密码", f"[{C_NS_GREEN} bold]{_esc_markup(pwd)}[/]"))
                elapsed = live.get("elapsed")
                if elapsed is not None:
                    kv_lines.append(("kv", "耗时", f"[{C_NS_GRAY}]{elapsed:.1f} 秒[/]"))

            kv_lines.append(("mid",))
            kv_lines.append(("section", "环境状态"))
            if hashcat_ok:
                kv_lines.append(("kv", "Hashcat", f"[{C_NS_GREEN}]可用[/]"))
            else:
                kv_lines.append(("kv", "Hashcat", f"[{C_NS_RED}]未找到[/]"))

            kv_lines.append(("mid",))
            kv_lines.append(("section", "配置检查"))
            checks = []
            if not archive_val.strip():
                checks.append(("raw", f"[{C_NS_RED}]✗ 未选择压缩包[/]"))
            elif not Path(archive_val.strip()).exists():
                checks.append(("raw", f"[{C_NS_RED}]✗ 压缩包不存在[/]"))
            else:
                checks.append(("raw", f"[{C_NS_GREEN}]✓ 压缩包已选择[/]"))
            if page == "crack_mask":
                if not inputs.get("crack_mask_expr", "").strip():
                    checks.append(("raw", f"[{C_NS_RED}]✗ 未设置掩码[/]"))
                else:
                    checks.append(("raw", f"[{C_NS_GREEN}]✓ 掩码已设置[/]"))
            elif page == "crack_rule":
                if not inputs.get("crack_rule_dict", "").strip():
                    checks.append(("raw", f"[{C_NS_RED}]✗ 未选择字典[/]"))
                else:
                    checks.append(("raw", f"[{C_NS_GREEN}]✓ 字典已选择[/]"))
                if not Path(inputs.get("crack_rule_file", "")).exists():
                    checks.append(("raw", f"[{C_NS_RED}]✗ 规则文件不存在[/]"))
                else:
                    checks.append(("raw", f"[{C_NS_GREEN}]✓ 规则文件已选择[/]"))
            else:
                if not inputs.get("crack_brute_custom", "").strip() and not any(
                    state["toggles"].get(k) for k in (
                        "crack_brute_lower", "crack_brute_upper",
                        "crack_brute_digit", "crack_brute_special",
                    )
                ):
                    checks.append(("raw", f"[{C_NS_RED}]✗ 未选择字符集[/]"))
                else:
                    checks.append(("raw", f"[{C_NS_GREEN}]✓ 字符集已选择[/]"))
            if not hashcat_ok:
                checks.append(("raw", f"[{C_NS_RED}]✗ Hashcat不可用[/]"))
            kv_lines.extend(checks)

            if state["history"]:
                kv_lines.append(("mid",))
                kv_lines.append(("section", f"破解历史(共 {len(state['history'])} 次)"))
                for idx, record in enumerate(state["history"]):
                    ts_r, out_file_r, result_r = record[0], record[1], record[2]
                    extra_r = record[3] if len(record) > 3 else {}
                    kv_lines.append(("raw", f"[{C_NS_GRAY}]{_esc_markup(ts_r)}[/] {result_r}"))
                    for k, (v_markup, orig_path) in extra_r.items():
                        kv_lines.append(("kv", k, v_markup))
                    if idx < len(state["history"]) - 1:
                        kv_lines.append(("blank",))
            else:
                kv_lines.append(("mid",))
                kv_lines.append(("raw", f"[{C_NS_GRAY}]配置完成后,选择「开始破解」并回车[/]"))

            return "\n".join(_nushell_box(kv_lines, title, box_width))

        def _do_crack_dict_run(self) -> None:
            """执行字典攻击(调用 core 层 HashExtractor + HashcatExecutor)
            流程:
                1. 校验输入:压缩包路径/字典文件/输出目录/工作负载
                2. 校验 Hashcat 可用性
                3. 提取哈希(HashExtractor.extract)
                4. 构建 CrackConfig 并调用 HashcatExecutor.run
                5. 写入破解历史,弹窗显示结果
            """
            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 0. 退出输入模式(避免破解过程中输入模式残留导致上下键被拦截)
            self._crack_dict_input_mode = None
            self._crack_dict_input_buf = ""
            # 0.1 清理拖入路径状态(避免 drop_buffer=="" 残留导致破解后上下键失效)
            # 先停掉空闲超时定时器
            try:
                if self._crack_dict_drop_timer is not None:
                    try:
                        self._crack_dict_drop_timer.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._crack_dict_drop_timer = None
            except Exception:  # noqa: BLE001
                pass
            # 清空路径累积缓冲区(必须置 None,不能置 "",否则 on_key L3902 会拦截上下键)
            try:
                self._crack_dict_drop_buffer = None
            except Exception:  # noqa: BLE001
                pass
            # 退出拖入等待模式
            try:
                self._crack_dict_drop_mode = False
            except Exception:  # noqa: BLE001
                pass

            # 1. 校验压缩包路径
            archive_val = self._crack_dict_inputs.get("crack_dict_archive", "").strip()
            if not archive_val:
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:未选择压缩包[/]",
                ))
                self._trim_history("crack_dict")
                return
            if not Path(archive_val).exists():
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:压缩包不存在[/]",
                ))
                self._trim_history("crack_dict")
                return

            # 2. 校验字典文件
            dict_val = self._crack_dict_inputs.get("crack_dict_dict", "").strip()
            if not dict_val:
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:未选择字典文件[/]",
                ))
                self._trim_history("crack_dict")
                return
            # 解析字典列表,校验每个文件是否存在
            dict_paths = [d.strip() for d in dict_val.split(",") if d.strip()]
            missing = [d for d in dict_paths if not Path(d).exists()]
            if missing:
                missing_escaped = _esc_markup(missing[0])
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:字典不存在: {missing_escaped}[/]",
                ))
                self._trim_history("crack_dict")
                return

            # 3. 校验工作负载
            try:
                workload = int(self._crack_dict_inputs.get("crack_dict_workload", "3"))
                if workload < 1 or workload > 4:
                    workload = 3
            except ValueError:
                workload = 3

            # 4.1 解析设备类型
            device_val = self._crack_dict_inputs.get("crack_dict_device", "auto").strip().lower()
            if device_val in ("gpu", "2", "force_gpu"):
                device_type = "force_gpu"
            elif device_val in ("cpu", "1", "force_cpu"):
                device_type = "force_cpu"
            else:
                device_type = "auto"

            # 5. 校验 Hashcat 可用性
            if not self._cracker.is_available():
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误:Hashcat不可用,请检查bin目录[/]",
                ))
                self._trim_history("crack_dict")
                return

            # 6. 防止重复启动:已有任务在跑则拒绝
            if self._crack_dict_running:
                self._crack_dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_YELLOW}]已有破解任务正在运行,请等待结束[/]",
                ))
                self._trim_history("crack_dict")
                return

            # 7. 启动异步破解(后台线程执行,UI 不阻塞)
            # 历史记录首条先占位"正在提取哈希",异步任务内逐步更新
            self._crack_dict_history.insert(0, (
                ts_now, None,
                f"[{C_NS_YELLOW}]正在提取哈希...[/]",
            ))
            self._trim_history("crack_dict")
            self._crack_dict_running = True
            self._crack_dict_live = {"status_text": "初始化", "elapsed": 0.0}
            self._render_content()

            # 启动后台 worker(独立线程跑 hashcat,通过回调刷新 UI)
            self._crack_dict_worker(
                archive_val=archive_val,
                dict_paths=dict_paths,
                workload=workload,
                device_type=device_type,
                ts_now=ts_now,
            )

        @work(exclusive=True, thread=True, name="crack_dict_worker")
        def _crack_dict_worker(self, archive_val: str, dict_paths: list,
                               workload: int, device_type: str,
                               ts_now: str) -> None:
            """异步执行破解任务(后台线程)
            :param archive_val: 压缩包路径
            :param dict_paths: 字典文件列表
            :param workload: 工作负载 1~4
            :param device_type: 设备类型 auto/force_gpu/force_cpu
            :param ts_now: 任务启动时间戳字符串
            流程:
                1. 提取哈希(子进程,可能耗时数秒)
                2. 构建 CrackConfig 并调用 HashcatExecutor.run(带 progress_callback)
                3. progress_callback 内通过 call_from_thread 刷新 UI 实时进度
                4. 任务结束后写入最终历史记录
            注:@work 装饰器让本方法在独立线程运行,不阻塞 Textual UI 事件循环
            """
            import time as _time
            start_ts = _time.time()

            # 1. 提取哈希(子进程调用 rar2john/zip2john)
            extract_result = self._extractor.extract(archive_val)
            if not extract_result.success:
                err_escaped = _esc_markup(extract_result.error_message or "未知错误")
                self.call_from_thread(self._update_crack_dict_final,
                    ts_now, False, None, extract_result, archive_val,
                    dict_paths, workload, err_escaped, _time.time() - start_ts)
                return

            # 2. 构建破解配置
            cfg = CrackConfig(
                hash_file_path=extract_result.hash_file_path or "",
                hashcat_mode=extract_result.hashcat_mode or 0,
                attack_mode=AttackMode.DICT,
                dictionary_paths=dict_paths,
                work_load_profile=workload,
                device_type=device_type,
            )

            # 3. 进度回调:每收到一行 hashcat stdout 就刷新 UI
            # hashcat --status 每 2 秒输出一次分段状态,这里逐字段解析
            def _on_progress(progress) -> None:
                # 状态值中文映射(英文 → 中文)
                _STATUS_CN = {
                    "running": "运行中",
                    "cracked": "已破解",
                    "exhausted": "字典试完",
                    "stopped": "已停止",
                    "error": "错误",
                    "init": "初始化",
                    "autotune": "调优中",
                }
                live: dict = {"elapsed": _time.time() - start_ts}
                if progress.status:
                    live["status_text"] = _STATUS_CN.get(progress.status.value, progress.status.value)
                if progress.speed_hs and progress.speed_hs > 0:
                    # 速度格式化:>1000 H/s 显示为 KH/s
                    if progress.speed_hs >= 1e6:
                        live["speed"] = f"{progress.speed_hs/1e6:.2f} MH/s"
                    elif progress.speed_hs >= 1e3:
                        live["speed"] = f"{progress.speed_hs/1e3:.2f} KH/s"
                    else:
                        live["speed"] = f"{progress.speed_hs:.0f} H/s"
                if progress.progress_percent and progress.progress_percent > 0:
                    live["percent"] = progress.progress_percent
                # 绝对进度数 "18/36"
                if progress.progress_abs:
                    live["progress_abs"] = progress.progress_abs
                # 当前候选密码区间 "a -> 9"
                if progress.candidates:
                    live["candidates"] = progress.candidates
                # 已破解密码:仅在 recovered>0 且 raw_line 是 hash:password 格式时提取
                # (之前误把 Recovered: 0/1 行的数字当作密码显示,根因是条件判断不严)
                # 注:不能用 isdigit() 过滤,否则纯数字密码(如 111)会被误杀
                _FAKE = ("hashcat.net/faq", "No device", "Invalid argument")
                if progress.recovered > 0 and progress.raw_line:
                    raw = progress.raw_line
                    # 排除 hashcat status 输出的字段行(这些行不是 hash:password 格式)
                    _STATUS_PREFIXES = (
                        "Recovered", "Progress", "Speed", "Status", "Candidates",
                        "Session", "Hash.Mode", "Hash.Target", "Time.", "Kernel",
                        "Guess.", "Restore.", "Rejected", "Hardware", "Started",
                        "Stopped", "Candidate.Engine", "Bitmaps", "Rules",
                        "Watchdog", "Initializing", "Host memory", "Dictionary",
                        "Approaching", "[s]tatus",
                    )
                    if ":" in raw and not raw.startswith(_STATUS_PREFIXES):
                        parts = raw.split(":", 1)
                        lhs = parts[0]
                        rhs = parts[1] if len(parts) > 1 else ""
                        # 左半必须是 hash(含 $ 或长度>32),右半非空
                        if ("$" in lhs or len(lhs) > 32) and rhs:
                            if not any(m in rhs for m in _FAKE):
                                live["recovered_pwd"] = rhs
                # 通过 call_from_thread 线程安全地刷新 UI
                self.call_from_thread(self._refresh_crack_dict_live, live)

            # 4. 执行破解(阻塞本线程,但 UI 线程不阻塞)
            # 注:用 try/except/finally 确保无论如何都写入历史记录
            # (之前没包 try,如果 run() 抛异常或卡住,历史记录永远停在"正在提取哈希...")
            crack_result = None
            try:
                crack_result = self._cracker.run(cfg, progress_callback=_on_progress)
            except Exception as exc:  # noqa: BLE001
                # run() 内部已有异常处理,这里兜底防止 worker 线程静默崩溃
                crack_result = type("ErrResult", (), {
                    "success": False,
                    "status": None,
                    "recovered_passwords": {},
                    "error_message": f"worker异常: {type(exc).__name__}: {exc}",
                })()
            finally:
                # 5. 任务结束,写入最终历史记录(无论成功/失败/异常都执行)
                # 关键修复:数据更新在 worker 线程内同步执行(不依赖 call_from_thread 调度)
                # 仅 UI 渲染走 call_from_thread
                # 原因:之前用 call_from_thread(self._update_crack_dict_final, ...),
                #       worker 函数返回后 Textual 可能丢弃未处理的 call_from_thread 消息,
                #       导致历史记录永远不更新(_final_debug.log 没生成就证明这一点)
                elapsed = _time.time() - start_ts
                success = crack_result.success if crack_result is not None else False
                error_msg = (crack_result.error_message if crack_result is not None
                             else "破解任务未返回结果")
                try:
                    # 同步调用数据更新(线程安全:仅操作 list/dict/文件,无 UI 组件访问)
                    self._update_crack_dict_final(
                        ts_now, success, crack_result, extract_result,
                        archive_val, dict_paths, workload, error_msg, elapsed,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 兜底:数据更新异常也不阻塞 UI 刷新
                    try:
                        self._crack_dict_history[0] = (
                            ts_now, None,
                            f"[{C_NS_RED}]内部错误: {type(exc).__name__}[/]",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # 保险:无论如何都强制重置全部 crack_dict 状态
                # 根因:破解运行中用户可能按 Enter 进入输入模式(input_mode 残留),
                #       破解结束后 input_mode 不是 None → _in_input=True → 上下键被拦截
                # 修复:统一清理 + UI 渲染都必须在主线程执行。
                # worker 线程直接改状态会与主线程排队中的按键事件竞争：
                # 若 Enter 在清理后处理，input_mode 会被重新设置并残留。
                try:
                    self.call_from_thread(self._finalize_crack_dict_cleanup)
                except Exception:  # noqa: BLE001
                    pass

        def _finalize_crack_dict_cleanup(self) -> None:
            """破解收尾（主线程执行）：清理全部运行态后重渲染。

            通过 call_from_thread 调度，确保与主线程按键事件按 FIFO 顺序处理，
            避免 worker 线程清理与排队中的 Enter/ctrl+@ 事件竞争，
            导致 input_mode/drop_buffer 残留并拦截上下键。
            """
            self._reset_crack_dict_states(clear_inputs=True)
            try:
                self._render_content()
            except Exception:  # noqa: BLE001
                pass

        def _refresh_crack_dict_live(self, live: dict) -> None:
            """刷新破解实时进度(UI 线程执行,由 call_from_thread 调用)
            :param live: 实时进度字段 dict
            """
            self._crack_dict_live.update(live)
            import time as _time
            now = _time.time()
            if now - self._last_live_render >= 0.3:
                self._last_live_render = now
                self._render_content()

        def _update_crack_dict_final(self, ts_now: str, success: bool,
                                     crack_result, extract_result,
                                     archive_val: str, dict_paths: list,
                                     workload: int, error_msg: Optional[str],
                                     elapsed: float) -> None:
            """破解任务结束,写入最终历史记录(UI 线程执行)
            :param ts_now: 启动时间戳
            :param success: 是否成功
            :param crack_result: CrackResult(失败时可能为 None)
            :param extract_result: 哈希提取结果
            :param archive_val: 压缩包路径
            :param dict_paths: 字典列表
            :param workload: 工作负载
            :param error_msg: 错误信息(失败时)
            :param elapsed: 总耗时秒
            """
            self._crack_dict_running = False
            self._crack_dict_live = {}
            # 0.1 清理拖入路径状态(避免 drop_buffer=="" 残留导致破解后上下键失效)
            # 先停掉空闲超时定时器
            try:
                if self._crack_dict_drop_timer is not None:
                    try:
                        self._crack_dict_drop_timer.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._crack_dict_drop_timer = None
            except Exception:  # noqa: BLE001
                pass
            # 清空路径累积缓冲区(必须置 None,不能置 "",否则 on_key L3902 会拦截上下键)
            try:
                self._crack_dict_drop_buffer = None
            except Exception:  # noqa: BLE001
                pass
            # 退出拖入等待模式
            try:
                self._crack_dict_drop_mode = False
            except Exception:  # noqa: BLE001
                pass
            archive_escaped = _esc_markup(Path(archive_val).name)

            # ===== 清理破解缓存文件(保持目录干净) =====
            # 用户要求:不保存任何破解结果文件,密码只在页面显示
            # 删除:*.hash(提取的hash文件,临时中间产物)
            #      hashcat.potfile(hashcat历史记录,含密码可能泄露)
            #      hashcat.indb / hashcat.log / *.restore(hashcat运行残留)
            #      cracked_*.txt(历史破解结果文件,用户要求不再保存)
            try:
                out_dir = _app_base() / "data" / "output"
                if out_dir.exists():
                    # 删除 hash 文件(由 hash_extractor 生成,格式 {stem}_{type}.hash)
                    for hash_file in out_dir.glob("*.hash"):
                        try:
                            hash_file.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                    # 删除 potfile(含历史破解密码,安全考虑必须删)
                    potfile = out_dir / "hashcat.potfile"
                    if potfile.exists():
                        try:
                            potfile.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                    # 删除 hashcat 运行残留文件 + 历史破解结果文件
                    for pattern in ("hashcat.indb", "hashcat.log", "*.restore", "cracked_*.txt"):
                        for residue in out_dir.glob(pattern):
                            try:
                                residue.unlink()
                            except Exception:  # noqa: BLE001
                                pass
            except Exception:  # noqa: BLE001
                pass

            if success and crack_result and crack_result.recovered_passwords:
                # 破解成功:取第一个恢复的密码
                passwords = crack_result.recovered_passwords
                # 防御性过滤:即使 cracker.py 漏过误识别,这里也要挡住
                # 真实 hash 以 $ 开头(如 $rar5$、$zip2$、$7z$),启动信息不是
                _real_pwds = [
                    v for k, v in passwords.items()
                    if k.startswith("$") and v and not v.strip().startswith(" ")
                ]
                pwd = _real_pwds[0] if _real_pwds else ""
                pwd_escaped = _esc_markup(pwd)

                # 不再保存 cracked_*.txt 文件(用户要求:只在页面显示密码,保持干净)
                # 密码直接显示在历史记录的"密码"字段
                result_text = f"[{C_NS_GREEN}]破解成功[/]"
                extra = {
                    "压缩包": (f"[{C_NS_CYAN}]{archive_escaped}[/]", None),
                    "密码": (f"[{C_NS_GREEN} bold]{pwd_escaped}[/]", None),
                    "类型": (f"[{C_NS_GRAY}]{extract_result.archive_type.value}[/]", None),
                    "字典数": (f"[{C_NS_YELLOW}]{len(dict_paths)}[/]", None),
                    "耗时": (f"[{C_NS_GRAY}]{elapsed:.2f} 秒[/]", None),
                }
                self._crack_dict_history[0] = (
                    ts_now, None, result_text, extra,
                )
            else:
                # 破解失败:根据状态分类显示
                status = crack_result.status if crack_result else None
                if status and status.value == "exhausted":
                    status_text = f"[{C_NS_YELLOW}]字典试完未命中[/]"
                elif status and status.value == "stopped":
                    status_text = f"[{C_NS_GRAY}]已停止[/]"
                elif status and status.value == "error":
                    status_text = f"[{C_NS_RED}]执行异常[/]"
                elif not extract_result.success:
                    status_text = f"[{C_NS_RED}]哈希提取失败[/]"
                else:
                    status_text = f"[{C_NS_RED}]失败[/]"

                err_escaped = _esc_markup(error_msg or "") if error_msg else ""
                extra = {
                    "压缩包": (f"[{C_NS_CYAN}]{archive_escaped}[/]", None),
                    "类型": (f"[{C_NS_GRAY}]{extract_result.archive_type.value}[/]", None),
                    "字典数": (f"[{C_NS_YELLOW}]{len(dict_paths)}[/]", None),
                    "耗时": (f"[{C_NS_GRAY}]{elapsed:.2f} 秒[/]", None),
                }
                if err_escaped:
                    extra["错误信息"] = (f"[{C_NS_RED}]{err_escaped}[/]", None)
                self._crack_dict_history[0] = (
                    ts_now, None, status_text, extra,
                )
            self._trim_history("crack_dict")
            # 注:不在此处调用 _render_content()
            # 原因:本函数被 worker 线程调用,_render_content 涉及 Textual UI 组件操作,
            # 必须由主线程执行;worker 通过 call_from_thread 单独触发渲染
            # (之前在 finally 用 call_from_thread 调用本函数,worker 返回后消息丢失,
            #  导致历史记录不更新)

        def _do_dict_social_generate(self) -> None:
            """执行社工字典生成(调用 core 层 DictGenerator.generate_social)
            根据当前输入构建 SocialConfig,调用生成器,结果存入 _dict_social_history
            支持多次生成:每次结果插入列表头部,超过上限删除最早记录
            """
            from datetime import datetime
            # 1. 构建输出路径
            out_dir = self._dict_social_inputs.get("soc_out_dir", "")
            if not out_dir:
                self._dict_social_history.insert(0, (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    None,
                    f"[{C_NS_RED}]输出目录不能为空[/]",
                ))
                self._trim_history("social")
                return
            out_dir_path = Path(out_dir)
            try:
                out_dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                err_escaped = _esc_markup(str(exc))
                self._dict_social_history.insert(0, (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    None,
                    f"[{C_NS_RED}]创建输出目录失败: {err_escaped}[/]",
                ))
                self._trim_history("social")
                return
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = out_dir_path / f"social_dict_{ts}.txt"

            # 2. 构建 SocialConfig(所有字段从输入项读取)
            sc = SocialConfig(
                name_cn=self._dict_social_inputs.get("soc_name_cn", ""),
                name_pinyin=self._dict_social_inputs.get("soc_name_pinyin", ""),
                name_en=self._dict_social_inputs.get("soc_name_en", ""),
                nickname=self._dict_social_inputs.get("soc_nickname", ""),
                birth_year=self._dict_social_inputs.get("soc_birth_year", ""),
                birth_month=self._dict_social_inputs.get("soc_birth_month", ""),
                birth_day=self._dict_social_inputs.get("soc_birth_day", ""),
                birth_full=self._dict_social_inputs.get("soc_birth_full", ""),
                phone=self._dict_social_inputs.get("soc_phone", ""),
                qq=self._dict_social_inputs.get("soc_qq", ""),
                wechat=self._dict_social_inputs.get("soc_wechat", ""),
                email=self._dict_social_inputs.get("soc_email", ""),
                id_card=self._dict_social_inputs.get("soc_id_card", ""),
                company=self._dict_social_inputs.get("soc_company", ""),
                position=self._dict_social_inputs.get("soc_position", ""),
                employee_id=self._dict_social_inputs.get("soc_employee_id", ""),
                school=self._dict_social_inputs.get("soc_school", ""),
                school_year=self._dict_social_inputs.get("soc_school_year", ""),
                spouse_name=self._dict_social_inputs.get("soc_spouse_name", ""),
                child_name=self._dict_social_inputs.get("soc_child_name", ""),
                pet_name=self._dict_social_inputs.get("soc_pet_name", ""),
                anniversary=self._dict_social_inputs.get("soc_anniversary", ""),
                car_plate=self._dict_social_inputs.get("soc_car_plate", ""),
                favorite_words=self._dict_social_inputs.get("soc_favorite_words", ""),
                lucky_numbers=self._dict_social_inputs.get("soc_lucky_numbers", ""),
                area_code=self._dict_social_inputs.get("soc_area_code", ""),
                common_suffixes=self._dict_social_inputs.get("soc_common_suffixes", ""),
                output_file=str(out_file),
            )

            # 3. 调用生成器
            result = self._dict_gen.generate_social(sc)

            # 4. 存入历史列表(支持多次生成)
            if result.success:
                out_file_escaped = _esc_markup(result.output_file or "")
                result_text = (
                    f"[{C_NS_GREEN}]成功[/]"
                )
                # 额外信息存为 kv 行(渲染时拼接)
                extra = {
                    "输出文件": (f"[{C_NS_CYAN}]{out_file_escaped}[/]", result.output_file),
                    "总行数": (f"[{C_NS_YELLOW}]{result.total_lines:,}[/]", None),
                    "文件大小": (f"[{C_NS_YELLOW}]{result.size_bytes:,} 字节[/]", None),
                    "耗时": (f"[{C_NS_GRAY}]{result.duration_seconds:.3f} 秒[/]", None),
                }
                self._dict_social_history.insert(0, (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    result.output_file,
                    result_text,
                    extra,
                ))
            else:
                err_escaped = _esc_markup(result.error_message or "")
                self._dict_social_history.insert(0, (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    None,
                    f"[{C_NS_RED}]失败[/]",
                    {"错误信息": (f"[{C_NS_RED}]{err_escaped}[/]", None)},
                ))
            self._trim_history("social")

        def _do_dict_generate(self) -> None:
            """执行经典字典生成(调用 core 层 DictGenerator)
            流程:
                1. 校验字符集/长度/输出目录
                2. 解析生成数量(max_lines, 0=全部)
                3. 预估行数和字节数
                4. 磁盘空间检查:预估大小 > 剩余空间 → 弹 InfoScreen 阻断
                5. 数量过多:预估行数 > 阈值 → 弹 ConfirmScreen 二次确认
                6. 确认后调用 _execute_dict_generate 执行生成
            """
            from datetime import datetime
            ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 1. 校验：至少勾选一个字符集
            if not any(self._dict_toggles.values()):
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：至少勾选一个字符集[/]",
                ))
                self._trim_history("classic")
                return
            # 2. 拼接字符集
            charset_parts = []
            if self._dict_toggles.get("dict_lower"):
                charset_parts.append(_CHARSET_LOWER)
            if self._dict_toggles.get("dict_upper"):
                charset_parts.append(_CHARSET_UPPER)
            if self._dict_toggles.get("dict_digit"):
                charset_parts.append(_CHARSET_DIGIT)
            if self._dict_toggles.get("dict_special"):
                charset_parts.append(_CHARSET_SPECIAL)
            charset = "".join(charset_parts)

            # 3. 解析长度
            try:
                min_l = int(self._dict_inputs.get("dict_min_len", "4"))
            except ValueError:
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：最小长度必须是数字[/]",
                ))
                self._trim_history("classic")
                return
            try:
                max_l = int(self._dict_inputs.get("dict_max_len", "6"))
            except ValueError:
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：最大长度必须是数字[/]",
                ))
                self._trim_history("classic")
                return

            # 4. 单字符模式：强制长度为1
            if self._dict_toggles.get("dict_single"):
                min_l = 1
                max_l = 1

            # 5. 长度校验
            if min_l <= 0 or max_l < min_l:
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：长度范围不合法（min={min_l}, max={max_l}）[/]",
                ))
                self._trim_history("classic")
                return

            # 6. 解析生成数量(0 或空=全部生成;>0=只生成指定行数)
            try:
                max_lines = int(self._dict_inputs.get("dict_max_lines", "0") or "0")
                if max_lines < 0:
                    max_lines = 0
            except ValueError:
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：生成数量必须是数字[/]",
                ))
                self._trim_history("classic")
                return

            # 7. 构建输出路径
            out_dir = self._dict_inputs.get("dict_out_dir", "")
            if not out_dir:
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：输出目录不能为空[/]",
                ))
                self._trim_history("classic")
                return
            out_dir_path = Path(out_dir)
            try:
                out_dir_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                err_escaped = _esc_markup(str(exc))
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]错误：创建输出目录失败: {err_escaped}[/]",
                ))
                self._trim_history("classic")
                return
            # 输出文件名：dict_YYYYMMDD_HHMMSS.txt
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_file = out_dir_path / f"dict_{ts}.txt"

            # 8. 构建配置
            cfg = GenConfig(
                output_file=str(out_file),
                mode=GenMode.CHARSET_COMB,
                charset=charset,
                min_length=min_l,
                max_length=max_l,
                max_lines=max_lines,
            )

            # 9. 预估行数和字节数(用于磁盘检查和数量确认)
            est_lines, est_bytes = self._dict_gen.estimate(cfg)

            # 10. 磁盘空间检查:预估大小超过剩余空间 → 弹窗阻断
            free = _disk_free_bytes(out_dir)
            if free > 0 and est_bytes > free:
                msg = (
                    f"预估字典大小: {_fmt_bytes(est_bytes)}\n"
                    f"磁盘剩余空间: {_fmt_bytes(free)}\n"
                    f"存储空间不足,请减少字典生成数量\n"
                    f"或更换余量更充足的盘符"
                )
                self.push_screen(
                    InfoScreen(msg, "空间不足"),
                    lambda _: self._render_content(),
                )
                return

            # 11. 数量过多确认:预估行数超过阈值 → 弹窗二次确认
            if est_lines > _DICT_LARGE_COUNT_THRESHOLD:
                msg = (
                    f"预估生成数量: {est_lines:,} 行\n"
                    f"预估大小: {_fmt_bytes(est_bytes)}\n"
                    f"数量可能过多,确定要生成吗?"
                )
                # 暂存待执行配置,确认后使用
                self._pending_dict_cfg = cfg
                self._pending_dict_ts = ts_now
                self.push_screen(
                    ConfirmScreen(msg, "数量确认"),
                    lambda confirmed: self._on_dict_confirm(confirmed),
                )
                return

            # 12. 正常执行(无需确认)
            self._execute_dict_generate(cfg, ts_now)

        def _on_dict_confirm(self, confirmed: bool) -> None:
            """数量过多确认弹窗的回调
            :param confirmed: True=确认生成, False=取消
            """
            if confirmed:
                cfg = self._pending_dict_cfg
                ts = self._pending_dict_ts
                if cfg is not None:
                    self._execute_dict_generate(cfg, ts)
            # 清理暂存
            self._pending_dict_cfg = None
            self._pending_dict_ts = ""
            self._render_content()

        def _execute_dict_generate(self, cfg: GenConfig, ts_now: str) -> None:
            """实际执行字典生成并写入历史记录
            :param cfg: 已构建好的生成配置(含 max_lines)
            :param ts_now: 时间戳字符串(用于历史记录)
            """
            result = self._dict_gen.generate(cfg)
            # 存入历史列表(支持多次生成)
            if result.success:
                out_file_escaped = _esc_markup(result.output_file or "")
                result_text = f"[{C_NS_GREEN}]成功[/]"
                extra = {
                    "输出文件": (f"[{C_NS_CYAN}]{out_file_escaped}[/]", result.output_file),
                    "总行数": (f"[{C_NS_YELLOW}]{result.total_lines:,}[/]", None),
                    "文件大小": (f"[{C_NS_YELLOW}]{_fmt_bytes(result.size_bytes)}[/]", None),
                    "耗时": (f"[{C_NS_GRAY}]{result.duration_seconds:.3f} 秒[/]", None),
                }
                self._dict_history.insert(0, (
                    ts_now, result.output_file, result_text, extra,
                ))
            else:
                err_escaped = _esc_markup(result.error_message or "")
                self._dict_history.insert(0, (
                    ts_now, None,
                    f"[{C_NS_RED}]失败[/]",
                    {"错误信息": (f"[{C_NS_RED}]{err_escaped}[/]", None)},
                ))
            self._trim_history("classic")

        def _trim_history(self, kind: str) -> None:
            """截断历史记录到上限,避免内存膨胀
            :param kind: "classic"=经典字典, "social"=社工字典, "mask"=掩码字典,
                         "crack_dict"=字典攻击破解历史
            """
            if kind == "classic":
                limit = self._dict_history_limit
                if len(self._dict_history) > limit:
                    del self._dict_history[limit:]
            elif kind == "social":
                limit = self._dict_social_history_limit
                if len(self._dict_social_history) > limit:
                    del self._dict_social_history[limit:]
            elif kind == "mask":
                limit = self._dict_mask_history_limit
                if len(self._dict_mask_history) > limit:
                    del self._dict_mask_history[limit:]
            elif kind == "crack_dict":
                limit = self._crack_dict_history_limit
                if len(self._crack_dict_history) > limit:
                    del self._crack_dict_history[limit:]
            elif kind.startswith("crack_mode_"):
                page = kind[len("crack_mode_"):]
                state = self._crack_state(page)
                limit = state.get("history_limit", 10)
                if len(state["history"]) > limit:
                    del state["history"][limit:]

        def _render_bottom_bar(self) -> None:
            """渲染底部横线 + 版本号 + 实时监控（nushell 风格配色）"""
            w = self._term_width
            # 底部蓝色横线（nushell separator 风格）
            bar = f"[{C_NS_BLUE}]{'─' * w}[/]"
            # 监控数据（nushell 配色：CPU紫/GPU绿/显存紫/内存蓝/版本灰）
            try:
                stats = collect_realtime_stats()
                # GPU 显存使用率（无显存数据时跳过显示）
                if stats.gpu_vram_total_mb > 0:
                    vram_pct = (
                        stats.gpu_vram_used_mb * 100.0 / stats.gpu_vram_total_mb
                    )
                    vram_part = (
                        f"    显存使用率:[{C_NS_PURPLE}]{stats.gpu_vram_used_mb}"
                        f"/{stats.gpu_vram_total_mb}MB"
                        f"({vram_pct:.0f}%)[/]"
                    )
                else:
                    vram_part = ""
                stats_line = (
                    f"当前版本：[{C_NS_GRAY}]V 0.1[/]    "
                    f"CPU使用率:[{C_NS_PURPLE}]{stats.cpu_percent:.1f}%[/]    "
                    f"GPU使用率:[{C_NS_GREEN}]{stats.gpu_percent:.0f}%[/]"
                    f"{vram_part}    "
                    f"内存使用率:[{C_NS_BLUE}]{stats.memory_used_gb:.1f}"
                    f"/{int(stats.memory_total_gb)}G"
                    f"({stats.memory_percent:.0f}%)[/]"
                )
            except Exception:  # noqa: BLE001
                stats_line = (
                    f"当前版本：[{C_NS_GRAY}]V 0.1[/]    "
                    f"CPU使用率:--    GPU使用率:--    内存使用率:--"
                )
            # 快捷键提示：始终可见，超宽时按显示宽度截断，避免撑乱布局
            hint_plain = "W/S 上下选择 | A/D 返回/确认 | J/K 下翻/上翻 | 回车 编辑/确定 | 空格 确定 | Ctrl+Q 退出"
            if _disp_w(hint_plain) > w:
                hint_text = ""
                hint_width = 0
                for ch in hint_plain:
                    ch_width = _disp_w(ch)
                    if hint_width + ch_width > w:
                        break
                    hint_text += ch
                    hint_width += ch_width
                hint_plain = hint_text.rstrip()
            hint_line = f"[{C_NS_GRAY}]{hint_plain}[/]"
            self.query_one("#bottom_bar", Static).update(
                bar + "\n" + stats_line + "\n" + hint_line
            )

        def _refresh_stats(self) -> None:
            """定时回调：只刷新底部监控行（不重绘整个界面）"""
            self._render_bottom_bar()

        # ================================================================
        # Ctrl+点击路径跳转（资源管理器打开并选中文件）
        # ================================================================

        @staticmethod
        def _open_in_explorer(path: str) -> bool:
            """调用 Windows 资源管理器打开文件所在目录
            :param path: 文件或目录的绝对路径
            :return: True=已调用, False=失败或不支持
            """
            # 仅 Windows 支持,非 Windows 静默忽略
            if sys.platform != "win32":
                return False
            import os
            try:
                # 用 os.startfile 打开父目录(不阻塞 UI 线程)
                # 之前用 explorer /select, 在 TUI 下会卡死,改用 os.startfile
                p = Path(path)
                target_dir = str(p.parent) if p.is_file() else str(p)
                os.startfile(target_dir)
                return True
            except Exception:  # noqa: BLE001
                return False

        def on_click(self, event) -> None:
            """鼠标点击事件:Ctrl+点击 content_panel 中的路径行跳转
            - 仅响应 Ctrl+左键点击
            - 通过 event.y 查 _content_path_map 获取该行对应的原始路径
            - 调用 explorer /select 打开资源管理器并选中文件
            """
            # 非 Ctrl 点击不处理(避免干扰正常交互)
            if not getattr(event, "ctrl", False):
                return
            # 只处理 content_panel 的点击(菜单区点击不跳转)
            try:
                content_panel = self.query_one("#content_panel")
            except Exception:  # noqa: BLE001
                return
            if getattr(event, "widget", None) is not content_panel:
                return
            # 查映射:event.y 是相对 content_panel 的行号(从0开始)
            y = getattr(event, "y", -1)
            path = self._content_path_map.get(y)
            if path:
                # 调用资源管理器打开,阻止事件冒泡
                self._open_in_explorer(path)
                event.stop()

        # ================================================================
        # 键盘交互
        # ================================================================

        def action_menu_up(self) -> None:
            """上一项菜单
            - 字典输入模式：忽略上下键（输入模式下不切换项）
            - 子页面模式：在子页面操作项间移动，只更新菜单标记
            - 主菜单模式：切换时重置进入状态（右侧回到设备信息）
            """
            # 字典输入模式：上下键不切换项，只刷新菜单（保持光标）
            if self._dict_input_mode is not None:
                self._render_menu()
                return
            # 社工字典输入模式:上下键不切换项
            if self._dict_social_input_mode is not None:
                self._render_menu()
                return
            # 掩码字典输入模式:上下键不切换项
            if self._dict_mask_input_mode is not None:
                self._render_menu()
                return
            # 字典攻击输入模式:上下键不切换项
            if self._crack_dict_input_mode is not None:
                self._render_menu()
                return
            if self._sub_page is not None:
                # 子页面：根据子页面类型确定操作项数量
                items = self._sub_page_items()
                self._sub_index = (self._sub_index - 1) % len(items)
                self._render_menu()
                # 经典字典生成子页面：勾选状态变化影响右侧预估数量，需重渲染内容
                if self._sub_page == "dict_classic":
                    self._render_content()
                return
            was_entered = self._menu_entered
            self._menu_index = (self._menu_index - 1) % len(self._MENU_ITEMS)
            self._menu_entered = False
            self._render_menu()
            # 仅在从功能页切回时才重渲染内容区；设备信息状态下内容没变，跳过
            if was_entered:
                self._render_content()

        def action_menu_down(self) -> None:
            """下一项菜单
            - 字典输入模式：忽略上下键
            - 子页面模式：在子页面操作项间移动，只更新菜单标记
            - 主菜单模式：切换时重置进入状态（右侧回到设备信息）
            """
            # 字典输入模式：上下键不切换项
            if self._dict_input_mode is not None:
                self._render_menu()
                return
            # 社工字典输入模式:上下键不切换项
            if self._dict_social_input_mode is not None:
                self._render_menu()
                return
            # 掩码字典输入模式:上下键不切换项
            if self._dict_mask_input_mode is not None:
                self._render_menu()
                return
            # 字典攻击输入模式:上下键不切换项
            if self._crack_dict_input_mode is not None:
                self._render_menu()
                return
            if self._sub_page is not None:
                items = self._sub_page_items()
                self._sub_index = (self._sub_index + 1) % len(items)
                self._render_menu()
                if self._sub_page == "dict_classic":
                    self._render_content()
                return
            was_entered = self._menu_entered
            self._menu_index = (self._menu_index + 1) % len(self._MENU_ITEMS)
            self._menu_entered = False
            self._render_menu()
            if was_entered:
                self._render_content()

        def action_menu_enter(self) -> None:
            """回车确认
            - 字典输入模式：确认输入，退出输入模式
            - 子页面模式（工具自检）：执行子页面操作
            - 子页面模式（字典生成）：toggle 勾选 / input 编辑 / action 执行
            - 主菜单模式：进入当前菜单项；若为工具自检/字典生成，则进入子页面
            """
            # 字典输入模式：回车确认输入
            if self._dict_input_mode is not None:
                item_id = self._dict_input_mode
                self._dict_inputs[item_id] = self._dict_input_buf
                self._dict_input_mode = None
                self._dict_input_buf = ""
                self._render_menu()
                self._render_content()
                return

            # 社工字典输入模式:回车确认输入
            if self._dict_social_input_mode is not None:
                item_id = self._dict_social_input_mode
                self._dict_social_inputs[item_id] = self._dict_social_input_buf
                self._dict_social_input_mode = None
                self._dict_social_input_buf = ""
                self._render_menu()
                self._render_content()
                return

            # 掩码字典输入模式:回车确认输入
            if self._dict_mask_input_mode is not None:
                item_id = self._dict_mask_input_mode
                self._dict_mask_inputs[item_id] = self._dict_mask_input_buf
                self._dict_mask_input_mode = None
                self._dict_mask_input_buf = ""
                self._render_menu()
                self._render_content()
                return

            # 字典攻击输入模式:回车确认输入
            if self._crack_dict_input_mode is not None:
                item_id = self._crack_dict_input_mode
                self._crack_dict_inputs[item_id] = self._crack_dict_input_buf
                self._crack_dict_input_mode = None
                self._crack_dict_input_buf = ""
                self._render_menu()
                self._render_content()
                return

            # 掩码/规则/暴力输入模式:回车确认输入
            for page in _CRACK_MODE_PAGES:
                state = self._crack_state(page)
                if state["input_mode"] is not None:
                    item_id = state["input_mode"]
                    state["inputs"][item_id] = state["input_buf"]
                    state["input_mode"] = None
                    state["input_buf"] = ""
                    self._render_menu()
                    self._render_content()
                    return

            # 子页面模式：工具自检
            if self._sub_page == "tools":
                sub_id = _TOOLS_SUB_ITEMS[self._sub_index][0]
                if sub_id == "sub_recheck":
                    # 重新检测：清缓存重跑，完成后弹窗
                    self._tools_check_cache = None
                    checks = self._run_tool_check()
                    self._render_content()
                    # 统计通过/失败
                    passed = [n for n, p in checks if p]
                    failed = [n for n, p in checks if not p]
                    self.push_screen(ToolCheckResultScreen(passed, failed))
                elif sub_id == "sub_download":
                    # 下载工具：待命，暂不实现
                    pass
                elif sub_id == "sub_back":
                    # 返回上一层：退出子页面，回到主菜单
                    self._exit_sub_page()
                return

            # 子页面模式：字典生成二级菜单（经典/社工/随机/其他/返回）
            if self._sub_page == "dict":
                sub_id = _DICT_MENU_ITEMS[self._sub_index][0]
                if sub_id == "dict_classic":
                    # 进入经典字典生成子页面（原字典生成功能）
                    self._enter_sub_page("dict_classic")
                elif sub_id == "dict_social":
                    # 进入社工字典生成子页面
                    self._enter_sub_page("dict_social")
                elif sub_id == "dict_mask":
                    # 进入掩码字典生成子页面
                    self._enter_sub_page("dict_mask")
                elif sub_id in ("dict_random", "dict_other"):
                    # 随机/其他：功能开发中，暂不实现
                    pass
                elif sub_id == "dict_help":
                    # 字典帮助:弹出字典生成帮助弹窗
                    self.push_screen(HelpScreen(_HELP_DICT, "字典生成帮助"))
                    return
                elif sub_id == "dict_back":
                    # 返回上一层：退回主菜单
                    self._exit_sub_page()
                return

            # 子页面模式:密码破解二级菜单(字典/掩码/规则/暴力 + 帮助 + 返回)
            if self._sub_page == "crack":
                sub_id = _CRACK_MENU_ITEMS[self._sub_index][0]
                if sub_id == "crack_dict":
                    # 字典攻击:进入子页面
                    self._enter_sub_page("crack_dict")
                elif sub_id in ("crack_mask", "crack_rule", "crack_brute"):
                    # 掩码/规则/暴力:进入对应子页面
                    self._enter_sub_page(sub_id)
                elif sub_id == "crack_help":
                    # 密码破解帮助:弹出帮助弹窗
                    self.push_screen(HelpScreen(_HELP_CRACK, "密码破解帮助"))
                    return
                elif sub_id == "crack_back":
                    # 返回上一层:退回主菜单
                    self._exit_sub_page()
                return

            # 子页面模式:字典攻击(输入/执行逻辑)
            if self._sub_page == "crack_dict":
                item_id, item_type, _ = _CRACK_DICT_ITEMS[self._sub_index]
                if item_type == "input":
                    # input 项:进入输入模式,缓冲初始化为当前值
                    self._crack_dict_input_mode = item_id
                    self._crack_dict_input_buf = self._crack_dict_inputs.get(item_id, "")
                    self._render_menu()
                elif item_type == "action":
                    if item_id == "crack_dict_drop":
                        # 拖入文件:进入等待模式,等待 Paste 事件
                        self._crack_dict_drop_mode = True
                        self._render_menu()
                        self._render_content()
                    elif item_id == "crack_dict_run":
                        # 开始破解(运行中拒绝重复启动)
                        if self._crack_dict_running:
                            self._crack_dict_history.insert(0, (
                                __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                None,
                                f"[{C_NS_YELLOW}]已有破解任务正在运行,按 ESC 中断[/]",
                            ))
                            self._trim_history("crack_dict")
                            self._render_content()
                            return
                        self._do_crack_dict_run()
                        self._render_content()
                    elif item_id == "crack_dict_help":
                        # 字典攻击帮助:弹出帮助弹窗
                        self.push_screen(HelpScreen(_HELP_CRACK_DICT, "字典攻击帮助"))
                        return
                    elif item_id == "crack_dict_back":
                        # 返回上一层:从字典攻击退回密码破解二级菜单
                        self._exit_to_parent()
                return

            # 子页面模式:掩码/规则/暴力(通用引擎)
            if self._sub_page in _CRACK_MODE_PAGES:
                self._action_crack_mode_enter(self._sub_page)
                return

            # 子页面模式：经典字典生成（原字典生成勾选/输入/执行逻辑）
            if self._sub_page == "dict_classic":
                item_id, item_type, _ = _DICT_SUB_ITEMS[self._sub_index]
                if item_type == "toggle":
                    # toggle 项：切换勾选状态
                    self._dict_toggles[item_id] = not self._dict_toggles[item_id]
                    self._render_menu()
                    self._render_content()
                elif item_type == "input":
                    # input 项：进入输入模式，缓冲初始化为当前值
                    self._dict_input_mode = item_id
                    self._dict_input_buf = self._dict_inputs.get(item_id, "")
                    self._render_menu()
                elif item_type == "action":
                    if item_id == "dict_gen":
                        # 开始生成
                        self._do_dict_generate()
                        self._render_content()
                    elif item_id == "dict_help":
                        # 经典字典帮助:弹出帮助弹窗
                        self.push_screen(HelpScreen(_HELP_CLASSIC, "经典字典帮助"))
                        return
                    elif item_id == "dict_back":
                        # 返回上一层：从经典字典生成退回字典二级菜单
                        self._exit_to_parent()
                return

            # 子页面模式:社工字典生成(输入/执行逻辑)
            if self._sub_page == "dict_social":
                item_id, item_type, _ = _DICT_SOCIAL_ITEMS[self._sub_index]
                if item_type == "input":
                    # input 项:进入输入模式,缓冲初始化为当前值
                    self._dict_social_input_mode = item_id
                    self._dict_social_input_buf = self._dict_social_inputs.get(item_id, "")
                    self._render_menu()
                elif item_type == "action":
                    if item_id == "soc_gen":
                        # 开始生成
                        self._do_dict_social_generate()
                        self._render_content()
                    elif item_id == "soc_help":
                        # 社工字典帮助:弹出帮助弹窗
                        self.push_screen(HelpScreen(_HELP_SOCIAL, "社工字典帮助"))
                        return
                    elif item_id == "soc_back":
                        # 返回上一层:从社工字典生成退回字典二级菜单
                        self._exit_to_parent()
                return

            # 子页面模式:掩码字典生成(输入/执行逻辑)
            if self._sub_page == "dict_mask":
                item_id, item_type, _ = _DICT_MASK_ITEMS[self._sub_index]
                if item_type == "input":
                    # input 项:进入输入模式,缓冲初始化为当前值
                    self._dict_mask_input_mode = item_id
                    self._dict_mask_input_buf = self._dict_mask_inputs.get(item_id, "")
                    self._render_menu()
                elif item_type == "action":
                    if item_id == "mask_gen":
                        # 开始生成
                        self._do_dict_mask_generate()
                        self._render_content()
                    elif item_id == "mask_preset":
                        # 快速模板:循环切换预设掩码
                        self._mask_preset_index = (
                            (self._mask_preset_index + 1) % len(_MASK_PRESETS)
                        )
                        preset_mask = _MASK_PRESETS[self._mask_preset_index][0]
                        self._dict_mask_inputs["mask_input"] = preset_mask
                        self._render_menu()
                        self._render_content()
                    elif item_id == "mask_help":
                        # 掩码字典帮助:弹出帮助弹窗
                        self.push_screen(HelpScreen(_HELP_MASK, "掩码字典帮助"))
                        return
                    elif item_id == "mask_back":
                        # 返回上一层:从掩码字典生成退回字典二级菜单
                        self._exit_to_parent()
                return

            # 主菜单：进入当前菜单项
            menu_id = self._MENU_ITEMS[self._menu_index][0]
            if menu_id == "menu_tools":
                # 工具自检：进入子页面
                self._enter_sub_page("tools")
                return
            if menu_id == "menu_crack":
                # 密码破解:进入二级菜单(攻击模式选择)
                self._enter_sub_page("crack")
                return
            if menu_id == "menu_dict":
                # 字典生成：进入子页面
                self._enter_sub_page("dict")
                return
            if menu_id == "menu_help":
                # 帮助说明:弹出帮助弹窗
                self.push_screen(HelpScreen(_HELP_MAIN, "使用帮助"))
                return
            if menu_id == "menu_about":
                # 软件说明:弹出软件介绍
                self.push_screen(HelpScreen(_HELP_ABOUT, "软件说明"))
                return
            if menu_id == "menu_quit":
                # 退出软件:直接退出,不保留后台
                self.action_do_quit()
                return
            # 其他菜单项：进入功能内容
            self._menu_entered = True
            self._render_menu()
            self._render_content()

        def _reset_crack_dict_states(self, clear_inputs: bool = False) -> None:
            """统一清理字典攻击子页面的全部运行时状态(原子重置)
            :param clear_inputs: True=同时清空输入缓冲(进入/退出子页面时用);
                                 False=仅清运行态(破解结束/中断时用,保留用户填的路径)
            设计意图:
                - 修复"破解完/退出再进入 crack_dict 后上下键卡死"的根因:
                  drop_buffer 残留空字符串"" 会触发 on_key L3967 拦截上下键
                - 统一入口避免多处分散清理遗漏(之前 _enter/_exit/暴力中断都没清)
            每行独立 try/except,确保单行异常不中断后续清理
            """
            # 1. 停止拖入路径空闲超时定时器(防止残留定时器回调乱填路径)
            try:
                if self._crack_dict_drop_timer is not None:
                    try:
                        self._crack_dict_drop_timer.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._crack_dict_drop_timer = None
            except Exception:  # noqa: BLE001
                pass
            # 2. 清空路径累积缓冲区(必须置 None,不能置 "",否则 on_key L3967 拦截上下键)
            try:
                self._crack_dict_drop_buffer = None
            except Exception:  # noqa: BLE001
                pass
            # 3. 退出拖入等待模式
            try:
                self._crack_dict_drop_mode = False
            except Exception:  # noqa: BLE001
                pass
            # 4. 重置破解运行标志(防止残留 True 导致 Enter 提示"已有任务运行中")
            try:
                self._crack_dict_running = False
            except Exception:  # noqa: BLE001
                pass
            # 5. 清空实时进度缓存(防止残留进度数据误导渲染)
            try:
                self._crack_dict_live = {}
            except Exception:  # noqa: BLE001
                pass
            # 6. 可选:清空输入模式与缓冲(进入/退出子页面时用,破解结束保留路径)
            if clear_inputs:
                try:
                    self._crack_dict_input_mode = None
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._crack_dict_input_buf = ""
                except Exception:  # noqa: BLE001
                    pass

        def _enter_sub_page(self, sub: str) -> None:
            """进入子页面
            :param sub: 子页面标识
                  "tools"=工具自检, "dict"=字典生成二级菜单,
                  "dict_classic"=经典字典生成, "dict_social"=社工字典生成
            """
            self._sub_page = sub
            self._sub_index = 0
            # 子页面默认不显示 [已进入] 标记
            self._menu_entered = False
            # 重置字典输入模式（防止从其他页面带入状态）
            self._dict_input_mode = None
            self._dict_input_buf = ""
            # 重置社工字典输入模式
            self._dict_social_input_mode = None
            self._dict_social_input_buf = ""
            # 重置掩码字典输入模式
            self._dict_mask_input_mode = None
            self._dict_mask_input_buf = ""
            # 重置字典攻击输入模式
            self._crack_dict_input_mode = None
            self._crack_dict_input_buf = ""
            # 进入字典攻击子页面时,额外清理运行态(drop_buffer/running/live/timer)
            # 修复根因:从其他页面带着脏状态进入 crack_dict 会导致上下键卡死
            if sub == "crack_dict":
                self._reset_crack_dict_states(clear_inputs=False)
            if sub in _CRACK_MODE_PAGES:
                self._reset_crack_mode_states(sub, clear_inputs=False)
            # 动态调整菜单面板宽度:
            # 字典生成子页面输入项含长路径,需要更宽的面板才能不换行
            # 设为终端宽度一半,与换行阈值一致(路径不超过一半则一行显示)
            self._update_menu_width()
            self._render_menu()
            self._render_content()

        def _update_menu_width(self) -> None:
            """根据当前子页面动态调整菜单面板宽度
            - 字典生成子页面(dict_classic/dict_social/dict_mask): 终端宽度一半,容纳长路径
            - 字典攻击子页面(crack_dict): 同样需要宽菜单容纳长路径
            - 其他页面: 固定30,保持紧凑布局
            """
            try:
                panel = self.query_one("#menu_panel")
                if self._sub_page in ("dict_classic", "dict_social", "dict_mask", "crack_dict") \
                        or self._sub_page in _CRACK_MODE_PAGES:
                    # 终端宽度一半,最小40(保证路径有足够空间)
                    new_width = max(40, self._term_width // 2)
                else:
                    new_width = 30
                panel.styles.width = new_width
            except Exception:  # noqa: BLE001
                pass

        def _sub_page_items(self) -> list:
            """获取当前子页面对应的操作项列表
            :return: 不同子页面的操作项列表（结构因页面而异）
            """
            if self._sub_page == "tools":
                return _TOOLS_SUB_ITEMS
            if self._sub_page == "dict":
                return _DICT_MENU_ITEMS
            if self._sub_page == "crack":
                return _CRACK_MENU_ITEMS
            if self._sub_page == "dict_classic":
                return _DICT_SUB_ITEMS
            if self._sub_page == "dict_social":
                return _DICT_SOCIAL_ITEMS
            if self._sub_page == "dict_mask":
                return _DICT_MASK_ITEMS
            if self._sub_page == "crack_dict":
                return _CRACK_DICT_ITEMS
            if self._sub_page in _CRACK_MODE_PAGES:
                return self._crack_mode_items(self._sub_page)
            return []

        def _exit_to_parent(self) -> None:
            """分层退出：退回当前子页面的上一层
            - 经典字典/社工字典/掩码字典生成 → 退回字典二级菜单(dict)
            - 字典攻击 → 退回密码破解二级菜单(crack)
            - 字典二级菜单(dict)/密码破解二级菜单(crack)/工具自检(tools) → 退回主菜单
            """
            if self._sub_page in ("dict_classic", "dict_social", "dict_mask"):
                # 退回字典二级菜单
                self._enter_sub_page("dict")
            elif self._sub_page == "crack_dict":
                # 字典攻击退回密码破解二级菜单
                # 先清理 crack_dict 运行态(防止 drop_buffer/running 残留导致卡死)
                self._reset_crack_dict_states(clear_inputs=True)
                self._enter_sub_page("crack")
            elif self._sub_page in _CRACK_MODE_PAGES:
                # 掩码/规则/暴力退回密码破解二级菜单
                if self._crack_state(self._sub_page)["running"]:
                    try:
                        self._cracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                self._reset_crack_mode_states(self._sub_page, clear_inputs=True)
                self._enter_sub_page("crack")
            else:
                # dict / crack / tools 退回主菜单
                self._exit_sub_page()

        def _exit_sub_page(self) -> None:
            """退出子页面，回到主菜单（重置进入状态，右侧回到设备信息）"""
            # 退出前若在 crack_dict 子页面,清理其运行态(防止残留导致卡死)
            if self._sub_page == "crack_dict":
                self._reset_crack_dict_states(clear_inputs=True)
            if self._sub_page in _CRACK_MODE_PAGES:
                if self._crack_state(self._sub_page)["running"]:
                    try:
                        self._cracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                self._reset_crack_mode_states(self._sub_page, clear_inputs=True)
            self._sub_page = None
            self._sub_index = 0
            # 回到主菜单时重置进入状态，右侧显示设备信息
            self._menu_entered = False
            # 重置字典输入模式
            self._dict_input_mode = None
            self._dict_input_buf = ""
            # 恢复菜单面板宽度为默认30
            self._update_menu_width()
            self._render_menu()
            self._render_content()

        def action_sub_back(self) -> None:
            """ESC 退出（优先级从高到低）
            1. 拖入等待模式：取消等待
            2. 各输入模式：取消输入，恢复原值
            3. 经典字典生成中：退回字典二级菜单
            4. 字典二级菜单/工具自检中：退回主菜单
            修复：之前只处理了子页面退出，没处理输入模式的取消
            """
            # 拖入等待模式：取消等待
            if self._crack_dict_drop_mode:
                self._crack_dict_drop_mode = False
                self._render_menu()
                self._render_content()
                return
            if self._sub_page in _CRACK_MODE_PAGES and self._crack_state(self._sub_page)["drop_mode"]:
                self._crack_state(self._sub_page)["drop_mode"] = False
                self._render_menu()
                self._render_content()
                return

            # 字典输入模式：ESC 取消输入，恢复原值
            if self._dict_input_mode is not None:
                self._dict_input_mode = None
                self._dict_input_buf = ""
                self._render_menu()
                return

            # 社工字典输入模式：ESC 取消输入
            if self._dict_social_input_mode is not None:
                self._dict_social_input_mode = None
                self._dict_social_input_buf = ""
                self._render_menu()
                return

            # 掩码字典输入模式：ESC 取消输入
            if self._dict_mask_input_mode is not None:
                self._dict_mask_input_mode = None
                self._dict_mask_input_buf = ""
                self._render_menu()
                return

            # 字典攻击输入模式：ESC 取消输入
            if self._crack_dict_input_mode is not None:
                self._crack_dict_input_mode = None
                self._crack_dict_input_buf = ""
                self._render_menu()
                return
            if self._sub_page in _CRACK_MODE_PAGES:
                state = self._crack_state(self._sub_page)
                if state["input_mode"] is not None:
                    state["input_mode"] = None
                    state["input_buf"] = ""
                    self._render_menu()
                    return

            # 子页面：分层退出到父级
            if self._sub_page is not None:
                self._exit_to_parent()

        def action_go_crack(self) -> None:
            """跳转到 密码破解（重置进入状态，退出子页面）
            优化：仅在从功能页切回时才重渲染内容区
            """
            in_sub = self._sub_page is not None
            self._sub_page = None
            self._sub_index = 0
            was_entered = self._menu_entered
            self._menu_index = 0
            self._menu_entered = False
            self._update_menu_width()
            self._render_menu()
            # 从子页面或功能页切回时才重渲染内容区
            if was_entered or in_sub:
                self._render_content()

        def action_go_dict(self) -> None:
            """跳转到 字典生成（重置进入状态，退出子页面）
            优化：仅在从功能页切回时才重渲染内容区
            """
            in_sub = self._sub_page is not None
            self._sub_page = None
            self._sub_index = 0
            was_entered = self._menu_entered
            self._menu_index = 1
            self._menu_entered = False
            self._update_menu_width()
            self._render_menu()
            if was_entered or in_sub:
                self._render_content()

        def action_go_tools(self) -> None:
            """跳转到 工具自检（重置进入状态，退出子页面）
            优化：仅在从功能页切回时才重渲染内容区
            """
            in_sub = self._sub_page is not None
            self._sub_page = None
            self._sub_index = 0
            was_entered = self._menu_entered
            self._menu_index = 2
            self._menu_entered = False
            self._update_menu_width()
            self._render_menu()
            if was_entered or in_sub:
                self._render_content()

        def action_go_help(self) -> None:
            """跳转到 帮助说明（重置进入状态，退出子页面）
            优化：仅在从功能页切回时才重渲染内容区
            """
            in_sub = self._sub_page is not None
            self._sub_page = None
            self._sub_index = 0
            was_entered = self._menu_entered
            self._menu_index = 3
            self._menu_entered = False
            self._update_menu_width()
            self._render_menu()
            if was_entered or in_sub:
                self._render_content()

        def action_do_quit(self) -> None:
            """退出程序(先停止可能运行的破解任务,不保留后台)"""
            try:
                if self._cracker is not None:
                    self._cracker.stop()
            except Exception:  # noqa: BLE001
                pass
            self.exit()

        def action_show_cmd_cn(self) -> None:
            """命令面板（简化版：直接在底部提示）"""
            # codex-cli 风格不做弹窗，直接在终端打印提示
            pass

        def on_key(self, event) -> None:
            """全局按键兜底（优先级从高到低）
            第1优先级：全局快捷键（任何状态下都生效，不被任何模式拦截）
            第2优先级：拖入路径捕获（crack_dict 子页面，处理 ctrl+@ 和路径字符）
            第3优先级：拖入等待模式（ESC 取消，允许上下键/全局快捷键）
            第4优先级：破解运行中 ESC 强制中断
            第5优先级：各输入模式（只拦截字符/退格/ESC，其他控制键不拦截交给 BINDINGS）
            第6优先级：Ctrl+1~4 兜底跳转（兼容旧逻辑）
            """
            # ================================================================
            # 调试日志：记录所有按键事件（用于诊断上下键失效问题）
            # ================================================================
            try:
                from datetime import datetime as _dt
                from pathlib import Path as _P
                _log_dir = _P("data/output")
                _log_dir.mkdir(parents=True, exist_ok=True)
                with open(_log_dir / "_key_debug.log", "a", encoding="utf-8") as _f:
                    _f.write(f"[{_dt.now().strftime('%H:%M:%S.%f')}] "
                             f"key={event.key!r} char={event.character!r} "
                             f"sub_page={self._sub_page!r} "
                             f"running={getattr(self, '_crack_dict_running', 'N/A')!r} "
                             f"input_mode={getattr(self, '_crack_dict_input_mode', 'N/A')!r} "
                             f"drop_buffer={getattr(self, '_crack_dict_drop_buffer', 'N/A')!r} "
                             f"drop_mode={getattr(self, '_crack_dict_drop_mode', 'N/A')!r}\n")
            except Exception:
                pass

            # ================================================================
            # 第1优先级：全局快捷键 + 导航键（任何状态下强制生效）
            # 这是修复所有快捷键/上下键失效的核心：永远放在最前面处理
            # ================================================================
            _key = event.key

            # Ctrl+1~4：直接调用已有的 action_go_* 方法，确保任何状态可跳转
            if _key == "ctrl+1":
                self.action_go_crack()
                event.stop()
                return
            if _key == "ctrl+2":
                self.action_go_dict()
                event.stop()
                return
            if _key == "ctrl+3":
                self.action_go_tools()
                event.stop()
                return
            if _key == "ctrl+4":
                self.action_go_help()
                event.stop()
                return
            # Ctrl+Q：退出
            if _key == "ctrl+q":
                self.action_do_quit()
                event.stop()
                return
            # Ctrl+P：命令面板（保持绑定）
            if _key == "ctrl+p":
                self.action_show_cmd_cn()
                event.stop()
                return
            # W/S 映射上下键，A/D 映射返回/确认，空格映射回车
            # 输入模式/路径拼接中不映射，避免把用户输入的字母和空格吞掉
            if not isinstance(self.screen, ModalScreen):
                _in_input_now = (
                    self._dict_input_mode is not None
                    or self._dict_social_input_mode is not None
                    or self._dict_mask_input_mode is not None
                    or self._crack_dict_input_mode is not None
                    or self._crack_mode_in_input()
                )
                _drop_accumulating = (
                    (self._sub_page == "crack_dict"
                     and self._crack_dict_drop_buffer not in (None, ""))
                    or (self._sub_page in _CRACK_MODE_PAGES
                        and self._crack_state(self._sub_page)["drop_buffer"] not in (None, ""))
                )
                if _key in ("w", "s", "W", "S"):
                    if _in_input_now or _drop_accumulating:
                        _key = None
                    else:
                        _key = "up" if _key.lower() == "w" else "down"
                elif _key in ("a", "d", "A", "D"):
                    if _in_input_now or _drop_accumulating:
                        _key = None
                    else:
                        _key = "escape" if _key.lower() == "a" else "enter"
                elif _key == "space":
                    if not (_in_input_now or _drop_accumulating):
                        _key = "enter"
                elif _key in ("j", "k", "J", "K"):
                    # J/K 滚动右侧内容(不依赖方向键/PageUp，PowerShell 更稳)
                    if _in_input_now or _drop_accumulating:
                        _key = None
                    else:
                        _key = "pagedown" if _key.lower() == "j" else "pageup"
            # 右侧内容滚动:PageUp/PageDown/Home/End(输入模式下不拦截)
            if _key in ("pageup", "pagedown", "home", "end"):
                _in_any_input = (
                    self._dict_input_mode is not None
                    or self._dict_social_input_mode is not None
                    or self._dict_mask_input_mode is not None
                    or self._crack_dict_input_mode is not None
                    or self._crack_mode_in_input()
                )
                if not _in_any_input:
                    try:
                        sc = self.query_one("#content_scroll", VerticalScroll)
                        if _key == "pageup":
                            sc.scroll_up(animate=False, immediate=True)
                        elif _key == "pagedown":
                            sc.scroll_down(animate=False, immediate=True)
                        elif _key == "home":
                            sc.scroll_home(animate=False, immediate=True)
                        else:
                            sc.scroll_end(animate=False, immediate=True)
                    except Exception:  # noqa: BLE001
                        pass
                    event.stop()
                    return
            # 上/下键：直接在 on_key 中处理，不依赖 action 方法
            # 修复：action_menu_up/down 可能在 worker 线程运行时抛异常（query_one 竞争），
            #       导致 event.stop() 未执行，事件被 Textual 默认行为吞掉
            #       现在用 try/except 包裹，异常时也保证 event.stop()
            if _key == "up" or _key == "down":
                # 诊断日志:记录 up/down 按键时的全部状态(排查卡死根因)
                try:
                    from pathlib import Path as _P
                    from datetime import datetime as _dt
                    _log = _P("data/output/_key_debug.log")
                    _log.parent.mkdir(parents=True, exist_ok=True)
                    with open(_log, "a", encoding="utf-8") as _f:
                        _f.write(f"\n[{_dt.now().strftime('%H:%M:%S.%f')}] key={_key} "
                                 f"sub_page={self._sub_page} sub_index={self._sub_index} "
                                 f"drop_buffer={self._crack_dict_drop_buffer!r} "
                                 f"drop_mode={self._crack_dict_drop_mode} "
                                 f"running={self._crack_dict_running} "
                                 f"input_mode={self._crack_dict_input_mode!r} "
                                 f"dict_input={self._dict_input_mode!r} "
                                 f"social_input={self._dict_social_input_mode!r} "
                                 f"mask_input={self._dict_mask_input_mode!r}\n")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    # 检查是否在拖入路径累积中（crack_dict/掩码/规则/暴力 子页面）
                    # 空字符串表示仅误触 ctrl+@、尚未收到路径字符，不拦截导航
                    if (
                        (self._sub_page == "crack_dict"
                         and self._crack_dict_drop_buffer not in (None, ""))
                        or (self._sub_page in _CRACK_MODE_PAGES
                            and self._crack_state(self._sub_page)["drop_buffer"] not in (None, ""))
                    ):
                        # 诊断:记录拦截原因
                        try:
                            from pathlib import Path as _P
                            _log = _P("data/output/_key_debug.log")
                            with open(_log, "a", encoding="utf-8") as _f:
                                _f.write(f"  → BLOCKED by drop_buffer={self._crack_dict_drop_buffer!r}\n")
                        except Exception:  # noqa: BLE001
                            pass
                        event.stop()
                        return
                    # 检查输入模式：输入模式下上下键不切换项
                    _in_input = (
                        self._dict_input_mode is not None
                        or self._dict_social_input_mode is not None
                        or self._dict_mask_input_mode is not None
                        or self._crack_dict_input_mode is not None
                        or self._crack_mode_in_input()
                    )
                    if not _in_input:
                        if self._sub_page is not None:
                            # 子页面：切换 _sub_index
                            items = self._sub_page_items()
                            if items:
                                if _key == "up":
                                    self._sub_index = (self._sub_index - 1) % len(items)
                                else:
                                    self._sub_index = (self._sub_index + 1) % len(items)
                                self._render_menu()
                                if self._sub_page == "dict_classic":
                                    self._render_content()
                            else:
                                # 防御:_sub_page_items() 返回空(异常状态)
                                # 记录调试日志,避免黑盒死局(上下键无反应无报错)
                                try:
                                    from pathlib import Path as _P
                                    _log = _P("data/output/_key_debug.log")
                                    _log.parent.mkdir(parents=True, exist_ok=True)
                                    with open(_log, "a", encoding="utf-8") as _f:
                                        _f.write(f"[WARN] items empty, sub_page={self._sub_page}, "
                                                 f"sub_index={self._sub_index}\n")
                                except Exception:  # noqa: BLE001
                                    pass
                        else:
                            # 主菜单：切换 _menu_index
                            was_entered = self._menu_entered
                            if _key == "up":
                                self._menu_index = (self._menu_index - 1) % len(self._MENU_ITEMS)
                            else:
                                self._menu_index = (self._menu_index + 1) % len(self._MENU_ITEMS)
                            self._menu_entered = False
                            self._render_menu()
                            if was_entered:
                                self._render_content()
                except Exception as _exc:  # noqa: BLE001
                    # 诊断:记录被吞的异常(这可能就是"上下键无反应"的根因)
                    try:
                        from pathlib import Path as _P
                        import traceback as _tb
                        _log = _P("data/output/_key_debug.log")
                        with open(_log, "a", encoding="utf-8") as _f:
                            _f.write(f"  → EXCEPTION in up/down: {type(_exc).__name__}: {_exc}\n")
                            _f.write(f"  → {chr(10).join(_tb.format_exc().splitlines()[:5])}\n")
                    except Exception:  # noqa: BLE001
                        pass
                event.stop()
                return
            # 回车：调用 action_menu_enter
            if _key == "enter":
                try:
                    self.action_menu_enter()
                except Exception:  # noqa: BLE001
                    pass
                event.stop()
                return
            # ESC：调用 action_sub_back（返回上一级/退出输入）
            if _key == "escape":
                try:
                    self.action_sub_back()
                except Exception:  # noqa: BLE001
                    pass
                event.stop()
                return

            # ================================================================
            # 第2优先级：字典攻击子页面-拖入文件路径捕获
            # （只有这个阶段会吞所有字符键，为了拼接路径）
            # ================================================================
            if self._sub_page == "crack_dict":
                # 正在累积路径:优先处理
                if self._crack_dict_drop_buffer is not None:
                    if event.key == "ctrl+@":
                        # ctrl+@ 只是路径分段分隔符，收到它也要续期，
                        # 否则拖入时间超过空闲阈值时路径会被截断。
                        self._reset_crack_dict_drop_timer()
                        event.stop()
                        return
                    char = event.character
                    if char:
                        self._crack_dict_drop_buffer += char
                        # 每收到一个路径字符都重建空闲定时器（与原设计注释一致），
                        # 避免长路径/慢速粘贴在 800ms 内被误判为拖入结束。
                        self._reset_crack_dict_drop_timer()
                    event.stop()
                    return
                # 未在累积:检测 ctrl+@ 开启累积模式
                if event.key == "ctrl+@":
                    self._crack_dict_drop_buffer = ""
                    if self._crack_dict_input_mode is not None:
                        self._crack_dict_input_mode = None
                        self._crack_dict_input_buf = ""
                    self._crack_dict_drop_mode = False
                    self._reset_crack_dict_drop_timer()
                    event.stop()
                    return

            # 掩码/规则/暴力 拖入路径捕获(与字典攻击同一套机制)
            if self._sub_page in _CRACK_MODE_PAGES:
                state = self._crack_state(self._sub_page)
                # 输入模式下忽略 ctrl+@：IME/Shift+/ 可能先发 NUL，
                # 若把它当拖入开始，会把正在编辑的输入模式清掉。
                if state["input_mode"] is not None and event.key == "ctrl+@":
                    event.stop()
                    return
                if state["drop_buffer"] is not None:
                    if event.key == "ctrl+@":
                        self._reset_crack_mode_drop_timer(self._sub_page)
                        event.stop()
                        return
                    char = event.character
                    if char:
                        state["drop_buffer"] += char
                        self._reset_crack_mode_drop_timer(self._sub_page)
                    event.stop()
                    return
                if event.key == "ctrl+@":
                    state["drop_buffer"] = ""
                    if state["input_mode"] is not None:
                        state["input_mode"] = None
                        state["input_buf"] = ""
                    state["drop_mode"] = False
                    self._reset_crack_mode_drop_timer(self._sub_page)
                    event.stop()
                    return

            # ================================================================
            # 第3优先级：拖入等待模式（ESC 取消；不再拦截其他键）
            # 修复：之前拦截所有键 -> 现在除了ESC只做标记不拦截，交给前面的第1优先级处理
            # ================================================================
            if self._crack_dict_drop_mode:
                # 注意：ESC 已在第1优先级交给 action_sub_back 统一处理，
                # 这里只做模式清理：如果确实取消了拖入等待就重置标记
                # 不 event.stop，不拦截任何其他键
                pass

            # ================================================================
            # 第4优先级：破解运行中 ESC 强制中断（ESC 已在第1优先级处理）
            # 这里保留仅为兼容 action_sub_back 不处理的更暴力终止逻辑
            # ================================================================
            if self._sub_page == "crack_dict" and self._crack_dict_running:
                # ESC 在第1优先级会触发 action_sub_back（退回主菜单），
                # 同时这里额外做一次 worker 终止，确保万无一失
                if event.key == "escape":
                    try:
                        if self._cracker is not None:
                            self._cracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    # 统一清理 crack_dict 全部运行态(含 drop_buffer/mode/timer)
                    # 修复根因:暴力中断只清 running/live,没清 drop 系列,
                    # 导致 drop_buffer 残留"" → 上下键被 on_key 拦截 → 卡死
                    self._reset_crack_dict_states(clear_inputs=False)
                    self._crack_dict_history.insert(0, (
                        __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        None,
                        f"[{C_NS_YELLOW}]用户已中断破解[/]",
                    ))
                    self._trim_history("crack_dict")
                    self._render_content()
                    event.stop()
                    return

            # 掩码/规则/暴力 破解运行中 ESC 强制中断
            if self._sub_page in _CRACK_MODE_PAGES and self._crack_state(self._sub_page)["running"]:
                if event.key == "escape":
                    try:
                        if self._cracker is not None:
                            self._cracker.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self._reset_crack_mode_states(self._sub_page, clear_inputs=False)
                    state = self._crack_state(self._sub_page)
                    state["history"].insert(0, (
                        __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        None,
                        f"[{C_NS_YELLOW}]用户已中断破解[/]",
                    ))
                    self._trim_history("crack_mode_" + self._sub_page)
                    self._render_content()
                    event.stop()
                    return

            # ================================================================
            # 第5优先级：各输入模式（只处理字符/退格，其他控制键全部放行）
            # 关键修复：把"其他控制键忽略 event.stop() return"改为"直接 return 不拦截"，
            # 这样前面第1优先级没捕获的控制键不会被吞掉
            # ================================================================

            # 社工字典输入模式
            if self._dict_social_input_mode is not None:
                key = event.key
                # ESC/回车：已在第1优先级由 action_* 处理，这里直接放行
                if key == "escape" or key == "enter":
                    return
                # Backspace:删除最后一个字符
                if key == "backspace":
                    if self._dict_social_input_buf:
                        self._dict_social_input_buf = self._dict_social_input_buf[:-1]
                        self._render_menu()
                    event.stop()
                    return
                # 普通可打印字符:追加到缓冲
                ch = event.character
                if ch and len(ch) == 1 and ch.isprintable():
                    self._dict_social_input_buf += ch
                    self._render_menu()
                    event.stop()
                    return
                # 其他控制键：直接不拦截（不再 event.stop()），交给上层
                return

            # 掩码字典输入模式
            if self._dict_mask_input_mode is not None:
                key = event.key
                if key == "escape" or key == "enter":
                    return
                if key == "backspace":
                    if self._dict_mask_input_buf:
                        self._dict_mask_input_buf = self._dict_mask_input_buf[:-1]
                        self._render_menu()
                    event.stop()
                    return
                ch = event.character
                if ch and len(ch) == 1 and ch.isprintable():
                    self._dict_mask_input_buf += ch
                    self._render_menu()
                    event.stop()
                    return
                return  # 其他控制键：不拦截

            # 字典攻击输入模式
            if self._crack_dict_input_mode is not None:
                key = event.key
                if key == "escape" or key == "enter":
                    return
                if key == "backspace":
                    if self._crack_dict_input_buf:
                        self._crack_dict_input_buf = self._crack_dict_input_buf[:-1]
                        self._render_menu()
                    event.stop()
                    return
                ch = event.character
                if ch and len(ch) == 1 and ch.isprintable():
                    self._crack_dict_input_buf += ch
                    self._render_menu()
                    event.stop()
                    return
                return  # 其他控制键：不拦截

            # 掩码/规则/暴力输入模式
            for page in _CRACK_MODE_PAGES:
                state = self._crack_state(page)
                if state["input_mode"] is not None:
                    key = event.key
                    if key == "escape" or key == "enter":
                        return
                    if key == "backspace":
                        if state["input_buf"]:
                            state["input_buf"] = state["input_buf"][:-1]
                            self._render_menu()
                        event.stop()
                        return
                    if key == "tab":
                        # 掩码表达式快速补全: Tab 插入 ?, 再 Tab 补成 ?d
                        if state["input_mode"] == "crack_mask_expr":
                            if state["input_buf"].endswith("?"):
                                state["input_buf"] += "d"
                            else:
                                state["input_buf"] += "?"
                            self._render_menu()
                            event.stop()
                            return
                        return
                    ch = event.character
                    if ch and len(ch) == 1 and ch.isprintable():
                        state["input_buf"] += ch
                        self._render_menu()
                        event.stop()
                        return
                    return

            # 字典输入模式
            if self._dict_input_mode is not None:
                key = event.key
                if key == "escape" or key == "enter":
                    return
                if key == "backspace":
                    if self._dict_input_buf:
                        self._dict_input_buf = self._dict_input_buf[:-1]
                        self._render_menu()
                    event.stop()
                    return
                ch = event.character
                if ch and len(ch) == 1 and ch.isprintable():
                    self._dict_input_buf += ch
                    self._render_menu()
                    event.stop()
                    return
                return  # 其他控制键：不拦截

            # ================================================================
            # 第6优先级：Ctrl+1~4 兜底兼容（前面第1优先级已经处理过，理论上到不了这里）
            # ================================================================
            ctrl_map = {
                "ctrl+1": 0,
                "ctrl+2": 1,
                "ctrl+3": 2,
                "ctrl+4": 3,
            }
            if event.key in ctrl_map:
                in_sub = self._sub_page is not None
                self._sub_page = None
                self._sub_index = 0
                was_entered = self._menu_entered
                self._menu_index = ctrl_map[event.key]
                self._menu_entered = False
                self._update_menu_width()
                self._render_menu()
                if was_entered or in_sub:
                    self._render_content()
                event.stop()


# ======================================================================
# 启动入口
# ======================================================================
def main() -> int:
    """
    主启动函数：做 textual 可用性检查，不存在时给出 pip 安装提示
    """
    if not _TEXTUAL_AVAILABLE:
        print("=" * 60)
        print("[错误] 未安装 textual 依赖包，无法启动 TUI 界面。")
        print()
        print("请执行以下命令安装：")
        print("    pip install textual rich")
        print()
        print("安装完成后再次运行: python main.py")
        print("=" * 60)

        # 降级模式：core 模块基础自检（无界面）
        print()
        print("[降级模式] 正在运行 core 模块基础自检（无界面）...")
        try:
            pm = PathManager()
            print(f"项目根: {pm.project_root}")
            paths = pm.discover()
            print(f"Hashcat : {'OK' if paths.hashcat else '未找到'} ({paths.hashcat_root})")
            print(f"zip2john: {'OK' if paths.zip2john else '未找到'}")
            print(f"rar2john: {'OK' if paths.rar2john else '未找到'}")
            print(f"7z2john : {'OK' if paths.seven2john_perl else '未找到'}")
            cracker = HashcatExecutor(pm)
            print()
            print("Hashcat 设备枚举:")
            for ln in cracker.list_devices().splitlines():
                print("   ", ln)
        except Exception as exc:  # noqa: BLE001
            print(f"core 自检异常: {type(exc).__name__}: {exc}")
        return 1

    app = CrackerApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
