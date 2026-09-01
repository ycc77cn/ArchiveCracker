# -*- coding: utf-8 -*-
"""
文件名称：main_gui.py
功能描述：ArchiveCracker GUI 版入口（PyQt6）
          与 TUI 版（main.py）功能完全一致，布局/配色还原 nushell 风格。
          左侧菜单 + 右侧内容区 + 顶部标题横线 + 底部监控状态栏。
          密码破解4种模式、字典生成3种模式、工具自检、帮助/软件说明 全部还原。
创建日期：2026-08-31
依赖：PyQt6（Python 3.13 已安装）
"""

import os
import re
import sys
import shutil
import time
import unicodedata
from pathlib import Path
from datetime import datetime
from typing import Optional, List

# PyQt6 导入
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QStackedWidget, QScrollArea,
    QPushButton, QLineEdit, QCheckBox, QFileDialog, QDialog,
    QTextEdit, QFrame, QGroupBox, QSplitter, QGridLayout, QSizePolicy,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QMimeData, QEvent,
)
from PyQt6.QtGui import (
    QFont, QColor, QDragEnterEvent, QDropEvent, QPalette, QPixmap,
    QAction, QKeySequence, QShortcut, QIcon,
)

# 项目内核心模块（与 main.py 共用，零重复）
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


# ======================================================================
# 全局工具函数
# ======================================================================

