# -*- coding: utf-8 -*-
"""
文件名称：path_manager.py
功能描述：跨平台二进制路径管理模块
         负责根据当前操作系统（Windows/macOS/Linux）自动定位 Hashcat、
         John the Ripper 系列工具（zip2john/rar2john/7z2john）等外部可执行文件路径
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：完成平台检测与路径映射基础框架
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# 压缩包格式与对应 Hashcat -m 参数映射常量
HASHCAT_MODE_ZIP_AES = 13600       # ZIP AES-256 (WinZip) 模式
HASHCAT_MODE_RAR3 = 12500          # RAR3-hp 模式
HASHCAT_MODE_RAR5 = 13000          # RAR5 模式
HASHCAT_MODE_7Z = 11600            # 7-Zip 模式


@dataclass
class ToolPaths:
    """
    外部工具路径集合
    统一存放各可执行文件的绝对路径，None 表示对应工具未找到
    """
    hashcat: Optional[str] = None         # Hashcat 主程序路径
    zip2john: Optional[str] = None        # ZIP 哈希提取器路径
    rar2john: Optional[str] = None        # RAR 哈希提取器路径
    seven2john_perl: Optional[str] = None # 7Z 哈希提取器(Perl脚本，备用)
    john_root: Optional[str] = None       # John run 根目录（备用定位用）
    hashcat_root: Optional[str] = None    # Hashcat 根目录（必须切到此目录运行，防止找不到 kernels/ 与 OpenCL/）


class PathManager:
    """
    跨平台路径管理器
    使用方法：
        pm = PathManager()
        paths = pm.discover()
        if paths.hashcat:
            print(paths.hashcat)
    """

    def __init__(self, project_root: Optional[str] = None):
        """
        初始化路径管理器
        :param project_root: 项目根目录，默认自动根据当前文件位置推断
        """
        if project_root:
            self.project_root = Path(project_root).resolve()
        else:
            # 打包运行时优先使用 PyInstaller 的资源根目录(_internal)，
            # 开发运行时取当前文件的上上级目录（core/path_manager.py -> 上两级）
            self.project_root = Path(
                getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
            )
        self.bin_root = self.project_root / "bin"

    def _detect_platform_dir(self) -> Optional[str]:
        """
        根据 sys.platform 检测当前平台对应 bin/ 下的子目录名
        :return: 'windows' / 'macos' / 'linux' 或 None(未知平台)
        """
        plat = sys.platform
        if plat.startswith("win"):
            return "windows"
        elif plat == "darwin":
            return "macos"
        elif plat.startswith("linux"):
            return "linux"
        return None

    def _find_executable(self, directory: Path, filename_no_ext: str) -> Optional[str]:
        """
        在指定目录下查找可执行文件（自动适配 .exe 后缀）
        :param directory: 搜索目录
        :param filename_no_ext: 不含扩展名的文件名（如 zip2john）
        :return: 绝对路径字符串，找不到返回 None
        """
        if not directory.exists():
            return None
        # Windows 优先查 .exe，其次无扩展名；类 Unix 查无扩展名
        candidates = []
        if sys.platform.startswith("win"):
            candidates.append(f"{filename_no_ext}.exe")
            candidates.append(filename_no_ext)
        else:
            candidates.append(filename_no_ext)
            candidates.append(f"{filename_no_ext}.sh")
        for name in candidates:
            p = directory / name
            if p.exists() and p.is_file():
                return str(p.resolve())
        return None

    def discover(self) -> ToolPaths:
        """
        执行全量工具路径发现
        :return: ToolPaths 数据类，包含各工具路径
        """
        paths = ToolPaths()
        platform_dir_name = self._detect_platform_dir()
        if not platform_dir_name:
            # 未知平台，直接返回空集合（上层可通过各字段为 None 提示用户）
            return paths

        platform_bin = self.bin_root / platform_dir_name

        # --- 1. 定位 Hashcat ---
        hashcat_root_candidates = [
            platform_bin / "hashcat",
            platform_bin / "hashcat-7.1.2",
            platform_bin / "hashcat-6.2.6",
        ]
        for hc_root in hashcat_root_candidates:
            if hc_root.exists() and hc_root.is_dir():
                exe_path = self._find_executable(hc_root, "hashcat")
                if exe_path:
                    paths.hashcat = exe_path
                    paths.hashcat_root = str(hc_root.resolve())
                    break

        # --- 2. 定位 John the Ripper 工具链 ---
        john_root_candidates = [
            platform_bin / "john" / "run",
            platform_bin / "john" / "JtR" / "run",
            platform_bin / "JtR" / "run",
        ]
        for j_run in john_root_candidates:
            if j_run.exists() and j_run.is_dir():
                paths.zip2john = self._find_executable(j_run, "zip2john")
                paths.rar2john = self._find_executable(j_run, "rar2john")
                # 7z2john 可能是 pl（Perl）也可能是 exe，先看 pl 脚本
                pl_file = j_run / "7z2john.pl"
                if pl_file.exists():
                    paths.seven2john_perl = str(pl_file.resolve())
                # 再尝试找 exe 版覆盖
                exe_7z2john = self._find_executable(j_run, "7z2john")
                if exe_7z2john:
                    paths.seven2john_perl = exe_7z2john  # 字段复用，exe 优先
                paths.john_root = str(j_run.resolve())
                break

        return paths

    def ensure_data_dirs(self) -> dict:
        """
        确保数据输出目录存在，并返回其路径字典
        :return: {'dictionaries': ..., 'output': ...}
        """
        dicts_dir = self.project_root / "data" / "dictionaries"
        output_dir = self.project_root / "data" / "output"
        dicts_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "dictionaries": str(dicts_dir.resolve()),
            "output": str(output_dir.resolve()),
        }


if __name__ == "__main__":
    # 调试用：单独运行本文件可输出各工具路径发现结果
    pm = PathManager()
    discovered = pm.discover()
    print(f"项目根目录: {pm.project_root}")
    print(f"平台目录:   {pm._detect_platform_dir()}")
    print()
    print("Hashcat 主程序:", discovered.hashcat or "未找到")
    print("Hashcat 根目录:", discovered.hashcat_root or "未找到")
    print("zip2john:", discovered.zip2john or "未找到")
    print("rar2john:", discovered.rar2john or "未找到")
    print("7z2john (pl/exe):", discovered.seven2john_perl or "未找到")
    print("John run 目录:", discovered.john_root or "未找到")
    print()
    data_dirs = pm.ensure_data_dirs()
    print("字典目录:", data_dirs["dictionaries"])
    print("输出目录:", data_dirs["output"])
