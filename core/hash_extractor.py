# -*- coding: utf-8 -*-
"""
文件名称：hash_extractor.py
功能描述：压缩包哈希提取模块
         负责将加密的 ZIP/RAR/7Z 压缩包转换为 Hashcat 可识别的标准哈希字符串。
         设计原则：
             - ZIP: 调用 John 的 zip2john.exe（官方工具，兼容性最稳）
             - RAR (RAR3/RAR5): 调用 John 的 rar2john.exe（官方工具）
             - 7Z : 使用 Python 原生实现哈希提取（不依赖 Perl / 7z2john.pl，
                    走方案一；此处先输出函数框架与接口，后续具体解析算法补全）
创建日期：2026-08-03
修改记录：
    2026-08-03  初始版本：完成统一调度框架 + ZIP/RAR 子进程调用占位 + 7Z 原生接口占位
"""

import os
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# 兼容两种运行方式：作为 core 包 import 或直接 python core/xxx.py 调试
try:
    from .archive_detector import ArchiveDetector, ArchiveType
    from .path_manager import PathManager, ToolPaths, HASHCAT_MODE_ZIP_AES, HASHCAT_MODE_RAR3, HASHCAT_MODE_RAR5, HASHCAT_MODE_7Z
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from core.archive_detector import ArchiveDetector, ArchiveType  # type: ignore[no-redef]
    from core.path_manager import (  # type: ignore[no-redef]
        PathManager, ToolPaths,
        HASHCAT_MODE_ZIP_AES, HASHCAT_MODE_RAR3, HASHCAT_MODE_RAR5, HASHCAT_MODE_7Z,
    )


@dataclass
class ExtractResult:
    """
    哈希提取结果数据类
    """
    success: bool                        # 是否提取成功
    archive_type: ArchiveType            # 检测出的压缩包类型
    hashcat_mode: Optional[int]          # Hashcat 对应 -m 参数
    hash_string: Optional[str]           # 标准哈希字符串（Hashcat 可直接用）
    hash_file_path: Optional[str]        # 写入到 data/output 的 hash 文件路径
    error_message: Optional[str] = None  # 失败时错误信息