def _app_base() -> Path:
    """返回应用资源根目录。
    开发运行时取脚本所在目录；打包运行时取 exe 所在目录（外置 bin 同级）。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后，sys.executable 指向 exe 本身，父目录即为外置 bin 所在位置
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _fmt_bytes(num: int) -> str:
    """字节数格式化为人类可读字符串"""
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
    """获取路径所在盘符的剩余可用字节数"""
    try:
        usage = shutil.disk_usage(path)
        return usage.free
    except Exception:
        return 0


def _disp_w(s: str) -> int:
    """计算字符串显示宽度（全角2列，半角1列）"""
    width = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width


def _pad_w(text: str, target_width: int) -> str:
    """用空格将文本补齐到目标显示宽度"""
    current = _disp_w(text)
    if current >= target_width:
        return text
    return text + ' ' * (target_width - current)


def _markup_to_html(text: str) -> str:
    """将 Textual rich markup 文本转换为 Qt 富文本 HTML。

    core 层 format_report_text 返回的文本含 Textual 颜色标记，
    形如 [#00ff00]内容[/]，TUI 能直接渲染，QLabel 不能。
    这里转换为 <span style="color:..."> 并保留空格与换行，
    使 GUI 设备信息页呈现与 TUI 一致的配色。

    处理顺序：
      1. 先用占位符提取 [#RRGGBB] 与 [/] 标记
      2. 对剩余普通文本做 HTML 转义 + 空格&nbsp; + 换行<br>
      3. 把占位符还原为真实 span 标签（避免 &nbsp; 污染标签本身）

    :param text: 含 Textual markup 的源文本
    :return: 可直接 setHtml 的 HTML 片段
    """
    import html as _html_mod
    # 1. 用不可见占位符替换 markup 标记（\x00 不会出现在正常文本中）
    token_re = re.compile(r'\[#([0-9a-fA-F]{6})\]|\[/\]')

    def _token_repl(m: "re.Match") -> str:
        if m.group(0) == "[/]":
            return "\x00CLOSE\x00"
        return f"\x00COLOR:{m.group(1)}\x00"

    masked = token_re.sub(_token_repl, text)
    # 2. 转义 + 空格/换行处理（此时无 HTML 标签，安全）
    masked = _html_mod.escape(masked)
    masked = masked.replace("\n", "<br>").replace(" ", "&nbsp;")
    # 3. 还原 span 标签
    masked = re.sub(r'\x00COLOR:([0-9a-fA-F]{6})\x00',
                    r'<span style="color:#\1;">', masked)
    masked = masked.replace("\x00CLOSE\x00", "</span>")
    return masked


def _hw_report_to_html(report) -> str:
    """将硬件报告渲染为 HTML 表格，右侧边框天然对齐。

    弃用 core 层 format_report_text 的「空格填充 + 等宽字体」方案——
    那种方案在非等宽字体/不同系统下右边界会错位。
    改用 HTML <table> 由 Qt 排版，右边框在任何系统任何字体下都对齐。

    :param report: HardwareReport 对象
    :return: HTML 表格字符串（含 <table> 根节点）
    """
    import html as _html_mod

    esc = _html_mod.escape

    def _row(key: str, value: str, indent: int = 0) -> str:
        """普通键值行（td 带绿色边框，右侧竖线天然对齐）"""
        pad = "&nbsp;" * indent
        return (
            f"<tr>"
            f"<td style=\"border:1px solid {C_NS_GREEN};color:{C_NS_WHITE};"
            f"padding:2px 8px;\">{pad}{esc(key)}</td>"
            f"<td style=\"border:1px solid {C_NS_GREEN};color:{C_NS_WHITE};"
            f"padding:2px 8px;\">{esc(value)}</td>"
            f"</tr>"
        )

    def _section(title: str) -> str:
        """分区标题行（跨两列，青蓝色加粗）"""
        return (
            f"<tr>"
            f"<td colspan=\"2\" style=\"border:1px solid {C_NS_GREEN};"
            f"color:{C_NS_CYAN};font-weight:bold;padding:2px 8px;\">"
            f"{esc(title)}</td>"
            f"</tr>"
        )

    rows: list[str] = []
    # 表格标题行（跨两列，绿色加粗）
    rows.append(
        f"<tr>"
        f"<td colspan=\"2\" style=\"border:1px solid {C_NS_GREEN};"
        f"color:{C_NS_GREEN};font-weight:bold;padding:2px 8px;\">"
        f"设备信息</td>"
        f"</tr>"
    )
    # 基本信息
    rows.append(_row("OS", report.os_name))
    rows.append(_row("主机名", report.hostname))

    # CPU
    rows.append(_section("CPU"))
    rows.append(_row("型号", report.cpu.name, 2))
    rows.append(_row("物理核心", f"{report.cpu.physical_cores} 核", 2))
    rows.append(_row("逻辑线程", f"{report.cpu.logical_cores} 线程", 2))
    if report.cpu.max_frequency_mhz:
        rows.append(_row("最大频率", f"{report.cpu.max_frequency_mhz} MHz", 2))

    # 内存
    rows.append(_section("内存"))
    if report.memory_sticks:
        total_gb = sum(s.capacity_gb for s in report.memory_sticks)
        rows.append(_row("总容量", f"{total_gb:.1f} GB ({len(report.memory_sticks)} 条)", 2))
        for idx, stick in enumerate(report.memory_sticks, 1):
            rows.append(_row(
                f"槽位 {idx}",
                f"{stick.capacity_gb:.1f} GB  {stick.ddr_type}  {stick.speed_mt_s} MT/s",
                2,
            ))
    else:
        rows.append(_row("总容量", "未知", 2))

    # 磁盘（按物理磁盘分组）
    rows.append(_section("磁盘"))
    if report.disks:
        for idx, disk in enumerate(report.disks, 1):
            rows.append(_row(
                f"磁盘 {idx}",
                f"{disk.model}  {disk.size_gb:.1f} GB",
                2,
            ))
            if disk.partitions:
                for part in disk.partitions:
                    used_gb = part.total_gb - part.free_gb
                    rows.append(_row(
                        part.letter,
                        f"{used_gb:.1f}GB/{part.total_gb:.1f}GB",
                        4,
                    ))
            else:
                rows.append(_row("-", "无分区", 4))
    else:
        rows.append(_row("磁盘", "未检测到", 2))

    # GPU
    rows.append(_section("GPU"))
    if report.gpus:
        for idx, gpu in enumerate(report.gpus, 1):
            vram = f"{gpu.vram_mb} MB" if gpu.vram_mb else "未知"
            rows.append(_row(f"GPU {idx}", f"{gpu.name}  {vram}", 2))
    else:
        rows.append(_row("GPU", "未检测到", 2))

    # 软件信息
    rows.append(_section("软件信息"))
    rows.append(_row("软件名称", APP_NAME, 2))
    rows.append(_row("版本", APP_VERSION, 2))
    rows.append(_row("开发者", APP_AUTHOR, 2))
    rows.append(_row("开源地址", APP_GITHUB, 2))
    rows.append(_row("B站", APP_BILI, 2))
    rows.append(_row("粉丝群", APP_GROUP, 2))
    rows.append(_row("破解引擎", APP_ENGINE, 2))
    rows.append(_row("支持格式", "ZIP / RAR / 7Z", 2))

    # 拼表格：绿色边框，右竖线由 Qt 保证对齐
    return (
        f"<table cellspacing=\"0\" cellpadding=\"0\" "
        f"style=\"border-collapse:collapse;background-color:{C_BG_DARK};\">"
        + "".join(rows)
        + "</table>"
    )


# ======================================================================
# 软件元信息（版本号等统一在此维护，避免散落写死）
# ======================================================================
APP_NAME    = "ArchiveCracker 压缩包密码爆破工具"
APP_VERSION = "V 0.3"
APP_AUTHOR  = "杨CC"
APP_GITHUB  = "https://github.com/ycc77cn/ArchiveCracker"
APP_BILI    = "疯狂的杨CC"
APP_GROUP   = "660264846"
APP_ENGINE  = "Hashcat + John the Ripper"

# ======================================================================
# nushell 配色常量
# ======================================================================
C_NS_GREEN  = "#00ff00"
C_NS_BLUE   = "#82cfff"
C_NS_PURPLE = "#ff00ff"
C_NS_CYAN   = "#00ffff"
C_NS_YELLOW = "#ffff00"
C_NS_RED    = "#ff0000"
C_NS_GRAY   = "#808080"
C_NS_WHITE  = "#ffffff"

# 深色背景
C_BG_DARK    = "#0c0c0c"
C_BG_PANEL   = "#1a1a2e"
C_BG_CARD    = "#16213e"
C_BG_INPUT   = "#0f0f23"
C_BG_LIST    = "#1a1a2e"
C_BG_LIST_SEL = "#2a2a4e"
C_BORDER     = "#333366"

# ======================================================================
# 菜单项与子页面操作项常量（与 main.py 完全一致）
# ======================================================================

_MENU_ITEMS = [
    ("menu_crack", "1. 密码破解"),
    ("menu_dict",  "2. 字典生成"),
    ("menu_tools", "3. 工具自检"),
    ("menu_help",  "4. 帮助说明"),
    ("menu_about", "5. 软件说明"),
    ("menu_quit",  "6. 退出软件"),
]

_TOOLS_SUB_ITEMS = [
    ("sub_recheck",  "1. 重新检测"),
    ("sub_download", "2. 下载工具（待命）"),
    ("sub_back",     "3. 返回上一层"),
]

_DICT_MENU_ITEMS = [
    ("dict_classic", "1. 经典字典生成"),
    ("dict_social",  "2. 社工字典生成"),
    ("dict_mask",    "3. 掩码字典生成"),
    ("dict_other",   "4. 其他字典生成"),
    ("dict_help",    "5. 帮助使用说明"),
    ("dict_back",    "6. 返回上一层"),
]

_CRACK_MENU_ITEMS = [
    ("crack_dict",   "1. 字典攻击"),
    ("crack_mask",   "2. 掩码攻击"),
    ("crack_rule",   "3. 字典加规则"),
    ("crack_brute",  "4. 暴力穷举"),
    ("crack_help",   "5. 帮助说明"),
    ("crack_back",   "6. 返回上一层"),
]

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

_DICT_MASK_ITEMS = [
    ("mask_input",    "input",  "1. 输入掩码"),
    ("mask_preset",   "action", "2. 快速模板"),
    ("mask_max_lines","input",  "3. 生成数量"),
    ("mask_out_dir",  "input",  "4. 输出目录"),
    ("mask_gen",      "action", "5. 开始生成"),
    ("mask_help",     "action", "6. 帮助说明"),
    ("mask_back",     "action", "7. 返回上一层"),
]

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

_CRACK_MODE_PAGES = ("crack_mask", "crack_rule", "crack_brute")

_MASK_PRESETS = [
    # 纯数字
    ("?d?d?d",                "纯数字3位(000~999, 密码锁常见)"),
    ("?d?d?d?d",              "纯数字4位(0000~9999)"),
    ("?d?d?d?d?d",            "纯数字5位(00000~99999)"),
    ("?d?d?d?d?d?d",          "纯数字6位(000000~999999)"),
    ("?d?d?d?d?d?d?d",        "纯数字7位(0000000~9999999)"),
    ("?d?d?d?d?d?d?d?d",      "纯数字8位(00000000~99999999)"),
    ("?d?d?d?d?d?d?d?d?d?d?d", "纯数字11位(手机号)"),
    # 日期格式
    ("?d?d?d?d?d?d",          "日期6位(如199912, 年月)"),
    ("?d?d?d?d?d?d?d?d",      "日期8位(如19991231, 年月日)"),
    ("?d?d?d?d?l?l",          "年份4位+月份缩写(如2000ab)"),
    # 纯字母
    ("?l?l?l",                "小写字母3位"),
    ("?l?l?l?l",              "小写字母4位"),
    ("?l?l?l?l?l?l",          "小写字母6位"),
    ("?l?l?l?l?l?l?l?l",      "小写字母8位"),
    ("?u?u?u?u",              "大写字母4位"),
    # 大写首字母+小写
    ("?u?l?l?l",              "大写首字母+小写3位"),
    ("?u?l?l?l?l?l",          "大写首字母+小写5位"),
    ("?u?l?l?l?l?l?l?l",      "大写首字母+小写7位"),
    # 字母+数字混合
    ("?l?l?l?l?d?d?d?d",      "小写4位+数字4位"),
    ("?l?l?l?l?l?d?d?d?d",    "小写5位+数字4位"),
    ("?u?l?l?l?d?d",          "大写首字母+小写3位+数字2位"),
    ("?u?l?l?l?l?d?d?d?d",    "大写首字母+小写4位+数字4位"),
    ("?u?l?l?l?l?l?d?d?d",    "大写首字母+小写5位+数字3位"),
    ("?l?d?d?d?d?d?d",        "字母1位+数字6位"),
    ("?d?l?l?l?l?l",          "数字1位+小写5位"),
    ("?l?l?d?d?d?d?d?d",      "字母2位+数字6位"),
    ("?l?l?l?d?d?d?d",        "字母3位+数字4位"),
    # 常见前缀
    ("pass?d?d?d",            "pass前缀+数字3位"),
    ("pass?d?d?d?d",          "pass前缀+数字4位"),
    ("pass?d?d?d?d?d?d",      "pass前缀+数字6位"),
    ("admin?d?d?d",           "admin前缀+数字3位"),
    ("admin?d?d?d?d",         "admin前缀+数字4位"),
    ("root?d?d?d",            "root前缀+数字3位"),
    ("root?d?d?d?d",          "root前缀+数字4位"),
    ("test?d?d?d",            "test前缀+数字3位"),
    ("test?d?d?d?d",          "test前缀+数字4位"),
    ("abc?d?d?d",             "abc前缀+数字3位"),
    ("abc?d?d?d?d",           "abc前缀+数字4位"),
    ("qwerty?d?d?d",          "qwerty前缀+数字3位"),
    # 数字开头+字母
    ("123?l?l?l",             "123前缀+字母3位"),
    ("123?l?l?l?l",           "123前缀+字母4位"),
    ("123?d?d?d?d?d?d",       "123前缀+数字6位"),
    ("000?d?d?d?d?d",         "000前缀+数字5位"),
    # 邮箱/特殊格式
    ("?d?d?d?d?d?d@?l?l",     "数字6位+@+字母2位"),
    ("?d?d?d?d?d?d@?d?d?d",   "数字6位+@+数字3位"),
    ("?l?l?l?d?d?d@?l?l?l",   "字母3位+数字3位+@+字母3位"),
    # 包含特殊字符
    ("?l?l?l?l!?",            "小写4位+感叹号"),
    ("pass!?d?d?d",           "pass!+数字3位"),
    ("?d?d?d?d#!",            "数字4位+井号感叹号"),
]

# 暴力穷举专用快速模板
# 每条: (标题, {勾选开关}, 最小长度, 最大长度, 中文说明)
_BRUTE_PRESETS = [
    ("纯数字 4位",      {"lower": False, "upper": False, "digit": True,  "special": False}, 4, 4,
     "0000~9999, 最常见的纯数字密码"),
    ("纯数字 6位",      {"lower": False, "upper": False, "digit": True,  "special": False}, 6, 6,
     "000000~999999, 6位数字密码"),
    ("纯数字 8位",      {"lower": False, "upper": False, "digit": True,  "special": False}, 8, 8,
     "00000000~99999999, 8位数字密码"),
    ("纯数字 4-8位",    {"lower": False, "upper": False, "digit": True,  "special": False}, 4, 8,
     "4到8位纯数字, 覆盖常见数字密码长度"),
    ("小写字母 4-6位",  {"lower": True,  "upper": False, "digit": False, "special": False}, 4, 6,
     "纯小写字母, 适合英文单词型密码"),
    ("小写+数字 4-8位", {"lower": True,  "upper": False, "digit": True,  "special": False}, 4, 8,
     "小写字母+数字混合, 最常见的组合密码"),
    ("大小写+数字 6-8位", {"lower": True,  "upper": True,  "digit": True,  "special": False}, 6, 8,
     "大小写字母+数字, 中等强度密码"),
    ("全字符集 6-8位",  {"lower": True,  "upper": True,  "digit": True,  "special": True},  6, 8,
     "字母+数字+特殊字符, 高强度密码"),
    ("全字符集 8-12位", {"lower": True,  "upper": True,  "digit": True,  "special": True},  8, 12,
     "8到12位全字符集, 极高强度但耗时巨大"),
]

_RULE_PRESETS = [
    ("best66.rule",         str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "best66.rule")),
    ("rockyou-30000.rule",  str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "rockyou-30000.rule")),
    ("dive.rule",           str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "dive.rule")),
    ("d3ad0ne.rule",        str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "d3ad0ne.rule")),
    ("toggles5.rule",       str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "toggles5.rule")),
    ("leetspeak.rule",      str(_app_base() / "bin" / "windows" / "hashcat" / "rules" / "leetspeak.rule")),
]

_RULE_DESCRIPTIONS = {
    "best66.rule": "常用高频规则。覆盖大小写、数字后缀、首尾追加等最常见变形，速度快，适合日常快速破解。",
    "combinator.rule": "组合拼接规则。把字典词与常见字符/词缀拼接，覆盖\"前后缀+单词\"类密码。",
    "d3ad0ne.rule": "高强度综合规则。包含大量替换、大小写、数字组合，覆盖面广，速度较慢，适合时间充裕时使用。",
    "dive.rule": "深度变形规则。规则数量极大，覆盖非常广，适合字典不够用或追求高命中率时使用，速度最慢。",
    "generated.rule": "自动生成规则。由规则生成器产出，覆盖常见变形组合，通用性强，速度中等。",
    "generated2.rule": "自动生成规则扩展版。比 generated 覆盖更多组合，速度更慢，适合进一步扩大候选集。",
    "Incisive-leetspeak.rule": "Leetspeak 黑客文替换规则。把 a→4、e→3、o→0 等字符替换成数字/符号，并叠加常见变形。",
    "InsidePro-HashManager.rule": "InsidePro HashManager 通用规则。覆盖常规大小写、数字、替换组合，偏向通用口令测试。",
    "InsidePro-PasswordsPro.rule": "InsidePro PasswordsPro 规则。偏重数字和大小写混合变形，适合爆破常见业务密码。",
    "leetspeak.rule": "纯 Leetspeak 替换规则。a→4、e→3、o→0、s→5 等字符替换，命中\"黑客文\"风格密码。",
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
    "T0XlC-insert_top_100_passwords_1_G.rule": "T0XlC Top100 弱密码规则。把常用弱密码作为片段插入，命中\"单词+弱密码\"组合。",
    "toggles1.rule": "大小写切换 1 级。最轻量的大小写变化，速度快，适合只做少量大写变体。",
    "toggles2.rule": "大小写切换 2 级。在 1 级基础上增加更多切换位置，速度较快。",
    "toggles3.rule": "大小写切换 3 级。覆盖较多种大小写组合，速度中等。",
    "toggles4.rule": "大小写切换 4 级。组合更多，适合爆破混合大小写密码，速度较慢。",
    "toggles5.rule": "大小写切换 5 级。最全的大小写切换组合，覆盖最多，速度最慢。",
    "top10_2025.rule": "2025 年 Top10 高频规则。基于最新常见密码变形整理，适合最新字典库快速扩展。",
    "unix-ninja-leetspeak.rule": "Unix-ninja Leetspeak 规则。专业 Leetspeak 替换集合，覆盖大量字符替换组合。",
}

_CHARSET_LOWER   = "abcdefghijklmnopqrstuvwxyz"
_CHARSET_UPPER   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CHARSET_DIGIT   = "0123456789"
_CHARSET_SPECIAL = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"

_DICT_LARGE_COUNT_THRESHOLD: int = 10_000_000
_DICT_LONG_VAL_THRESHOLD = 20

_ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
_DICT_EXTS = {".txt", ".dic", ".lst"}
_RULE_EXTS = {".rule"}

# 帮助文本常量（与 main.py 一致，但用纯文本格式）
_HELP_ABOUT = [
    ("section", "软件名称"),
    ("raw", APP_NAME),
    ("raw", f"版本 {APP_VERSION}"),
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
    ("raw", "GUI 界面基于 PyQt6 构建"),
    ("raw", "哈希提取由 John the Ripper 完成"),
    ("raw", "破解执行由 Hashcat 完成"),
    ("raw", "实时进度与历史记录在页面内展示"),
    ("blank",),
    ("section", "使用提示"),
    ("raw", "文件可直接拖入窗口自动识别"),
    ("raw", "支持 WASD/方向键/空格 快捷键操作"),
    ("raw", "破解中可点击中断按钮"),
    ("raw", "退出软件请选「退出软件」或 Ctrl+Q"),
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
    ("kv", "鼠标点击", "选择菜单/操作项"),
    ("kv", "W/S 或 上/下", "上下切换菜单项"),
    ("kv", "D/回车/空格", "确认进入当前项"),
    ("kv", "A/ESC", "返回上一层"),
    ("kv", "J/K", "右侧内容上下翻页"),
    ("kv", "拖拽", "文件拖入窗口自动识别"),
    ("kv", "Ctrl+Q", "退出程序"),
    ("kv", "Ctrl+1/2/3/4", "快速跳转破解/字典/自检/帮助"),
    ("blank",),
    ("section", "破解提示"),
    ("raw", "文件可直接拖入窗口自动识别"),
    ("raw", "掩码 ?d/?l/?u/?s/?a 含义见掩码攻击页"),
    ("raw", "规则文件可在字典加规则页选择,带中文说明"),
    ("raw", "破解中可点击中断按钮"),
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
]

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
]

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
    ("kv", "5", "开始破解"),
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
    ("section", "拖入文件"),
    ("raw", "zip/rar/7z → 压缩包字段"),
    ("raw", "txt/dic/lst → 字典字段"),
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
    ("blank",),
    ("section", "示例"),
    ("raw", "?d?d?d?d = 0000-9999"),
    ("raw", "pass?d?d?d = pass000-999"),
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
    ("kv", "2", "快速模板(循环切换)"),
    ("kv", "3", "生成数量(0=全部)"),
    ("kv", "4", "输出目录路径"),
    ("kv", "5", "开始生成字典"),
    ("blank",),
    ("section", "示例"),
    ("raw", "?d?d?d?d → 0000~9999"),
    ("raw", "pass?d?d → pass00~pass99"),
    ("raw", "?l?l?d?d → aa00~zz99"),
]

# 社工字典子页面操作项
_DICT_SOCIAL_ITEMS = [
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
    ("soc_company",      "input",  "14. 公司名"),
    ("soc_position",     "input",  "15. 职位"),
    ("soc_employee_id",  "input",  "16. 工号"),
    ("soc_school",       "input",  "17. 学校名"),
    ("soc_school_year",  "input",  "18. 入学年份"),
    ("soc_spouse_name",  "input",  "19. 配偶姓名"),
    ("soc_child_name",   "input",  "20. 子女姓名"),
    ("soc_pet_name",     "input",  "21. 宠物名"),
    ("soc_anniversary",  "input",  "22. 纪念日"),
    ("soc_car_plate",    "input",  "23. 车牌号"),
    ("soc_favorite_words", "input", "24. 喜好词汇(逗号分隔)"),
    ("soc_lucky_numbers",  "input", "25. 幸运数字(逗号分隔)"),
    ("soc_area_code",      "input", "26. 地区区号"),
    ("soc_common_suffixes","input", "27. 自定义后缀(逗号分隔)"),
    ("soc_out_dir",      "input",  "28. 输出目录"),
    ("soc_gen",          "action", "29. 开始生成"),
    ("soc_help",         "action", "30. 帮助说明"),
    ("soc_back",         "action", "31. 返回上一层"),
]


# ======================================================================
# QSS 全局样式表（还原 nushell 深色配色）
# ======================================================================
QSS_GLOBAL = f"""
QMainWindow, QDialog {{
    background-color: {C_BG_DARK};
    color: {C_NS_WHITE};
    font-family: "Consolas", "Microsoft YaHei", "Courier New", monospace;
    font-size: 13px;
}}
QLabel {{
    color: {C_NS_WHITE};
    background: transparent;
}}
QListWidget {{
    background-color: {C_BG_LIST};
    color: {C_NS_WHITE};
    border: 1px solid {C_BORDER};
    border-radius: 2px;
    padding: 4px;
    font-size: 13px;
    outline: none;
}}
QListWidget::item {{
    padding: 6px 8px;
    border-radius: 2px;
    color: {C_NS_WHITE};
}}
QListWidget::item:selected {{
    background-color: {C_BG_LIST_SEL};
    color: {C_NS_GREEN};
    font-weight: bold;
}}
QListWidget::item:hover {{
    background-color: #222244;
}}
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {C_BG_INPUT};
    color: {C_NS_YELLOW};
    border: 1px solid {C_BORDER};
    border-radius: 2px;
    padding: 4px 6px;
    font-size: 13px;
    selection-background-color: #333366;
}}
QLineEdit:focus {{
    border: 1px solid {C_NS_CYAN};
}}
QPushButton {{
    background-color: {C_BG_PANEL};
    color: {C_NS_WHITE};
    border: 1px solid {C_BORDER};
    border-radius: 2px;
    padding: 6px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    background-color: #2a2a4e;
    border-color: {C_NS_CYAN};
    color: {C_NS_CYAN};
}}
QPushButton:pressed {{
    background-color: #1a1a3e;
}}
QPushButton:disabled {{
    color: {C_NS_GRAY};
    background-color: #1a1a1a;
}}
QCheckBox {{
    color: {C_NS_WHITE};
    spacing: 6px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {C_NS_BLUE};
    border-radius: 2px;
    background-color: {C_BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background-color: {C_NS_GREEN};
    border: 1px solid {C_NS_GREEN};
    image: none;
}}
QScrollArea {{
    background-color: {C_BG_DARK};
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background-color: {C_BG_DARK};
}}
QScrollBar:vertical {{
    background: {C_BG_PANEL};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    min-height: 30px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {C_NS_BLUE};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: {C_BG_PANEL};
    height: 8px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {C_BORDER};
    min-width: 30px;
    border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QGroupBox {{
    color: {C_NS_CYAN};
    border: 1px solid {C_BORDER};
    border-radius: 3px;
    margin-top: 10px;
    padding-top: 6px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QFrame[frameShape="4"] {{
    color: {C_BORDER};
}}
QSplitter::handle {{
    background-color: {C_BORDER};
}}
QSplitter::handle:hover {{
    background-color: {C_NS_BLUE};
}}
"""


# ======================================================================
# 等宽字体（用于状态栏/标题等）
# ======================================================================
_FONT_MONO = QFont("Consolas", 10)
_FONT_MONO.setStyleHint(QFont.StyleHint.Monospace)
_FONT_TITLE = QFont("Consolas", 11, QFont.Weight.Bold)
_FONT_LABEL = QFont("Microsoft YaHei", 10)
_FONT_SMALL = QFont("Consolas", 9)


# ======================================================================
# Worker 线程类：字典生成 + 密码破解
# ======================================================================

class DictGenWorker(QThread):
    """字典生成后台线程
    :param dict_gen: DictGenerator 实例
    :param cfg: GenConfig 配置
    :param ts: 时间戳字符串
    """
    finished_signal = pyqtSignal(object, str)  # (GenResult, timestamp)

    def __init__(self, dict_gen: DictGenerator, cfg: GenConfig, ts: str):
        super().__init__()
        self._dict_gen = dict_gen
        self._cfg = cfg
        self._ts = ts

    def run(self):
        """线程执行：生成字典，发送结果信号"""
        result = self._dict_gen.generate(self._cfg)
        self.finished_signal.emit(result, self._ts)


class SocialGenWorker(QThread):
    """社工字典生成后台线程"""
    finished_signal = pyqtSignal(object, str)  # (GenResult, timestamp)

    def __init__(self, dict_gen: DictGenerator, social_cfg: SocialConfig, ts: str):
        super().__init__()
        self._dict_gen = dict_gen
        self._social_cfg = social_cfg
        self._ts = ts

    def run(self):
        """线程执行：生成社工字典"""
        result = self._dict_gen.generate_social(self._social_cfg)
        self.finished_signal.emit(result, self._ts)


class CrackWorker(QThread):
    """密码破解后台线程
    :param extractor: HashExtractor 实例
    :param cracker: HashcatExecutor 实例
    :param archive_path: 压缩包路径
    :param cfg: CrackConfig 配置
    :param page: 页面标识（crack_dict / crack_mask / crack_rule / crack_brute）
    :param ts: 时间戳字符串
    """
    progress_signal = pyqtSignal(dict)   # 实时进度
    finished_signal = pyqtSignal(str, bool, object, object, str, float)

    def __init__(self, extractor: HashExtractor, cracker: HashcatExecutor,
                 archive_path: str, cfg: CrackConfig, page: str, ts: str):
        super().__init__()
        self._extractor = extractor
        self._cracker = cracker
        self._archive_path = archive_path
        self._cfg = cfg
        self._page = page
        self._ts = ts
        self._start_ts = time.time()
        self._stop_flag = False

    def request_stop(self):
        """请求停止破解（设置标志位，cracker.run 内部会检测）"""
        self._stop_flag = True

    def run(self):
        """线程执行：提取哈希 → 运行 Hashcat → 发送结果"""
        # 1. 提取哈希
        extract_result = self._extractor.extract(self._archive_path)
        if not extract_result.success:
            err_msg = extract_result.error_message or "未知错误"
            self.finished_signal.emit(
                self._ts, False, None, extract_result, err_msg, time.time() - self._start_ts)
            return

        # 2. 构建哈希配置
        self._cfg.hash_file_path = extract_result.hash_file_path or ""
        self._cfg.hashcat_mode = extract_result.hashcat_mode or 0

        # 3. 进度回调
        _STATUS_CN = {
            "running": "运行中", "cracked": "已破解", "exhausted": "字典试完",
            "stopped": "已停止", "error": "错误", "init": "初始化",
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

        def _on_progress(progress: CrackProgress):
            """hashcat 进度回调（在 worker 线程执行，通过信号转发到主线程）"""
            live: dict = {"elapsed": time.time() - self._start_ts}
            if progress.status:
                live["status_text"] = _STATUS_CN.get(
                    progress.status.value, progress.status.value)
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
            self.progress_signal.emit(live)

        # 4. 执行破解
        crack_result = None
        try:
            crack_result = self._cracker.run(self._cfg, progress_callback=_on_progress)
        except Exception as exc:
            crack_result = type("ErrResult", (), {
                "success": False, "status": None,
                "recovered_passwords": {},
                "error_message": f"worker异常: {type(exc).__name__}: {exc}",
            })()

        elapsed = time.time() - self._start_ts
        success = crack_result.success if crack_result is not None else False
        error_msg = (crack_result.error_message if crack_result is not None
                     else "破解任务未返回结果")

        # 5. 发送完成信号
        self.finished_signal.emit(
            self._ts, success, crack_result, extract_result, error_msg, elapsed)


# ======================================================================
# 弹窗对话框类
# ======================================================================

class HelpDialog(QDialog):
    """帮助内容弹窗（还原 TUI HelpScreen）
    以 nushell box 风格显示多行帮助内容，支持分区标题和 kv 格式。
    """

    def __init__(self, sections: list, title: str = "使用帮助", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.setMinimumHeight(400)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # 标题
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {C_NS_CYAN}; font-size: 15px; font-weight: bold; padding: 4px;")
        layout.addWidget(title_label)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 内容区
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {C_BG_PANEL};
                color: {C_NS_WHITE};
                border: 1px solid {C_BORDER};
                border-radius: 3px;
                padding: 8px;
                font-size: 13px;
            }}
        """)
        html_parts = []
        for item in sections:
            kind = item[0]
            if kind == "section":
                html_parts.append(
                    f'<div style="color:{C_NS_CYAN}; font-weight:bold; '
                    f'margin-top:10px; margin-bottom:4px;">{item[1]}</div>')
            elif kind == "kv":
                key_esc = item[1].replace("<", "&lt;").replace(">", "&gt;")
                val_esc = item[2].replace("<", "&lt;").replace(">", "&gt;")
                html_parts.append(
                    f'<div style="margin-left:12px;">'
                    f'<span style="color:{C_NS_GRAY};">{key_esc}: </span>'
                    f'<span style="color:{C_NS_WHITE};">{val_esc}</span>'
                    f'</div>')
            elif kind == "raw":
                raw_esc = item[1].replace("<", "&lt;").replace(">", "&gt;")
                html_parts.append(
                    f'<div style="margin-left:12px; color:{C_NS_WHITE};">{raw_esc}</div>')
            elif kind == "blank":
                html_parts.append('<div style="height:8px;"></div>')
        text_edit.setHtml("\n".join(html_parts))
        layout.addWidget(text_edit)

        # 关闭按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("关闭 (回车/空格/ESC)")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        # 快捷键: 回车/空格确认关闭, A/ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Return"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Space"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("A"), self).activated.connect(self.accept)


class ConfirmDialog(QDialog):
    """通用确认弹窗（还原 TUI ConfirmScreen）
    Y 确认 / N 或 ESC 取消
    """

    def __init__(self, message: str, title: str = "确认操作", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._confirmed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {C_NS_YELLOW}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        msg_label = QLabel(message)
        msg_label.setStyleSheet(f"color: {C_NS_WHITE}; font-size: 13px;")
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消 (N / A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        confirm_btn = QPushButton("确认 (Y / 回车 / 空格)")
        confirm_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; font-weight: bold; }}")
        confirm_btn.clicked.connect(self._on_confirm)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        # 快捷键: Y/回车/空格确认, N/A/ESC取消
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("N"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Y"), self).activated.connect(self._on_confirm)
        QShortcut(QKeySequence("Return"), self).activated.connect(self._on_confirm)
        QShortcut(QKeySequence("Space"), self).activated.connect(self._on_confirm)

    def _on_confirm(self):
        self._confirmed = True
        self.accept()

    def is_confirmed(self) -> bool:
        return self._confirmed


class InfoDialog(QDialog):
    """通用提示弹窗（还原 TUI InfoScreen）
    仅显示信息,回车/ESC关闭
    """

    def __init__(self, message: str, title: str = "提示", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {C_NS_RED}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        msg_label = QLabel(message)
        msg_label.setStyleSheet(f"color: {C_NS_WHITE}; font-size: 13px;")
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("关闭 (回车/空格/ESC)")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        # 快捷键: 回车/空格确认关闭, A/ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Return"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Space"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("A"), self).activated.connect(self.accept)


class RuleSelectDialog(QDialog):
    """规则选择弹窗（还原 TUI RuleSelectScreen）
    自动读取 .rule 文件，列表选择，带中文说明。
    """

    def __init__(self, rules: list, parent=None):
        """rules: [(文件名, 路径, 中文说明), ...]"""
        super().__init__(parent)
        self.setWindowTitle("选择规则文件")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._rules = rules
        self._selected = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title_label = QLabel("选择规则文件")
        title_label.setStyleSheet(f"color: {C_NS_GREEN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 规则列表（支持滚动）
        list_widget = QListWidget()
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: 1px solid {C_BORDER};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {C_BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
            }}
        """)
        for name, path, desc in rules:
            display_text = f"{name}\n    {desc}"
            item = QListWidgetItem(display_text)
            list_widget.addItem(item)
        list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(list_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("返回 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; }}")
        ok_btn.clicked.connect(lambda: self._on_select(list_widget.currentItem()))
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._list_widget = list_widget

        # 快捷键: W/S/↑/↓上下选择, D/回车/空格确认, A/ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Return"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("D"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("Space"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("W"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("S"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))

        if list_widget.count() > 0:
            list_widget.setCurrentRow(0)

    @staticmethod
    def _move_selection(list_widget, delta):
        """上下移动列表选中行"""
        count = list_widget.count()
        if count > 0:
            row = list_widget.currentRow()
            if row < 0:
                row = 0
            else:
                row = (row + delta) % count
            list_widget.setCurrentRow(row)

    def _on_select(self, item):
        if item:
            row = self._list_widget.row(item)
            self._selected = self._rules[row]
            self.accept()

    def get_selected(self):
        """返回选中的 (文件名, 路径, 中文说明) 或 None"""
        return self._selected


class WorkloadDialog(QDialog):
    """工作负载选择弹窗（还原 TUI 工作负载说明）
    提供 1-4 档负载等级列表选择，每档带中文说明。
    """

    # 工作负载选项: (值, 标题, 说明)
    _WORKLOAD_OPTIONS = [
        ("1", "1=低 (后台)", "后台任务,不卡顿"),
        ("2", "2=中低", "轻度影响"),
        ("3", "3=高 (默认)", "默认,显卡满载"),
        ("4", "4=极致", "系统可能卡顿"),
    ]

    def __init__(self, current_val: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择工作负载")
        self.setMinimumWidth(520)
        self.setMinimumHeight(380)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._selected = current_val

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title_label = QLabel("工作负载 (1-4)")
        title_label.setStyleSheet(f"color: {C_NS_GREEN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 负载列表（支持滚动）
        list_widget = QListWidget()
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: 1px solid {C_BORDER};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {C_BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
            }}
        """)
        for _val, label, desc in self._WORKLOAD_OPTIONS:
            display_text = f"{label}\n    {desc}"
            list_widget.addItem(QListWidgetItem(display_text))
        list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(list_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("返回 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; }}")
        ok_btn.clicked.connect(lambda: self._on_select(list_widget.currentItem()))
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._list_widget = list_widget

        # 快捷键: W/S/↑/↓上下选择, A/D/回车/空格确认, ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Return"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("D"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("Space"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("W"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("S"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))

        # 定位到当前值（默认 3）
        current_row = 0
        for i, (val, _label, _desc) in enumerate(self._WORKLOAD_OPTIONS):
            if val == current_val:
                current_row = i
                break
        list_widget.setCurrentRow(current_row)

    def _on_select(self, item):
        if item:
            row = self._list_widget.row(item)
            self._selected = self._WORKLOAD_OPTIONS[row][0]
            self.accept()

    @staticmethod
    def _move_selection(list_widget, delta):
        """上下移动列表选中行"""
        count = list_widget.count()
        if count > 0:
            row = list_widget.currentRow()
            if row < 0:
                row = 0
            else:
                row = (row + delta) % count
            list_widget.setCurrentRow(row)

    def get_selected(self) -> str:
        """返回选中的工作负载值 "1"~"4" """
        return self._selected


class DeviceDialog(QDialog):
    """设备选择弹窗（还原 TUI 设备类型说明）
    提供 auto / gpu / cpu 三档设备类型列表选择，每档带中文说明。
    """

    # 设备选项: (值, 标题, 说明)
    _DEVICE_OPTIONS = [
        ("auto", "auto = 自动", "自动选择,优先GPU"),
        ("gpu",  "gpu = 强制GPU", "只用GPU计算, 速度最快"),
        ("cpu",  "cpu = 强制CPU", "只用CPU计算(兼容性最好)"),
    ]

    def __init__(self, current_val: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择设备类型")
        self.setMinimumWidth(520)
        self.setMinimumHeight(300)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._selected = current_val

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title_label = QLabel("设备类型 (auto/gpu/cpu)")
        title_label.setStyleSheet(f"color: {C_NS_GREEN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 设备列表（支持滚动）
        list_widget = QListWidget()
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: 1px solid {C_BORDER};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {C_BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
            }}
        """)
        for _val, label, desc in self._DEVICE_OPTIONS:
            display_text = f"{label}\n    {desc}"
            list_widget.addItem(QListWidgetItem(display_text))
        list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(list_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("返回 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; }}")
        ok_btn.clicked.connect(lambda: self._on_select(list_widget.currentItem()))
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._list_widget = list_widget

        # 快捷键: W/S/↑/↓上下选择, A/D/回车/空格确认, ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Return"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("D"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("Space"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("W"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("S"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))

        # 定位到当前值（默认 auto）
        current_row = 0
        for i, (val, _label, _desc) in enumerate(self._DEVICE_OPTIONS):
            if val == current_val:
                current_row = i
                break
        list_widget.setCurrentRow(current_row)

    def _on_select(self, item):
        if item:
            row = self._list_widget.row(item)
            self._selected = self._DEVICE_OPTIONS[row][0]
            self.accept()

    @staticmethod
    def _move_selection(list_widget, delta):
        """上下移动列表选中行"""
        count = list_widget.count()
        if count > 0:
            row = list_widget.currentRow()
            if row < 0:
                row = 0
            else:
                row = (row + delta) % count
            list_widget.setCurrentRow(row)

    def get_selected(self) -> str:
        """返回选中的设备值 "auto"/"gpu"/"cpu" """
        return self._selected


class MaskPresetDialog(QDialog):
    """掩码模板选择弹窗（还原 TUI _MASK_PRESETS 列表选择）
    展示常见掩码模板,每条带中文说明,用户自行选择。
    """

    def __init__(self, presets: list, current_mask: str, parent=None):
        """presets: [(掩码, 中文说明), ...]  current_mask: 当前值用于定位"""
        super().__init__(parent)
        self.setWindowTitle("选择掩码模板")
        self.setMinimumWidth(560)
        self.setMinimumHeight(500)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._presets = presets
        self._selected = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title_label = QLabel("掩码模板 (?)")
        title_label.setStyleSheet(f"color: {C_NS_GREEN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 模板列表（支持滚动）
        list_widget = QListWidget()
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: 1px solid {C_BORDER};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {C_BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
            }}
        """)
        for mask, desc in presets:
            display_text = f"{mask}\n    {desc}"
            list_widget.addItem(QListWidgetItem(display_text))
        list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(list_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("返回 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; }}")
        ok_btn.clicked.connect(lambda: self._on_select(list_widget.currentItem()))
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._list_widget = list_widget

        # 快捷键: W/S/↑/↓上下选择, A/D/回车/空格确认, ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Return"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("D"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("Space"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("W"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("S"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))

        # 定位到当前掩码值
        current_row = 0
        for i, (mask, _desc) in enumerate(presets):
            if mask == current_mask:
                current_row = i
                break
        list_widget.setCurrentRow(current_row)

    def _on_select(self, item):
        if item:
            row = self._list_widget.row(item)
            self._selected = self._presets[row][0]
            self.accept()

    @staticmethod
    def _move_selection(list_widget, delta):
        """上下移动列表选中行"""
        count = list_widget.count()
        if count > 0:
            row = list_widget.currentRow()
            if row < 0:
                row = 0
            else:
                row = (row + delta) % count
            list_widget.setCurrentRow(row)

    def get_selected(self):
        """返回选中的掩码字符串, 或 None(未选)"""
        return self._selected


class BrutePresetDialog(QDialog):
    """暴力穷举模板选择弹窗
    展示专用快速模板, 每条带字符集+长度范围+中文说明, 用户自行选择。
    选中后返回模板索引, 由主窗口应用字符集勾选和长度范围。
    """

    def __init__(self, presets: list, parent=None):
        """presets: _BRUTE_PRESETS 列表"""
        super().__init__(parent)
        self.setWindowTitle("选择暴力穷举模板")
        self.setMinimumWidth(580)
        self.setMinimumHeight(500)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")
        self._presets = presets
        self._selected_index = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        title_label = QLabel("暴力穷举模板")
        title_label.setStyleSheet(f"color: {C_NS_GREEN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(title_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_BORDER};")
        layout.addWidget(sep)

        # 模板列表（支持滚动）
        list_widget = QListWidget()
        list_widget.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: 1px solid {C_BORDER};
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {C_BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
            }}
        """)
        for title, _toggles, min_len, max_len, desc in presets:
            display_text = f"{title}  ({min_len}~{max_len}位)\n    {desc}"
            list_widget.addItem(QListWidgetItem(display_text))
        list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(list_widget)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("返回 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; }}")
        ok_btn.clicked.connect(lambda: self._on_select(list_widget.currentItem()))
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._list_widget = list_widget

        # 快捷键: W/S/↑/↓上下选择, A/D/回车/空格确认, ESC返回
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("Return"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("D"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("Space"), self).activated.connect(
            lambda: self._on_select(list_widget.currentItem()))
        QShortcut(QKeySequence("W"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("S"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))
        QShortcut(QKeySequence("Up"), self).activated.connect(
            lambda: self._move_selection(list_widget, -1))
        QShortcut(QKeySequence("Down"), self).activated.connect(
            lambda: self._move_selection(list_widget, 1))

        if list_widget.count() > 0:
            list_widget.setCurrentRow(0)

    @staticmethod
    def _move_selection(list_widget, delta):
        """上下移动列表选中行"""
        count = list_widget.count()
        if count > 0:
            row = list_widget.currentRow()
            if row < 0:
                row = 0
            else:
                row = (row + delta) % count
            list_widget.setCurrentRow(row)

    def _on_select(self, item):
        if item:
            self._selected_index = self._list_widget.row(item)
            self.accept()

    def get_selected_index(self):
        """返回选中的模板索引, 或 None(未选)"""
        return self._selected_index


# ======================================================================
# 主窗口类
# ======================================================================

class CrackerMainWindow(QMainWindow):
    """ArchiveCracker GUI 主窗口
    布局：顶部标题横线 + 左菜单 QListWidget + 右内容 QScrollArea + 底部监控
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ArchiveCracker — 压缩包密码爆破工具")
        self.setMinimumSize(900, 600)
        self.setStyleSheet(QSS_GLOBAL)
        self.resize(1100, 700)

        # core 层管理器
        self._pm = PathManager()
        self._extractor = HashExtractor(self._pm)
        self._cracker = HashcatExecutor(self._pm)
        self._dict_gen = DictGenerator(self._pm)

        # 导航状态
        self._current_level = "main"   # main / crack / dict / tools / 子页面ID
        self._menu_index = 0
        self._sub_index = 0
        self._menu_entered = False

        # 硬件报告缓存
        self._hw_report_cache = None
        self._tools_check_cache = None

        # ===== 字典生成状态 =====
        self._dict_toggles = {
            "dict_lower":   True,
            "dict_upper":   False,
            "dict_digit":   True,
            "dict_special": False,
            "dict_single":  False,
        }
        self._dict_inputs = {
            "dict_min_len": "4",
            "dict_max_len": "6",
            "dict_out_dir": str(_app_base() / "data" / "output"),
            "dict_max_lines": "0",
        }
        self._dict_history: list = []
        self._dict_history_limit = 10
        self._pending_dict_cfg: Optional[GenConfig] = None
        self._pending_dict_ts = ""

        self._dict_mask_inputs = {
            "mask_input":     "?d?d?d?d",
            "mask_max_lines": "0",
            "mask_out_dir":   str(_app_base() / "data" / "output"),
        }
        self._dict_mask_history: list = []
        self._dict_mask_history_limit = 10
        self._pending_mask_cfg: Optional[GenConfig] = None
        self._pending_mask_ts = ""
        self._mask_preset_index = 0

        # ===== 字典攻击状态 =====
        self._crack_dict_inputs = {
            "crack_dict_archive":  "",
            "crack_dict_dict":     "",
            "crack_dict_workload": "3",
            "crack_dict_device":   "auto",
        }
        self._crack_dict_running = False
        self._crack_dict_live: dict = {}
        self._crack_dict_history: list = []
        self._crack_dict_history_limit = 10
        self._crack_dict_worker: Optional[CrackWorker] = None

        # ===== 通用破解模式状态（crack_mask / crack_rule / crack_brute） =====
        self._crack_mode_states = {
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
                "brute_index": 0,
            },
        }
        self._crack_mode_workers: dict = {}

        # ===== 社工字典状态 =====
        self._dict_social_inputs = {
            "soc_name_cn":        "",
            "soc_name_pinyin":    "",
            "soc_name_en":        "",
            "soc_nickname":       "",
            "soc_birth_year":     "",
            "soc_birth_month":    "",
            "soc_birth_day":      "",
            "soc_birth_full":     "",
            "soc_phone":          "",
            "soc_qq":             "",
            "soc_wechat":         "",
            "soc_email":          "",
            "soc_id_card":        "",
            "soc_company":        "",
            "soc_position":       "",
            "soc_employee_id":    "",
            "soc_school":         "",
            "soc_school_year":    "",
            "soc_spouse_name":    "",
            "soc_child_name":     "",
            "soc_pet_name":       "",
            "soc_anniversary":    "",
            "soc_car_plate":      "",
            "soc_favorite_words": "",
            "soc_lucky_numbers":  "",
            "soc_area_code":      "",
            "soc_common_suffixes":"",
            "soc_out_dir":        str(_app_base() / "data" / "output"),
        }
        self._dict_social_history: list = []
        self._dict_social_history_limit = 10

        # 字典生成 worker 引用
        self._dict_worker: Optional[QThread] = None

        # UI 组件引用
        self._left_list = None       # 左侧 QListWidget
        self._content_area = None     # 右侧 QScrollArea
        self._content_widget = None   # 右侧内容容器
        self._bottom_label = None     # 底栏 QLabel
        self._title_label = None     # 顶栏 QLabel

        # 构建 UI
        self._build_ui()

        # 初始化左侧菜单
        self._navigate_to("main")

        # 启动底部监控定时器（每2秒刷新）
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_bottom_bar)
        self._stats_timer.start(2000)

        # 启动实时进度刷新定时器（破解运行期间每0.3秒刷新右侧内容）
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._refresh_live_content)
        self._live_timer.start(300)

        # 全局快捷键
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.close)
        QShortcut(QKeySequence("Ctrl+1"), self).activated.connect(
            lambda: self._quick_jump("menu_crack"))
        QShortcut(QKeySequence("Ctrl+2"), self).activated.connect(
            lambda: self._quick_jump("menu_dict"))
        QShortcut(QKeySequence("Ctrl+3"), self).activated.connect(
            lambda: self._quick_jump("menu_tools"))
        QShortcut(QKeySequence("Ctrl+4"), self).activated.connect(
            lambda: self._quick_jump("menu_help"))

        # 回车键 = 激活当前选中项（相当于双击）
        QShortcut(QKeySequence("Return"), self).activated.connect(self._activate_current)
        QShortcut(QKeySequence("Enter"), self).activated.connect(self._activate_current)

        # ESC 键 = 返回上一层
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._go_back)

        # 初始底部栏
        self._refresh_bottom_bar()

    # ==================================================================
    # UI 构建
    # ==================================================================

    def _build_ui(self):
        """构建主窗口布局：顶栏 + 左菜单/右内容 + 底栏"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- 顶栏 ----
        self._title_label = QLabel()
        self._title_label.setStyleSheet(f"""
            QLabel {{
                background-color: {C_BG_DARK};
                color: {C_NS_GREEN};
                font-size: 14px;
                font-weight: bold;
                padding: 4px 12px;
                border-bottom: 1px solid {C_BORDER};
            }}
        """)
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setText(
            f'<span style="color:{C_NS_BLUE};">{"─"*5}</span>'
            f' ArchiveCracker — 压缩包密码爆破工具 '
            f'<span style="color:{C_NS_BLUE};">{"─"*5}</span>')
        main_layout.addWidget(self._title_label)

        # ---- 中央区域：左右分栏 ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"QSplitter {{ background-color: {C_BG_DARK}; }}")

        # 左侧菜单
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(0)
        self._left_list = QListWidget()
        self._left_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {C_BG_LIST};
                border: none;
                padding: 4px;
                font-size: 13px;
            }}
            QListWidget::item {{
                padding: 8px 10px;
                border-radius: 2px;
            }}
            QListWidget::item:selected {{
                background-color: {C_BG_LIST_SEL};
                color: {C_NS_GREEN};
                font-weight: bold;
            }}
            QListWidget::item:hover {{
                background-color: #222244;
            }}
        """)
        self._left_list.currentRowChanged.connect(self._on_left_select)
        self._left_list.itemDoubleClicked.connect(self._on_left_double_click)
        # 安装事件过滤器：拦截 WASD/空格，防止被 QListWidget 键盘搜索吃掉
        self._left_list.installEventFilter(self)
        left_layout.addWidget(self._left_list)
        splitter.addWidget(left_container)

        # 右侧内容区
        self._content_area = QScrollArea()
        self._content_area.setWidgetResizable(True)
        self._content_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._content_area.setStyleSheet(f"""
            QScrollArea {{
                background-color: {C_BG_DARK};
                border: none;
            }}
        """)
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet(f"background-color: {C_BG_DARK};")
        self._content_area.setWidget(self._content_widget)
        splitter.addWidget(self._content_area)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 840])
        main_layout.addWidget(splitter, 1)

        # ---- 底栏 ----
        self._bottom_label = QLabel()
        self._bottom_label.setStyleSheet(f"""
            QLabel {{
                background-color: {C_BG_DARK};
                color: {C_NS_GRAY};
                font-size: 11px;
                padding: 4px 12px;
                border-top: 1px solid {C_BORDER};
            }}
        """)
        self._bottom_label.setFont(_FONT_SMALL)
        main_layout.addWidget(self._bottom_label)

        # 启用窗口拖入文件
        self.setAcceptDrops(True)

    # ==================================================================
    # 左侧菜单构建与导航
    # ==================================================================

    def _navigate_to(self, level: str):
        """切换到指定层级，重建左侧菜单"""
        self._current_level = level
        self._sub_index = 0
        self._build_left_menu()
        self._render_content()

    def _build_left_menu(self):
        """根据当前层级构建左侧菜单项"""
        self._left_list.blockSignals(True)
        self._left_list.clear()
        level = self._current_level

        if level == "main":
            items = _MENU_ITEMS
        elif level == "crack":
            items = _CRACK_MENU_ITEMS
        elif level == "dict":
            items = _DICT_MENU_ITEMS
        elif level == "tools":
            items = _TOOLS_SUB_ITEMS
        elif level in ("dict_classic", "dict_social", "dict_mask",
                        "crack_dict", "crack_mask", "crack_rule", "crack_brute"):
            items = self._get_sub_page_items(level)
        else:
            items = _MENU_ITEMS

        for item_id, *rest in items:
            if len(rest) == 1:
                label = rest[0]
            else:
                # 子页面项: (id, type, label) → 追加状态显示
                item_type = rest[0]
                base_label = rest[1]
                label = self._get_item_display_label(level, item_id, item_type, base_label)
            list_item = QListWidgetItem(label)
            list_item.setData(Qt.ItemDataRole.UserRole, item_id)
            self._left_list.addItem(list_item)

        if self._left_list.count() > 0:
            # 恢复之前的选中行，避免弹窗确认后跳回第一行
            restore_row = self._sub_index if 0 <= self._sub_index < self._left_list.count() else 0
            self._left_list.setCurrentRow(restore_row)

        self._left_list.blockSignals(False)

    def _get_sub_page_items(self, page: str) -> list:
        """根据子页面ID返回操作项列表"""
        if page == "dict_classic":
            return _DICT_SUB_ITEMS
        elif page == "dict_social":
            return _DICT_SOCIAL_ITEMS
        elif page == "dict_mask":
            return _DICT_MASK_ITEMS
        elif page == "crack_dict":
            return _CRACK_DICT_ITEMS
        elif page in _CRACK_MODE_PAGES:
            if page == "crack_mask":
                return _CRACK_MASK_ITEMS
            elif page == "crack_rule":
                return _CRACK_RULE_ITEMS
            elif page == "crack_brute":
                return _CRACK_BRUTE_ITEMS
        return []

    def _get_item_display_label(self, level: str, item_id: str, item_type: str, base_label: str) -> str:
        """根据 item 类型和页面状态, 在标签后追加状态显示
        toggle → [✓] 或 [ ]
        input  → : 当前值（长路径截断, workload/device 友好显示）
        action → 原样返回
        """
        if item_type == "toggle":
            checked = self._get_toggle_state(level, item_id)
            mark = "[✓]" if checked else "[ ]"
            return f"{base_label}  {mark}"

        if item_type == "input":
            val = self._get_input_state(level, item_id)
            # workload / device 友好显示
            if item_id.endswith("_workload"):
                wl_map = {"1": "低(后台)", "2": "中低", "3": "高(默认)", "4": "极致"}
                val = wl_map.get(val, val)
            elif item_id.endswith("_device"):
                dev_map = {"auto": "自动", "gpu": "强制GPU", "cpu": "强制CPU"}
                val = dev_map.get(val, val)
            if not val:
                val_display = "—"
            elif len(val) > 30:
                # 长路径截断: 保留文件名部分
                from pathlib import Path as _P
                val_display = "…" + _P(val).name if _P(val).name else val[:30] + "…"
            else:
                val_display = val
            return f"{base_label} : {val_display}"

        return base_label

    def _get_toggle_state(self, level: str, item_id: str) -> bool:
        """获取指定页面 toggle 项的勾选状态"""
        if level == "dict_classic":
            return self._dict_toggles.get(item_id, False)
        elif level in _CRACK_MODE_PAGES:
            state = self._crack_state(level)
            return state["toggles"].get(item_id, False)
        return False

    def _get_input_state(self, level: str, item_id: str) -> str:
        """获取指定页面 input 项的当前值"""
        if level == "dict_classic":
            return self._dict_inputs.get(item_id, "")
        elif level == "dict_social":
            return self._dict_social_inputs.get(item_id, "")
        elif level == "dict_mask":
            return self._dict_mask_inputs.get(item_id, "")
        elif level == "crack_dict":
            return self._crack_dict_inputs.get(item_id, "")
        elif level in _CRACK_MODE_PAGES:
            state = self._crack_state(level)
            return state["inputs"].get(item_id, "")
        return ""

    def _on_left_select(self, row):
        """左侧菜单项选中变化"""
        self._sub_index = row
        # 主菜单切换时重置进入状态
        if self._current_level == "main":
            self._menu_index = row
            self._menu_entered = False

    def _on_left_double_click(self, item):
        """左侧菜单双击 = 进入/执行"""
        self._activate_current()

    def _activate_current(self):
        """执行当前选中项的操作（相当于 TUI 的回车）"""
        level = self._current_level
        row = self._left_list.currentRow()
        if row < 0:
            return
        self._sub_index = row

        if level == "main":
            self._handle_main_menu(row)
        elif level == "crack":
            self._handle_crack_menu(row)
        elif level == "dict":
            self._handle_dict_menu(row)
        elif level == "tools":
            self._handle_tools_action(row)
        elif level in ("dict_classic", "dict_social", "dict_mask",
                        "crack_dict", "crack_mask", "crack_rule", "crack_brute"):
            self._handle_sub_page_action(level, row)

    def _handle_main_menu(self, row):
        """主菜单项处理"""
        menu_id = _MENU_ITEMS[row][0]
        if menu_id == "menu_crack":
            self._navigate_to("crack")
        elif menu_id == "menu_dict":
            self._navigate_to("dict")
        elif menu_id == "menu_tools":
            self._navigate_to("tools")
        elif menu_id == "menu_help":
            dlg = HelpDialog(_HELP_MAIN, "帮助说明", self)
            dlg.exec()
        elif menu_id == "menu_about":
            dlg = HelpDialog(_HELP_ABOUT, "软件说明", self)
            dlg.exec()
        elif menu_id == "menu_quit":
            self.close()

    def _handle_crack_menu(self, row):
        """密码破解二级菜单处理"""
        item_id = _CRACK_MENU_ITEMS[row][0]
        if item_id == "crack_help":
            dlg = HelpDialog(_HELP_CRACK, "密码破解帮助", self)
            dlg.exec()
        elif item_id == "crack_back":
            self._navigate_to("main")
        else:
            self._navigate_to(item_id)

    def _handle_dict_menu(self, row):
        """字典生成二级菜单处理"""
        item_id = _DICT_MENU_ITEMS[row][0]
        if item_id == "dict_help":
            dlg = HelpDialog(_HELP_DICT, "字典生成帮助", self)
            dlg.exec()
        elif item_id == "dict_back":
            self._navigate_to("main")
        elif item_id == "dict_other":
            dlg = InfoDialog("其他字典生成功能开发中…", "提示", self)
            dlg.exec()
        else:
            self._navigate_to(item_id)

    def _handle_tools_action(self, row):
        """工具自检子页面操作"""
        item_id = _TOOLS_SUB_ITEMS[row][0]
        if item_id == "sub_recheck":
            self._tools_check_cache = None
            self._render_content()
            checks = self._run_tool_check()
            passed = [name for name, path in checks if path]
            failed = [name for name, path in checks if not path]
            total = len(checks)
            msg_lines = [f"检测完成（{len(passed)}/{total} 通过）", ""]
            if failed:
                msg_lines.append("以下工具未检测到：")
                for name in failed:
                    msg_lines.append(f"  FAIL  {name}")
                msg_lines.append("")
                msg_lines.append("请使用「下载工具」补齐。")
            else:
                msg_lines.append("所有工具检测通过。")
            dlg = InfoDialog("\n".join(msg_lines), "工具检测结果", self)
            dlg.exec()
        elif item_id == "sub_download":
            dlg = InfoDialog("下载工具功能待命，暂未启用。", "提示", self)
            dlg.exec()
        elif item_id == "sub_back":
            self._navigate_to("main")

    def _quick_jump(self, target_menu_id: str):
        """Ctrl+1~4 快速跳转"""
        for i, (mid, _) in enumerate(_MENU_ITEMS):
            if mid == target_menu_id:
                self._navigate_to("main")
                self._left_list.setCurrentRow(i)
                if target_menu_id in ("menu_crack", "menu_dict", "menu_tools"):
                    self._activate_current()
                elif target_menu_id == "menu_help":
                    dlg = HelpDialog(_HELP_MAIN, "帮助说明", self)
                    dlg.exec()
                return

    def _go_back(self):
        """返回上一层"""
        level = self._current_level
        if level in ("dict_classic", "dict_social", "dict_mask"):
            self._navigate_to("dict")
        elif level in ("crack_dict", "crack_mask", "crack_rule", "crack_brute"):
            self._navigate_to("crack")
        elif level in ("crack", "dict", "tools"):
            self._navigate_to("main")
        else:
            self._navigate_to("main")

    def eventFilter(self, obj, event):
        """事件过滤器：拦截左侧列表的 WASD/空格按键。

        QListWidget 获得焦点时，字母键会触发内置键盘搜索（按字母跳转），
        空格会切换选中项——这些事件被吃掉，不会传到主窗口 keyPressEvent。
        这里提前拦截，将这些键转发给主窗口处理，保证 WASD/空格始终可用。
        方向键 ↑/↓ 不拦截，保留 QListWidget 原生导航。
        """
        if obj is self._left_list and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            # WASD / 空格：拦截后交给主窗口 keyPressEvent 处理
            if key in (Qt.Key.Key_W, Qt.Key.Key_S, Qt.Key.Key_A, Qt.Key.Key_D,
                       Qt.Key.Key_Space):
                self.keyPressEvent(event)
                return True  # 事件已处理，阻止 QListWidget 吃掉
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        """全局键盘事件兜底：还原 TUI 的 WASD/空格/JK/PageUp 等快捷键。

        优先级：
          1. 焦点在可输入控件（QLineEdit/QTextEdit 等）时不拦截，交给控件编辑。
          2. Ctrl+Q / Ctrl+1~4 等由 QShortcut 处理，这里只补 WASD/空格/JK/滚动。
          3. 空格 = 回车（确认）；A/D = 返回/确认；W/S = 上下切换；J/K = 右侧滚动。
          4. PageUp/PageDown/Home/End = 右侧滚动区翻页/定位。
        """
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QLineEdit, QTextEdit, QPlainTextEdit
        from PyQt6.QtCore import Qt

        key = event.key()
        # 焦点落在可编辑控件上时，不拦截任何按键（让输入正常进行）
        fw = self.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
            super().keyPressEvent(event)
            return

        # 全局导航：W/S 映射上下，A/D 映射返回/确认，空格映射确认
        # 方向键上/下也在主窗口兜底处理，保证焦点不在列表时也能切换
        if key in (Qt.Key.Key_W, Qt.Key.Key_S, Qt.Key.Key_Up, Qt.Key.Key_Down):
            # 切换左侧选中项
            count = self._left_list.count()
            if count > 0:
                row = self._left_list.currentRow()
                is_up = key in (Qt.Key.Key_W, Qt.Key.Key_Up)
                new_row = (row - 1) % count if (is_up and row >= 0) else (row + 1) % count if row >= 0 else 0
                self._left_list.setCurrentRow(new_row)
            event.accept()
            return
        if key in (Qt.Key.Key_A, Qt.Key.Key_D):
            # A = 返回上一层，D = 确认（与 TUI 一致）
            if key == Qt.Key.Key_A:
                self._go_back()
            else:
                self._activate_current()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            # 空格 = 回车 = 确认
            self._activate_current()
            event.accept()
            return
        if key in (Qt.Key.Key_J, Qt.Key.Key_K):
            # J = 右侧下滚，K = 右侧上滚
            sb = self._content_area.verticalScrollBar()
            if sb is not None:
                step = sb.pageStep()
                if key == Qt.Key.Key_J:
                    sb.setValue(sb.value() + step)
                else:
                    sb.setValue(sb.value() - step)
            event.accept()
            return

        # PageUp/PageDown/Home/End：右侧内容滚动
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp,
                   Qt.Key.Key_Home, Qt.Key.Key_End):
            sb = self._content_area.verticalScrollBar()
            if sb is not None:
                if key == Qt.Key.Key_PageDown:
                    sb.setValue(sb.value() + sb.pageStep())
                elif key == Qt.Key.Key_PageUp:
                    sb.setValue(sb.value() - sb.pageStep())
                elif key == Qt.Key.Key_Home:
                    sb.setValue(sb.minimum())
                else:  # End
                    sb.setValue(sb.maximum())
            event.accept()
            return

        # 其余按键交给默认处理（方向键、回车、ESC 等由 QShortcut/默认行为处理）
        super().keyPressEvent(event)

    # ==================================================================
    # 右侧内容区渲染
    # ==================================================================

    def _render_content(self):
        """根据当前层级渲染右侧内容区"""
        # 清空旧内容（保留 layout 对象，只清 widget）
        layout = self._content_widget.layout()
        if layout is None:
            layout = QVBoxLayout(self._content_widget)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)
        else:
            self._clear_layout(layout)

        level = self._current_level

        if level == "main":
            self._render_main_content(layout)
        elif level == "crack":
            self._render_crack_menu_content(layout)
        elif level == "dict":
            self._render_dict_menu_content(layout)
        elif level == "tools":
            self._render_tools_content(layout)
        elif level == "dict_classic":
            self._render_dict_classic_content(layout)
        elif level == "dict_social":
            self._render_dict_social_content(layout)
        elif level == "dict_mask":
            self._render_dict_mask_content(layout)
        elif level == "crack_dict":
            self._render_crack_dict_content(layout)
        elif level in _CRACK_MODE_PAGES:
            self._render_crack_mode_content(layout, level)

        layout.addStretch()

    def _clear_layout(self, layout):
        """递归清空 layout 中所有 widget"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            else:
                child_layout = item.layout()
                if child_layout is not None:
                    self._clear_layout(child_layout)

    def _make_section_label(self, text, color=C_NS_CYAN):
        """创建分区标题 Label"""
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {color}; font-size: 14px; font-weight: bold; "
            f"padding: 4px 0; border-bottom: 1px solid {C_BORDER};")
        return label

    def _make_kv_label(self, key, value, key_color=C_NS_GRAY, val_color=C_NS_WHITE):
        """创建键值对 Label"""
        label = QLabel(
            f'<span style="color:{key_color};">{key}: </span>'
            f'<span style="color:{val_color};">{value}</span>')
        label.setStyleSheet("background: transparent; padding: 2px 8px;")
        label.setWordWrap(True)
        return label

    def _make_card(self, title, items, color_border=C_NS_GREEN, color_section=C_NS_CYAN):
        """创建 nushell 风格卡片（QGroupBox + kv/raw 行）
        items: [(type, *args)]
            ("kv", "key", "value")
            ("raw", "text")
            ("section", "title")
            ("blank")
        """
        group = QGroupBox(title)
        group.setStyleSheet(f"""
            QGroupBox {{
                color: {color_border};
                border: 1px solid {color_border};
                border-radius: 3px;
                margin-top: 8px;
                padding-top: 14px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
        """)
        vlayout = QVBoxLayout(group)
        vlayout.setContentsMargins(12, 16, 12, 8)
        vlayout.setSpacing(2)

        for item in items:
            kind = item[0]
            if kind == "section":
                sec = QLabel(item[1])
                sec.setStyleSheet(
                    f"color: {color_section}; font-weight: bold; "
                    f"padding-top: 6px;")
                vlayout.addWidget(sec)
            elif kind == "kv":
                vlayout.addWidget(self._make_kv_label(item[1], item[2]))
            elif kind == "raw":
                raw_label = QLabel(item[1])
                raw_label.setStyleSheet(
                    f"color: {C_NS_WHITE}; padding: 1px 8px;")
                raw_label.setWordWrap(True)
                vlayout.addWidget(raw_label)
            elif kind == "blank":
                vlayout.addSpacing(6)

        return group

    # ---- 主菜单内容 ----
    def _render_main_content(self, layout):
        """主菜单默认右侧：设备信息"""
        layout.addWidget(self._make_section_label("设备信息", C_NS_GREEN))
        try:
            report = self._get_hw_report()
            # HTML <table> 渲染：右侧边框由 Qt 排版保证，任何系统/字体都对齐
            html = _hw_report_to_html(report)
            label = QLabel(html)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setStyleSheet(
                f"color: {C_NS_WHITE}; font-family: Consolas; "
                f"font-size: 12px; padding: 8px; background-color: {C_BG_DARK};")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        except Exception as exc:
            err_label = QLabel(f"设备信息采集失败: {type(exc).__name__}: {exc}")
            err_label.setStyleSheet(f"color: {C_NS_RED};")
            layout.addWidget(err_label)

    def _get_hw_report(self):
        """获取硬件报告（带缓存）"""
        if self._hw_report_cache is None:
            self._hw_report_cache = collect_hardware_report()
        return self._hw_report_cache

    # ---- 密码破解二级菜单 ----
    def _render_crack_menu_content(self, layout):
        """密码破解二级菜单右侧说明页"""
        layout.addWidget(self._make_section_label("攻击模式选择", C_NS_GREEN))
        layout.addWidget(self._make_card("字典攻击", [
            ("kv", "原理", "用字典文件逐行试密码"),
            ("kv", "速度", "最快(字典质量决定成败)"),
            ("kv", "适用", "有社工字典或常见弱口令"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("掩码攻击", [
            ("kv", "原理", "按位置规则穷举(?d?l?u)"),
            ("kv", "速度", "GPU飞速,位数长则爆炸"),
            ("kv", "适用", "已知密码结构(如4位数字)"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("字典加规则", [
            ("kv", "原理", "字典基础上做变体(大小写/加数字)"),
            ("kv", "速度", "比纯字典慢,覆盖面广"),
            ("kv", "适用", "字典不够用时榨干每个词"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("暴力穷举", [
            ("kv", "原理", "无脑全试,所有字符所有长度"),
            ("kv", "速度", "最慢,6位以上基本绝望"),
            ("kv", "适用", "无任何线索,兜底方案"),
        ], C_NS_GREEN, C_NS_CYAN))
        hint = QLabel("← 左侧选择攻击模式，双击进入")
        hint.setStyleSheet(f"color: {C_NS_GRAY}; padding: 8px;")
        layout.addWidget(hint)

    # ---- 字典生成二级菜单 ----
    def _render_dict_menu_content(self, layout):
        """字典生成二级菜单右侧说明页"""
        layout.addWidget(self._make_section_label("字典模式选择", C_NS_GREEN))
        layout.addWidget(self._make_card("1. 经典字典生成", [
            ("kv", "原理", "基于字符集组合生成密码字典"),
            ("kv", "支持", "小写/大写/数字/特殊字符勾选"),
            ("kv", "特性", "可设长度范围、生成数量、单字符密码"),
            ("kv", "状态", "可用"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("2. 社工字典生成", [
            ("kv", "原理", "基于目标个人信息组合生成密码"),
            ("kv", "支持", "姓名/生日/手机号/QQ/微信号等组合"),
            ("kv", "状态", "可用"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("3. 掩码字典生成", [
            ("kv", "原理", "按掩码占位符生成密码"),
            ("kv", "支持", "?l ?u ?d ?s ?a 占位符+字面混合"),
            ("kv", "示例", "?d?d?d?d / pass?d?d / ?l?l?d?d"),
            ("kv", "状态", "可用"),
        ], C_NS_GREEN, C_NS_CYAN))
        layout.addWidget(self._make_card("4. 其他字典生成", [
            ("kv", "原理", "其他生成策略"),
            ("kv", "状态", "开发中"),
        ], C_NS_GREEN, C_NS_CYAN))
        hint = QLabel("← 左侧选择字典模式，双击进入")
        hint.setStyleSheet(f"color: {C_NS_GRAY}; padding: 8px;")
        layout.addWidget(hint)

    # ---- 工具自检 ----
    def _render_tools_content(self, layout):
        """工具自检子页面右侧内容"""
        layout.addWidget(self._make_section_label("工具自检", C_NS_GREEN))
        hint = QLabel("左侧选择操作：重新检测 / 下载工具 / 返回上一层")
        hint.setStyleSheet(f"color: {C_NS_GRAY}; padding: 4px;")
        layout.addWidget(hint)

        checks = self._run_tool_check()
        layout.addWidget(self._make_tool_check_table(checks))

    def _run_tool_check(self) -> list:
        """执行工具路径检测（带缓存）"""
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

    def _make_tool_check_table(self, checks):
        """构建工具检测表格（用 QGridLayout 模拟）"""
        group = QGroupBox("检测结果")
        group.setStyleSheet(f"""
            QGroupBox {{
                color: {C_NS_BLUE};
                border: 1px solid {C_BORDER};
                border-radius: 3px;
                margin-top: 8px;
                padding-top: 14px;
            }}
        """)
        grid = QGridLayout(group)
        grid.setContentsMargins(12, 18, 12, 8)
        grid.setSpacing(4)

        # 表头
        headers = ["#", "工具名称", "状态", "路径"]
        for col, hdr in enumerate(headers):
            lbl = QLabel(hdr)
            lbl.setStyleSheet(
                f"color: {C_NS_GREEN}; font-weight: bold; padding: 4px; "
                f"border-bottom: 1px solid {C_BORDER};")
            grid.addWidget(lbl, 0, col)

        # 数据行
        for idx, (name, path) in enumerate(checks, 1):
            num_label = QLabel(str(idx))
            num_label.setStyleSheet(f"color: {C_NS_PURPLE}; padding: 4px;")
            grid.addWidget(num_label, idx, 0)

            name_label = QLabel(name)
            name_label.setStyleSheet(f"color: {C_NS_WHITE}; padding: 4px;")
            grid.addWidget(name_label, idx, 1)

            if path:
                status_label = QLabel("OK")
                status_label.setStyleSheet(
                    f"color: {C_NS_GREEN}; font-weight: bold; padding: 4px;")
                path_label = QLabel(str(path))
                path_label.setStyleSheet(f"color: {C_NS_CYAN}; padding: 4px;")
                path_label.setWordWrap(False)
            else:
                status_label = QLabel("FAIL")
                status_label.setStyleSheet(
                    f"color: {C_NS_RED}; font-weight: bold; padding: 4px;")
                path_label = QLabel("未找到")
                path_label.setStyleSheet(f"color: {C_NS_RED}; padding: 4px;")
            grid.addWidget(status_label, idx, 2)
            grid.addWidget(path_label, idx, 3)

        grid.setColumnStretch(3, 1)
        return group

    # ==================================================================
    # 经典字典生成子页面
    # ==================================================================

    def _render_dict_classic_content(self, layout):
        """经典字典生成子页面右侧内容 + 左侧菜单构建"""
        # 右侧配置信息卡片
        layout.addWidget(self._make_section_label("经典字典生成", C_NS_GREEN))

        # 收集当前配置
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
            enabled_sets.append("单字符(1位)")

        charset_str = " ".join(enabled_sets) if enabled_sets else "未选择"
        min_len = self._dict_inputs.get("dict_min_len", "4")
        max_len = self._dict_inputs.get("dict_max_len", "6")
        out_dir = self._dict_inputs.get("dict_out_dir", "")
        max_lines = self._dict_inputs.get("dict_max_lines", "0")

        # 预估数量
        est_count = "—"
        est_size = "—"
        if enabled_sets and not self._dict_toggles.get("dict_single"):
            charset_size = 0
            if self._dict_toggles.get("dict_lower"):
                charset_size += 26
            if self._dict_toggles.get("dict_upper"):
                charset_size += 26
            if self._dict_toggles.get("dict_digit"):
                charset_size += 10
            if self._dict_toggles.get("dict_special"):
                charset_size += 33
            try:
                min_l = int(min_len) if min_len else 4
                max_l = int(max_len) if max_len else 6
                total = sum(charset_size ** l for l in range(min_l, max_l + 1))
                est_count = f"{total:,}"
                est_size = _fmt_bytes(total * 2)  # 每行约2字节(字符+换行)
            except (ValueError, OverflowError):
                pass
        elif self._dict_toggles.get("dict_single"):
            # 单字符密码:数量=字符集大小
            cs = 0
            if self._dict_toggles.get("dict_lower"):
                cs += 26
            if self._dict_toggles.get("dict_upper"):
                cs += 26
            if self._dict_toggles.get("dict_digit"):
                cs += 10
            if self._dict_toggles.get("dict_special"):
                cs += 33
            est_count = str(cs) if cs > 0 else "—"
            est_size = "极小"

        # 磁盘剩余
        disk_free = _fmt_bytes(_disk_free_bytes(out_dir)) if out_dir else "—"

        config_items = [
            ("kv", "字符集", charset_str),
            ("kv", "最小长度", min_len),
            ("kv", "最大长度", max_len),
            ("kv", "输出目录", out_dir or "未设置"),
            ("kv", "生成数量", f"{max_lines} (0=全部)" if max_lines else "全部"),
            ("blank",),
            ("section", "预估"),
            ("kv", "预估数量", est_count),
            ("kv", "预估大小", est_size),
            ("kv", "磁盘剩余", disk_free),
        ]
        layout.addWidget(self._make_card("当前配置", config_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 历史记录
        if self._dict_history:
            history_items = [("section", f"生成历史(共 {len(self._dict_history)} 次)")]
            for record in self._dict_history:
                ts, out_file, result_text, extra = (
                    record[0], record[1], record[2],
                    record[3] if len(record) > 3 else {})
                history_items.append(("raw", f"  {ts}  {result_text}"))
                for k, v_tuple in extra.items():
                    v_text = v_tuple[0] if isinstance(v_tuple, tuple) else str(v_tuple)
                    history_items.append(("kv", f"  {k}", v_text))
                history_items.append(("blank",))
            layout.addWidget(self._make_card("历史记录", history_items,
                                             C_NS_BLUE, C_NS_CYAN))

    # ==================================================================
    # 社工字典生成子页面
    # ==================================================================

    def _render_dict_social_content(self, layout):
        """社工字典生成子页面右侧内容"""
        layout.addWidget(self._make_section_label("社工字典生成", C_NS_GREEN))

        filled_count = sum(1 for v in self._dict_social_inputs.values() if v.strip())
        out_dir = self._dict_social_inputs.get("soc_out_dir", "")

        config_items = [
            ("kv", "已填字段", f"{filled_count} / {len(_DICT_SOCIAL_ITEMS) - 3}"),
            ("kv", "输出目录", out_dir or "未设置"),
            ("blank",),
            ("section", "说明"),
            ("raw", "在左侧填写个人信息字段"),
            ("raw", "信息越全,命中率越高"),
            ("raw", "字段可留空,不影响生成"),
            ("raw", "自动去重,不会重复输出"),
        ]
        layout.addWidget(self._make_card("当前配置", config_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 历史记录
        if self._dict_social_history:
            history_items = [("section", f"生成历史(共 {len(self._dict_social_history)} 次)")]
            for record in self._dict_social_history:
                ts = record[0]
                result_text = record[2]
                history_items.append(("raw", f"  {ts}  {result_text}"))
                if len(record) > 3:
                    for k, v_tuple in record[3].items():
                        v_text = v_tuple[0] if isinstance(v_tuple, tuple) else str(v_tuple)
                        history_items.append(("kv", f"  {k}", v_text))
                history_items.append(("blank",))
            layout.addWidget(self._make_card("历史记录", history_items,
                                             C_NS_BLUE, C_NS_CYAN))

    # ==================================================================
    # 掩码字典生成子页面
    # ==================================================================

    def _render_dict_mask_content(self, layout):
        """掩码字典生成子页面右侧内容"""
        layout.addWidget(self._make_section_label("掩码字典生成", C_NS_GREEN))

        mask = self._dict_mask_inputs.get("mask_input", "?d?d?d?d")
        max_lines = self._dict_mask_inputs.get("mask_max_lines", "0")
        out_dir = self._dict_mask_inputs.get("mask_out_dir", "")

        # 预估数量
        est_count = "—"
        mask_map = {"?l": 26, "?u": 26, "?d": 10, "?s": 33,
                    "?a": 95, "?b": 256}
        try:
            import re as _re
            tokens = _re.findall(r'\?[ludsa1b]', mask)
            total = 1
            for t in tokens:
                total *= mask_map.get(t, 1)
            est_count = f"{total:,}"
        except Exception:
            pass

        config_items = [
            ("kv", "掩码", mask or "未设置"),
            ("kv", "生成数量", f"{max_lines} (0=全部)" if max_lines else "全部"),
            ("kv", "输出目录", out_dir or "未设置"),
            ("kv", "预估数量", est_count),
            ("blank",),
            ("section", "占位符"),
            ("kv", "?l", "小写字母 a-z (26个)"),
            ("kv", "?u", "大写字母 A-Z (26个)"),
            ("kv", "?d", "数字 0-9 (10个)"),
            ("kv", "?s", "特殊字符 (33个)"),
            ("kv", "?a", "全部可打印字符 (95个)"),
        ]
        layout.addWidget(self._make_card("当前配置", config_items,
                                         C_NS_GREEN, C_NS_CYAN))

        if self._dict_mask_history:
            history_items = [("section", f"生成历史(共 {len(self._dict_mask_history)} 次)")]
            for record in self._dict_mask_history:
                ts = record[0]
                result_text = record[2]
                history_items.append(("raw", f"  {ts}  {result_text}"))
                if len(record) > 3:
                    for k, v_tuple in record[3].items():
                        v_text = v_tuple[0] if isinstance(v_tuple, tuple) else str(v_tuple)
                        history_items.append(("kv", f"  {k}", v_text))
                history_items.append(("blank",))
            layout.addWidget(self._make_card("历史记录", history_items,
                                             C_NS_BLUE, C_NS_CYAN))

    # ==================================================================
    # 字典攻击子页面
    # ==================================================================

    def _render_crack_dict_content(self, layout):
        """字典攻击子页面右侧内容"""
        layout.addWidget(self._make_section_label("字典攻击", C_NS_GREEN))

        archive = self._crack_dict_inputs.get("crack_dict_archive", "")
        dict_val = self._crack_dict_inputs.get("crack_dict_dict", "")
        workload = self._crack_dict_inputs.get("crack_dict_workload", "3")
        device = self._crack_dict_inputs.get("crack_dict_device", "auto")

        workload_map = {"1": "1=低(后台)", "2": "2=中低", "3": "3=高(默认)", "4": "4=极致"}
        workload_str = workload_map.get(workload, f"{workload}(自定义)")

        device_map = {"auto": "自动", "gpu": "强制GPU", "cpu": "强制CPU"}
        device_str = device_map.get(device.lower(), device)

        hashcat_ok = self._cracker.is_available()

        config_items = [
            ("kv", "压缩包", Path(archive).name if archive else "未选择"),
            ("kv", "压缩包路径", archive or "未选择"),
            ("kv", "字典文件", dict_val or "未选择"),
            ("kv", "工作负载", workload_str),
            ("kv", "设备", device_str),
        ]
        layout.addWidget(self._make_card("当前配置", config_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 实时进度
        if self._crack_dict_running and self._crack_dict_live:
            live_items = [("section", "实时进度")]
            live = self._crack_dict_live
            if live.get("status_text"):
                live_items.append(("kv", "状态", live["status_text"]))
            if live.get("speed"):
                live_items.append(("kv", "速度", live["speed"]))
            if live.get("progress_abs"):
                live_items.append(("kv", "已试/总数", live["progress_abs"]))
            if live.get("percent") is not None:
                live_items.append(("kv", "百分比", f"{live['percent']:.1f}%"))
            if live.get("candidates"):
                live_items.append(("kv", "当前候选", live["candidates"]))
            if live.get("recovered_pwd"):
                live_items.append(("kv", "已破解密码", live["recovered_pwd"]))
            if live.get("elapsed") is not None:
                live_items.append(("kv", "耗时", f"{live['elapsed']:.1f} 秒"))
            layout.addWidget(self._make_card("实时进度", live_items,
                                             C_NS_YELLOW, C_NS_CYAN))

        # 环境状态
        env_items = [
            ("kv", "Hashcat", "可用" if hashcat_ok else "未找到"),
        ]
        layout.addWidget(self._make_card("环境状态", env_items,
                                         C_NS_BLUE, C_NS_CYAN))

        # 配置检查
        check_items = []
        if not archive.strip():
            check_items.append(("raw", "✗ 未选择压缩包"))
        elif not Path(archive.strip()).exists():
            check_items.append(("raw", "✗ 压缩包不存在"))
        else:
            check_items.append(("raw", "✓ 压缩包已选择"))
        if not dict_val.strip():
            check_items.append(("raw", "✗ 未选择字典"))
        else:
            check_items.append(("raw", "✓ 字典已选择"))
        if not hashcat_ok:
            check_items.append(("raw", "✗ Hashcat不可用"))
        layout.addWidget(self._make_card("配置检查", check_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 历史
        if self._crack_dict_history:
            history_items = [("section", f"破解历史(共 {len(self._crack_dict_history)} 次)")]
            for record in self._crack_dict_history:
                ts = record[0]
                result_text = record[2]
                history_items.append(("raw", f"  {ts}  {result_text}"))
                if len(record) > 3:
                    for k, v_tuple in record[3].items():
                        v_text = v_tuple[0] if isinstance(v_tuple, tuple) else str(v_tuple)
                        history_items.append(("kv", f"  {k}", v_text))
                history_items.append(("blank",))
            layout.addWidget(self._make_card("历史记录", history_items,
                                             C_NS_BLUE, C_NS_CYAN))

    # ==================================================================
    # 通用破解模式子页面（crack_mask / crack_rule / crack_brute）
    # ==================================================================

    def _render_crack_mode_content(self, layout, page: str):
        """通用破解模式子页面右侧内容"""
        title_map = {
            "crack_mask": "掩码攻击",
            "crack_rule": "字典加规则",
            "crack_brute": "暴力穷举",
        }
        layout.addWidget(self._make_section_label(title_map.get(page, page), C_NS_GREEN))

        state = self._crack_mode_states[page]
        inputs = state["inputs"]
        prefix = f"{page}_"

        archive_val = inputs.get(prefix + "archive", "")
        workload_val = inputs.get(prefix + "workload", "3")
        workload_map = {"1": "1=低(后台)", "2": "2=中低", "3": "3=高(默认)", "4": "4=极致"}
        workload_str = workload_map.get(workload_val, f"{workload_val}(自定义)")
        device_val = inputs.get(prefix + "device", "auto")
        device_map = {"auto": "自动", "gpu": "强制GPU", "cpu": "强制CPU"}
        device_str = device_map.get(device_val.lower(), device_val)
        hashcat_ok = self._cracker.is_available()

        config_items = [
            ("kv", "压缩包", Path(archive_val).name if archive_val else "未选择"),
            ("kv", "压缩包路径", archive_val or "未选择"),
        ]

        if page == "crack_mask":
            config_items.append(("kv", "掩码", inputs.get("crack_mask_expr", "") or "未设置"))
        elif page == "crack_rule":
            config_items.append(("kv", "字典", inputs.get("crack_rule_dict", "") or "未选择"))
            rule_path = inputs.get("crack_rule_file", "")
            config_items.append(("kv", "规则", Path(rule_path).name if rule_path else "未选择"))
        else:
            toggles = state["toggles"]
            names = []
            if toggles.get("crack_brute_lower"):
                names.append("小写")
            if toggles.get("crack_brute_upper"):
                names.append("大写")
            if toggles.get("crack_brute_digit"):
                names.append("数字")
            if toggles.get("crack_brute_special"):
                names.append("特殊")
            if inputs.get("crack_brute_custom", "").strip():
                names.append("自定义")
            config_items.append(("kv", "字符集", " ".join(names) or "未选择"))
            config_items.append(("kv", "长度", f"{inputs.get('crack_brute_min_len','')}-{inputs.get('crack_brute_max_len','')}"))

        config_items.append(("kv", "工作负载", workload_str))
        config_items.append(("kv", "设备", device_str))
        layout.addWidget(self._make_card("当前配置", config_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 掩码说明
        if page == "crack_mask":
            mask_items = [
                ("kv", "?d", "数字 0-9"),
                ("kv", "?l", "小写字母 a-z"),
                ("kv", "?u", "大写字母 A-Z"),
                ("kv", "?s", "常见特殊字符"),
                ("kv", "?a", "所有可打印字符"),
                ("raw", "普通字符直接写,如 pass?d?d?d"),
                ("raw", "示例: ?d?d?d?d = 4位数字"),
            ]
            layout.addWidget(self._make_card("掩码说明", mask_items,
                                             C_NS_BLUE, C_NS_CYAN))

        # 规则说明
        if page == "crack_rule":
            rule_path = inputs.get("crack_rule_file", "")
            rule_name = Path(rule_path).name if rule_path else ""
            rule_desc = _RULE_DESCRIPTIONS.get(
                rule_name, "hashcat 内置规则，用于字典变形扩展。")
            rule_items = [
                ("kv", "当前规则", rule_name or "未选择"),
                ("raw", rule_desc),
                ("raw", "可在左侧「选择规则文件」查看全部规则说明"),
            ]
            layout.addWidget(self._make_card("规则说明", rule_items,
                                             C_NS_BLUE, C_NS_CYAN))

        # 实时进度
        if state["running"] and state["live"]:
            live = state["live"]
            live_items = [("section", "实时进度")]
            if live.get("status_text"):
                live_items.append(("kv", "状态", live["status_text"]))
            if live.get("speed"):
                live_items.append(("kv", "速度", live["speed"]))
            if live.get("progress_abs"):
                live_items.append(("kv", "已试/总数", live["progress_abs"]))
            if live.get("percent") is not None:
                live_items.append(("kv", "百分比", f"{live['percent']:.1f}%"))
            if live.get("candidates"):
                live_items.append(("kv", "当前候选", live["candidates"]))
            if live.get("recovered_pwd"):
                live_items.append(("kv", "已破解密码", live["recovered_pwd"]))
            if live.get("elapsed") is not None:
                live_items.append(("kv", "耗时", f"{live['elapsed']:.1f} 秒"))
            layout.addWidget(self._make_card("实时进度", live_items,
                                             C_NS_YELLOW, C_NS_CYAN))

        # 环境状态
        env_items = [("kv", "Hashcat", "可用" if hashcat_ok else "未找到")]
        layout.addWidget(self._make_card("环境状态", env_items,
                                         C_NS_BLUE, C_NS_CYAN))

        # 配置检查
        check_items = []
        if not archive_val.strip():
            check_items.append(("raw", "✗ 未选择压缩包"))
        elif not Path(archive_val.strip()).exists():
            check_items.append(("raw", "✗ 压缩包不存在"))
        else:
            check_items.append(("raw", "✓ 压缩包已选择"))
        if page == "crack_mask":
            if not inputs.get("crack_mask_expr", "").strip():
                check_items.append(("raw", "✗ 未设置掩码"))
            else:
                check_items.append(("raw", "✓ 掩码已设置"))
        elif page == "crack_rule":
            if not inputs.get("crack_rule_dict", "").strip():
                check_items.append(("raw", "✗ 未选择字典"))
            else:
                check_items.append(("raw", "✓ 字典已选择"))
            if not Path(inputs.get("crack_rule_file", "")).exists():
                check_items.append(("raw", "✗ 规则文件不存在"))
            else:
                check_items.append(("raw", "✓ 规则文件已选择"))
        else:
            toggles = state["toggles"]
            if not inputs.get("crack_brute_custom", "").strip() and not any(
                toggles.get(k) for k in (
                    "crack_brute_lower", "crack_brute_upper",
                    "crack_brute_digit", "crack_brute_special",
                )
            ):
                check_items.append(("raw", "✗ 未选择字符集"))
            else:
                check_items.append(("raw", "✓ 字符集已选择"))
        if not hashcat_ok:
            check_items.append(("raw", "✗ Hashcat不可用"))
        layout.addWidget(self._make_card("配置检查", check_items,
                                         C_NS_GREEN, C_NS_CYAN))

        # 历史
        if state["history"]:
            history_items = [("section", f"破解历史(共 {len(state['history'])} 次)")]
            for record in state["history"]:
                ts = record[0]
                result_text = record[2]
                history_items.append(("raw", f"  {ts}  {result_text}"))
                if len(record) > 3:
                    for k, v_tuple in record[3].items():
                        v_text = v_tuple[0] if isinstance(v_tuple, tuple) else str(v_tuple)
                        history_items.append(("kv", f"  {k}", v_text))
                history_items.append(("blank",))
            layout.addWidget(self._make_card("历史记录", history_items,
                                             C_NS_BLUE, C_NS_CYAN))

    # ==================================================================
    # 第五部分：子页面操作分发 + 字典/破解执行 + 拖入 + 底栏 + 入口
    # ==================================================================

    def _crack_state(self, page: str) -> dict:
        """获取通用破解模式页面状态（快捷方法）"""
        return self._crack_mode_states[page]

    # ----- 子页面操作分发总入口 -----

    def _handle_sub_page_action(self, page: str, row: int):
        """子页面操作分发总入口（双击/回车触发）"""
        items = self._get_sub_page_items(page)
        if row < 0 or row >= len(items):
            return
        item_id, item_type, _label = items[row]

        if page == "dict_classic":
            self._handle_dict_classic_action(item_id, item_type)
        elif page == "dict_social":
            self._handle_dict_social_action(item_id, item_type)
        elif page == "dict_mask":
            self._handle_dict_mask_action(item_id, item_type)
        elif page == "crack_dict":
            self._handle_crack_dict_action(item_id, item_type)
        elif page in _CRACK_MODE_PAGES:
            self._handle_crack_mode_action(page, item_id, item_type)

    # ----- 经典字典 -----

    def _handle_dict_classic_action(self, item_id: str, item_type: str):
        """经典字典操作分发"""
        if item_type == "toggle":
            self._dict_toggles[item_id] = not self._dict_toggles.get(item_id, False)
            self._build_left_menu()
            self._render_content()
        elif item_type == "input":
            if item_id in ("dict_out_dir",):
                self._pick_directory(item_id, self._dict_inputs)
            else:
                self._show_input_dialog(self._dict_inputs.get(item_id, ""), item_id,
                                        self._dict_inputs)
        elif item_id == "dict_gen":
            self._do_dict_classic_generate()
        elif item_id == "dict_help":
            dlg = HelpDialog(_HELP_CLASSIC, "经典字典帮助", self)
            dlg.exec()
        elif item_id == "dict_back":
            self._navigate_to("dict")

    # ----- 社工字典 -----

    def _handle_dict_social_action(self, item_id: str, item_type: str):
        """社工字典操作分发"""
        if item_type == "input":
            if item_id == "soc_out_dir":
                self._pick_directory(item_id, self._dict_social_inputs)
            else:
                self._show_input_dialog(self._dict_social_inputs.get(item_id, ""), item_id,
                                        self._dict_social_inputs)
        elif item_id == "soc_gen":
            self._do_dict_social_generate()
        elif item_id == "soc_help":
            dlg = HelpDialog(_HELP_SOCIAL, "社工字典帮助", self)
            dlg.exec()
        elif item_id == "soc_back":
            self._navigate_to("dict")

    # ----- 掩码字典 -----

    def _handle_dict_mask_action(self, item_id: str, item_type: str):
        """掩码字典操作分发"""
        if item_type == "input":
            if item_id == "mask_out_dir":
                self._pick_directory(item_id, self._dict_mask_inputs)
            else:
                self._show_input_dialog(self._dict_mask_inputs.get(item_id, ""), item_id,
                                        self._dict_mask_inputs)
        elif item_id == "mask_preset":
            current = self._dict_mask_inputs.get("mask_input", "")
            dlg = MaskPresetDialog(_MASK_PRESETS, current, self)
            if dlg.exec():
                selected = dlg.get_selected()
                if selected:
                    self._dict_mask_inputs["mask_input"] = selected
                    self._build_left_menu()
                    self._render_content()
        elif item_id == "mask_gen":
            self._do_dict_mask_generate()
        elif item_id == "mask_help":
            dlg = HelpDialog(_HELP_MASK, "掩码字典帮助", self)
            dlg.exec()
        elif item_id == "mask_back":
            self._navigate_to("dict")

    # ----- 字典攻击 -----

    def _handle_crack_dict_action(self, item_id: str, item_type: str):
        """字典攻击操作分发"""
        if item_type == "input":
            self._show_input_dialog(self._crack_dict_inputs.get(item_id, ""), item_id,
                                    self._crack_dict_inputs)
        elif item_id == "crack_dict_drop":
            dlg = InfoDialog("请将压缩包或字典文件拖入主窗口。\n系统会自动识别文件类型。", "拖入文件", self)
            dlg.exec()
        elif item_id == "crack_dict_run":
            if self._crack_dict_running:
                dlg = InfoDialog("已有破解任务正在运行，请等待结束。", "提示", self)
                dlg.exec()
            else:
                self._do_crack_dict_run()
        elif item_id == "crack_dict_help":
            dlg = HelpDialog(_HELP_CRACK_DICT, "字典攻击帮助", self)
            dlg.exec()
        elif item_id == "crack_dict_back":
            self._navigate_to("crack")

    # ----- 通用破解模式（mask / rule / brute） -----

    def _handle_crack_mode_action(self, page: str, item_id: str, item_type: str):
        """通用破解模式操作分发"""
        state = self._crack_state(page)

        if item_type == "toggle":
            state["toggles"][item_id] = not state["toggles"].get(item_id, False)
            self._build_left_menu()
            self._render_content()
        elif item_type == "input":
            self._show_input_dialog(state["inputs"].get(item_id, ""), item_id,
                                    state["inputs"])
        elif item_id == f"{page}_drop":
            dlg = InfoDialog("请将压缩包或字典文件拖入主窗口。\n系统会自动识别文件类型。", "拖入文件", self)
            dlg.exec()
        elif item_id == f"{page}_pick":
            self._open_rule_select_dialog()
        elif item_id == "crack_mask_preset":
            self._open_mask_preset_dialog()
        elif item_id == "crack_brute_preset":
            self._open_brute_preset_dialog()
        elif item_id == f"{page}_preset":
            self._cycle_crack_mode_preset(page)
            self._build_left_menu()
            self._render_content()
        elif item_id == f"{page}_run":
            if state["running"]:
                dlg = InfoDialog("已有破解任务正在运行，请等待结束。", "提示", self)
                dlg.exec()
            else:
                self._do_crack_mode_run(page)
                self._render_content()
        elif item_id == f"{page}_help":
            help_map = {
                "crack_mask": _HELP_CRACK_MASK,
                "crack_rule": _HELP_CRACK_RULE,
                "crack_brute": _HELP_CRACK_BRUTE,
            }
            title_map = {
                "crack_mask": "掩码攻击帮助",
                "crack_rule": "字典加规则帮助",
                "crack_brute": "暴力穷举帮助",
            }
            dlg = HelpDialog(help_map.get(page, []), title_map.get(page, "帮助"), self)
            dlg.exec()
        elif item_id == f"{page}_back":
            self._navigate_to("crack")

    # ----- 快速模板循环 -----

    def _cycle_crack_mode_preset(self, page: str):
        """循环切换模式预设（仅规则预设, 暴力穷举已改为弹窗）"""
        state = self._crack_state(page)
        if page == "crack_mask":
            state["mask_index"] = (state["mask_index"] + 1) % len(_MASK_PRESETS)
            preset = _MASK_PRESETS[state["mask_index"]]
            state["inputs"]["crack_mask_expr"] = preset[0]
        elif page == "crack_rule":
            state["rule_index"] = (state["rule_index"] + 1) % len(_RULE_PRESETS)
            _, rule_path = _RULE_PRESETS[state["rule_index"]]
            state["inputs"]["crack_rule_file"] = rule_path

    def _apply_brute_preset(self, state: dict, preset_index: int):
        """把暴力穷举模板应用到状态（字符集勾选+长度范围）"""
        _, toggles, min_len, max_len, _desc = _BRUTE_PRESETS[preset_index]
        state["inputs"]["crack_brute_min_len"] = str(min_len)
        state["inputs"]["crack_brute_max_len"] = str(max_len)
        state["inputs"]["crack_brute_custom"] = ""
        state["toggles"]["crack_brute_lower"] = toggles["lower"]
        state["toggles"]["crack_brute_upper"] = toggles["upper"]
        state["toggles"]["crack_brute_digit"] = toggles["digit"]
        state["toggles"]["crack_brute_special"] = toggles["special"]

    # ----- 规则文件目录扫描 + 弹窗 -----

    def _rule_file_list(self) -> list:
        """自动扫描规则目录，返回 [(文件名, 路径, 中文说明), ...]"""
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
        except Exception:
            pass
        if not out:
            for name, path in _RULE_PRESETS:
                out.append((
                    name,
                    path,
                    _RULE_DESCRIPTIONS.get(name, "hashcat 内置规则，用于字典变形扩展。"),
                ))
        return out

    def _open_rule_select_dialog(self):
        """打开规则文件选择弹窗，选中后写入 crack_rule 状态"""
        rules = self._rule_file_list()
        dlg = RuleSelectDialog(rules, self)
        if dlg.exec():
            selected = dlg.get_selected()
            if selected:
                state = self._crack_state("crack_rule")
                state["inputs"]["crack_rule_file"] = selected[1]
                self._build_left_menu()
                self._render_content()

    def _open_mask_preset_dialog(self):
        """打开掩码模板选择弹窗，选中后写入 crack_mask 状态"""
        state = self._crack_state("crack_mask")
        current = state["inputs"].get("crack_mask_expr", "")
        dlg = MaskPresetDialog(_MASK_PRESETS, current, self)
        if dlg.exec():
            selected = dlg.get_selected()
            if selected:
                state["inputs"]["crack_mask_expr"] = selected
                self._build_left_menu()
                self._render_content()

    def _open_brute_preset_dialog(self):
        """打开暴力穷举模板选择弹窗，选中后应用字符集勾选+长度范围"""
        state = self._crack_state("crack_brute")
        dlg = BrutePresetDialog(_BRUTE_PRESETS, self)
        if dlg.exec():
            idx = dlg.get_selected_index()
            if idx is not None:
                self._apply_brute_preset(state, idx)
                self._build_left_menu()
                self._render_content()

    # ----- 通用输入对话框 + 目录选择 -----

    def _show_input_dialog(self, current_val: str, item_id: str, inputs_dict: dict):
        """通用输入对话框：弹出小窗口编辑当前值，确认后写入 inputs_dict[item_id]
        工作负载(workload)使用专用 1-4 选项弹窗；
        设备(device)使用专用 auto/gpu/cpu 选项弹窗；其余走文本输入。
        """
        if item_id.endswith("_workload"):
            dlg = WorkloadDialog(current_val, self)
            if dlg.exec():
                inputs_dict[item_id] = dlg.get_selected()
                self._build_left_menu()
                self._render_content()
            return
        if item_id.endswith("_device"):
            dlg = DeviceDialog(current_val, self)
            if dlg.exec():
                inputs_dict[item_id] = dlg.get_selected()
                self._build_left_menu()
                self._render_content()
            return
        # 自定义字符集: 附带使用说明
        hint_map = {
            "crack_brute_custom": (
                "自定义字符集（可选）\n"
                "填入你指定的字符, 只使用这些字符范围内穷举。\n"
                "例: abc123 → 只试 a/b/c/1/2/3 的组合\n"
                "例: !@#$% → 只试这些特殊字符\n"
                "留空 = 不使用自定义字符集, 以上方勾选的字符集为准。\n"
                "与勾选项是「合并」关系: 勾了小写字母又填 abc, 实际用 a-z 加上你填的。"
            ),
        }
        hint = hint_map.get(item_id, "")
        dlg = _InputDialog(current_val, item_id, self, hint=hint)
        if dlg.exec():
            new_val = dlg.get_value()
            inputs_dict[item_id] = new_val
            self._build_left_menu()
            self._render_content()

    def _pick_directory(self, item_id: str, inputs_dict: dict):
        """打开目录选择对话框"""
        cur_dir = inputs_dict.get(item_id, "")
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", cur_dir or "")
        if d:
            inputs_dict[item_id] = d
            self._build_left_menu()
            self._render_content()

    # ----- 历史记录截断 -----

    def _trim_history(self, kind: str):
        """截断历史记录到上限，避免内存膨胀"""
        if kind == "classic":
            if len(self._dict_history) > self._dict_history_limit:
                del self._dict_history[self._dict_history_limit:]
        elif kind == "social":
            if len(self._dict_social_history) > self._dict_social_history_limit:
                del self._dict_social_history[self._dict_social_history_limit:]
        elif kind == "mask":
            if len(self._dict_mask_history) > self._dict_mask_history_limit:
                del self._dict_mask_history[self._dict_mask_history_limit:]
        elif kind == "crack_dict":
            if len(self._crack_dict_history) > self._crack_dict_history_limit:
                del self._crack_dict_history[self._crack_dict_history_limit:]
        elif kind.startswith("crack_mode_"):
            page = kind[len("crack_mode_"):]
            state = self._crack_state(page)
            limit = state.get("history_limit", 10)
            if len(state["history"]) > limit:
                del state["history"][limit:]

    # ----- 经典字典生成执行 -----

    def _do_dict_classic_generate(self):
        """执行经典字典生成（后台线程）"""
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 校验字符集
        if not any(self._dict_toggles.values()):
            self._dict_history.insert(0, (ts_now, None, "错误：至少勾选一个字符集"))
            self._trim_history("classic")
            self._render_content()
            return
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

        # 2. 解析长度
        try:
            min_l = int(self._dict_inputs.get("dict_min_len", "4"))
        except ValueError:
            self._dict_history.insert(0, (ts_now, None, "错误：最小长度必须是数字"))
            self._trim_history("classic")
            self._render_content()
            return
        try:
            max_l = int(self._dict_inputs.get("dict_max_len", "6"))
        except ValueError:
            self._dict_history.insert(0, (ts_now, None, "错误：最大长度必须是数字"))
            self._trim_history("classic")
            self._render_content()
            return

        if self._dict_toggles.get("dict_single"):
            min_l = 1
            max_l = 1
        if min_l <= 0 or max_l < min_l:
            self._dict_history.insert(0, (ts_now, None, f"错误：长度范围不合法（min={min_l}, max={max_l}）"))
            self._trim_history("classic")
            self._render_content()
            return

        # 3. 解析数量
        try:
            max_lines = int(self._dict_inputs.get("dict_max_lines", "0") or "0")
            if max_lines < 0:
                max_lines = 0
        except ValueError:
            self._dict_history.insert(0, (ts_now, None, "错误：生成数量必须是数字"))
            self._trim_history("classic")
            self._render_content()
            return

        # 4. 输出目录
        out_dir = self._dict_inputs.get("dict_out_dir", "")
        if not out_dir:
            self._dict_history.insert(0, (ts_now, None, "错误：输出目录不能为空"))
            self._trim_history("classic")
            self._render_content()
            return
        out_dir_path = Path(out_dir)
        try:
            out_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._dict_history.insert(0, (ts_now, None, f"错误：创建输出目录失败: {exc}"))
            self._trim_history("classic")
            self._render_content()
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir_path / f"dict_{ts}.txt"

        cfg = GenConfig(
            output_file=str(out_file),
            mode=GenMode.CHARSET_COMB,
            charset=charset,
            min_length=min_l,
            max_length=max_l,
            max_lines=max_lines,
        )

        # 5. 预估
        try:
            est_lines, est_bytes = self._dict_gen.estimate(cfg)
        except Exception as exc:
            self._dict_history.insert(0, (ts_now, None, f"错误：预估失败: {exc}"))
            self._trim_history("classic")
            self._render_content()
            return

        # 6. 磁盘空间检查
        free = _disk_free_bytes(out_dir)
        if free > 0 and est_bytes > free:
            dlg = InfoDialog(
                f"预估字典大小: {_fmt_bytes(est_bytes)}\n"
                f"磁盘剩余空间: {_fmt_bytes(free)}\n"
                f"存储空间不足，请减少字典生成数量\n"
                f"或更换余量更充足的盘符",
                "空间不足", self)
            dlg.exec()
            return

        # 7. 数量过多确认
        if est_lines > _DICT_LARGE_COUNT_THRESHOLD:
            msg = (
                f"预估生成数量: {est_lines:,} 行\n"
                f"预估大小: {_fmt_bytes(est_bytes)}\n"
                f"数量可能过多，确定要生成吗?"
            )
            dlg = ConfirmDialog(msg, "数量确认", self)
            if not dlg.exec() or not dlg.is_confirmed():
                self._render_content()
                return

        # 8. 启动后台线程
        self._dict_history.insert(0, (ts_now, None, "正在生成…"))
        self._trim_history("classic")
        self._render_content()

        worker = DictGenWorker(self._dict_gen, cfg, ts_now)
        worker.finished_signal.connect(self._on_dict_gen_finished)
        self._dict_worker = worker
        worker.start()

    def _on_dict_gen_finished(self, result, ts: str):
        """经典字典生成完成回调"""
        if result.success:
            result_text = "成功"
            extra = {
                "输出文件": result.output_file or "",
                "总行数": f"{result.total_lines:,}",
                "文件大小": _fmt_bytes(result.size_bytes),
                "耗时": f"{result.duration_seconds:.3f} 秒",
            }
            self._dict_history[0] = (ts, result.output_file, result_text, extra)
        else:
            self._dict_history[0] = (ts, None, "失败",
                                     {"错误信息": result.error_message or ""})
        self._trim_history("classic")
        self._render_content()

    # ----- 社工字典生成执行 -----

    def _do_dict_social_generate(self):
        """执行社工字典生成（后台线程）"""
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        out_dir = self._dict_social_inputs.get("soc_out_dir", "")
        if not out_dir:
            self._dict_social_history.insert(0, (ts_now, None, "输出目录不能为空"))
            self._trim_history("social")
            self._render_content()
            return
        out_dir_path = Path(out_dir)
        try:
            out_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._dict_social_history.insert(0, (ts_now, None, f"创建输出目录失败: {exc}"))
            self._trim_history("social")
            self._render_content()
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir_path / f"social_dict_{ts}.txt"

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

        self._dict_social_history.insert(0, (ts_now, None, "正在生成…"))
        self._trim_history("social")
        self._render_content()

        worker = SocialGenWorker(self._dict_gen, sc, ts_now)
        worker.finished_signal.connect(self._on_social_gen_finished)
        self._dict_worker = worker
        worker.start()

    def _on_social_gen_finished(self, result, ts: str):
        """社工字典生成完成回调"""
        if result.success:
            result_text = "成功"
            extra = {
                "输出文件": result.output_file or "",
                "总行数": f"{result.total_lines:,}",
                "文件大小": f"{result.size_bytes:,} 字节",
                "耗时": f"{result.duration_seconds:.3f} 秒",
            }
            self._dict_social_history[0] = (ts, result.output_file, result_text, extra)
        else:
            self._dict_social_history[0] = (ts, None, "失败",
                                            {"错误信息": result.error_message or ""})
        self._trim_history("social")
        self._render_content()

    # ----- 掩码字典生成执行 -----

    def _do_dict_mask_generate(self):
        """执行掩码字典生成（后台线程）"""
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        mask_val = self._dict_mask_inputs.get("mask_input", "").strip()
        if not mask_val:
            self._dict_mask_history.insert(0, (ts_now, None, "错误：掩码不能为空"))
            self._trim_history("mask")
            self._render_content()
            return

        try:
            max_lines = int(self._dict_mask_inputs.get("mask_max_lines", "0") or "0")
            if max_lines < 0:
                max_lines = 0
        except ValueError:
            self._dict_mask_history.insert(0, (ts_now, None, "错误：生成数量必须是数字"))
            self._trim_history("mask")
            self._render_content()
            return

        out_dir = self._dict_mask_inputs.get("mask_out_dir", "")
        if not out_dir:
            self._dict_mask_history.insert(0, (ts_now, None, "错误：输出目录不能为空"))
            self._trim_history("mask")
            self._render_content()
            return
        out_dir_path = Path(out_dir)
        try:
            out_dir_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._dict_mask_history.insert(0, (ts_now, None, f"错误：创建输出目录失败: {exc}"))
            self._trim_history("mask")
            self._render_content()
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = out_dir_path / f"mask_{ts}.txt"

        cfg = GenConfig(
            output_file=str(out_file),
            mode=GenMode.MASK,
            mask=mask_val,
            max_lines=max_lines,
        )

        # 预估（同时验证掩码合法性）
        try:
            est_lines, est_bytes = self._dict_gen.estimate(cfg)
        except Exception as exc:
            self._dict_mask_history.insert(0, (ts_now, None, f"错误：掩码解析失败: {exc}"))
            self._trim_history("mask")
            self._render_content()
            return
        if est_lines == 0:
            self._dict_mask_history.insert(0, (ts_now, None, "错误：掩码无效，请检查占位符"))
            self._trim_history("mask")
            self._render_content()
            return

        # 磁盘空间检查
        free = _disk_free_bytes(out_dir)
        if free > 0 and est_bytes > free:
            dlg = InfoDialog(
                f"预估字典大小: {_fmt_bytes(est_bytes)}\n"
                f"磁盘剩余空间: {_fmt_bytes(free)}\n"
                f"存储空间不足，请减少字典生成数量\n"
                f"或更换余量更充足的盘符",
                "空间不足", self)
            dlg.exec()
            return

        # 数量过多确认
        if est_lines > _DICT_LARGE_COUNT_THRESHOLD:
            msg = (
                f"预估生成数量: {est_lines:,} 行\n"
                f"预估大小: {_fmt_bytes(est_bytes)}\n"
                f"数量可能过多，确定要生成吗?"
            )
            dlg = ConfirmDialog(msg, "数量确认", self)
            if not dlg.exec() or not dlg.is_confirmed():
                self._render_content()
                return

        # 启动后台线程
        self._dict_mask_history.insert(0, (ts_now, None, "正在生成…"))
        self._trim_history("mask")
        self._render_content()

        worker = DictGenWorker(self._dict_gen, cfg, ts_now)
        worker.finished_signal.connect(self._on_mask_gen_finished)
        self._dict_worker = worker
        worker.start()

    def _on_mask_gen_finished(self, result, ts: str):
        """掩码字典生成完成回调"""
        if result.success:
            result_text = "成功"
            extra = {
                "输出文件": result.output_file or "",
                "总行数": f"{result.total_lines:,}",
                "文件大小": _fmt_bytes(result.size_bytes),
                "耗时": f"{result.duration_seconds:.3f} 秒",
            }
            self._dict_mask_history[0] = (ts, result.output_file, result_text, extra)
        else:
            self._dict_mask_history[0] = (ts, None, "失败",
                                          {"错误信息": result.error_message or ""})
        self._trim_history("mask")
        self._render_content()

    # ----- 字典攻击破解执行 -----

    def _do_crack_dict_run(self):
        """执行字典攻击（后台线程）"""
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 校验压缩包
        archive_val = self._crack_dict_inputs.get("crack_dict_archive", "").strip()
        if not archive_val:
            self._crack_dict_history.insert(0, (ts_now, None, "错误:未选择压缩包"))
            self._trim_history("crack_dict")
            self._render_content()
            return
        if not Path(archive_val).exists():
            self._crack_dict_history.insert(0, (ts_now, None, "错误:压缩包不存在"))
            self._trim_history("crack_dict")
            self._render_content()
            return

        # 2. 校验字典
        dict_val = self._crack_dict_inputs.get("crack_dict_dict", "").strip()
        if not dict_val:
            self._crack_dict_history.insert(0, (ts_now, None, "错误:未选择字典文件"))
            self._trim_history("crack_dict")
            self._render_content()
            return
        dict_paths = [d.strip() for d in dict_val.split(",") if d.strip()]
        missing = [d for d in dict_paths if not Path(d).exists()]
        if missing:
            self._crack_dict_history.insert(0, (ts_now, None, f"错误:字典不存在: {missing[0]}"))
            self._trim_history("crack_dict")
            self._render_content()
            return

        # 3. 工作负载
        try:
            workload = int(self._crack_dict_inputs.get("crack_dict_workload", "3"))
            if workload < 1 or workload > 4:
                workload = 3
        except ValueError:
            workload = 3

        # 4. 设备类型
        device_val = self._crack_dict_inputs.get("crack_dict_device", "auto").strip().lower()
        if device_val in ("gpu", "2", "force_gpu"):
            device_type = "force_gpu"
        elif device_val in ("cpu", "1", "force_cpu"):
            device_type = "force_cpu"
        else:
            device_type = "auto"

        # 5. Hashcat 可用性
        if not self._cracker.is_available():
            self._crack_dict_history.insert(0, (ts_now, None, "错误:Hashcat不可用,请检查bin目录"))
            self._trim_history("crack_dict")
            self._render_content()
            return

        # 6. 防重复
        if self._crack_dict_running:
            self._crack_dict_history.insert(0, (ts_now, None, "已有破解任务正在运行,请等待结束"))
            self._trim_history("crack_dict")
            self._render_content()
            return

        # 7. 构建 CrackConfig
        cfg = CrackConfig(
            hash_file_path="",
            hashcat_mode=0,
            attack_mode=AttackMode.DICT,
            dictionary_paths=dict_paths,
            work_load_profile=workload,
            device_type=device_type,
        )

        # 8. 占位历史 + 启动线程
        self._crack_dict_history.insert(0, (ts_now, None, "正在提取哈希..."))
        self._trim_history("crack_dict")
        self._crack_dict_running = True
        self._crack_dict_live = {"status_text": "初始化", "elapsed": 0.0}
        self._render_content()

        worker = CrackWorker(self._extractor, self._cracker, archive_val, cfg,
                             "crack_dict", ts_now)
        worker.progress_signal.connect(self._on_crack_dict_progress)
        worker.finished_signal.connect(self._on_crack_dict_finished)
        self._crack_dict_worker = worker
        worker.start()

    def _on_crack_dict_progress(self, live: dict):
        """字典攻击实时进度回调（主线程）"""
        self._crack_dict_live.update(live)

    def _on_crack_dict_finished(self, ts: str, success: bool,
                                crack_result, extract_result, error_msg: str,
                                elapsed: float):
        """字典攻击破解完成回调"""
        self._crack_dict_running = False
        self._crack_dict_live = {}
        self._crack_dict_worker = None
        self._cleanup_crack_files()

        archive_val = self._crack_dict_inputs.get("crack_dict_archive", "")
        dict_val = self._crack_dict_inputs.get("crack_dict_dict", "")
        dict_paths = [d.strip() for d in dict_val.split(",") if d.strip()]
        archive_name = Path(archive_val).name if archive_val else ""

        if success and crack_result and crack_result.recovered_passwords:
            passwords = crack_result.recovered_passwords
            _real_pwds = [
                v for k, v in passwords.items()
                if k.startswith("$") and v and not v.strip().startswith(" ")
            ]
            pwd = _real_pwds[0] if _real_pwds else ""
            result_text = "破解成功"
            extra = {
                "压缩包": archive_name,
                "密码": pwd,
                "类型": extract_result.archive_type.value if extract_result and extract_result.success else "未知",
                "字典数": str(len(dict_paths)),
                "耗时": f"{elapsed:.2f} 秒",
            }
        else:
            status = crack_result.status if crack_result else None
            if status and status.value == "exhausted":
                status_text = "字典试完未命中"
            elif status and status.value == "stopped":
                status_text = "已停止"
            elif status and status.value == "error":
                status_text = "执行异常"
            elif not extract_result or not extract_result.success:
                status_text = "哈希提取失败"
            else:
                status_text = "失败"
            result_text = status_text
            extra = {
                "压缩包": archive_name,
                "类型": extract_result.archive_type.value if extract_result and extract_result.success else "未知",
                "字典数": str(len(dict_paths)),
                "耗时": f"{elapsed:.2f} 秒",
            }
            if error_msg:
                extra["错误信息"] = error_msg

        self._crack_dict_history[0] = (ts, None, result_text, extra)
        self._trim_history("crack_dict")
        self._render_content()

    # ----- 通用破解模式执行 -----

    def _build_crack_mode_config(self, page: str):
        """按模式构造 CrackConfig（哈希字段由 worker 填充）"""
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

        # 暴力穷举
        custom = inputs.get("crack_brute_custom", "").strip()
        toggles = state["toggles"]
        if custom:
            charset = custom
        else:
            parts = []
            if toggles.get("crack_brute_lower"):
                parts.append("abcdefghijklmnopqrstuvwxyz")
            if toggles.get("crack_brute_upper"):
                parts.append("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            if toggles.get("crack_brute_digit"):
                parts.append("0123456789")
            if toggles.get("crack_brute_special"):
                parts.append("!@#$%^&*()-_=+[]{};:,.<>?/")
            charset = "".join(parts)
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

        charset_hex = charset.encode("utf-8").hex()
        extra_args = [
            "--hex-charset",
            "--increment",
            "--increment-min", str(min_len),
            "--increment-max", str(max_len),
            f"--custom-charset1={charset_hex}",
        ]
        return CrackConfig(
            hash_file_path="", hashcat_mode=0,
            attack_mode=AttackMode.MASK, mask="?1" * max_len,
            work_load_profile=workload, device_type=device_type,
            extra_args=extra_args,
        )

    def _do_crack_mode_run(self, page: str):
        """执行通用模式破解（后台线程）"""
        ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = self._crack_state(page)
        inputs = state["inputs"]

        archive_val = inputs.get(f"{page}_archive", "").strip()
        if not archive_val:
            state["history"].insert(0, (ts_now, None, "错误:未选择压缩包"))
            self._trim_history("crack_mode_" + page)
            self._render_content()
            return
        if not Path(archive_val).exists():
            state["history"].insert(0, (ts_now, None, "错误:压缩包不存在"))
            self._trim_history("crack_mode_" + page)
            self._render_content()
            return

        try:
            cfg = self._build_crack_mode_config(page)
        except ValueError as exc:
            state["history"].insert(0, (ts_now, None, f"错误:{exc}"))
            self._trim_history("crack_mode_" + page)
            self._render_content()
            return

        if not self._cracker.is_available():
            state["history"].insert(0, (ts_now, None, "错误:Hashcat不可用,请检查bin目录"))
            self._trim_history("crack_mode_" + page)
            self._render_content()
            return

        if state["running"]:
            state["history"].insert(0, (ts_now, None, "已有破解任务正在运行,请等待结束"))
            self._trim_history("crack_mode_" + page)
            self._render_content()
            return

        state["history"].insert(0, (ts_now, None, "正在提取哈希..."))
        self._trim_history("crack_mode_" + page)
        state["running"] = True
        state["live"] = {"status_text": "初始化", "elapsed": 0.0}
        self._render_content()

        worker = CrackWorker(self._extractor, self._cracker, archive_val, cfg,
                             page, ts_now)
        worker.progress_signal.connect(lambda live, p=page: self._on_crack_mode_progress(p, live))
        worker.finished_signal.connect(
            lambda ts, s, cr, er, em, el, p=page: self._on_crack_mode_finished(p, ts, s, cr, er, em, el))
        self._crack_mode_workers[page] = worker
        worker.start()

    def _on_crack_mode_progress(self, page: str, live: dict):
        """通用模式实时进度回调"""
        state = self._crack_state(page)
        state["live"].update(live)

    def _on_crack_mode_finished(self, page: str, ts: str, success: bool,
                                crack_result, extract_result, error_msg: str,
                                elapsed: float):
        """通用模式破解完成回调"""
        state = self._crack_state(page)
        state["running"] = False
        state["live"] = {}
        if page in self._crack_mode_workers:
            del self._crack_mode_workers[page]
        self._cleanup_crack_files()

        inputs = state["inputs"]
        archive_val = inputs.get(f"{page}_archive", "")
        archive_name = Path(archive_val).name if archive_val else ""
        extra = {
            "压缩包": archive_name,
            "耗时": f"{elapsed:.2f} 秒",
        }
        if page == "crack_mask":
            extra["掩码"] = inputs.get("crack_mask_expr", "")
        elif page == "crack_rule":
            dict_count = len([d for d in inputs.get("crack_rule_dict", "").split(",") if d.strip()])
            extra["字典数"] = str(dict_count)
            extra["规则"] = Path(inputs.get("crack_rule_file", "")).name if inputs.get("crack_rule_file") else ""
        else:
            extra["字符集"] = inputs.get("crack_brute_custom", "") or "勾选字符集"
            extra["长度"] = f"{inputs.get('crack_brute_min_len', '')}-{inputs.get('crack_brute_max_len', '')}"

        if success and crack_result and crack_result.recovered_passwords:
            passwords = crack_result.recovered_passwords
            _real_pwds = [
                v for k, v in passwords.items()
                if k.startswith("$") and v and not v.strip().startswith(" ")
            ]
            pwd = _real_pwds[0] if _real_pwds else ""
            extra["密码"] = pwd
            state["history"][0] = (ts, None, "破解成功", extra)
        else:
            status = crack_result.status if crack_result else None
            if status and status.value == "exhausted":
                status_text = "字典试完未命中"
            elif status and status.value == "stopped":
                status_text = "已停止"
            elif status and status.value == "error":
                status_text = "执行异常"
            elif not extract_result or not extract_result.success:
                status_text = "哈希提取失败"
            else:
                status_text = "失败"
            if error_msg:
                extra["错误信息"] = error_msg
            state["history"][0] = (ts, None, status_text, extra)

        self._trim_history("crack_mode_" + page)
        self._render_content()

    # ----- 破解临时文件清理 -----

    def _cleanup_crack_files(self):
        """清理破解产生的临时文件（.hash / potfile / .restore / cracked_*.txt 等）"""
        try:
            out_dir = _app_base() / "data" / "output"
            if not out_dir.exists():
                return
            for hash_file in out_dir.glob("*.hash"):
                try:
                    hash_file.unlink()
                except Exception:
                    pass
            potfile = out_dir / "hashcat.potfile"
            if potfile.exists():
                try:
                    potfile.unlink()
                except Exception:
                    pass
            for pattern in ("hashcat.indb", "hashcat.log", "*.restore", "cracked_*.txt"):
                for residue in out_dir.glob(pattern):
                    try:
                        residue.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    # ----- 实时进度刷新（定时器回调） -----

    def _refresh_live_content(self):
        """0.3 秒定时刷新：仅在破解运行中重绘右侧内容"""
        need_refresh = False
        if self._crack_dict_running:
            need_refresh = True
        for page in _CRACK_MODE_PAGES:
            if self._crack_state(page)["running"]:
                need_refresh = True
                break
        if need_refresh:
            self._render_content()

    # ----- 文件拖入 -----

    def dragEnterEvent(self, event):
        """拖入事件：接受包含文件 URL 的事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """拖放事件：自动识别文件类型并填入对应字段"""
        urls = event.mimeData().urls()
        if not urls:
            return
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if not paths:
            return

        for path in paths:
            ext = Path(path).suffix.lower()
            if ext in _ARCHIVE_EXTS:
                self._handle_dropped_file(path, "archive")
            elif ext in _DICT_EXTS:
                self._handle_dropped_file(path, "dict")
            elif ext == ".rule":
                self._handle_dropped_file(path, "rule")
            else:
                self._handle_dropped_file(path, "unknown")

    def _handle_dropped_file(self, path: str, file_type: str):
        """根据当前页面和文件类型，把路径填入对应输入字段"""
        level = self._current_level
        if file_type == "rule":
            if level == "crack_rule":
                state = self._crack_state("crack_rule")
                state["inputs"]["crack_rule_file"] = path
                self._build_left_menu()
                self._render_content()
            return

        if file_type == "dict":
            if level == "crack_dict":
                existing = self._crack_dict_inputs.get("crack_dict_dict", "").strip()
                if existing:
                    self._crack_dict_inputs["crack_dict_dict"] = existing + "," + path
                else:
                    self._crack_dict_inputs["crack_dict_dict"] = path
                self._build_left_menu()
                self._render_content()
            elif level == "crack_rule":
                state = self._crack_state("crack_rule")
                existing = state["inputs"].get("crack_rule_dict", "").strip()
                if existing:
                    state["inputs"]["crack_rule_dict"] = existing + "," + path
                else:
                    state["inputs"]["crack_rule_dict"] = path
                self._build_left_menu()
                self._render_content()
            return

        if file_type == "archive":
            if level == "crack_dict":
                self._crack_dict_inputs["crack_dict_archive"] = path
                self._build_left_menu()
                self._render_content()
            elif level in _CRACK_MODE_PAGES:
                state = self._crack_state(level)
                state["inputs"][f"{level}_archive"] = path
                self._build_left_menu()
                self._render_content()
            return

        # 未知类型：尝试按当前页面填入 archive 字段
        if level == "crack_dict":
            self._crack_dict_inputs["crack_dict_archive"] = path
            self._build_left_menu()
            self._render_content()
        elif level in _CRACK_MODE_PAGES:
            state = self._crack_state(level)
            state["inputs"][f"{level}_archive"] = path
            self._build_left_menu()
            self._render_content()

    # ----- 底栏监控刷新 -----

    def _refresh_bottom_bar(self):
        """2 秒定时刷新底栏：版本 + CPU/GPU/内存监控"""
        try:
            stats = collect_realtime_stats()
            if stats.gpu_vram_total_mb > 0:
                vram_pct = stats.gpu_vram_used_mb * 100.0 / stats.gpu_vram_total_mb
                vram_part = (
                    f"    显存使用率: {stats.gpu_vram_used_mb}"
                    f"/{stats.gpu_vram_total_mb}MB"
                    f"({vram_pct:.0f}%)"
                )
            else:
                vram_part = ""
            stats_line = (
                f"当前版本: {APP_VERSION}    "
                f"CPU使用率: {stats.cpu_percent:.1f}%    "
                f"GPU使用率: {stats.gpu_percent:.0f}%"
                f"{vram_part}    "
                f"内存使用率: {stats.memory_used_gb:.1f}"
                f"/{int(stats.memory_total_gb)}G"
                f"({stats.memory_percent:.0f}%)"
            )
        except Exception:
            stats_line = f"当前版本: {APP_VERSION}    CPU使用率:--    GPU使用率:--    内存使用率:--"
        self._bottom_label.setText(stats_line)

    # ----- 关闭事件 -----

    def closeEvent(self, event):
        """关闭窗口时停止后台线程"""
        # 停止字典生成线程
        if self._dict_worker is not None and self._dict_worker.isRunning():
            self._dict_worker.quit()
            self._dict_worker.wait(2000)
        # 停止字典攻击线程
        if self._crack_dict_worker is not None and self._crack_dict_worker.isRunning():
            self._crack_dict_worker.request_stop()
            self._crack_dict_worker.quit()
            self._crack_dict_worker.wait(3000)
        # 停止通用模式线程
        for page, worker in list(self._crack_mode_workers.items()):
            if worker.isRunning():
                worker.request_stop()
                worker.quit()
                worker.wait(3000)
        # 停止 hashcat
        try:
            self._cracker.stop()
        except Exception:
            pass
        event.accept()


# ======================================================================
# 通用输入对话框
# ======================================================================

class _InputDialog(QDialog):
    """单行输入对话框：编辑当前值，确认后返回"""

    def __init__(self, current_val: str, item_id: str, parent=None, hint: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑输入")
        self.setMinimumWidth(500)
        self.setStyleSheet(f"QDialog {{ background-color: {C_BG_DARK}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        label = QLabel(f"编辑: {item_id}")
        label.setStyleSheet(f"color: {C_NS_CYAN}; font-size: 14px; font-weight: bold;")
        layout.addWidget(label)

        # 提示说明（可选）
        if hint:
            hint_label = QLabel(hint)
            hint_label.setWordWrap(True)
            hint_label.setStyleSheet(f"color: {C_NS_GRAY}; font-size: 12px;")
            layout.addWidget(hint_label)

        self._line_edit = QLineEdit(str(current_val))
        layout.addWidget(self._line_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消 (A / ESC)")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("确认 (D / 回车)")
        ok_btn.setStyleSheet(
            f"QPushButton {{ color: {C_NS_GREEN}; border-color: {C_NS_GREEN}; font-weight: bold; }}")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        # 自动全选
        self._line_edit.selectAll()
        self._line_edit.setFocus()
        # 回车/D确认, A/ESC取消; 空格在输入框聚焦时用于输入空格,不拦截
        QShortcut(QKeySequence("Return"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("A"), self).activated.connect(self.reject)
        QShortcut(QKeySequence("D"), self).activated.connect(self.accept)

    def get_value(self) -> str:
        return self._line_edit.text()


# ======================================================================
# 入口函数
# ======================================================================

def main():
    """GUI 版入口"""
    app = QApplication(sys.argv)
    app.setApplicationName("ArchiveCracker GUI")
    # 设置窗口/任务栏图标为根目录 logo.png
    icon_path = _app_base() / "logo.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = CrackerMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
