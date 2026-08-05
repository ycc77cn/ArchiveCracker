# -*- coding: utf-8 -*-
"""
文件名称：core/hardware_info.py
功能描述：硬件信息采集模块（纯标准库 + 子进程调用系统命令）
         采集项目：操作系统 / CPU / 内存 / 磁盘 / GPU 等基础信息
         设计原则：
             1. 零第三方依赖，仅用 Python 标准库 + 系统自带命令
             2. Windows 优先使用 wmic / PowerShell，跨平台 fallback
             3. 任何子命令失败不抛异常，返回"未知"，保证整体可用
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：实现 CPU/内存/磁盘/GPU/OS 五类信息采集
"""

import platform
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import psutil
import ctypes


# DDR 内存类型映射表（SMBIOS Memory Type 值 -> 显示名称）
# 参考 SMBIOS 规范，wmic 的 SMBIOSMemoryType 字段对应此值
_DDR_TYPE_MAP = {
    20: "DDR",
    21: "DDR2",
    24: "DDR3",
    26: "DDR4",
    34: "DDR5",
}

# wmic 子进程超时时间（秒），超时则放弃该字段
_WMIC_TIMEOUT = 8


@dataclass
class CpuInfo:
    """CPU 信息数据结构"""
    name: str = "未知"               # CPU 型号名称
    physical_cores: int = 0          # 物理核心数
    logical_cores: int = 0           # 逻辑核心数（线程数）
    max_frequency_mhz: int = 0       # 最大频率（MHz）


@dataclass
class MemoryStick:
    """单根内存条信息"""
    capacity_gb: float = 0.0         # 容量（GB）
    speed_mt_s: int = 0              # 频率（MT/s，即 MHz）
    ddr_type: str = "未知"           # DDR 类型（DDR4 / DDR5 等）


@dataclass
class DrivePartition:
    """单个盘符（逻辑盘）信息"""
    letter: str = ""                # 盘符，如 C:
    total_gb: float = 0.0           # 该盘总容量（GB）
    free_gb: float = 0.0            # 该盘可用容量（GB）


@dataclass
class DiskInfo:
    """单块磁盘信息"""
    model: str = "未知"              # 磁盘型号
    size_gb: float = 0.0            # 容量（GB）
    drive_letters: list = field(default_factory=list)  # 关联盘符列表，如 ['C:','D:']
    partitions: list = field(default_factory=list)     # 关联逻辑盘详细信息列表 DrivePartition


@dataclass
class GpuInfo:
    """单块 GPU 信息"""
    name: str = "未知"               # GPU 型号
    vram_mb: int = 0                 # 显存（MB）


@dataclass
class HardwareReport:
    """硬件信息汇总报告"""
    os_name: str = "未知"            # 操作系统
    hostname: str = "未知"           # 主机名
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory_sticks: list = field(default_factory=list)  # 内存条列表
    disks: list = field(default_factory=list)          # 磁盘列表
    gpus: list = field(default_factory=list)           # GPU 列表