class HashExtractor:
    """
    压缩包哈希提取调度器
    使用方法：
        from core import PathManager, HashExtractor
        pm = PathManager()
        he = HashExtractor(pm)
        result = he.extract("demo.zip")
        print(result.hash_string)
    """

    def __init__(self, path_manager: PathManager):
        """
        初始化提取器
        :param path_manager: 已初始化的 PathManager 实例，用于定位工具与数据目录
        """
        self.pm = path_manager
        self.detector = ArchiveDetector()
        self.tool_paths: ToolPaths = self.pm.discover()
        self.data_dirs: dict = self.pm.ensure_data_dirs()
        self.output_dir: Path = Path(self.data_dirs["output"])

    # ======================================================================
    # 对外统一接口
    # ======================================================================
    def extract(self, archive_path: str) -> ExtractResult:
        """
        【对外主接口】自动识别压缩包类型并提取哈希
        :param archive_path: 加密压缩包路径
        :return: ExtractResult，包含 hash 字符串 / Hashcat mode / 错误信息等
        """
        # --- 基础输入校验 ---
        archive_file = Path(archive_path)
        if not archive_file.exists() or not archive_file.is_file():
            return ExtractResult(
                success=False,
                archive_type=ArchiveType.UNKNOWN,
                hashcat_mode=None,
                hash_string=None,
                hash_file_path=None,
                error_message=f"压缩包不存在或不是文件: {archive_path}",
            )

        # --- 类型检测 ---
        atype, _ = self.detector.detect(str(archive_file))
        if atype == ArchiveType.UNKNOWN:
            return ExtractResult(
                success=False,
                archive_type=ArchiveType.UNKNOWN,
                hashcat_mode=None,
                hash_string=None,
                hash_file_path=None,
                error_message="不支持的压缩包类型（仅支持 ZIP / RAR3 / RAR5 / 7Z）",
            )

        # --- 按类型分派到对应提取器 ---
        if atype == ArchiveType.ZIP:
            return self._extract_zip(archive_file)
        elif atype in (ArchiveType.RAR3, ArchiveType.RAR5):
            return self._extract_rar(archive_file, atype)
        elif atype == ArchiveType.SEVEN_Z:
            return self._extract_7z_native(archive_file)
        else:
            return ExtractResult(
                success=False, archive_type=atype,
                hashcat_mode=None, hash_string=None, hash_file_path=None,
                error_message="未匹配到具体提取实现分支",
            )

    # ======================================================================
    # ZIP 提取：调用 zip2john.exe
    # ======================================================================
    def _extract_zip(self, archive_file: Path) -> ExtractResult:
        """
        ZIP 哈希提取：调用 John 官方 zip2john 子进程
        zip2john.exe 输出形如： <文件名>:$zip2$*0*1*0*...
        我们取冒号之后的部分作为 Hashcat 标准哈希。
        """
        tool = self.tool_paths.zip2john
        if not tool:
            return self._miss_tool_error(ArchiveType.ZIP, "zip2john.exe")

        try:
            # zip2john 输出编码不稳定(中文 Windows 可能 UTF-8 也可能 GBK),
            # 用 bytes 捕获后先尝试 UTF-8 解码,失败则 GBK 兜底,确保 $zip2$ 前缀不丢失
            completed = subprocess.run(
                [tool, str(archive_file)],
                capture_output=True, timeout=30,
            )
            out_bytes = (completed.stdout or b"") + (completed.stderr or b"")
            try:
                out = out_bytes.decode("utf-8")
            except UnicodeDecodeError:
                out = out_bytes.decode("gbk", errors="replace")
            hash_line = self._pick_hash_line(out, ("$zip2$",))
            if not hash_line:
                return ExtractResult(
                    success=False, archive_type=ArchiveType.ZIP,
                    hashcat_mode=HASHCAT_MODE_ZIP_AES,
                    hash_string=None, hash_file_path=None,
                    error_message=f"zip2john 未输出有效 hash (返回码={completed.returncode}), 原始输出:\n{out[-500:]}",
                )
            # _pick_hash_line 已返回纯 hash(含 $zip2$ 前缀),不再额外 split
            hash_str = hash_line.strip()
            return self._wrap_success(ArchiveType.ZIP, HASHCAT_MODE_ZIP_AES, hash_str, archive_file)
        except subprocess.TimeoutExpired:
            return self._timeout_error(ArchiveType.ZIP, HASHCAT_MODE_ZIP_AES)
        except Exception as exc:  # noqa: BLE001 — 统一包装异常
            return self._wrap_exception(ArchiveType.ZIP, HASHCAT_MODE_ZIP_AES, exc)

    # ======================================================================
    # RAR3/RAR5 提取：调用 rar2john.exe
    # ======================================================================
    def _extract_rar(self, archive_file: Path, atype: ArchiveType) -> ExtractResult:
        """
        RAR 哈希提取：调用 John 官方 rar2john 子进程
        RAR3 输出前缀: $RAR3$   → Hashcat mode 12500
        RAR5 输出前缀: $rar5$   → Hashcat mode 13000
        rar2john 本身会自动识别 RAR3/RAR5 并输出对应前缀，
        我们再根据前缀二次确认 mode，保证与 ArchiveType 匹配。
        """
        tool = self.tool_paths.rar2john
        if not tool:
            return self._miss_tool_error(atype, "rar2john.exe")

        default_mode = HASHCAT_MODE_RAR5 if atype == ArchiveType.RAR5 else HASHCAT_MODE_RAR3
        try:
            # rar2john 输出编码不稳定(中文 Windows 可能 UTF-8 也可能 GBK),
            # 用 bytes 捕获后先尝试 UTF-8 解码,失败则 GBK 兜底,确保 $rar5$/$RAR3$ 前缀不丢失
            completed = subprocess.run(
                [tool, str(archive_file)],
                capture_output=True, timeout=30,
            )
            out_bytes = (completed.stdout or b"") + (completed.stderr or b"")
            try:
                out = out_bytes.decode("utf-8")
            except UnicodeDecodeError:
                out = out_bytes.decode("gbk", errors="replace")
            hash_line = self._pick_hash_line(out, ("$rar5$", "$RAR3$", "$rar3$"))
            if not hash_line:
                return ExtractResult(
                    success=False, archive_type=atype,
                    hashcat_mode=default_mode,
                    hash_string=None, hash_file_path=None,
                    error_message=f"rar2john 未输出有效 hash (返回码={completed.returncode}), 原始输出:\n{out[-500:]}",
                )
            # _pick_hash_line 已返回纯 hash(含 $rar5$/$RAR3$ 前缀),不再额外 split
            hash_str = hash_line.strip()
            # 根据实际前缀确认 mode
            mode = default_mode
            upper = hash_str.upper()
            if upper.startswith("$RAR5$"):
                mode = HASHCAT_MODE_RAR5
            elif upper.startswith("$RAR3$"):
                mode = HASHCAT_MODE_RAR3
            return self._wrap_success(atype, mode, hash_str, archive_file)
        except subprocess.TimeoutExpired:
            return self._timeout_error(atype, default_mode)
        except Exception as exc:  # noqa: BLE001
            return self._wrap_exception(atype, default_mode, exc)

    # ======================================================================
    # 7Z 提取：Python 原生实现占位（后续补全算法）
    # ======================================================================
    def _extract_7z_native(self, archive_file: Path) -> ExtractResult:
        """
        7Z 哈希提取（Python 原生，不依赖 7z2john.pl / Perl 环境）
        【当前为骨架接口】只做基本文件头读取与参数占位，
        具体算法（解析 7z 头、提取 AES-256 SHA-256 参数、拼装 $7z$ 字符串）
        在后续迭代补全。
        """
        # TODO: 实现 7Z 原生哈希解析：读签名头→解析 StartHeader→定位 EncodedHeader
        #       → 提取 Salt/IV/Coder 属性→按 Hashcat 规范拼 $7z$ 字符串。
        try:
            with open(archive_file, "rb") as fp:
                magic = fp.read(6)  # 37 7A BC AF 27 1C
                if magic != b"\x37\x7a\xbc\xaf\x27\x1c":
                    return ExtractResult(
                        success=False, archive_type=ArchiveType.SEVEN_Z,
                        hashcat_mode=HASHCAT_MODE_7Z,
                        hash_string=None, hash_file_path=None,
                        error_message="7Z 文件签名校验失败",
                    )
            # 骨架阶段：返回 Not Implemented 明确信息，便于上层 UI 提示
            return ExtractResult(
                success=False, archive_type=ArchiveType.SEVEN_Z,
                hashcat_mode=HASHCAT_MODE_7Z,
                hash_string=None, hash_file_path=None,
                error_message="[骨架阶段] 7Z Python 原生哈希提取算法将在后续迭代实现，"
                              "当前可手动切换为 7z2john.pl + Perl 模式。",
            )
        except Exception as exc:  # noqa: BLE001
            return self._wrap_exception(ArchiveType.SEVEN_Z, HASHCAT_MODE_7Z, exc)

    # ======================================================================
    # 内部公共工具
    # ======================================================================
    @staticmethod
    def _pick_hash_line(raw_output: str, prefixes: tuple) -> Optional[str]:
        """
        从 zip2john/rar2john 的原始输出（可能含警告/错误多行）中过滤出有效 hash
        :param raw_output: 子进程 stdout+stderr 合并文本
        :param prefixes: 有效 hash 的识别前缀（如 $zip2$/ $rar5$/ $RAR3$）
        :return: 纯 hash 字符串(已剥离文件名前缀),可直接写入 hash 文件供 hashcat 使用
        """
        import re as _re
        # 构建正则:匹配任一前缀开头,到行尾/空格/冒号前的整段 hash 字符串
        # (hash 格式:前缀+*或$分隔的十六进制/数字,不含空格/中文/冒号)
        # 排除冒号:zip2john/rar2john 输出 "filename:$hash",正则从 $hash 开始匹配,
        #          但 hash 后可能跟 ":other_filename",需在冒号处截断
        _pipes = "|".join(_re.escape(p) for p in prefixes)
        _pat = _re.compile(rf"({_pipes})[^ \t\r\n\u4e00-\u9fff:]+", _re.IGNORECASE)
        _m = _pat.search(raw_output)
        if _m:
            return _m.group(0)
        # 兜底:逐行扫描(兼容特殊格式)
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            candidates = [stripped]
            if ":" in stripped:
                candidates.append(stripped.split(":", 1)[1].strip())
            for seg in candidates:
                for p in prefixes:
                    if seg.upper().startswith(p.upper()):
                        return seg
        return None

    def _wrap_success(self, atype: ArchiveType, mode: int, hash_str: str,
                      src_archive: Path) -> ExtractResult:
        """
        包装成功结果：写入 hash 文件到 data/output/ 并返回统一结构
        """
        safe_stem = src_archive.stem.replace(" ", "_")
        out_file = self.output_dir / f"{safe_stem}_{atype.value}.hash"
        try:
            out_file.write_text(hash_str + "\n", encoding="utf-8")
            return ExtractResult(
                success=True, archive_type=atype,
                hashcat_mode=mode, hash_string=hash_str,
                hash_file_path=str(out_file.resolve()),
            )
        except Exception as exc:  # noqa: BLE001
            # hash 写文件失败不影响 hash 字符串本身可用，成功但提示保存失败
            return ExtractResult(
                success=True, archive_type=atype,
                hashcat_mode=mode, hash_string=hash_str,
                hash_file_path=None,
                error_message=f"hash 写文件失败({exc})，但 hash 字符串已返回可直接用。",
            )

    # ---------------- 错误构造快捷方法 ----------------
    def _miss_tool_error(self, atype: ArchiveType, tool_name: str) -> ExtractResult:
        mode_map = {
            ArchiveType.ZIP: HASHCAT_MODE_ZIP_AES,
            ArchiveType.RAR3: HASHCAT_MODE_RAR3,
            ArchiveType.RAR5: HASHCAT_MODE_RAR5,
            ArchiveType.SEVEN_Z: HASHCAT_MODE_7Z,
        }
        return ExtractResult(
            success=False, archive_type=atype,
            hashcat_mode=mode_map.get(atype),
            hash_string=None, hash_file_path=None,
            error_message=f"缺少必要工具 {tool_name}，请检查 bin/{self.pm._detect_platform_dir()}/john/run/ 目录。",
        )

    @staticmethod
    def _timeout_error(atype: ArchiveType, mode: int) -> ExtractResult:
        return ExtractResult(
            success=False, archive_type=atype, hashcat_mode=mode,
            hash_string=None, hash_file_path=None,
            error_message="哈希提取超时（>30秒），压缩包可能损坏或过大。",
        )

    @staticmethod
    def _wrap_exception(atype: ArchiveType, mode: int, exc: Exception) -> ExtractResult:
        return ExtractResult(
            success=False, archive_type=atype, hashcat_mode=mode,
            hash_string=None, hash_file_path=None,
            error_message=f"提取异常: {type(exc).__name__}: {exc}",
        )


if __name__ == "__main__":
    # 调试入口：python hash_extractor.py <压缩包路径>
    import sys
    if len(sys.argv) < 2:
        print("用法: python hash_extractor.py <archive.zip|.rar|.7z>")
    else:
        extractor = HashExtractor(PathManager())
        r = extractor.extract(sys.argv[1])
        print(f"success       : {r.success}")
        print(f"archive_type  : {r.archive_type.value}")
        print(f"hashcat_mode  : {r.hashcat_mode}")
        print(f"hash_file     : {r.hash_file_path}")
        if r.hash_string:
            print(f"hash_string   : {r.hash_string[:100]}..." if len(r.hash_string) > 100 else f"hash_string   : {r.hash_string}")
        if r.error_message:
            print(f"error_message : {r.error_message}")