def _run_command(cmd: str, timeout: int = _WMIC_TIMEOUT) -> str:
    """
    执行系统命令并返回标准输出文本（失败返回空字符串）
    :param cmd: 要执行的命令字符串
    :param timeout: 超时时间（秒）
    :return: 命令标准输出，去除首尾空白
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
            check=False,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _parse_wmic_list(output: str) -> list[dict]:
    """
    解析 wmic /format:list 输出为字典列表
    wmic list 格式：每行 Key=Value，字段间可能有不定数量空行
    采用「字段名重复出现即新记录开始」的策略，兼容空行不固定的情况
    :param output: wmic 命令原始输出
    :return: 每条记录一个字典的列表
    """
    records: list[dict] = []
    current: dict = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        # 字段名重复出现：说明上一条记录结束、新记录开始
        if key in current:
            records.append(current)
            current = {}
        current[key] = value
    # 处理最后一条记录
    if current:
        records.append(current)
    return records


def _collect_os_info() -> tuple[str, str]:
    """
    采集操作系统与主机名
    :return: (操作系统描述, 主机名)
    """
    os_desc = platform.platform()
    hostname = platform.node()
    return os_desc or "未知", hostname or "未知"


def _collect_cpu_info() -> CpuInfo:
    """
    采集 CPU 信息
    Windows：wmic cpu get Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed
    通用 fallback：platform.processor() + os.cpu_count()
    :return: CpuInfo 数据对象
    """
    info = CpuInfo()
    if platform.system() == "Windows":
        output = _run_command(
            'wmic cpu get Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed /format:list'
        )
        records = _parse_wmic_list(output)
        if records:
            first = records[0]
            info.name = first.get("Name", "未知")
            info.physical_cores = int(first.get("NumberOfCores", 0) or 0)
            info.logical_cores = int(first.get("NumberOfLogicalProcessors", 0) or 0)
            info.max_frequency_mhz = int(first.get("MaxClockSpeed", 0) or 0)
            return info
    # 跨平台 fallback
    info.name = platform.processor() or "未知"
    info.logical_cores = _get_logical_cores()
    return info


def _get_logical_cores() -> int:
    """获取逻辑核心数（跨平台）"""
    try:
        import os
        return os.cpu_count() or 0
    except Exception:  # noqa: BLE001
        return 0


def _collect_memory_info() -> list[MemoryStick]:
    """
    采集内存信息（每根内存条）
    Windows：wmic memorychip get Capacity,Speed,SMBIOSMemoryType,MemoryType
    :return: MemoryStick 列表
    """
    sticks: list[MemoryStick] = []
    if platform.system() != "Windows":
        return sticks
    output = _run_command(
        'wmic memorychip get Capacity,Speed,SMBIOSMemoryType,MemoryType /format:list'
    )
    records = _parse_wmic_list(output)
    for rec in records:
        stick = MemoryStick()
        # 容量单位为字节，转换为 GB
        capacity_bytes = int(rec.get("Capacity", 0) or 0)
        stick.capacity_gb = round(capacity_bytes / (1024 ** 3), 1) if capacity_bytes else 0.0
        stick.speed_mt_s = int(rec.get("Speed", 0) or 0)
        # 优先用 SMBIOSMemoryType 判断 DDR 代数，回退到 MemoryType
        smbios_type = int(rec.get("SMBIOSMemoryType", 0) or 0)
        legacy_type = int(rec.get("MemoryType", 0) or 0)
        ddr_code = smbios_type if smbios_type else legacy_type
        stick.ddr_type = _DDR_TYPE_MAP.get(ddr_code, "未知")
        sticks.append(stick)
    return sticks


def _collect_disk_to_drive_map() -> dict[int, list[str]]:
    """
    构建「物理磁盘号 -> 盘符列表」映射
    Windows 链路：Win32_DiskDrive(Index) -> Win32_DiskPartition(DiskIndex)
                 -> Win32_LogicalDiskToPartition -> Win32_LogicalDisk(DeviceID)
    :return: {disk_index: ['C:', 'D:', ...]}
    """
    result: dict[int, list[str]] = {}
    if platform.system() != "Windows":
        return result

    # Step 1: 分区 -> 物理磁盘号（DiskIndex）
    # wmic partition get DiskIndex,DeviceID /format:list
    part_out = _run_command(
        'wmic partition get DiskIndex,DeviceID /format:list'
    )
    part_records = _parse_wmic_list(part_out)
    # partition_device_id -> disk_index
    part_to_disk: dict[str, int] = {}
    for pr in part_records:
        try:
            disk_idx = int(pr.get("DiskIndex", 0) or 0)
        except ValueError:
            continue
        part_dev = pr.get("DeviceID", "").strip()
        if part_dev:
            part_to_disk[part_dev] = disk_idx

    # Step 2: 逻辑盘（盘符） -> 分区
    # Win32_LogicalDiskToPartition 的 Antecedent 是分区 DeviceID
    # Dependent 是逻辑盘 DeviceID（即盘符）
    # wmic path Win32_LogicalDiskToPartition get Antecedent,Dependent
    ld_out = _run_command(
        'wmic path Win32_LogicalDiskToPartition get Antecedent,Dependent /format:list'
    )
    ld_records = _parse_wmic_list(ld_out)
    for ld in ld_records:
        # Antecedent: 字符串中包含 DeviceID="磁盘分区ID"
        antecedent = ld.get("Antecedent", "") or ""
        # Dependent: 字符串中包含 DeviceID="C:"
        dependent = ld.get("Dependent", "") or ""
        # 从 Antecedent 中提取分区 DeviceID（Win32_DiskPartition.DeviceID）
        # 格式样例：\\\\PC-NAME\\root\\cimv2:Win32_DiskPartition.DeviceID="Disk #0, Partition #1"
        part_match = re.search(r'DeviceID="([^"]+)"', antecedent)
        if not part_match:
            continue
        part_id = part_match.group(1)
        # 从 Dependent 中提取盘符（如 C:）
        drive_match = re.search(r'DeviceID="([A-Z]:)"', dependent)
        if not drive_match:
            continue
        drive_letter = drive_match.group(1)
        # 分区 -> 物理磁盘号
        disk_idx = part_to_disk.get(part_id)
        if disk_idx is None:
            continue
        result.setdefault(disk_idx, []).append(drive_letter)

    # 盘符按字母排序
    for k in result:
        result[k].sort()
    return result


def _collect_logical_disks() -> dict[str, DrivePartition]:
    """
    采集所有逻辑盘（盘符）的容量信息
    Windows：wmic logicaldisk get DeviceID,Size,FreeSpace /format:list
    :return: {盘符: DrivePartition}
    """
    result: dict[str, DrivePartition] = {}
    if platform.system() != "Windows":
        return result

    output = _run_command(
        'wmic logicaldisk get DeviceID,Size,FreeSpace /format:list'
    )
    records = _parse_wmic_list(output)
    for rec in records:
        letter = rec.get("DeviceID", "").strip()
        if not letter or not letter.endswith(":"):
            continue
        part = DrivePartition(letter=letter)
        try:
            total_bytes = int(rec.get("Size", 0) or 0)
            free_bytes = int(rec.get("FreeSpace", 0) or 0)
            part.total_gb = round(total_bytes / (1024 ** 3), 1) if total_bytes else 0.0
            part.free_gb = round(free_bytes / (1024 ** 3), 1) if free_bytes else 0.0
        except ValueError:
            pass
        result[letter] = part
    return result


def _collect_disk_info() -> list[DiskInfo]:
    """
    采集磁盘信息
    Windows：wmic diskdrive get Index,Model,Size + 盘符映射 + 逻辑盘容量
    :return: DiskInfo 列表（每块磁盘含其下所有盘符的已用/总容量）
    """
    disks: list[DiskInfo] = []
    if platform.system() != "Windows":
        return disks

    # 先拿磁盘号 -> 盘符映射
    disk_to_drives = _collect_disk_to_drive_map()
    # 再拿盘符 -> 容量信息映射
    letter_to_part = _collect_logical_disks()

    output = _run_command(
        'wmic diskdrive get Index,Model,Size /format:list'
    )
    records = _parse_wmic_list(output)
    for rec in records:
        disk = DiskInfo()
        disk.model = rec.get("Model", "未知")
        size_bytes = int(rec.get("Size", 0) or 0)
        disk.size_gb = round(size_bytes / (1024 ** 3), 1) if size_bytes else 0.0
        # 取物理磁盘号用于关联盘符
        try:
            disk_idx = int(rec.get("Index", 0) or 0)
        except ValueError:
            disk_idx = -1
        # 挂上关联的盘符列表
        disk.drive_letters = disk_to_drives.get(disk_idx, [])
        # 挂上关联的盘符详细容量信息
        disk.partitions = [
            letter_to_part[letter]
            for letter in disk.drive_letters
            if letter in letter_to_part
        ]
        # 过滤掉容量为 0 的设备（如 USB 设备不报告大小，无参考价值）
        if disk.size_gb > 0:
            disks.append(disk)
    return disks


def _collect_gpu_info() -> list[GpuInfo]:
    """
    采集 GPU 信息
    优先：nvidia-smi（NVIDIA 显卡最准确，含显存）
    回退：hashcat -I（需 hashcat 在 PATH 或项目 bin 目录，此处仅尝试 nvidia-smi）
    :return: GpuInfo 列表
    """
    gpus: list[GpuInfo] = []
    # 尝试 nvidia-smi
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        output = _run_command(
            f'"{nvidia_smi}" --query-gpu=name,memory.total --format=csv,noheader,nounits'
        )
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # 输出格式：GPU 名称, 显存数值（MB）
            parts = [p.strip() for p in line.split(",")]
            gpu = GpuInfo()
            if len(parts) >= 1:
                gpu.name = parts[0]
            if len(parts) >= 2:
                try:
                    gpu.vram_mb = int(parts[1])
                except ValueError:
                    gpu.vram_mb = 0
            gpus.append(gpu)
    return gpus


def collect_hardware_report() -> HardwareReport:
    """
    采集完整硬件信息报告（入口函数）
    依次采集：OS -> CPU -> 内存 -> 磁盘 -> GPU
    :return: HardwareReport 汇总对象
    """
    report = HardwareReport()
    report.os_name, report.hostname = _collect_os_info()
    report.cpu = _collect_cpu_info()
    report.memory_sticks = _collect_memory_info()
    report.disks = _collect_disk_info()
    report.gpus = _collect_gpu_info()
    return report


def _display_width(s: str) -> int:
    """
    计算字符串在终端中的显示宽度
    全角字符(中文/全角符号)占 2 列,半角字符占 1 列
    :param s: 输入字符串
    :return: 显示宽度(列数)
    """
    width = 0
    for ch in s:
        if unicodedata.east_asian_width(ch) in ('F', 'W'):
            width += 2
        else:
            width += 1
    return width


def _strip_markup(s: str) -> str:
    """
    去除 Textual markup 标签,返回纯文本
    用于计算带 markup 的字符串的实际显示宽度
    :param s: 含 Textual markup 标签的字符串
    :return: 去除标签后的纯文本
    """
    return re.sub(r'\[/?[^\]]*\]', '', s)


def _pad_to_width(text: str, target_width: int) -> str:
    """
    用空格将文本补齐到目标显示宽度
    自动处理 markup 标签(标签不计入显示宽度)
    :param text: 输入文本(可含 markup)
    :param target_width: 目标显示宽度(列数)
    :return: 补齐后的文本
    """
    current = _display_width(_strip_markup(text))
    if current >= target_width:
        return text
    return text + ' ' * (target_width - current)


def format_report_text(report: HardwareReport, width: int = 60) -> str:
    """
    将硬件信息报告格式化为 nushell 风格表格(绿色 box-drawing 边框)
    视觉结构:
        ╭─ 设备信息 ───────────────────────╮
        │ OS          Windows-10-...       │
        │ Hostname    WIN-XXX              │
        ├──────────────────────────────────┤
        │ CPU                              │
        │   型号      Intel(R) Core...     │
        │   ...                            │
        ╰──────────────────────────────────╯
    宽度自适应:box_width = width,最小 50 保证内容可读
    所有 markup 闭合标签统一用 [/] 避免跨行匹配冲突
    :param report: HardwareReport 对象
    :param width: 表格目标宽度(字符数)
    :return: 格式化后的多行文本(含 Textual markup 标签)
    """
    # 颜色常量
    C_GREEN = "#00ff00"   # 边框 绿色
    C_CYAN = "#00ffff"    # 分区标题 青色

    # 表格宽度,最小 50
    box_width = max(50, width)
    # 内容区宽度 = 表格宽 - 左│+空格(2) - 右│+空格(2)
    inner = box_width - 4

    def _top(title: str) -> str:
        """构建顶部边框行: ╭─ title ───╮"""
        t = f" {title} "
        tw = _display_width(t)
        dashes = max(0, box_width - 2 - tw)
        return f"[{C_GREEN}]╭{t}" + "─" * dashes + "╮[/]"

    def _mid() -> str:
        """构建中间分隔行: ├───┤"""
        return f"[{C_GREEN}]├" + "─" * (box_width - 2) + "┤[/]"

    def _bottom() -> str:
        """构建底部边框行: ╰───╯"""
        return f"[{C_GREEN}]╰" + "─" * (box_width - 2) + "╯[/]"

    def _row(content: str) -> str:
        """构建内容行: │ content │"""
        padded = _pad_to_width(content, inner)
        return f"[{C_GREEN}]│[/] {padded} [{C_GREEN}]│[/]"

    def _section(title: str) -> str:
        """构建分区标题行: │ [青色]title[/] │"""
        return _row(f"[{C_CYAN}]{title}[/]")

    def _kv(key: str, value: str, indent: int = 0, kw: int = 12) -> str:
        """构建键值行: │   key    value │"""
        prefix = " " * indent
        key_padded = _pad_to_width(key, kw)
        return _row(f"{prefix}{key_padded} {value}")

    lines: list[str] = []

    # 顶部边框 + 标题
    lines.append(_top("设备信息"))

    # 基本信息
    lines.append(_kv("OS", report.os_name))
    lines.append(_kv("Hostname", report.hostname))

    # CPU 信息
    lines.append(_mid())
    lines.append(_section("CPU"))
    lines.append(_kv("型号", report.cpu.name, indent=2))
    lines.append(_kv("物理核心", f"{report.cpu.physical_cores} 核", indent=2))
    lines.append(_kv("逻辑线程", f"{report.cpu.logical_cores} 线程", indent=2))
    if report.cpu.max_frequency_mhz:
        lines.append(_kv("最大频率", f"{report.cpu.max_frequency_mhz} MHz", indent=2))

    # 内存信息
    lines.append(_mid())
    lines.append(_section("内存"))
    if report.memory_sticks:
        total_gb = sum(s.capacity_gb for s in report.memory_sticks)
        lines.append(_kv(
            "总容量",
            f"{total_gb:.1f} GB ({len(report.memory_sticks)} 条)",
            indent=2,
        ))
        for idx, stick in enumerate(report.memory_sticks, 1):
            lines.append(_kv(
                f"槽位 {idx}",
                f"{stick.capacity_gb:.1f} GB  {stick.ddr_type}  {stick.speed_mt_s} MT/s",
                indent=2,
            ))
    else:
        lines.append(_kv("总容量", "未知", indent=2))

    # 磁盘信息（按物理磁盘分组，每盘符一行：盘符 + 已用/总容量）
    lines.append(_mid())
    lines.append(_section("磁盘"))
    if report.disks:
        for idx, disk in enumerate(report.disks, 1):
            # 磁盘标题行：磁盘1  型号  总容量
            lines.append(_kv(
                f"磁盘 {idx}",
                f"{disk.model}  {disk.size_gb:.1f} GB",
                indent=2,
            ))
            # 该磁盘下每个盘符一行：盘符  已用GB/总容量GB
            if disk.partitions:
                for part in disk.partitions:
                    used_gb = part.total_gb - part.free_gb
                    lines.append(_kv(
                        part.letter,
                        f"{used_gb:.1f}GB/{part.total_gb:.1f}GB",
                        indent=4,
                        kw=8,
                    ))
            else:
                lines.append(_kv("-", "无分区", indent=4, kw=8))
    else:
        lines.append(_kv("磁盘", "未检测到", indent=2))

    # GPU 信息
    lines.append(_mid())
    lines.append(_section("GPU"))
    if report.gpus:
        for idx, gpu in enumerate(report.gpus, 1):
            vram = f"{gpu.vram_mb} MB" if gpu.vram_mb else "未知"
            lines.append(_kv(
                f"GPU {idx}",
                f"{gpu.name}  {vram}",
                indent=2,
            ))
    else:
        lines.append(_kv("GPU", "未检测到", indent=2))

    # 软件信息（展示在 GPU 下方）
    lines.append(_mid())
    lines.append(_section("软件信息"))
    lines.append(_kv("开发者", "杨CC", indent=2))
    lines.append(_kv("开源地址", "https://github.com/ycc77cn/ArchiveCracker", indent=2))
    lines.append(_kv("B站", "疯狂的杨CC", indent=2))
    lines.append(_kv("粉丝群", "660264846", indent=2))

    # 底部边框
    lines.append(_bottom())

    return "\n".join(lines)


# ======================================================================
# 实时监控数据（CPU/GPU/内存 使用率）
# ======================================================================

def _get_memory_via_windows_api() -> tuple[float, float, float]:
    """
    Windows 平台：通过 ctypes 调用 Kernel32 GlobalMemoryStatusEx 读取内存
    （psutil 异常时的兜底函数）
    :return: (总内存 GB, 已用 GB, 使用率%)  失败则 (0,0,0)
    """
    if platform.system() != "Windows":
        return (0.0, 0.0, 0.0)

    class MemoryStatusEx(ctypes.Structure):
        """对应 Windows API MEMORYSTATUSEX 结构体"""
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        if not ok:
            return (0.0, 0.0, 0.0)
        total_gb = round(status.ullTotalPhys / (1024 ** 3), 1)
        used_gb = round((status.ullTotalPhys - status.ullAvailPhys) / (1024 ** 3), 1)
        percent = round(status.dwMemoryLoad, 1)
        return (total_gb, used_gb, percent)
    except Exception:  # noqa: BLE001
        return (0.0, 0.0, 0.0)


@dataclass
class RealtimeStats:
    """实时资源使用率数据"""
    cpu_percent: float = 0.0           # CPU 使用率（%）
    memory_used_gb: float = 0.0        # 已用内存（GB）
    memory_total_gb: float = 0.0       # 总内存（GB）
    memory_percent: float = 0.0        # 内存占用率（%）
    gpu_percent: float = 0.0           # GPU 使用率（%）
    gpu_vram_used_mb: int = 0          # GPU 已用显存（MB）
    gpu_vram_total_mb: int = 0         # GPU 总显存（MB）


def collect_realtime_stats() -> RealtimeStats:
    """
    采集实时资源使用率（CPU / 内存 / GPU）
    CPU/内存：psutil（跨平台，轻量）
    GPU：nvidia-smi（仅 NVIDIA 显卡，无则 GPU 字段为 0）
    :return: RealtimeStats 实时数据对象
    """
    stats = RealtimeStats()

    # ---- CPU 使用率（psutil，interval=None 非阻塞返回上次调用以来的平均值） ----
    try:
        stats.cpu_percent = psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001
        stats.cpu_percent = 0.0

    # ---- 内存使用率（psutil 优先，失败则用 Windows API 兜底）----
    try:
        mem = psutil.virtual_memory()
        stats.memory_total_gb = round(mem.total / (1024 ** 3), 1)
        stats.memory_used_gb = round(mem.used / (1024 ** 3), 1)
        stats.memory_percent = round(mem.percent, 1)
    except Exception:  # noqa: BLE001
        stats.memory_total_gb, stats.memory_used_gb, stats.memory_percent = (
            _get_memory_via_windows_api()
        )

    # ---- GPU 使用率（nvidia-smi） ----
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            output = _run_command(
                f'"{nvidia_smi}" --query-gpu=utilization.gpu,memory.used,memory.total '
                f'--format=csv,noheader,nounits',
                timeout=3,
            )
            if output:
                parts = [p.strip() for p in output.split(",")]
                if len(parts) >= 3:
                    stats.gpu_percent = float(parts[0] or 0)
                    stats.gpu_vram_used_mb = int(parts[1] or 0)
                    stats.gpu_vram_total_mb = int(parts[2] or 0)
        except Exception:  # noqa: BLE001
            pass

    return stats


def format_stats_text(stats: RealtimeStats) -> str:
    """
    格式化实时监控数据为短文本（供侧边栏底部显示）
    标签刻意简短，确保侧边栏 16~20 字符宽度内一行能完整显示，不折行
    :param stats: RealtimeStats 对象
    :return: 三行文本（CPU / GPU / 内存，各占一行）
    """
    C_BLUE = "#66ccff"
    # CPU 行：label 直接跟 markup 数值，避免冒号后+空格导致换行
    cpu_line = f"CPU:[{C_BLUE}]{stats.cpu_percent:.1f}%[/]"
    gpu_line = f"GPU:[{C_BLUE}]{stats.gpu_percent:.0f}%[/]"
    # 内存行：刻意简写 GB→G、去掉空格，确保一行 ≤ 20 字符
    if stats.memory_total_gb > 0:
        # 格式示例："内存:14.7/32G(46%)" 纯文本 19 字符
        mem_line = (
            f"内存:[{C_BLUE}]{stats.memory_used_gb:.1f}[/]"
            f"/{int(stats.memory_total_gb)}G"
            f"([{C_BLUE}]{int(stats.memory_percent)}%[/])"
        )
    else:
        mem_line = f"内存:未知"
    return "\n".join([cpu_line, gpu_line, mem_line])
